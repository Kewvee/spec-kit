"""Team chat: the journal, thread.md, the bridge (with an in-memory chat app), owner commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.extensions.autopilot.test_autopilot_driver import _cli, _epic, _plan, make_project  # noqa: F401

from autopilot_core import bridge as bridge_mod
from autopilot_core import chat, driver
from autopilot_core.config import ConfigError, load_config
from autopilot_core.connector import Caps, ConnectorError, Inbound, Out

OWNER = "U0OWNER1"


class FakeApp:
    """A chat app in memory: threads of posted messages, plus replies people type."""

    name = "slack"
    caps = Caps(hidden_metadata=True, tags=True, edit_status=True, top_level=False, max_text=4000)

    def __init__(self) -> None:
        self.threads: dict[str, dict] = {}
        self.typed: dict[str, list[Inbound]] = {}
        self.reactions: list[tuple[str, bool]] = []
        self.fail_next: ConnectorError | None = None
        self.counter = 0

    def _ref(self) -> str:
        self.counter += 1
        return f"m{self.counter:04d}"

    def _maybe_fail(self) -> None:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error

    def check(self):
        return {"problems": []}

    def open_thread(self, title, status, tags, first: Out):
        self._maybe_fail()
        ref = self._ref()
        self.threads[ref] = {"title": title, "status": status, "tags": tags, "messages": [first]}
        return ref

    def post(self, thread, message: Out):
        self._maybe_fail()
        self.threads[thread]["messages"].append(message)
        return [self._ref()]

    def set_status(self, thread, title, status, tags):
        self.threads[thread].update(status=status, tags=tags)

    def fetch(self, thread, cursor):
        fresh = self.typed.pop(thread, [])
        return fresh, f"c{self.counter}"

    def fetch_top_level(self, cursor):
        return [], cursor or ""

    def acknowledge(self, thread, ref, ok):
        self.reactions.append((ref, ok))

    def thread_by_title(self, prefix: str) -> tuple[str, dict]:
        return next((ref, t) for ref, t in self.threads.items() if t["title"].startswith(prefix))

    def say(self, thread: str, text: str, author: str = OWNER, bot: bool = False) -> None:
        self.typed.setdefault(thread, []).append(Inbound(ref=self._ref(), author_id=author, is_bot=bot, text=text))


def _enable_chat(root: Path) -> dict:
    path = root / ".specify" / "extensions" / "autopilot" / "autopilot-config.yml"
    path.write_text(path.read_text() + f"""chat:
  provider: slack
  poll_seconds: 1
  max_posts_per_minute: 50
  slack:
    channel_id: C0TEAM01
    owner_id: {OWNER}
