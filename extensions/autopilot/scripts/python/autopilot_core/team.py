"""The team around each pipe: shared context, questions to the lead, decisions.

The autopilot runs like a development team. The conductor is the **team lead**,
each pipe is a **developer**, the person running it is the **product owner**.
No developer works blind:

* **Context** — before every step a developer reads ``.specify/autopilot/context/``:
  ``program.md`` (the lead's team handbook: product, architecture, conventions,
  contracts between epics), ``epic-context.md`` (generated here: where this
  epic fits, what is already built, who is working in parallel on what, who
  waits on it, how to ask) and ``decisions.md`` (every answer given so far).
* **Questions** — a developer that hits a costly-to-guess question writes a
  question file to ``.specify/autopilot/inbox/questions/`` and carries on with
  other work. Unresolved ``[NEEDS CLARIFICATION`` markers in spec.md become
  questions too. ``collect`` files them; the lead agent answers or marks one
  ``needs_human``; ``record`` turns answers into program-wide decisions and
  hands them back as spec edits or tasks. A question for the product owner
  pauses only that developer (``awaiting-human``).
* **Decisions** — every answer is published to the main checkout's decision
  log, so a later developer asking the same question gets the same answer
  without another lead call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import chat
from . import spec as spec_mod
from .gate import LOOP_END, _rel, ensure_pipe, pipe_dir, save_pipe
from .questions import answered as _answered
from .questions import load_questions
from .questions import needs_human as _needs_human
from .util import (
    ProcessLock,
    ensure_state_dir,
    now_iso,
    read_json_lenient,
    safe_id,
    tree_fingerprint,
    write_json,
)

CONTEXT_DIR = Path(".specify") / "autopilot" / "context"
INBOX_DIR = Path(".specify") / "autopilot" / "inbox" / "questions"
MARKER_RE = re.compile(r"\[NEEDS CLARIFICATION:?\s*([^\]]*)\]")
QUESTION_PROTOCOL = """## Asking the team lead

