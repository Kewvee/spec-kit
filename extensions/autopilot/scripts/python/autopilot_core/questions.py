"""A pipe's questions to the team lead, as stored on disk.

``.specify/autopilot/pipes/<feature>/questions/<id>.json`` holds a question and
``<id>.answer.json`` the lead's or the product owner's answer. Shared by the
team (asking, answering), the gate (an open blocking question keeps an
iteration red) and the scheduler (who waits for the product owner).

Readers are lenient: a half-written or malformed answer file counts as no
answer, so polling, status and reports keep working and the normal retry and
escalation path handles it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import read_json_lenient


def questions_dir(project_root: Path, feature_dir: Path) -> Path:
    return project_root / ".specify" / "autopilot" / "pipes" / feature_dir.name / "questions"


def load_questions(qdir: Path) -> list[dict[str, Any]]:
    questions = []
    if not qdir.is_dir():
        return questions
    for path in sorted(qdir.glob("*.json")):
        if path.name.endswith(".answer.json"):
            continue
        data = read_json_lenient(path)
        if isinstance(data, dict) and isinstance(data.get("id"), str) and isinstance(data.get("question"), str):
            answer = read_json_lenient(qdir / f"{data['id']}.answer.json")
            data["answer"] = answer if isinstance(answer, dict) else None
            questions.append(data)
    return questions


def answered(question: dict[str, Any]) -> bool:
    answer = question.get("answer")
    return isinstance(answer, dict) and isinstance(answer.get("answer"), str) and bool(answer["answer"].strip())


def needs_human(question: dict[str, Any]) -> bool:
    answer = question.get("answer")
    return isinstance(answer, dict) and answer.get("needs_human") is True and not answered(question)


def open_blocking(qdir: Path) -> list[dict[str, Any]]:
    """Blocking questions without a usable answer (the lead's or the owner's)."""
    return [
        q for q in load_questions(qdir)
        if q.get("blocking", True) is not False and not q.get("applied") and not answered(q)
    ]