""", encoding="utf-8")
    return load_config(root)


def _bridge(root: Path, app: FakeApp | None = None):
    config = _enable_chat(root) if load_config(root)["chat"]["provider"] == "none" else load_config(root)
    app = app or FakeApp()
    return bridge_mod.Bridge(root, config, app), app


# ── journal and thread.md ────────────────────────────────────────────────────


def test_journal_round_trip_and_thread_md(tmp_path):
    chat.post(tmp_path, "r1127-ab12-E1", "dev-E1", "Which time zone?", kind="question", meta={"question": "001-Q1"})
    chat.post(tmp_path, "r1127-ab12-E1", "owner", "Use UTC everywhere.", kind="note")
    chat.post(tmp_path, "run-r1127-ab12", "owner", "No new infrastructure.", kind="note")
    (chat.thread_path(tmp_path, "r1127-ab12-E1")).open("a").write('{"torn": ')  # a crash mid-write
    messages = chat.load_thread(tmp_path, "r1127-ab12-E1")
    assert [m["author"] for m in messages] == ["dev-E1", "owner"]
    out = chat.write_thread_context(tmp_path, tmp_path / "ctx", "r1127-ab12-E1", "E1 · Export", "run-r1127-ab12")
    text = out.read_text()
    assert "No new infrastructure." in text.split("## This epic")[0]  # team-wide owner notes first
    assert "**Developer (E1)**" in text and "question=001-Q1" in text and "**Product owner**" in text


def test_chat_config_is_validated_and_never_holds_tokens(tmp_path):
    from autopilot_core import config as config_mod

    path = tmp_path / "c.yml"
    path.write_text("chat:\n  provider: teams\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_mod.load_config(tmp_path, path)
    path.write_text("chat:\n  provider: slack\n  slack:\n    channel_id: xoxb-123-456-abc\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="token"):
        config_mod.load_config(tmp_path, path)


# ── the bridge with a whole run ──────────────────────────────────────────────


def test_a_run_is_mirrored_and_the_owner_approves_and_answers_in_chat(make_project, monkeypatch):
    root = make_project([_epic("E1", "alpha", extra="ASK-HUMAN"), _epic("E2", "beta")])
    code, out, _ = _cli(root, "plan-check")
    run = out["run_id"]
    bridge, app = _bridge(root)
    bridge.tick()
    ref, run_thread = app.thread_by_title(f"Run {run}")
    first = run_thread["messages"][0]
    assert f"approve plan {run}" in first.text and first.mention_owner is True
    assert "needs-owner" in run_thread["tags"]

    # Someone else's approval, a bot's, and a stale run ID never count.
    app.say(ref, f"approve plan {run}", author="U0INTRUDER")
    app.say(ref, f"approve plan {run}", bot=True)
    app.say(ref, "approve plan r0101-dead")
    bridge.tick()
    assert driver.run_status(root)["stage"] == "awaiting-plan-approval"
    assert app.reactions[-1][1] is False  # the stale-run approval was refused, visibly

    started = []
    monkeypatch.setattr(driver, "drive_detached", lambda r, cli, integ: (started.append(cli), (0, {"started": True}))[1])
    app.say(ref, f"<@UBOT> approve plan {run}")
    bridge.tick()
    assert started and driver.run_status(root)["approvals"]["plan"]["via"] == "slack"
    assert "the team starts now" in app.threads[ref]["messages"][-1].text

    # The drive runs (in the foreground here); the bridge mirrors it.
    _cli(root, "drive")
    bridge.tick()
    e1_ref, e1 = app.thread_by_title("E1 · Epic alpha")
    texts = [m.text for m in e1["messages"]]
    assert any("Product question" in t and "Which export format?" in t for t in texts)
    assert any(m.mention_owner for m in e1["messages"])
    assert e1["tags"] == ["epic", "needs-owner"]
    _, e2 = app.thread_by_title("E2 · Epic beta")
    assert e2["tags"] == ["epic", "merged"] and any("gate green" in m.text for m in e2["messages"])
    assert any(m.author == "reviewer" for m in e2["messages"])

    # A plain reply in the epic's thread answers its only open question.
    app.say(e1_ref, "CSV only for now")
    bridge.tick()
    assert driver.human_questions(root) == []
    assert "Recorded as the answer" in app.threads[e1_ref]["messages"][-1].text
    _cli(root, "drive")
    bridge.tick()
    decision = next(json.loads(p.read_text()) for p in (root / ".specify/autopilot/decisions").glob("*.json"))
    assert decision["answer"] == "CSV only for now" and decision["decided_by"] == "product owner"
    assert app.thread_by_title("E1 · Epic alpha")[1]["tags"] == ["epic", "merged"]
    report = app.threads[ref]["messages"][-1].text
    assert f"approve land {run}" in report

    # The owner's words are in the journal, so the next developer reads them.
    journal = chat.load_thread(root, chat.epic_thread(run, "E1"))
    assert any(m["author"] == "owner" and m.get("origin") for m in journal)


def test_owner_notes_reach_the_developers(make_project):
    root = make_project([_epic("E1", "alpha")])
    run = _plan(root)
    bridge, app = _bridge(root)
    bridge.tick()
    ref, _ = app.thread_by_title(f"Run {run}")
    app.say(ref, "Keep the CSV columns in the same order as the screen.")
    bridge.tick()
    assert app.reactions[-1][1] is True
    assert _cli(root, "drive")[0] == 0
    state = driver.load_state(root)
    thread_md = Path(state["epics"]["E1"]["worktree"]) / ".specify/autopilot/context/thread.md"
    assert "Keep the CSV columns in the same order" in thread_md.read_text()


def test_commands_stop_resume_status(make_project):
    root = make_project([_epic("E1", "alpha")])
    run = _plan(root)
    bridge, app = _bridge(root)
    bridge.tick()
    ref, thread = app.thread_by_title(f"Run {run}")
    app.say(ref, "stop")
    bridge.tick()
    assert driver.stop_file(root).exists()
    app.say(ref, "Resume")
    app.say(ref, "status")
    bridge.tick()
    assert not driver.stop_file(root).exists()
    assert thread["messages"][-1].text.startswith(f"Run {run}:")


# ── delivery guarantees ──────────────────────────────────────────────────────


def test_delivery_resumes_in_order_after_an_outage(tmp_path, make_project):
    root = make_project([_epic("E1", "alpha")])
    run = _plan(root)
    bridge, app = _bridge(root)
    bridge.tick()
    key = chat.run_thread(run)
    for index in range(3):
        chat.post(root, key, "autopilot", f"update {index}")
    app.fail_next = ConnectorError("slack chat.postMessage: rate limited", retryable=True, retry_after=0)
    result = bridge.tick()
    assert result["pending"] == 3 and bridge.last_error
    bridge.tick()
    _, thread = app.thread_by_title(f"Run {run}")
    assert [m.text for m in thread["messages"][-3:]] == ["update 0", "update 1", "update 2"]
    assert json.loads((root / ".specify/autopilot/chat/health.json").read_text())["ok"] is True


def test_a_backlog_is_folded_into_one_digest(make_project):
    root = make_project([_epic("E1", "alpha")])
    run = _plan(root)
    config = _enable_chat(root)
    config["chat"]["max_posts_per_minute"] = 5
    app = FakeApp()
    bridge = bridge_mod.Bridge(root, config, app)
    bridge.tick()  # connected: the plan is posted
    bridge.recent_posts.clear()
    for index in range(30):
        chat.post(root, chat.run_thread(run), "autopilot", f"loop {index}")
    bridge.tick()
    _, thread = app.thread_by_title(f"Run {run}")
    digest = thread["messages"][-1].text
    assert digest.startswith("30 updates") and "loop 29" in digest and len(thread["messages"]) <= 4
    assert bridge.pending() == 0


def test_bad_credentials_stop_the_bridge_instead_of_looping(make_project):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    config = _enable_chat(root)
    app = FakeApp()
    app.fail_next = ConnectorError("slack chat.postMessage: invalid_auth", fatal=True)
    code, out = bridge_mod.serve(root, config, connector=app)
    assert code == 1 and "invalid_auth" in out["error"]
    assert json.loads((root / ".specify/autopilot/chat/health.json").read_text())["ok"] is False


def test_a_new_app_needs_confirmation_before_posting_history(make_project):
    root = make_project([_epic("E1", "alpha")])
    run = _plan(root)
    chat.post(root, chat.run_thread(run), "owner", "An old note.", kind="note")
    bridge, app = _bridge(root)
    assert bridge.tick() == {"blocked": "replay confirmation needed"} and not app.threads
    code, out, _ = _cli(root, "chat", "replay")
    assert code == 1  # needs --confirm
    assert bridge_mod.confirm_replay(root, load_config(root))["replay"] is True
    bridge, app = _bridge(root, app)
    bridge.state["connected_at"] = "2099-01-01T00:00:00+00:00"  # everything so far counts as history
    bridge.tick()
    _, thread = app.thread_by_title(f"Run {run}")
    assert all("replayed" in m.text for m in thread["messages"])


def test_status_reports_chat_health(make_project):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    assert _cli(root, "status")[1]["chat"] == {"provider": "none"}
    _enable_chat(root)
    status = _cli(root, "status")[1]["chat"]
    assert status["provider"] == "slack" and status["bridge_running"] is False