Decide small things yourself and record them as assumptions (in research.md or
the spec's Assumptions section). Ask the lead when a wrong guess would be costly
to undo: a contract another epic uses, the data model, public behavior,
security, or requirements that seem to conflict. First check decisions.md: it may
already be answered.

To ask, write one JSON file per question to
`.specify/autopilot/inbox/questions/<short-name>.json`, then continue with work
the question does not block:

```json
{
  "question": "One specific question.",
  "context": "What you were doing, what you found, why it matters.",
  "options": ["Option A", "Option B"],
  "recommendation": "Your preferred option and why.",
  "blocking": true
}
```

Never ask in chat: nobody is reading it. The lead's answers come back as tasks in
tasks.md (and edits to spec.md for spec questions) and appear in decisions.md.
Your question and the answer also appear in thread.md, your epic's conversation,
which the product owner can follow and comment on.
"""


class TeamError(RuntimeError):
    pass


def main_root(project_root: Path) -> Path:
    """The conductor's checkout (shared decision log and journal); this checkout when standalone."""
    return chat.main_root(project_root)


# ── decision log ─────────────────────────────────────────────────────────────


def _decisions_dir(root: Path) -> Path:
    return ensure_state_dir(root) / "decisions"


def load_decisions(root: Path) -> list[dict[str, Any]]:
    directory = _decisions_dir(root)
    decisions = []
    for path in sorted(directory.glob("*.json")):
        data = read_json_lenient(path)
        if isinstance(data, dict) and isinstance(data.get("id"), str) and isinstance(data.get("question"), str):
            decisions.append(data)
    return sorted(decisions, key=lambda d: (d.get("at", ""), d.get("id", "")))


def decisions_for(root: Path, epic_id: str | None) -> list[dict[str, Any]]:
    """Decisions that bind one epic: program-wide ones plus its own epic-scoped ones."""
    return [
        d for d in load_decisions(root)
        if d.get("scope", "program") != "epic" or (epic_id is not None and d.get("epic") == epic_id)
    ]


def render_decisions(decisions: list[dict[str, Any]]) -> str:
    lines = [
        "# Team decisions",
        "",
        "Answers the lead or the product owner gave to developers' questions. They bind every epic:",
        "follow them, and do not ask again what is answered here.",
        "",
    ]
    if not decisions:
        lines.append("_No decisions yet._")
    for decision in decisions:
        scope = " · this epic only" if decision.get("scope") == "epic" else ""
        lines += [
            f"## {decision['id']} ({decision.get('epic') or 'standalone'}, decided by "
            f"{decision.get('decided_by', 'lead')}{scope})",
            "",
            f"**Question:** {decision['question']}",
            "",
            f"**Decision:** {decision['answer']}",
        ]
        if decision.get("rationale"):
            lines += ["", f"**Why:** {decision['rationale']}"]
        lines.append("")
    return "\n".join(lines) + "\n"


def publish_decision(root: Path, decision: dict[str, Any]) -> dict[str, Any]:
    """Publish a decision unless the same question is already decided; return the one in force.

    Parallel developers can ask the same question and get answers from separate
    lead calls. The check and the write happen under one lock, so the first
    answer published wins and later ones adopt it instead of contradicting it.
    """
    if not safe_id(decision.get("id")):
        raise TeamError(f"unsafe decision ID {decision.get('id')!r}")
    key = _normalise_question(decision["question"])
    with ProcessLock(_decisions_dir(root) / ".lock"):
        for existing in decisions_for(root, decision.get("epic")):
            if existing["id"] == decision["id"] or _normalise_question(existing["question"]) == key:
                return existing
        write_json(_decisions_dir(root) / f"{decision['id']}.json", decision)
        (ensure_state_dir(root) / "decisions.md").write_text(render_decisions(load_decisions(root)), encoding="utf-8")
    return decision


def _normalise_question(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


# ── context ──────────────────────────────────────────────────────────────────


def refresh_context(project_root: Path, epic_id: str | None = None, thread: str | None = None) -> Path:
    """Copy the lead's handbook, the decisions binding this epic and its thread into this checkout."""
    target = project_root / CONTEXT_DIR
    target.mkdir(parents=True, exist_ok=True)
    root = main_root(project_root)
    epic_id = epic_id or os.environ.get("AUTOPILOT_EPIC", "").strip() or None
    program = ensure_state_dir(root) / "program.md"
    if program.is_file():
        (target / "program.md").write_text(program.read_text(encoding="utf-8"), encoding="utf-8")
    (target / "decisions.md").write_text(render_decisions(decisions_for(root, epic_id)), encoding="utf-8")
    if not (target / "epic-context.md").is_file():
        # Standalone pipe: no conductor wrote an assignment, but the channel still works.
        (target / "epic-context.md").write_text(
            "# Your assignment\n\nA single feature, outside a planned autopilot run.\n\n" + QUESTION_PROTOCOL,
            encoding="utf-8",
        )
    if thread:
        run = os.environ.get("AUTOPILOT_RUN", "").strip()
        chat.write_thread_context(root, target, thread, thread, chat.run_thread(run) if run else None)
    return target


def write_epic_context(root: Path, worktree: Path, state: dict[str, Any], epic_id: str) -> None:
    """Generate a developer's onboarding brief from the scheduler state (plain code)."""
    epics = state["epics"]
    epic = epics[epic_id]
    program = ensure_state_dir(root) / "program.md"
    product = "this product"
    if program.is_file():
        heading = next((l[2:].strip() for l in program.read_text(encoding="utf-8").splitlines() if l.startswith("# ")), "")
        product = heading or product

    def line(other_id: str, extra: str = "") -> str:
        other = epics[other_id]
        provides = "; ".join(other.get("provides", [])) or "see its spec"
        return f"- **{other_id}: {other['title']}** (`{other['feature_dir']}`, {other['status']}): provides {provides}{extra}"

    lines = [
        f"# Your assignment: {epic_id} — {epic['title']}",
        "",
        f"You are a developer on the team building **{product}**. The team lead split the work into "
        f"{len(epics)} epics; this one is yours. Read `program.md` for the big picture (architecture, "
        "conventions, contracts between epics) and `decisions.md` for everything already decided. "
        "Your work must fit both.",
        "",
        "## Your epic",
        "",
        f"Feature directory `{epic['feature_dir']}`, branch `{epic['branch']}`.",
        "",
        epic["brief"],
        "",
    ]
    if epic.get("provides"):
        lines += ["Other epics rely on you to provide:", ""] + [f"- {p}" for p in epic["provides"]] + [""]
    deps = epic["depends_on"]
    lines += ["## Already built (merged into your starting point; the code and specs are in this checkout)", ""]
    lines += [line(d) for d in deps] if deps else ["- Nothing: you start from the base branch."]
    parallel = [i for i in state["order"] if i != epic_id and epics[i]["status"] == "running"]
    lines += ["", "## Teammates working in parallel right now", ""]
    if parallel:
        lines.append("Do not edit what they touch; their work merges separately and a conflict sends your epic to a human.")
        lines.append("")
        lines += [
            f"- **{i}: {epics[i]['title']}** touches " + (", ".join(f"`{t}`" for t in epics[i]["touches"]) or "_undeclared_")
            for i in parallel
        ]
    else:
        lines.append("- None.")
    downstream = [i for i in state["order"] if epic_id in epics[i]["depends_on"]]
    lines += ["", "## Waiting on your epic", ""]
    lines += [line(i, f" — needs: {epics[i]['title']}") for i in downstream] if downstream else ["- No epic depends on this one."]
    lines += ["", "## Your files", "", "Stay inside what this epic declared: "
              + (", ".join(f"`{t}`" for t in epic["touches"]) or "_undeclared_")
              + ". If you must change something else, ask the lead first.", "", QUESTION_PROTOCOL]
    target = worktree / CONTEXT_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "epic-context.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The handbook and the decisions must be there before the first step (specify) runs.
    if program.is_file():
        (target / "program.md").write_text(program.read_text(encoding="utf-8"), encoding="utf-8")
    (target / "decisions.md").write_text(render_decisions(decisions_for(root, epic_id)), encoding="utf-8")
    run = state.get("run_id") or ""
    if run:
        chat.write_thread_context(root, target, chat.epic_thread(run, epic_id),
                                  f"{epic_id} · {epic['title']} (run {run})", chat.run_thread(run))


# ── conversation ─────────────────────────────────────────────────────────────


def _say(project_root: Path, state: dict[str, Any], author: str, text: str, kind: str, **meta: Any) -> None:
    """Add to this epic's thread (the team's memory, mirrored to the chat app)."""
    mention = bool(meta.pop("mention_owner", False))
    chat.safe_post(main_root(project_root), chat.pipe_thread(state), author, text, kind=kind,
                   meta={"epic": state.get("epic_id"), **meta}, mention_owner=mention)


def _question_text(question: dict[str, Any]) -> str:
    lines = [f"❓ **{question['id']}**: {question['question']}"]
    if question.get("context"):
        lines.append(f"Context: {question['context']}")
    if question.get("options"):
        lines.append("Options: " + "; ".join(str(o) for o in question["options"]))
    if question.get("recommendation"):
        lines.append(f"My recommendation: {question['recommendation']}")
    return "\n".join(lines)


def _ask_owner(project_root: Path, state: dict[str, Any], question: dict[str, Any], answer: dict[str, Any]) -> None:
    options = answer.get("options") or question.get("options") or []
    lines = [f"**Product question {question['id']}** for you: {question['question']}"]
    if question.get("context"):
        lines.append(f"Context: {question['context']}")
    if options:
        lines.append("Options: " + "; ".join(str(o) for o in options))
    if answer.get("recommendation"):
        lines.append(f"I recommend: {answer['recommendation']}")
    if answer.get("why"):
        lines.append(f"Why it is yours to decide: {answer['why']}")
    lines.append(f"Reply in this thread with your decision (or `answer {question['id']} <decision>`). "
                 "Only this developer waits; the rest of the team carries on.")
    _say(project_root, state, "lead", "\n".join(lines), "owner-question", question=question["id"], mention_owner=True)


# ── questions ────────────────────────────────────────────────────────────────


def _questions_dir(project_root: Path, feature_dir: Path) -> Path:
    path = pipe_dir(project_root, feature_dir) / "questions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def collect(project_root: Path, config: dict[str, Any], stage: str) -> dict[str, Any]:
    """File new questions (inbox + spec markers); answer repeats from the decision log."""
    feature_dir = spec_mod.resolve_feature_dir(project_root)
    state = ensure_pipe(project_root, config, feature_dir)
    refresh_context(project_root, state.get("epic_id"), chat.pipe_thread(state))
    if state["status"] in LOOP_END:
        return {"open": 0, "finished": True, "status": state["status"], "lead_args": ""}
    flush_pending(project_root, config)  # decisions made before tasks.md existed
    qdir = _questions_dir(project_root, feature_dir)
    number = spec_mod.feature_number(feature_dir)
    existing = load_questions(qdir)
    known = {q["id"] for q in existing}
    problems: list[str] = []

    spec_path = feature_dir / "spec.md"
    if spec_path.is_file():
        for match in MARKER_RE.finditer(spec_path.read_text(encoding="utf-8")):
            qid = f"{number}-S{hashlib.sha256(match.group(0).encode()).hexdigest()[:6]}"
            if qid not in known:
                write_json(qdir / f"{qid}.json", {
                    "id": qid, "origin": "spec", "asked_by": "specify", "stage": stage, "asked_at": now_iso(),
                    "blocking": True, "question": match.group(1).strip() or "Unspecified clarification in spec.md",
                    "context": f"Marker left in {_rel(project_root, spec_path)}", "marker": match.group(0),
                })
                known.add(qid)

    inbox = project_root / INBOX_DIR
    if inbox.is_dir():
        counter = sum(1 for q in known if "-Q" in q)
        for path in sorted(inbox.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("question"), str) or not data["question"].strip():
                    raise ValueError("needs a non-empty 'question' string")
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                problems.append(f"{path.name}: {exc}")
                path.rename(path.with_suffix(".invalid"))
                continue
            counter += 1
            qid = f"{number}-Q{counter}"
            write_json(qdir / f"{qid}.json", {
                "id": qid, "origin": "developer", "asked_by": stage, "stage": stage, "asked_at": now_iso(),
                "blocking": data.get("blocking", True) is not False,
                "question": data["question"].strip(),
                "context": str(data.get("context", "")),
                "options": [str(o) for o in data.get("options", [])] if isinstance(data.get("options"), list) else [],
                "recommendation": str(data.get("recommendation", "")),
            })
            path.unlink()

    if problems:
        # A question the developer could not express is still a question: an epic
        # must not pass with it silently dropped. A human repairs it and resets the epic.
        state.update(status="escalated", reason="the developer wrote question file(s) that cannot be read ("
                     + "; ".join(problems) + "); fix them in " + _rel(project_root, inbox)
                     + " (renamed to .invalid) and reset the epic", finished_at=now_iso())
        save_pipe(project_root, feature_dir, state)
        return {"open": 0, "finished": True, "status": state["status"], "lead_args": "", "problems": problems}

    # A question the team already answered gets the logged answer, no lead call.
    decisions = {
        _normalise_question(d["question"]): d
        for d in decisions_for(main_root(project_root), state.get("epic_id"))
    }
    questions = load_questions(qdir)
    for question in questions:
        if question["id"] not in {q["id"] for q in existing}:
            _say(project_root, state, chat.developer(state), _question_text(question), "question", question=question["id"])
        if question.get("answer") is None:
            decision = decisions.get(_normalise_question(question["question"]))
            if decision:
                write_json(qdir / f"{question['id']}.answer.json", {
                    "answer": decision["answer"], "rationale": f"Already decided in {decision['id']}.",
                    "decided_by": "decision-log", "decision_id": decision["id"],
                })
                _say(project_root, state, "lead", f"{question['id']}: already decided in {decision['id']}: "
                     f"{decision['answer']}", "answer", question=question["id"], decision=decision["id"])
    questions = load_questions(qdir)

    if len(questions) > config["pipe"]["max_questions"]:
        state.update(status="escalated", reason=f"more than {config['pipe']['max_questions']} questions for one epic: "
                     "it is probably under-specified; re-plan it", finished_at=now_iso())
        save_pipe(project_root, feature_dir, state)
        return {"open": 0, "finished": True, "status": state["status"], "lead_args": "", "problems": problems}

    open_questions = [q for q in questions if not _answered(q) and not _needs_human(q)]
    if open_questions:
        state["consults"] = state.get("consults", 0) + 1
        state["lead_tree"] = tree_fingerprint(project_root)
        save_pipe(project_root, feature_dir, state)
    return {
        "open": len(open_questions),
        "open_ids": [q["id"] for q in open_questions],
        "finished": False,
        "status": state["status"],
        "problems": problems,
        "lead_args": (
            f"questions_dir={_rel(project_root, qdir)} feature_dir={state['feature_dir']} "
            f"ids={','.join(q['id'] for q in open_questions)}"
        ) if open_questions else "",
    }


def record(project_root: Path, config: dict[str, Any], stage: str) -> dict[str, Any]:
    """Publish answers as decisions, hand them to the developer, pause for the product owner."""
    feature_dir = spec_mod.resolve_feature_dir(project_root)
    state = ensure_pipe(project_root, config, feature_dir)
    if state["status"] in LOOP_END:
        return {"finished": True, "status": state["status"], "reason": state["reason"]}
    if state.get("lead_tree") and tree_fingerprint(project_root) != state["lead_tree"]:
        state.update(status="escalated", reason="the team lead changed the working tree while answering; "
                     "the lead must only answer questions", finished_at=now_iso())
        state.pop("lead_tree", None)
        save_pipe(project_root, feature_dir, state)
        return {"finished": True, "status": state["status"], "reason": state["reason"]}
    state.pop("lead_tree", None)

    qdir = _questions_dir(project_root, feature_dir)
    root = main_root(project_root)
    spec_path = feature_dir / "spec.md"
    applied: list[str] = []
    tasks: list[str] = []
    waiting: list[dict[str, Any]] = []
    for question in load_questions(qdir):
        if question.get("applied"):
            continue
        if _answered(question):
            answer = question["answer"]
            text = answer["answer"].strip()
            if answer.get("decided_by") == "decision-log" and safe_id(answer.get("decision_id")):
                decision_id = answer["decision_id"]
            else:
                # Decision IDs become file names: only our own, derived from the question ID.
                decision_id = f"D-{question['id']}"
                scope = "epic" if answer.get("applies_to") == "epic" and state.get("epic_id") else "program"
                in_force = publish_decision(root, {
                    "id": decision_id, "epic": state.get("epic_id"), "scope": scope,
                    "question": question["question"], "answer": text,
                    "rationale": str(answer.get("rationale", "")),
                    "decided_by": str(answer.get("decided_by", "lead")), "at": now_iso(),
                })
                if in_force["id"] != decision_id:
                    # A teammate's identical question was decided first: follow that decision.
                    decision_id, text = in_force["id"], in_force["answer"]
            if question.get("origin") == "spec" and spec_path.is_file() and question.get("marker"):
                spec_text = spec_path.read_text(encoding="utf-8")
                resolved = f"{question['question']} — **Decided ({decision_id}):** {text}"
                # Identical markers share one question ID: resolve every occurrence.
                spec_path.write_text(spec_text.replace(question["marker"], resolved), encoding="utf-8")
            else:
                tasks.append(
                    f"Apply team decision {decision_id} to your work (your question {question['id']}: "
                    f"{question['question']}): {text}"
                )
            question["applied"] = True
            applied.append(question["id"])
            if answer.get("decided_by") != "decision-log":
                who = str(answer.get("decided_by", "lead"))
                why = f" Why: {answer['rationale']}" if answer.get("rationale") and who != "product owner" else ""
                _say(project_root, state, "lead" if who == "lead" else "autopilot",
                     f"✅ **{decision_id}** ({who}) for {question['id']}: {text}{why}", "decision",
                     question=question["id"], decision=decision_id)
        elif _needs_human(question):
            waiting.append(question)
            if question.get("owner_notified"):
                continue
            _ask_owner(project_root, state, question, question["answer"] or {})
            question["owner_notified"] = True
        else:
            question["lead_attempts"] = question.get("lead_attempts", 0) + 1
            if question["lead_attempts"] >= 2:
                why = {"needs_human": True, "why": "the team lead did not answer it twice"}
                write_json(qdir / f"{question['id']}.answer.json", why)
                waiting.append(question)
                _ask_owner(project_root, state, question, why)
                question["owner_notified"] = True
        stored = {k: v for k, v in question.items() if k != "answer"}
        write_json(qdir / f"{question['id']}.json", stored)

    if tasks:
        if (feature_dir / "tasks.md").is_file():
            spec_mod.append_task_phase(feature_dir / "tasks.md", f"Team Decisions ({stage})", tasks)
        else:
            _carry_forward(qdir, tasks)
    if waiting:
        state.update(status="awaiting-human", reason="waiting for the product owner: "
                     + "; ".join(f"{q['id']}: {q['question']}" for q in waiting))
    save_pipe(project_root, feature_dir, state)
    # The next developer step must already see the decisions just made.
    refresh_context(project_root, state.get("epic_id"), chat.pipe_thread(state))
    return {
        "finished": state["status"] in LOOP_END,
        "status": state["status"],
        "applied": applied,
        "awaiting_human": [q["id"] for q in waiting],
        "reason": state["reason"],
    }


def _carry_forward(qdir: Path, tasks: list[str]) -> None:
    """Before tasks.md exists, park decisions for the tasks step via the context."""
    pending = qdir.parent / "pending-decisions.json"
    existing = read_json_lenient(pending, []) or []
    write_json(pending, existing + tasks)


def flush_pending(project_root: Path, config: dict[str, Any]) -> list[str]:
    """Append decisions recorded before tasks.md existed (called by the gate)."""
    feature_dir = spec_mod.resolve_feature_dir(project_root)
    pending = pipe_dir(project_root, feature_dir) / "pending-decisions.json"
    items = read_json_lenient(pending, []) or []
    if not items or not (feature_dir / "tasks.md").is_file():
        return []
    ids = spec_mod.append_task_phase(feature_dir / "tasks.md", "Team Decisions (before tasks)", items)
    pending.unlink()
    return ids


# ── product owner ────────────────────────────────────────────────────────────


def open_human_questions(worktrees: list[tuple[str | None, Path]]) -> list[dict[str, Any]]:
    """Questions waiting for the product owner across the given checkouts."""
    found = []
    for epic_id, worktree in worktrees:
        pipes = worktree / ".specify" / "autopilot" / "pipes"
        if not pipes.is_dir():
            continue
        for qdir in pipes.glob("*/questions"):
            for question in load_questions(qdir):
                if _needs_human(question):
                    answer = question["answer"] or {}
                    found.append({
                        "id": question["id"], "epic": epic_id or question.get("epic"),
                        "question": question["question"], "context": question.get("context", ""),
                        "options": answer.get("options") or question.get("options", []),
                        "developer_recommendation": question.get("recommendation", ""),
                        "lead_why": answer.get("why", ""), "lead_recommendation": answer.get("recommendation", ""),
                        "path": str(qdir / f"{question['id']}.answer.json"),
                    })
    return found


def answer_question(worktrees: list[tuple[str | None, Path]], question_id: str, text: str) -> dict[str, Any]:
    """Record the product owner's answer. The drive picks it up and resumes the epic."""
    if not text.strip():
        raise TeamError("the answer is empty")
    if not safe_id(question_id):
        raise TeamError(f"invalid question ID {question_id!r}")
    for question in open_human_questions(worktrees):
        if question["id"] == question_id:
            answer_path = Path(question["path"])
            write_json(answer_path, {"answer": text.strip(), "decided_by": "product owner",
                                     "rationale": "Answered by the product owner."})
            result: dict[str, Any] = {"ok": True, "id": question_id, "epic": question["epic"]}
            if question["epic"] is None:
                result.update(_reopen_standalone(answer_path.parent))
            return result
    raise TeamError(f"no open question {question_id} waiting for the product owner")


def _reopen_standalone(qdir: Path) -> dict[str, Any]:
    """A pipe outside a conductor run: reopen it once every product question is answered.

    In a planned run the scheduler does this and relaunches the epic. A
    standalone pipe has no scheduler, so the answer reopens it here and says how
    to continue: a new workflow run that starts at the first missing artifact
    (resuming the old run would only replay its finished result step).
    """
    pipe_file = qdir.parent / "pipe.json"
    pipe = read_json_lenient(pipe_file)
    if not isinstance(pipe, dict) or pipe.get("status") != "awaiting-human":
        return {}
    if any(_needs_human(q) for q in load_questions(qdir)):
        return {"reopened": False, "still_waiting": [q["id"] for q in load_questions(qdir) if _needs_human(q)]}
    pipe.update(status="running", reason="answered by the product owner")
    pipe.setdefault("history", []).append({"reopened": True, "at": now_iso()})
    write_json(pipe_file, pipe)
    project_root = qdir.parents[4]
    feature = project_root / str(pipe.get("feature_dir", ""))
    phase = "build" if (feature / "tasks.md").is_file() else "design" if (feature / "spec.md").is_file() else "full"
    return {
        "reopened": True,
        "next": f"specify workflow run autopilot-epic --input phase={phase} "
                f"(with SPECIFY_FEATURE_DIRECTORY={pipe.get('feature_dir')})",
    }


def all_answered(worktree: Path, feature_dir: str) -> bool:
    qdir = worktree / ".specify" / "autopilot" / "pipes" / Path(feature_dir).name / "questions"
    return qdir.is_dir() and not any(_needs_human(q) for q in load_questions(qdir))
