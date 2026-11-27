"""The team's conversation: one thread per epic plus one for the run.

Every message the team says (a developer's question, the lead's answer, a gate
result, a decision, the product owner's reply) is appended to a **thread
journal** in the conductor's checkout::

    .specify/autopilot/threads/run-<run-id>.jsonl    plan, approvals, progress, report
    .specify/autopilot/threads/<run-id>-<EPIC>.jsonl one epic's whole story

The journal is the team's permanent memory. Agents read it as
``context/thread.md`` before every step; the bridge (``bridge.py``) mirrors it
to the configured chat app (Slack or Discord) so humans can follow and reply.
Writing here never touches the network: a chat outage never stops the team.

One JSON object per line::

    {"id", "at", "thread", "author", "kind", "text", "meta", "mention_owner",
     "origin": {"provider", "ref"}}      # origin: only on messages from the chat app

``author`` is a role (``lead``, ``dev-<EPIC>``, ``reviewer``, ``autopilot``,
``owner``); ``meta`` holds flat lookup tags (run, epic, question, decision,
event, iteration, status).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .util import STATE_DIR, ProcessLock, append_jsonl, ensure_state_dir, read_json_lenient

THREADS_DIR = "threads"
CHAT_DIR = "chat"
CONTEXT_LIMIT = 80   # messages rendered into an agent's thread.md
CONTEXT_CLIP = 1500  # characters per message in thread.md
KINDS = ("plan", "approval", "event", "question", "answer", "owner-question", "decision", "gate", "review",
         "report", "note", "reply")


def main_root(project_root: Path) -> Path:
    """The conductor's checkout (shared journal and decisions); this checkout when standalone."""
    raw = os.environ.get("AUTOPILOT_ROOT", "").strip()
    return Path(raw) if raw else project_root


# ── thread keys ──────────────────────────────────────────────────────────────


def run_thread(run_id: str) -> str:
    return f"run-{run_id}"


def epic_thread(run_id: str, epic_id: str) -> str:
    return f"{run_id}-{epic_id}"


def pipe_thread(pipe_state: dict[str, Any]) -> str:
    """The thread of the pipe running here: its epic's, or its feature's when standalone."""
    run_id = os.environ.get("AUTOPILOT_RUN", "").strip()
    epic_id = pipe_state.get("epic_id")
    if run_id and epic_id:
        return epic_thread(run_id, epic_id)
    return Path(str(pipe_state.get("feature_dir") or "feature")).name


def developer(pipe_state: dict[str, Any]) -> str:
    return f"dev-{pipe_state.get('epic_id') or Path(str(pipe_state.get('feature_dir') or 'dev')).name}"


def _safe_name(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key) or "unnamed"


def threads_dir(root: Path) -> Path:
    path = ensure_state_dir(root) / THREADS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def thread_path(root: Path, key: str) -> Path:
    return threads_dir(root) / f"{_safe_name(key)}.jsonl"


def thread_keys(root: Path) -> list[str]:
    directory = root / STATE_DIR / THREADS_DIR
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.jsonl"))


# ── writing and reading ──────────────────────────────────────────────────────


def _message_id() -> str:
    return f"{time.time_ns():x}-{os.getpid():x}"


