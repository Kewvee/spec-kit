"""The autopilot workflows executed by the real ``WorkflowEngine``.

Agent dispatch is stubbed (``CommandStep.execute`` plays the developer, the
team lead and the reviewer); every shell step runs the real autopilot engine,
so conditions, loops, checkpoints and resume are exercised as shipped.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.extensions.autopilot.helpers import (
    FAKE_SPECIFY,
    SPEC_TEMPLATE,
    git,
    init_repo,
    install_extension,
)

REPO = Path(__file__).resolve().parents[2]
FEATURE = "specs/003-export"
TAGS = ["003-US1-AS1", "003-US1-AS2", "003-US2-AS1"]

pytestmark = pytest.mark.skipif(os.name == "nt", reason="shell steps call python3")


def _definition(workflow_id: str):
    from specify_cli.workflows.engine import WorkflowDefinition

    return WorkflowDefinition.from_yaml(REPO / "workflows" / workflow_id / "workflow.yml")


def _config(specify: str = "specify") -> str:
    return f"""gate:
  checks:
    - name: test
      run: "{Path(sys.executable).as_posix()} -c pass"
pipe:
  max_iterations: 4
  stuck_after: 3
run:
  specify_command: "{specify}"
"""


class Agents:
    """Stub agent dispatch with scripted behaviour per command."""

    def __init__(self, monkeypatch, root: Path, **behaviour):
        from specify_cli.workflows.base import StepResult, StepStatus
        from specify_cli.workflows.expressions import evaluate_expression
        from specify_cli.workflows.step.command import CommandStep

        self.root = root
        self.calls: list[str] = []
        self.behaviour = behaviour
        agents = self

        def execute(step_self, config, context):
            command = config["command"]
            agents.calls.append(command)
            raw = (config.get("input") or {}).get("args", "")
            args = evaluate_expression(raw, context) if isinstance(raw, str) else raw
            handler = agents.behaviour.get(command) or getattr(agents, command.replace(".", "_"), None)
            failed = handler(str(args or "")) if handler else None
            if failed:
                return StepResult(status=StepStatus.FAILED, error=failed)
            return StepResult(status=StepStatus.COMPLETED)

        monkeypatch.setattr(CommandStep, "execute", execute)

    def count(self, command: str) -> int:
        return self.calls.count(command)

    # Default behaviour: a developer that does the work and a reviewer that passes.
    def speckit_implement(self, _args: str) -> None:
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_export.py").write_text(
            "".join(f"def test_{t.replace('-', '_').lower()}():\n    pass\n" for t in TAGS), encoding="utf-8"
        )
        tasks = self.root / FEATURE / "tasks.md"
        tasks.write_text(tasks.read_text(encoding="utf-8").replace("- [ ]", "- [x]"), encoding="utf-8")

    def speckit_autopilot_review(self, args: str) -> None:
        fields = dict(part.split("=", 1) for part in args.split())
        (self.root / fields["review_file"]).write_text(json.dumps({"verdict": "pass", "findings": []}), encoding="utf-8")

    def speckit_autopilot_lead(self, args: str) -> None:
        fields = dict(part.split("=", 1) for part in args.split())
        for qid in fields["ids"].split(","):
            (self.root / fields["questions_dir"] / f"{qid}.answer.json").write_text(
                json.dumps({"answer": "Use ISO-8601 UTC", "applies_to": "program"}), encoding="utf-8"
            )


@pytest.fixture
def epic_project(tmp_path, monkeypatch):
    root = init_repo(tmp_path / "repo")
    install_extension(root, _config())
    feature = root / FEATURE
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text(SPEC_TEMPLATE, encoding="utf-8")
    (feature / "tasks.md").write_text("# Tasks\n\n## Phase 1: Build\n\n- [ ] T001 Build the exporter\n", encoding="utf-8")
    git(root, "add", "-A", "--", "specs")
    git(root, "commit", "-q", "-m", "spec")
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", FEATURE)
    monkeypatch.chdir(root)
    return root


def _pipe(root: Path) -> dict:
    return json.loads((root / ".specify/autopilot/pipes/003-export/pipe.json").read_text(encoding="utf-8"))


def _run_epic(root: Path, phase: str = "build"):
    from specify_cli.workflows.engine import WorkflowEngine

    engine = WorkflowEngine(root)
    definition = _definition("autopilot-epic")
    assert engine.validate(definition) == []
    return engine, engine.execute(definition, inputs={"brief": "Export reports", "phase": phase, "integration": "copilot"})


def test_a_green_iteration_passes_through_review(epic_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    agents = Agents(monkeypatch, epic_project)
    _, state = _run_epic(epic_project)
    assert state.status is RunStatus.COMPLETED, state.step_results.get("result")
    assert _pipe(epic_project)["status"] == "passed" and _pipe(epic_project)["iteration"] == 1
    assert agents.calls == ["speckit.implement", "speckit.converge", "speckit.autopilot.review"]


def test_a_red_iteration_loops_until_the_gate_is_green(epic_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    attempts = []

    def lazy_then_diligent(args: str) -> None:
        attempts.append(1)
        if len(attempts) > 1:
            Agents.speckit_implement(agents, args)

    agents = Agents(monkeypatch, epic_project, **{"speckit.implement": lazy_then_diligent})
    _, state = _run_epic(epic_project)
    assert state.status is RunStatus.COMPLETED
    pipe = _pipe(epic_project)
    assert pipe["status"] == "passed" and pipe["iteration"] == 2
    assert agents.count("speckit.implement") == 2 and agents.count("speckit.autopilot.review") == 1
    assert "Autopilot Remediation (iteration 1, gate)" in (epic_project / FEATURE / "tasks.md").read_text()


def test_a_failing_pipe_ends_the_workflow_failed(epic_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    Agents(monkeypatch, epic_project, **{"speckit.implement": lambda _a: None})
    _, state = _run_epic(epic_project)
    assert state.status is RunStatus.FAILED
    assert _pipe(epic_project)["status"] in ("failed", "escalated")


def test_a_question_goes_to_the_lead_and_comes_back_as_a_decision(epic_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    asked = []

    def ask_once(args: str) -> None:
        if not asked:
            asked.append(1)
            inbox = epic_project / ".specify/autopilot/inbox/questions"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "tz.json").write_text(json.dumps({"question": "Which time zone?"}), encoding="utf-8")
            return
        Agents.speckit_implement(agents, args)

    agents = Agents(monkeypatch, epic_project, **{"speckit.implement": ask_once})
    _, state = _run_epic(epic_project)
    assert state.status is RunStatus.COMPLETED
    assert agents.count("speckit.autopilot.lead") == 1
    assert "Apply team decision D-003-Q1" in (epic_project / FEATURE / "tasks.md").read_text()
    assert "Use ISO-8601 UTC" in (epic_project / ".specify/autopilot/context/decisions.md").read_text()


def test_a_crashed_agent_step_resumes_where_it_stopped(epic_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    crashes = []

    def crash_once(args: str):
        if not crashes:
            crashes.append(1)
            return "agent CLI crashed"
        Agents.speckit_implement(agents, args)
        return None

    agents = Agents(monkeypatch, epic_project, **{"speckit.implement": crash_once})
    engine, state = _run_epic(epic_project)
    assert state.status is RunStatus.FAILED
    resumed = engine.resume(state.run_id)
    assert resumed.status is RunStatus.COMPLETED
    assert _pipe(epic_project)["status"] == "passed"
    assert agents.count("speckit.implement") == 2


# ── conductor gates ──────────────────────────────────────────────────────────


@pytest.fixture
def conductor_project(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "specify"
    fake.parent.mkdir(parents=True)
    fake.write_text(f"#!{sys.executable}\n" + FAKE_SPECIFY.read_text(encoding="utf-8"), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    root = init_repo(tmp_path / "repo")
    install_extension(root, _config(fake.as_posix()))
    (root / "docs" / "requirements").mkdir(parents=True)
    (root / "docs" / "requirements" / "export.md").write_text("Export reports as CSV.\n", encoding="utf-8")
    git(root, "add", "docs")
    git(root, "commit", "-q", "-m", "requirements")
    monkeypatch.chdir(root)
    return root


def _plan_epics(root: Path):
    def plan(_args: str) -> None:
        state = root / ".specify" / "autopilot"
        state.mkdir(parents=True, exist_ok=True)
        (state / "epics.json").write_text(json.dumps({"schema": 1, "epics": [{
            "id": "E1", "slug": "export", "title": "Export", "goal": "Export reports.",
            "acceptance": ["a CSV is written"], "depends_on": [], "touches": ["src/export.txt", "tests/test_export.py"],
        }]}), encoding="utf-8")
        (state / "program.md").write_text("# Reports\n\n## Architecture\nOne module per feature.\n", encoding="utf-8")

    return plan


def _run_conductor(root: Path, monkeypatch, plan_verdict: str, land_verdict: str):
    from specify_cli.workflows.engine import WorkflowEngine
    from specify_cli.workflows.step import gate as gate_module

    Agents(monkeypatch, root, **{"speckit.autopilot.epics": _plan_epics(root)})
    monkeypatch.setattr(gate_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False)))
    engine = WorkflowEngine(root)
    definition = _definition("autopilot")
    assert engine.validate(definition) == []
    return engine.execute(definition, inputs={
        "requirements": "docs/requirements", "integration": "copilot",
        "autopilot_review_epics_verdict": plan_verdict, "autopilot_review_land_verdict": land_verdict,
    })


def test_rejecting_the_plan_never_starts_a_pipe(conductor_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    state = _run_conductor(conductor_project, monkeypatch, "reject", "approve")
    assert state.status is RunStatus.ABORTED
    autopilot = json.loads((conductor_project / ".specify/autopilot/state.json").read_text())
    assert autopilot["started_at"] is None
    assert not (conductor_project / ".specify/autopilot/approvals.json").exists()


def test_rejecting_the_final_merge_never_lands(conductor_project, monkeypatch):
    from specify_cli.workflows.base import RunStatus

    base_before = git(conductor_project, "rev-parse", "main")
    state = _run_conductor(conductor_project, monkeypatch, "approve", "reject")
    assert state.status is RunStatus.ABORTED
    autopilot = json.loads((conductor_project / ".specify/autopilot/state.json").read_text())
    assert autopilot["epics"]["E1"]["status"] == "merged" and not autopilot.get("landed_commit")
    approvals = json.loads((conductor_project / ".specify/autopilot/approvals.json").read_text())
    assert approvals["plan"]["verdict"] == "approved" and "land" not in approvals
    assert git(conductor_project, "rev-parse", "main") == base_before
