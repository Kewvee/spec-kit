"""The bridge: one background process that connects the team journal to a chat app.

Plain code, no AI, the only part of the autopilot that uses the network. Each
tick (``chat.poll_seconds``):

1. **Mirror** new scheduler events from ``log.jsonl`` into the journal.
2. **Listen** to the run thread and active epic threads. Only messages from
   ``chat.<provider>.owner_id`` count; everything else is ignored and never
   reaches an agent. Each owner message is journaled, then run as a command
   (approve/reject a gate, answer a question, stop, resume, status) or kept as a
   note for the developers.
3. **Deliver** journal messages the chat app has not seen, in order, one thread
   per epic plus one for the run, with live status tags. A flood guard caps
   posts per minute and folds a backlog into one digest.
4. Write ``.specify/autopilot/chat/health.json``.

Delivery state is local and per provider (``chat/<provider>.json``). If it is
missing while the journal already has messages (a new provider, a fresh clone),
nothing is posted until ``chat replay --confirm``, so a switch never floods a
channel by accident.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import chat, driver
from .connector import TAG_NAMES, Connector, ConnectorError, Inbound, Out, display_name, make_connector  # noqa: F401
from .util import LockHeld, ensure_state_dir, now_iso, read_json_lenient, write_json

CLI = Path(__file__).resolve().parents[1] / "autopilot.py"
EPIC_TAGS = {
    "pending": "pending", "running": "running", "passed": "running", "awaiting-human": "needs-owner",
    "merged": "merged", "failed": "failed", "escalated": "needs-owner", "blocked": "blocked",
}
EPIC_LINES = {
    "pending": "⚪ waiting to start", "running": "🟡 in progress", "passed": "🟡 passed, merging",
    "awaiting-human": "🔵 waiting for the product owner", "merged": "🟢 merged", "failed": "🔴 failed",
    "escalated": "🟠 needs a human", "blocked": "⚫ blocked by a dependency",
}
RUN_LINES = {
    "awaiting-plan-approval": ("needs-owner", "🔵 plan waiting for approval"),
    "plan-rejected": ("blocked", "⚫ plan rejected"),
    "driving": ("running", "🟡 running"),
    "awaiting-human": ("needs-owner", "🔵 questions for the product owner"),
    "interrupted": ("blocked", "⚪ interrupted"),
    "awaiting-land": ("needs-owner", "🔵 final merge waiting for approval"),
    "finished-nothing-merged": ("failed", "🔴 finished, nothing merged"),
    "land-rejected": ("blocked", "⚫ final merge held"),
    "landed": ("landed", "🟢 landed"),
}
FINAL_STAGES = ("landed", "plan-rejected", "land-rejected", "no-plan")
MENTION_RE = re.compile(r"^(?:\s*<@!?[A-Za-z0-9]+>\s*)+")
GATE_RE = re.compile(r"^(approve|reject)\s+(plan|land)\s+(\S+)\s*(.*)$", re.IGNORECASE | re.DOTALL)
ANSWER_RE = re.compile(r"^answer\s+(\S+?)[\s:]+(.+)$", re.IGNORECASE | re.DOTALL)
ACTIVE_SECONDS = 24 * 3600


class BridgeError(RuntimeError):
    pass


def state_path(root: Path, provider: str) -> Path:
    return chat.chat_dir(root) / f"{provider}.json"


def stop_path(root: Path) -> Path:
    return chat.chat_dir(root) / "STOP"


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


class Bridge:
    def __init__(self, root: Path, config: dict[str, Any], connector: Connector, clock=time.time) -> None:
        self.root = root
        self.config = config
        self.connector = connector
        self.provider = connector.name
        self.owner_id = str(config["chat"][self.provider].get("owner_id", "")).strip()
        self.clock = clock
        self.path = state_path(root, self.provider)
        loaded = read_json_lenient(self.path)
        self.fresh = not isinstance(loaded, dict)
        self.state: dict[str, Any] = loaded if isinstance(loaded, dict) else {
            "provider": self.provider, "log_offset": 0, "threads": {}, "top_cursor": None,
            "connected_at": now_iso(), "replay_confirmed": False,
        }
        self.recent_posts: list[float] = []
        self._stage: str | None = None
        self.last_error = ""
        self.backoff_until = 0.0

    # ── persistence ──────────────────────────────────────────────────────

    def save(self) -> None:
        write_json(self.path, self.state)

    def needs_replay_confirmation(self) -> bool:
        if not self.fresh or self.state.get("replay_confirmed"):
            return False
        return any(chat.load_thread(self.root, key) for key in chat.thread_keys(self.root))

    # ── one tick ─────────────────────────────────────────────────────────

    def tick(self) -> dict[str, Any]:
        if self.needs_replay_confirmation():
            self.last_error = ("the journal already has messages but this chat app has none: run "
                               "`autopilot.py chat replay --confirm` to post the history, or start fresh")
            self.write_health(blocked=True)
            return {"blocked": "replay confirmation needed"}
        if self.fresh:
            self.fresh = False
            self.save()
        mirrored = self.mirror_events()
        handled = 0
        delivered = 0
        if self.clock() >= self.backoff_until:
            try:
                handled = self.listen()
                delivered = self.deliver()
                self.last_error = ""
                self.state["last_ok_at"] = now_iso()
            except ConnectorError as exc:
                self.last_error = str(exc)
                if exc.fatal:
                    self.save()
                    self.write_health()
                    raise
                if exc.retryable:
                    self.backoff_until = self.clock() + (30.0 if exc.retry_after is None else exc.retry_after)
        self.save()
        self.write_health()
        return {"mirrored": mirrored, "handled": handled, "delivered": delivered, "pending": self.pending()}

    # ── 1. scheduler events → journal ────────────────────────────────────

    def mirror_events(self) -> int:
        log_path = ensure_state_dir(self.root) / "log.jsonl"
        if not log_path.is_file():
            self.state["log_offset"] = 0
            return 0
        size = log_path.stat().st_size
        offset = int(self.state.get("log_offset", 0))
        if offset > size:
            offset = 0  # the log was archived by new-run and started over
        with log_path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
        self.state["log_offset"] = offset + len(complete)
        count = 0
        state = read_json_lenient(ensure_state_dir(self.root) / "state.json")
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(state, dict):
                for message in render_event(self.root, self.config, state, event):
                    chat.post(self.root, **message)
                    count += 1
        return count

    # ── 2. owner replies → commands and notes ────────────────────────────

    def active_threads(self) -> list[str]:
        state = read_json_lenient(ensure_state_dir(self.root) / "state.json")
        keys: list[str] = []
        if isinstance(state, dict):
            run = driver.run_id(state)
            keys.append(chat.run_thread(run))
            for epic in state["epics"].values():
                key = chat.epic_thread(run, epic["id"])
                path = chat.thread_path(self.root, key)
                recent = path.is_file() and self.clock() - path.stat().st_mtime < ACTIVE_SECONDS
                if epic["status"] not in ("merged", "blocked") or recent:
                    keys.append(key)
        return [k for k in keys if self.state["threads"].get(k, {}).get("ref")]

    def listen(self) -> int:
        handled = 0
        if not self.owner_id:
            return 0
        for key in self.active_threads():
            info = self.state["threads"][key]
            messages, info["cursor"] = self.connector.fetch(info["ref"], info.get("cursor"))
            for message in messages:
                if not message.is_bot and message.author_id == self.owner_id:
                    self.handle(key, info["ref"], message)
                    handled += 1
            self.save()
        if self.connector.caps.top_level:
            messages, self.state["top_cursor"] = self.connector.fetch_top_level(self.state.get("top_cursor"))
            state = read_json_lenient(ensure_state_dir(self.root) / "state.json")
            run_key = chat.run_thread(driver.run_id(state)) if isinstance(state, dict) else None
            for message in messages:
                if not message.is_bot and message.author_id == self.owner_id and run_key:
                    self.handle(run_key, None, message)
                    handled += 1
        return handled

    def handle(self, key: str, thread_ref: str | None, message: Inbound) -> None:
        text = MENTION_RE.sub("", message.text).strip()
        if not text:
            return
        kind, action = self.classify(key, text)
        # The owner's words go into the journal first, so the thread reads in order.
        chat.post(self.root, key, "owner", text, kind=kind, origin={"provider": self.provider, "ref": message.ref})
        reply, ok = action() if action else ("", True)
        if reply:
            chat.post(self.root, key, "autopilot", reply, kind="reply")
        try:
            self.connector.acknowledge(thread_ref or "", message.ref, ok)
        except ConnectorError:
            pass  # a missing reaction is cosmetic

    def classify(self, key: str, text: str):
        """What an owner message means: (journal kind, action returning (reply, ok)) or a note."""
        lowered = text.lower().strip().rstrip(".!")
        gate = GATE_RE.match(text)
        if gate:
            verb, which, run, why = gate.groups()

            def decide() -> tuple[str, bool]:
                code, out = driver.approve(
                    self.root, self.config, which.lower(), accept=verb.lower() == "approve",
                    by=f"product owner ({self.provider} {self.owner_id})", via=self.provider, note=why,
                    expected_run=run, cli=CLI,
                )
                return _approval_reply(which.lower(), verb.lower(), code, out), code == 0

            return "command", decide
        answer = ANSWER_RE.match(text)
        if answer:
            return "answer", lambda: self.answer(answer.group(1), answer.group(2))
        if lowered == "stop":
            def stop_run() -> tuple[str, bool]:
                path = driver.stop_file(self.root)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("stop requested from chat\n", encoding="utf-8")
                return "Stop requested: no new epics start; running ones finish.", True

            return "command", stop_run
        if lowered == "resume":
            def resume_run() -> tuple[str, bool]:
                driver.unstop(self.root)
                return "Stop withdrawn: epics start again while the drive runs.", True

            return "command", resume_run
        if lowered == "status":
            return "command", lambda: (status_text(self.root), True)
        epic = _epic_of(key)
        if epic:
            waiting = [q for q in driver.human_questions(self.root) if q.get("epic") == epic]
            if len(waiting) == 1:
                return "answer", lambda: self.answer(waiting[0]["id"], text)
        return "note", None

    def answer(self, question_id: str, text: str) -> tuple[str, bool]:
        code, out = driver.answer(self.root, question_id, text.strip())
        if code == 0:
            rest = f" Still waiting: {', '.join(out['still_open'])}." if out.get("still_open") else ""
            return f"Recorded as the answer to {question_id}; it is now a team decision.{rest}", True
        return f"Could not record that answer: {out.get('error')}", False

    # ── 3. journal → chat app ────────────────────────────────────────────

    def pending(self) -> int:
        total = 0
        for key in chat.thread_keys(self.root):
            total += max(0, len(chat.load_thread(self.root, key)) - self.state["threads"].get(key, {}).get("delivered", 0))
        return total

    def _budget(self) -> int:
        now = self.clock()
        self.recent_posts = [t for t in self.recent_posts if now - t < 60]
        return max(0, int(self.config["chat"]["max_posts_per_minute"]) - len(self.recent_posts))

    def _out(self, message: dict[str, Any]) -> Out:
        text = message["text"]
        if _ts(str(message.get("at", ""))) < _ts(str(self.state.get("connected_at", ""))) - 1:
            text = f"_(from {str(message.get('at', ''))[:16].replace('T', ' ')} UTC, replayed)_\n{text}"
        meta = {"run": "", **message.get("meta", {}), "thread": message["thread"], "kind": message.get("kind", ""),
                "author": message["author"], "id": message["id"]}
        return Out(author=display_name(message["author"]), text=text,
                   mention_owner=bool(message.get("mention_owner")), meta={k: v for k, v in meta.items() if v})

    def deliver(self) -> int:
        delivered = 0
        self._stage = None
        keys = sorted(chat.thread_keys(self.root), key=lambda k: (not k.startswith("run-"), k))
        for key in keys:
            info = self.state["threads"].setdefault(key, {"delivered": 0})
            messages = chat.load_thread(self.root, key)
            start = info["delivered"]
            outgoing = [m for m in messages[start:] if not m.get("origin")]
            if not outgoing:
                info["delivered"] = len(messages)
                continue
            budget = self._budget()
            if budget == 0:
                continue
            title, status, tags = self.thread_header(key)
            if len(outgoing) > budget:
                # A backlog bigger than this minute's allowance: fold it into one digest.
                self._send(info, title, status, tags, _digest(outgoing))
                info["delivered"] = len(messages)
                delivered += 1
                self.save()
            else:
                for index in range(start, len(messages)):
                    if not messages[index].get("origin"):
                        self._send(info, title, status, tags, messages[index])
                        delivered += 1
                    info["delivered"] = index + 1  # saved per message: a crash never re-posts
                    self.save()
        # Live status of every thread (new epics state, merges, questions).
        for key, info in self.state["threads"].items():
            if info.get("ref"):
                title, status, tags = self.thread_header(key)
                if info.get("status") != status:
                    self.connector.set_status(info["ref"], title, status, tags)
                    info["status"] = status
                    self.save()
        return delivered

    def _send(self, info: dict[str, Any], title: str, status: str, tags: list[str], message: dict[str, Any]) -> None:
        out = self._out(message)
        if not info.get("ref"):
            info["ref"] = self.connector.open_thread(title, status, tags, out)
            info["status"] = status
        else:
            self.connector.post(info["ref"], out)
        self.recent_posts.append(self.clock())

    def _run_stage(self) -> str:
        if getattr(self, "_stage", None) is None:
            self._stage = driver.run_status(self.root)["stage"]
        return self._stage

    def thread_header(self, key: str) -> tuple[str, str, list[str]]:
        state = read_json_lenient(ensure_state_dir(self.root) / "state.json")
        if isinstance(state, dict):
            run = driver.run_id(state)
            if key == chat.run_thread(run):
                tag, line = RUN_LINES.get(self._run_stage(), ("running", "🟡 running"))
                return f"Run {run}", line, ["run", tag]
            for epic in state["epics"].values():
                if key == chat.epic_thread(run, epic["id"]):
                    return (f"{epic['id']} · {epic['title']} ({run})", EPIC_LINES.get(epic["status"], epic["status"]),
                            ["epic", EPIC_TAGS.get(epic["status"], "running")])
        if key.startswith("run-"):
            return f"Run {key[4:]}", "archived", ["run"]
        return key, "", ["epic"]

    # ── health ───────────────────────────────────────────────────────────

    def write_health(self, blocked: bool = False) -> None:
        write_json(chat.chat_dir(self.root) / "health.json", {
            "provider": self.provider,
            "ok": not self.last_error and not blocked,
            "last_ok_at": self.state.get("last_ok_at"),
            "last_error": self.last_error,
            "pending": self.pending(),
            "backoff_until": datetime.fromtimestamp(self.backoff_until, timezone.utc).isoformat()
            if self.backoff_until > self.clock() else None,
            "owner_configured": bool(self.owner_id),
            "checked_at": now_iso(),
        })


# ── rendering ────────────────────────────────────────────────────────────────


def _epic_of(key: str) -> str | None:
    match = re.fullmatch(r"r\d{4}-[0-9a-f]+-(.+)", key)
    return match.group(1) if match else None


def _digest(messages: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [f"{len(messages)} updates (folded to stay under the posting limit):"]
    for message in messages:
        first = " ".join(message["text"].split())[:200]
        lines.append(f"• {chat.author_label(message['author'])}: {first}")
    last = messages[-1]
    return {**last, "author": "autopilot", "kind": "event", "text": "\n".join(lines),
            "mention_owner": any(m.get("mention_owner") for m in messages)}


def _approval_reply(gate: str, verb: str, code: int, out: dict[str, Any]) -> str:
    if code != 0 and not out.get("verdict"):
        return f"Could not {verb} the {gate}: {out.get('error') or out}"
    if verb == "reject":
        return f"The {gate} is rejected for run {out.get('run_id')}. Nothing more happens until the lead revises it."
    if gate == "plan":
        drive = out.get("drive") or {}
        if drive.get("started") or drive.get("already_running"):
            return f"Plan approved for run {out.get('run_id')}: the team starts now."
        return f"Plan approved, but the drive did not start: {drive.get('error', 'unknown error')}"
    landed = out.get("land") or {}
    if landed.get("landed"):
        return f"Final merge approved and landed at `{str(landed.get('commit'))[:12]}`."
    return f"Final merge approved, but landing failed: {landed.get('reason')}"


def status_text(root: Path) -> str:
    status = driver.run_status(root)
    if "epics" not in status:
        return f"Stage: {status['stage']}."
    lines = [f"Run {status.get('run_id')}: {status['stage']}, agent calls ≈ {status.get('agent_calls_used')}."]
    lines += [f"• {e['id']}: {e['status']}" + (f" ({e['reason'][:120]})" if e.get("reason") else "") for e in status["epics"]]
    if status.get("questions_for_human"):
        lines.append("Questions for you: " + ", ".join(q["id"] for q in status["questions_for_human"]))
    return "\n".join(lines)


def _message(thread: str, author: str, text: str, kind: str = "event", mention: bool = False,
             **meta: Any) -> dict[str, Any]:
    return {"thread": thread, "author": author, "text": text, "kind": kind, "mention_owner": mention, "meta": meta}


def render_event(root: Path, config: dict[str, Any], state: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    """Scheduler events (``log.jsonl``) as journal messages."""
    run = driver.run_id(state)
    name = event.get("event")
    epic_id = event.get("epic")
    epic = state["epics"].get(epic_id) if epic_id else None
    run_key = chat.run_thread(run)
    epic_key = chat.epic_thread(run, epic_id) if epic else ""
    reason = str(event.get("reason", ""))[:500]
    meta = {"run": run, "event": name, "epic": epic_id}
    if name == "planned" and event.get("run") == run:
        return [_message(run_key, "lead", plan_text(config, state), "plan", True, **meta)]
    if name in ("approved", "rejected"):
        via = event.get("via", "")
        text = f"The {event.get('gate')} was {name} by the product owner" + (f" ({via})." if via else ".")
        return [_message(run_key, "autopilot", text, "approval", **meta)]
    if name == "drive":
        return [_message(run_key, "autopilot", f"The team is working: {event.get('epics')} epic(s), up to "
                         f"{event.get('max_parallel')} at a time.", **meta)]
    if name == "halt":
        return [_message(run_key, "autopilot", f"No new epics start: {reason}.", mention=True, **meta)]
    if name == "resumed":
        return [_message(run_key, "autopilot", "The stop was withdrawn; epics start again.", **meta)]
    if name == "drive-finished":
        return [_message(run_key, "autopilot", report_text(root, state), "report", True, **meta)]
    if name == "landed":
        return [_message(run_key, "autopilot", f"Landed on `{state['base_branch']}` at `{str(event.get('commit'))[:12]}`.",
                         **meta)]
    if not epic:
        return []
    if name == "start":
        return [_message(epic_key, "autopilot", f"Started (attempt {event.get('attempt')}).", **meta)]
    if name == "passed":
        return [_message(epic_key, "autopilot", f"Gate and independent review passed after "
                         f"{event.get('iterations')} iteration(s). Merging.", **meta)]
    if name == "merged":
        return [_message(epic_key, "autopilot", f"Merged into the integration branch: {reason}.", **meta),
                _message(run_key, "autopilot", f"{epic_id} ({epic['title']}) merged.", **meta)]
    if name in ("failed", "escalated", "blocked"):
        verb = {"failed": "failed", "escalated": "needs a human", "blocked": "is blocked"}[name]
        return [_message(epic_key, "autopilot", f"This epic {verb}: {reason}", mention=name != "blocked", **meta),
                _message(run_key, "autopilot", f"{epic_id} ({epic['title']}) {verb}: {reason[:200]}", **meta)]
    if name == "question":
        return [_message(run_key, "autopilot", f"{epic_id} waits for a product decision; see its thread.", **meta)]
    if name == "answered":
        return [_message(epic_key, "autopilot", "Answered by the product owner: resuming.", **meta)]
    if name == "retry":
        return [_message(epic_key, "autopilot", f"Restarting after an infrastructure failure (attempt "
                         f"{event.get('attempt')}).", **meta)]
    return []


def plan_text(config: dict[str, Any], state: dict[str, Any]) -> str:
    run = driver.run_id(state)
    epics = [state["epics"][i] for i in state["order"]]
    lines = [f"**Plan for run {run}**: {len(epics)} epic(s) on `{state['base_branch']}`.", ""]
    for epic in epics[:30]:
        deps = ", ".join(epic["depends_on"]) or "nothing"
        provides = "; ".join(epic.get("provides", []))[:160]
        lines.append(f"• **{epic['id']}** {epic['title']} (after {deps})" + (f": provides {provides}" if provides else ""))
    if len(epics) > 30:
        lines.append(f"• … and {len(epics) - 30} more")
    worst = driver.worst_case_calls(config) * len(epics)
    lines += [
        "",
        f"Worst case ≈ {worst} agent calls (budget {config['run']['max_agent_calls']}), up to "
        f"{config['run']['max_parallel']} in parallel. Full plan and team handbook: `.specify/autopilot/dag.md`.",
        "",
        f"Reply `approve plan {run}` to start the team, or `reject plan {run} <why>`.",
    ]
    return "\n".join(lines)


def report_text(root: Path, state: dict[str, Any]) -> str:
    run = driver.run_id(state)
    summary = driver.status_summary(state)
    counts = ", ".join(f"{n} {s}" for s, n in sorted(summary["counts"].items()))
    lines = [f"**Run {run} finished**: {counts}." + (f" Halted: {summary['halted']}." if summary["halted"] else "")]
    stuck = [e for e in summary["epics"] if e["status"] in ("failed", "escalated", "blocked")]
    for epic in stuck:
        lines.append(f"• {epic['id']} {epic['status']}: {epic['reason'][:200]}")
    if summary["counts"].get("merged"):
        lines += ["", f"Merged work is on `{state['integration_branch']}`. Report: `.specify/autopilot/report.md`.",
                  f"Reply `approve land {run}` to merge it into `{state['base_branch']}`, or `reject land {run} <why>`."]
    else:
        lines.append("Nothing merged, so there is nothing to land.")
    return "\n".join(lines)


# ── process control ──────────────────────────────────────────────────────────


def serve(root: Path, config: dict[str, Any], connector: Connector | None = None, once: bool = False,
          sleep=time.sleep) -> tuple[int, dict[str, Any]]:
    """Run the bridge until stopped, or the run is over and everything is delivered."""
    lock = chat.bridge_lock(root)
    try:
        lock.acquire()
    except LockHeld as exc:
        return 0, {"started": False, "already_running": True, **exc.owner}
    try:
        try:
            connector = connector or make_connector(config)
        except ConnectorError as exc:
            write_json(chat.chat_dir(root) / "health.json", {
                "provider": config["chat"]["provider"], "ok": False, "last_error": str(exc), "checked_at": now_iso()})
            return 1, {"ok": False, "error": str(exc)}
        bridge = Bridge(root, config, connector)
        stop_path(root).unlink(missing_ok=True)
        result: dict[str, Any] = {}
        while True:
            try:
                result = bridge.tick()
            except ConnectorError as exc:
                return 1, {"ok": False, "error": str(exc)}
            if once or stop_path(root).exists():
                break
            stage = driver.run_status(root)["stage"]
            if stage in FINAL_STAGES and result.get("pending", 0) == 0 and driver.drive_owner(root) is None:
                break
            for _ in range(max(1, int(config["chat"]["poll_seconds"]))):
                if stop_path(root).exists():
                    break
                sleep(1)
        stop_path(root).unlink(missing_ok=True)
        return 0, {"ok": True, **result}
    finally:
        lock.release()


def ensure_running(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Start the bridge in the background when a chat app is configured and it is not running."""
    if config["chat"]["provider"] == "none":
        return {"chat": "off"}
    owner = chat.bridge_lock(root).owner()
    if owner is not None:
        return {"chat": config["chat"]["provider"], "bridge_running": True, "pid": owner.get("pid")}
    import subprocess

    out_path = chat.chat_dir(root) / "bridge.out"
    kwargs: dict[str, Any] = {"cwd": str(root), "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    with out_path.open("ab") as out:
        process = subprocess.Popen([sys.executable, str(CLI), "chat", "bridge"], stdout=out, **kwargs)
    return {"chat": config["chat"]["provider"], "bridge_running": True, "pid": process.pid, "started": True}


def stop(root: Path) -> dict[str, Any]:
    owner = chat.bridge_lock(root).owner()
    if owner is None:
        return {"stopped": False, "running": False}
    stop_path(root).write_text("stop\n", encoding="utf-8")
    return {"stopped": True, "pid": owner.get("pid")}


def confirm_replay(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Allow a provider with no delivery state to post the whole journal (marked as replayed)."""
    provider = config["chat"]["provider"]
    if provider == "none":
        return {"ok": False, "error": "chat.provider is none"}
    path = state_path(root, provider)
    existing = read_json_lenient(path)
    if isinstance(existing, dict) and existing.get("threads"):
        return {"ok": True, "replay": False, "reason": "this provider already has delivery state; nothing to replay"}
    write_json(path, {"provider": provider, "log_offset": _log_size(root), "threads": {}, "top_cursor": None,
                      "connected_at": now_iso(), "replay_confirmed": True})
    threads = chat.thread_keys(root)
    return {"ok": True, "replay": True, "threads": len(threads),
            "messages": sum(len(chat.load_thread(root, k)) for k in threads)}


def _log_size(root: Path) -> int:
    path = ensure_state_dir(root) / "log.jsonl"
    return path.stat().st_size if path.is_file() else 0


def check(root: Path, config: dict[str, Any], connector: Connector | None = None, hello: bool = False) -> tuple[int, dict[str, Any]]:
    """Verify credentials, permissions and the owner ID; optionally post a hello message."""
    provider = config["chat"]["provider"]
    if provider == "none":
        return 1, {"ok": False, "error": "chat.provider is none: set slack or discord first"}
    problems = []
    if not str(config["chat"][provider].get("owner_id", "")).strip():
        problems.append(f"chat.{provider}.owner_id is empty: the bridge would ignore every reply")
    try:
        connector = connector or make_connector(config)
        report = connector.check()
    except ConnectorError as exc:
        return 1, {"ok": False, "provider": provider, "problems": problems + [str(exc)]}
    problems += report.pop("problems", [])
    if hello and not problems:
        try:
            ref = connector.open_thread("Autopilot connected", "", ["run"], Out(
                author="autopilot", text="The spec-kit autopilot can post here. Threads for runs and epics will appear "
                "in this place; only the product owner's replies count.", mention_owner=True))
            report["hello"] = ref
        except ConnectorError as exc:
            problems.append(f"posting failed: {exc}")
    return (1 if problems else 0), {"ok": not problems, "provider": provider, "problems": problems, **report}