def post(
    root: Path,
    thread: str,
    author: str,
    text: str,
    *,
    kind: str = "event",
    meta: dict[str, Any] | None = None,
    mention_owner: bool = False,
    origin: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Append one message to a thread journal. No network: the bridge delivers it."""
    record: dict[str, Any] = {
        "id": _message_id(),
        "thread": thread,
        "author": author,
        "kind": kind,
        "text": text.strip(),
        "meta": {k: str(v) for k, v in (meta or {}).items() if v not in (None, "")},
        "mention_owner": mention_owner,
    }
    if origin:
        record["origin"] = origin
    append_jsonl(thread_path(root, thread), record)
    return record


def safe_post(root: Path, thread: str, author: str, text: str, **kwargs: Any) -> None:
    """``post`` for pipe steps: the conversation must never fail a gate or a consult."""
    try:
        post(root, thread, author, text, **kwargs)
    except OSError:
        pass


def load_thread(root: Path, key: str) -> list[dict[str, Any]]:
    path = thread_path(root, key)
    if not path.is_file():
        return []
    messages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line from a crash mid-write
        if isinstance(record, dict) and isinstance(record.get("text"), str) and isinstance(record.get("author"), str):
            messages.append(record)
    return messages


# ── what agents see ──────────────────────────────────────────────────────────

AUTHOR_LABELS = {"owner": "Product owner", "lead": "Team lead", "reviewer": "Reviewer", "autopilot": "Autopilot"}


def author_label(author: str) -> str:
    if author.startswith("dev-"):
        return f"Developer ({author[4:]})"
    return AUTHOR_LABELS.get(author, author)


def _stamp(message: dict[str, Any]) -> str:
    return str(message.get("at", ""))[:16].replace("T", " ")


def render_thread(title: str, messages: list[dict[str, Any]], team_notes: list[dict[str, Any]]) -> str:
    """The thread as Markdown for agents (``context/thread.md``)."""
    lines = [
        f"# Team thread: {title}",
        "",
        "Your epic's conversation so far, oldest first: questions, the lead's answers, gate and review",
        "results, decisions, and notes from the product owner. It is the team's memory: read it to know",
        "what was already tried, asked and decided. A note from the **Product owner** binds like a team",
        "decision. Do not reply here; ask the lead through the inbox (see epic-context.md).",
        "",
    ]
    if team_notes:
        lines += ["## Notes from the product owner to the whole team", ""]
        lines += [f"- {_stamp(n)}: {' '.join(n['text'].split())[:CONTEXT_CLIP]}" for n in team_notes]
        lines.append("")
    lines += ["## This epic, oldest first", ""]
    if not messages:
        lines.append("_Nothing yet: you are the first to work on this epic._")
    shown = messages[-CONTEXT_LIMIT:]
    if len(messages) > len(shown):
        lines += [f"_{len(messages) - len(shown)} older message(s) omitted._", ""]
    for message in shown:
        text = message["text"]
        if len(text) > CONTEXT_CLIP:
            text = text[:CONTEXT_CLIP] + " …"
        tags = ", ".join(f"{k}={v}" for k, v in message.get("meta", {}).items() if k in ("question", "decision", "iteration"))
        lines += [f"**{author_label(message['author'])}** · {_stamp(message)}" + (f" · {tags}" if tags else ""), "", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def owner_notes(root: Path, key: str) -> list[dict[str, Any]]:
    return [m for m in load_thread(root, key) if m["author"] == "owner" and m.get("kind") == "note"]


def write_thread_context(root: Path, target_dir: Path, key: str, title: str, run_key: str | None = None) -> Path:
    """Render one thread (plus the run thread's owner notes) into a checkout's context directory."""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "thread.md"
    notes = owner_notes(root, run_key) if run_key else []
    path.write_text(render_thread(title, load_thread(root, key), notes), encoding="utf-8")
    return path


# ── chat status (read by `status` without importing the bridge) ──────────────


def chat_dir(root: Path) -> Path:
    path = ensure_state_dir(root) / CHAT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def bridge_lock(root: Path) -> ProcessLock:
    return ProcessLock(chat_dir(root) / "bridge.lock")


def health(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    provider = config.get("chat", {}).get("provider", "none")
    if provider == "none":
        return {"provider": "none"}
    owner = bridge_lock(root).owner()
    data = read_json_lenient(chat_dir(root) / "health.json", {}) or {}
    return {"provider": provider, "bridge_running": owner is not None,
            "bridge_pid": owner.get("pid") if owner else None, **(data if isinstance(data, dict) else {})}
