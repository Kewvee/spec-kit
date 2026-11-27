"""Slack and Discord connectors against a fake HTTP layer: request shapes, errors, reading."""

from __future__ import annotations

import json
import urllib.parse

import pytest

import tests.extensions.autopilot.helpers  # noqa: F401  (puts the stdlib-only core on sys.path)

from autopilot_core import connector as connector_mod
from autopilot_core.connector import ConnectorError, Out, split_text
from autopilot_core.discord import DiscordConnector
from autopilot_core.slack import SlackConnector, mrkdwn


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        status, payload, *extra = self.responses.pop(0)
        return status, (extra[0] if extra else {}), json.dumps(payload).encode()


def test_split_text_respects_the_limit_and_lines():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    chunks = split_text(text, 500)
    assert all(len(c) <= 500 for c in chunks) and "".join(chunks).count("line") == 100
    assert split_text("y" * 1200, 500) == ["y" * 500, "y" * 500, "y" * 200]


def test_tokens_come_from_the_environment_or_a_private_file(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_mod, "SECRETS_DIR", tmp_path)
    chat = {"slack": {"token_env": "TEST_SLACK_TOKEN"}}
    monkeypatch.delenv("TEST_SLACK_TOKEN", raising=False)
    with pytest.raises(ConnectorError) as excinfo:
        connector_mod.read_token("slack", chat)
    assert excinfo.value.fatal
    (tmp_path / "slack-token").write_text("xoxb-file\n", encoding="utf-8")
    assert connector_mod.read_token("slack", chat) == "xoxb-file"
    monkeypatch.setenv("TEST_SLACK_TOKEN", "xoxb-env")
    assert connector_mod.read_token("slack", chat) == "xoxb-env"


# ── Slack ────────────────────────────────────────────────────────────────────


def _slack(http):
    return SlackConnector({"channel_id": "C0TEAM01", "owner_id": "U0OWNER1"}, "xoxb-test", http)


def test_slack_mrkdwn_escapes_and_converts():
    assert mrkdwn("**E1** <script> & [docs](https://x.y/z)") == "*E1* &lt;script&gt; &amp; <https://x.y/z|docs>"


def test_slack_thread_is_a_header_with_replies_and_metadata():
    http = FakeHttp([(200, {"ok": True, "ts": "100.1"}), (200, {"ok": True, "ts": "100.2"}),
                     (200, {"ok": True, "ts": "100.3"})])
    slack = _slack(http)
    ref = slack.open_thread("E1 · Export", "🟡 in progress", ["epic", "running"],
                            Out(author="dev-E1", text="**Q**: CSV?", mention_owner=True, meta={"question": "001-Q1"}))
    assert ref == "100.1"
    header, first = http.calls[0]["body"], http.calls[1]["body"]
    assert header["text"] == "*E1 · Export*\n🟡 in progress" and "thread_ts" not in header
    assert first["thread_ts"] == "100.1" and first["username"] == "dev-E1"
    assert first["text"] == "<@U0OWNER1> *Q*: CSV?"
    assert first["metadata"] == {"event_type": "speckit_autopilot", "event_payload": {"question": "001-Q1"}}
    assert http.calls[1]["headers"]["Authorization"] == "Bearer xoxb-test"
    slack.set_status(ref, "E1 · Export", "🟢 merged", ["epic", "merged"])
    assert http.calls[2]["url"].endswith("chat.update") and http.calls[2]["body"]["ts"] == "100.1"


def test_slack_errors_are_classified():
    slack = _slack(FakeHttp([(429, {}, {"Retry-After": "7"})]))
    with pytest.raises(ConnectorError) as excinfo:
        slack.post("1.0", Out(author="lead", text="hi"))
    assert excinfo.value.retryable and excinfo.value.retry_after == 7.0
    slack = _slack(FakeHttp([(200, {"ok": False, "error": "invalid_auth"})]))
    with pytest.raises(ConnectorError) as excinfo:
        slack.post("1.0", Out(author="lead", text="hi"))
    assert excinfo.value.fatal and "xoxb" not in str(excinfo.value)
    slack = _slack(FakeHttp([(200, {"ok": False, "error": "msg_too_long"})]))
    with pytest.raises(ConnectorError) as excinfo:
        slack.post("1.0", Out(author="lead", text="hi"))
    assert not excinfo.value.fatal and not excinfo.value.retryable


