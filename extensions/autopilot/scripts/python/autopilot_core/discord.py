"""Discord connector: one forum channel per project, one post per run and per epic.

REST API v10 over HTTPS with the standard library. The bot needs the
**Message Content** intent (to read the owner's replies) and, on the forum:
View Channel, Send Messages in Threads, Read Message History, Add Reactions,
Manage Threads (status tags) and Manage Webhooks (the speaker webhook).

* A thread is a forum post created through a webhook, so every message shows
  its speaker (lead, dev-E1, reviewer, autopilot) as its name.
* Live status is the post's forum tags (``run``/``epic`` plus one status tag);
  ``check`` creates missing tags on the forum.
* Discord has no hidden message metadata: the journal in the repo holds it.
* Replies are read with the bot token; posts are unarchived before writing.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .connector import TAG_NAMES, Caps, ConnectorError, Http, Inbound, Out, read_secret, split_text, write_secret

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/github/spec-kit, 1.0)"
MAX_TEXT = 2000
ARCHIVED_CODES = {50083}  # "Thread is archived"
FATAL_STATUS = {401, 403}
WEBHOOK_NAME = "spec-kit autopilot"


class DiscordConnector:
    name = "discord"
    caps = Caps(hidden_metadata=False, tags=True, edit_status=False, custom_names=True, top_level=False,
                max_text=MAX_TEXT)

    def __init__(self, settings: dict[str, Any], token: str, http: Http) -> None:
        self.forum = str(settings.get("forum_channel_id", "")).strip()
        self.owner = str(settings.get("owner_id", "")).strip()
        self.token = token
        self.http = http
        self._tags: dict[str, str] | None = None
        self._webhook: dict[str, str] | None = None

    # ── transport ────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None, *, auth: bool = True) -> Any:
        headers = {"User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bot {self.token}"
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        status, response_headers, raw = self.http(method, f"{API}{path}", headers, body)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if status == 429:
            retry = data.get("retry_after") if isinstance(data, dict) else None
            retry = retry or response_headers.get("Retry-After") or response_headers.get("retry-after") or 5
            raise ConnectorError(f"discord {method} {path}: rate limited", retryable=True, retry_after=float(retry))
        if status >= 500:
            raise ConnectorError(f"discord {method} {path}: HTTP {status}", retryable=True)
        if status >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            message = data.get("message") if isinstance(data, dict) else ""
            error = ConnectorError(f"discord {method} {_redact(path)}: HTTP {status} {code or ''} {message}".strip(),
                                   fatal=status in FATAL_STATUS)
            error.code = code  # type: ignore[attr-defined]
            raise error
        return data

    # ── setup ────────────────────────────────────────────────────────────

    def _webhook_secret(self) -> str:
        return f"discord-webhook-{self.forum}.json"

    def webhook(self) -> dict[str, str]:
        """The forum's speaker webhook, created once and kept with the other secrets (never in the repo)."""
        if self._webhook:
            return self._webhook
        stored = read_secret(self._webhook_secret())
        if stored and stored.get("id") and stored.get("token"):
            self._webhook = {"id": str(stored["id"]), "token": str(stored["token"])}
            return self._webhook
        for hook in self._call("GET", f"/channels/{self.forum}/webhooks") or []:
            if hook.get("name") == WEBHOOK_NAME and hook.get("token"):
                self._webhook = {"id": str(hook["id"]), "token": str(hook["token"])}
                break
        else:
            hook = self._call("POST", f"/channels/{self.forum}/webhooks", {"name": WEBHOOK_NAME})
            self._webhook = {"id": str(hook["id"]), "token": str(hook["token"])}
        write_secret(self._webhook_secret(), self._webhook)
        return self._webhook

    def tags(self, wanted: tuple[str, ...] = ()) -> dict[str, str]:
        """Forum tag name -> ID, creating the autopilot's tags when missing."""
        if self._tags is not None and all(name in self._tags for name in wanted):
            return self._tags
        forum = self._call("GET", f"/channels/{self.forum}")
        available = forum.get("available_tags", []) if isinstance(forum, dict) else []
        names = {t["name"]: str(t["id"]) for t in available if "name" in t and "id" in t}
        missing = [name for name in wanted if name not in names]
        if missing:
            updated = [{"id": t["id"], "name": t["name"], "moderated": t.get("moderated", False)} for t in available]
            updated += [{"name": name} for name in missing]
            forum = self._call("PATCH", f"/channels/{self.forum}", {"available_tags": updated[:20]})
            names = {t["name"]: str(t["id"]) for t in forum.get("available_tags", []) if "name" in t and "id" in t}
        self._tags = names
        return names

    def check(self) -> dict[str, Any]:
        problems = []
        if not self.forum:
            problems.append("chat.discord.forum_channel_id is empty (right-click the forum, Copy Channel ID)")
            return {"problems": problems}
        me = self._call("GET", "/users/@me")
        forum = self._call("GET", f"/channels/{self.forum}")
        if isinstance(forum, dict) and forum.get("type") != 15:
            problems.append("chat.discord.forum_channel_id is not a forum channel")
        try:
            self.tags(TAG_NAMES)
        except ConnectorError as exc:
            problems.append(f"cannot create the status tags ({exc}); give the bot Manage Channels once, "
                            "or create these forum tags by hand: " + ", ".join(TAG_NAMES))
        try:
            self.webhook()
        except ConnectorError as exc:
            problems.append(f"cannot create the speaker webhook ({exc}); give the bot Manage Webhooks")
        if self.owner and not self.owner.isdigit():
            problems.append("chat.discord.owner_id should be your numeric user ID (Developer Mode, Copy User ID)")
        return {"bot_user": me.get("username") if isinstance(me, dict) else None, "problems": problems}

    # ── posting ──────────────────────────────────────────────────────────

    def _execute(self, payload: dict[str, Any], thread: str | None) -> dict[str, Any]:
        hook = self.webhook()
        query = {"wait": "true", **({"thread_id": thread} if thread else {})}
        return self._call("POST", f"/webhooks/{hook['id']}/{hook['token']}?{urllib.parse.urlencode(query)}",
                          payload, auth=False)

    def _payload(self, message: Out, text: str) -> dict[str, Any]:
        mention = message.mention_owner and self.owner
        return {
            "content": (f"<@{self.owner}> " if mention else "") + text,
            "username": message.author[:80],
            "allowed_mentions": {"users": [self.owner]} if mention else {"parse": []},
        }

    def _tag_ids(self, tags: list[str]) -> list[str]:
        known = self.tags(tuple(tags))
        return [known[t] for t in tags if t in known][:5]

    def open_thread(self, title: str, status: str, tags: list[str], first: Out) -> str:
        chunks = split_text(first.text, MAX_TEXT - 40) or [" "]
        payload = {**self._payload(first, chunks[0]), "thread_name": title[:100], "applied_tags": self._tag_ids(tags)}
        created = self._execute(payload, None)
        thread = str(created.get("channel_id"))
        for chunk in chunks[1:]:
            self._execute(self._payload(Out(first.author, chunk), chunk), thread)
        return thread

    def post(self, thread: str, message: Out) -> list[str]:
        refs = []
        for index, chunk in enumerate(split_text(message.text, MAX_TEXT - 40)):
            payload = self._payload(message if index == 0 else Out(message.author, chunk), chunk)
            try:
                sent = self._execute(payload, thread)
            except ConnectorError as exc:
                if getattr(exc, "code", None) not in ARCHIVED_CODES:
                    raise
                self._call("PATCH", f"/channels/{thread}", {"archived": False})
                sent = self._execute(payload, thread)
            refs.append(str(sent.get("id", "")))
        return refs

    def set_status(self, thread: str, title: str, status: str, tags: list[str]) -> None:
        self._call("PATCH", f"/channels/{thread}", {"applied_tags": self._tag_ids(tags)})

    # ── reading ──────────────────────────────────────────────────────────

    def fetch(self, thread: str, cursor: str | None) -> tuple[list[Inbound], str]:
        after = cursor or thread  # a forum post's ID is its starter message's ID
        found: list[dict[str, Any]] = []
        for _ in range(20):
            page = self._call("GET", f"/channels/{thread}/messages?after={after}&limit=100") or []
            if not page:
                break
            found += page
            after = max((str(m["id"]) for m in page), key=int)
            if len(page) < 100:
                break
        found.sort(key=lambda m: int(m["id"]))
        newest = max([cursor or thread] + [str(m["id"]) for m in found], key=int)
        return [
            Inbound(ref=str(m["id"]), author_id=str((m.get("author") or {}).get("id", "")),
                    is_bot=bool((m.get("author") or {}).get("bot")) or bool(m.get("webhook_id")),
                    text=str(m.get("content", "")))
            for m in found if m.get("type", 0) in (0, 19)
        ], newest

    def fetch_top_level(self, cursor: str | None) -> tuple[list[Inbound], str]:
        return [], cursor or ""

    def acknowledge(self, thread: str, ref: str, ok: bool) -> None:
        emoji = urllib.parse.quote("✅" if ok else "⚠️")
        self._call("PUT", f"/channels/{thread}/messages/{ref}/reactions/{emoji}/@me")


def _redact(path: str) -> str:
    """Never put a webhook token into an error message (they reach health.json and logs)."""
    parts = path.split("/")
    if len(parts) > 3 and parts[1] == "webhooks":
        parts[3] = "***"
    return "/".join(parts)
