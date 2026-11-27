"""Slack connector: one channel per project, one thread per run and per epic.

Web API over HTTPS with the standard library. Scopes (see
``slack-app-manifest.yml``): ``chat:write``, ``chat:write.customize`` (post as
lead / dev-E1 / reviewer), ``channels:history`` (or ``groups:history`` for a
private channel) and ``reactions:write``.

* A thread is a top-level header message (title + live status, edited in
  place) with the conversation as its replies.
* Every message carries Slack message metadata (``event_type:
  speckit_autopilot``, payload = the journal tags), hidden from people.
* Replies are read with ``conversations.replies``; top-level messages in the
  channel with ``conversations.history`` (commands outside a thread go to the
  run thread).
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

from .connector import Caps, ConnectorError, Http, Inbound, Out, split_text

API = "https://slack.com/api/"
FATAL = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "missing_scope",
         "channel_not_found", "not_in_channel", "is_archived", "no_permission", "ekm_access_denied"}
ICONS = {"lead": ":compass:", "reviewer": ":mag:", "autopilot": ":robot_face:"}
MAX_TEXT = 3500


def mrkdwn(text: str) -> str:
    """Our Markdown subset to Slack mrkdwn: escape, bold, links."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)


class SlackConnector:
    name = "slack"
    caps = Caps(hidden_metadata=True, tags=False, edit_status=True, custom_names=True, top_level=True, max_text=MAX_TEXT)

    def __init__(self, settings: dict[str, Any], token: str, http: Http) -> None:
        self.channel = str(settings.get("channel_id", "")).strip()
        self.owner = str(settings.get("owner_id", "")).strip()
        self.token = token
        self.http = http

    # ── transport ────────────────────────────────────────────────────────

    def _call(self, method: str, payload: dict[str, Any], *, read: bool = False) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if read:
            query = urllib.parse.urlencode({k: v for k, v in payload.items() if v is not None})
            status, response_headers, body = self.http("GET", f"{API}{method}?{query}", headers, None)
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            status, response_headers, body = self.http("POST", f"{API}{method}", headers, json.dumps(payload).encode())
        if status == 429:
            retry = response_headers.get("Retry-After") or response_headers.get("retry-after") or "30"
            raise ConnectorError(f"slack {method}: rate limited", retryable=True, retry_after=float(retry))
        if status >= 500:
            raise ConnectorError(f"slack {method}: HTTP {status}", retryable=True)
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError(f"slack {method}: unreadable response (HTTP {status})", retryable=True) from exc
        if not data.get("ok"):
            error = str(data.get("error", f"HTTP {status}"))
            if error == "ratelimited":
                raise ConnectorError(f"slack {method}: rate limited", retryable=True, retry_after=30.0)
            raise ConnectorError(f"slack {method}: {error}", fatal=error in FATAL)
        return data

    # ── connector interface ──────────────────────────────────────────────

    def check(self) -> dict[str, Any]:
        problems = []
        if not self.channel:
            problems.append("chat.slack.channel_id is empty (the channel's ID, like C0123ABCD)")
        identity = self._call("auth.test", {})
        if self.channel:
            try:
                self._call("conversations.history", {"channel": self.channel, "limit": 1}, read=True)
            except ConnectorError as exc:
                problems.append(f"cannot read the channel ({exc}); invite the bot to it and check its scopes")
        if self.owner and not re.fullmatch(r"[UW][A-Z0-9]{6,}", self.owner):
            problems.append("chat.slack.owner_id should be your Slack member ID (like U0123ABCD), not your name")
        return {"workspace": identity.get("team"), "bot_user": identity.get("user_id"), "problems": problems}

    def _message(self, message: Out, thread: str | None, text: str) -> dict[str, Any]:
        body = mrkdwn(text)
        if message.mention_owner and self.owner:
            body = f"<@{self.owner}> {body}"
        payload: dict[str, Any] = {
            "channel": self.channel, "text": body, "username": message.author,
            "icon_emoji": ICONS.get(message.author, ":hammer_and_wrench:"),
            "unfurl_links": False, "unfurl_media": False,
            "metadata": {"event_type": "speckit_autopilot", "event_payload": message.meta},
        }
        if thread:
            payload["thread_ts"] = thread
        return payload

    def open_thread(self, title: str, status: str, tags: list[str], first: Out) -> str:
        header = self._call("chat.postMessage", {
            "channel": self.channel, "text": self._header(title, status), "username": "autopilot",
            "icon_emoji": ICONS["autopilot"], "unfurl_links": False,
            "metadata": {"event_type": "speckit_autopilot", "event_payload": {**first.meta, "kind": "thread"}},
        })
        thread = str(header["ts"])
        self.post(thread, first)
        return thread

    def post(self, thread: str, message: Out) -> list[str]:
        refs = []
        for chunk in split_text(message.text, MAX_TEXT):
            data = self._call("chat.postMessage", self._message(message, thread, chunk))
            refs.append(str(data.get("ts", "")))
            message = Out(author=message.author, text="", mention_owner=False, meta=message.meta)
        return refs

    @staticmethod
    def _header(title: str, status: str) -> str:
        return f"*{mrkdwn(title)}*" + (f"\n{mrkdwn(status)}" if status else "")

    def set_status(self, thread: str, title: str, status: str, tags: list[str]) -> None:
        self._call("chat.update", {"channel": self.channel, "ts": thread, "text": self._header(title, status)})

    def _read(self, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):
            data = self._call(method, {**params, "limit": 200, "cursor": cursor}, read=True)
            messages += [m for m in data.get("messages", []) if isinstance(m, dict)]
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return messages

    @staticmethod
    def _inbound(message: dict[str, Any]) -> Inbound:
        return Inbound(ref=str(message.get("ts")), author_id=str(message.get("user", "")),
                       is_bot=bool(message.get("bot_id")) or message.get("subtype") == "bot_message",
                       text=str(message.get("text", "")))

    def fetch(self, thread: str, cursor: str | None) -> tuple[list[Inbound], str]:
        after = cursor or thread
        raw = self._read("conversations.replies", {"channel": self.channel, "ts": thread, "oldest": after})
        fresh = sorted((m for m in raw if float(m.get("ts", 0)) > float(after) and m.get("ts") != thread),
                       key=lambda m: float(m["ts"]))
        newest = max([after] + [str(m["ts"]) for m in fresh], key=float)
        return [self._inbound(m) for m in fresh if not m.get("subtype") or m.get("subtype") == "bot_message"], newest

    def fetch_top_level(self, cursor: str | None) -> tuple[list[Inbound], str]:
        if cursor is None:
            return [], f"{time.time():.6f}"  # start listening now, not from the channel's history
        raw = self._read("conversations.history", {"channel": self.channel, "oldest": cursor})
        fresh = sorted((m for m in raw if float(m.get("ts", 0)) > float(cursor)
                        and (not m.get("thread_ts") or m.get("thread_ts") == m.get("ts"))),
                       key=lambda m: float(m["ts"]))
        newest = max([cursor] + [str(m["ts"]) for m in fresh], key=float)
        return [self._inbound(m) for m in fresh if not m.get("subtype")], newest

    def acknowledge(self, thread: str, ref: str, ok: bool) -> None:
        try:
            self._call("reactions.add", {"channel": self.channel, "timestamp": ref,
                                         "name": "white_check_mark" if ok else "warning"})
        except ConnectorError as exc:
            if "already_reacted" not in str(exc):
                raise