def test_slack_reads_only_new_replies_after_the_cursor():
    page = {"ok": True, "messages": [
        {"ts": "100.1", "user": "U0BOT", "bot_id": "B1", "text": "header"},
        {"ts": "100.2", "user": "U0OWNER1", "text": "old"},
        {"ts": "100.4", "user": "U0OWNER1", "text": "approve plan r1127-ab12"},
        {"ts": "100.3", "bot_id": "B1", "subtype": "bot_message", "text": "bot reply"},
    ]}
    http = FakeHttp([(200, page)])
    messages, cursor = _slack(http).fetch("100.1", "100.2")
    assert [m.text for m in messages] == ["bot reply", "approve plan r1127-ab12"]
    assert [m.is_bot for m in messages] == [True, False] and cursor == "100.4"
    query = urllib.parse.parse_qs(urllib.parse.urlparse(http.calls[0]["url"]).query)
    assert http.calls[0]["method"] == "GET" and query["ts"] == ["100.1"] and query["oldest"] == ["100.2"]


def test_slack_top_level_listening_starts_now():
    messages, cursor = _slack(FakeHttp([])).fetch_top_level(None)
    assert messages == [] and float(cursor) > 0


# ── Discord ──────────────────────────────────────────────────────────────────


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_mod, "SECRETS_DIR", tmp_path)
    return tmp_path


def _discord(http):
    return DiscordConnector({"forum_channel_id": "555", "owner_id": "42"}, "bot-token", http)


def test_discord_creates_the_webhook_once_and_keeps_it_private(secrets):
    http = FakeHttp([(200, []), (200, {"id": "9", "token": "hook-secret"})])
    discord = _discord(http)
    assert discord.webhook() == {"id": "9", "token": "hook-secret"}
    stored = secrets / "discord-webhook-555.json"
    assert json.loads(stored.read_text())["token"] == "hook-secret"
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    assert _discord(FakeHttp([])).webhook()["id"] == "9"  # read back, no API call


def test_discord_posts_are_forum_threads_with_speakers_and_tags(secrets):
    (secrets / "discord-webhook-555.json").write_text(json.dumps({"id": "9", "token": "t"}))
    forum = {"type": 15, "available_tags": [{"id": "1", "name": "epic"}, {"id": "2", "name": "running"}]}
    http = FakeHttp([(200, forum), (200, {"id": "700", "channel_id": "700"}), (200, {"id": "701"}), (200, {"id": "702"})])
    discord = _discord(http)
    ref = discord.open_thread("E1 · Export", "", ["epic", "running"],
                              Out(author="dev-E1", text="CSV?", mention_owner=True))
    assert ref == "700"
    create = http.calls[1]
    assert "/webhooks/9/t?" in create["url"] and "Authorization" not in create["headers"]
    assert create["body"]["thread_name"] == "E1 · Export" and create["body"]["applied_tags"] == ["1", "2"]
    assert create["body"]["username"] == "dev-E1" and create["body"]["content"] == "<@42> CSV?"
    assert create["body"]["allowed_mentions"] == {"users": ["42"]}
    discord.post(ref, Out(author="lead", text="Use CSV.\n" * 300))
    assert "thread_id=700" in http.calls[2]["url"] and http.calls[2]["body"]["allowed_mentions"] == {"parse": []}


def test_discord_unarchives_a_quiet_post_before_writing(secrets):
    (secrets / "discord-webhook-555.json").write_text(json.dumps({"id": "9", "token": "t"}))
    http = FakeHttp([(400, {"code": 50083, "message": "Thread is archived"}), (200, {}), (200, {"id": "702"})])
    assert _discord(http).post("700", Out(author="lead", text="still here")) == ["702"]
    assert http.calls[1]["method"] == "PATCH" and http.calls[1]["body"] == {"archived": False}


def test_discord_errors_never_reveal_the_webhook_token(secrets):
    (secrets / "discord-webhook-555.json").write_text(json.dumps({"id": "9", "token": "hook-secret"}))
    http = FakeHttp([(401, {"code": 0, "message": "401: Unauthorized"})])
    with pytest.raises(ConnectorError) as excinfo:
        _discord(http).post("700", Out(author="lead", text="x"))
    assert excinfo.value.fatal and "hook-secret" not in str(excinfo.value)


def test_discord_reads_replies_in_order(secrets):
    page = [
        {"id": "905", "type": 0, "author": {"id": "42"}, "content": "approve land r1127-ab12"},
        {"id": "903", "type": 0, "author": {"id": "7", "bot": True}, "webhook_id": "9", "content": "lead says"},
        {"id": "904", "type": 7, "author": {"id": "8"}, "content": "joined"},
    ]
    http = FakeHttp([(200, page)])
    messages, cursor = _discord(http).fetch("700", None)
    assert [m.ref for m in messages] == ["903", "905"] and messages[0].is_bot and not messages[1].is_bot
    assert cursor == "905" and "after=700" in http.calls[0]["url"]
    assert http.calls[0]["headers"]["Authorization"] == "Bot bot-token"


def test_discord_rate_limit_uses_retry_after(secrets):
    http = FakeHttp([(429, {"retry_after": 1.5, "message": "slow down"})])
    with pytest.raises(ConnectorError) as excinfo:
        _discord(http).fetch("700", "800")
    assert excinfo.value.retryable and excinfo.value.retry_after == 1.5
