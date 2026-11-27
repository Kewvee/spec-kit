"""Conductor end to end: plan-check, parallel pipes in worktrees, merges, land.

``specify`` is replaced by tests/extensions/autopilot/fake_specify.py, which
plays the agents and calls the real deterministic gate steps.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.extensions.autopilot.helpers import FAKE_SPECIFY, git, init_repo, install_extension, run_cli

from autopilot_core import driver
from autopilot_core.config import load_config


def _config(fake: Path, **overrides: str) -> str:
    values = {"max_parallel": "2", "max_agent_calls": "500", "max_iterations": "3", **overrides}
    return f"""gate:
  checks:
    - name: test
      run: "{Path(sys.executable).as_posix()} -c pass"
pipe:
  max_iterations: {values['max_iterations']}
  stuck_after: 2
  timeout_minutes: 10
run:
  max_parallel: {values['max_parallel']}
  max_agent_calls: {values['max_agent_calls']}
  specify_command: "{fake.as_posix()}"
"""


def _fake_specify(tmp_path: Path) -> Path:
    fake = tmp_path / "bin" / "specify"
    fake.parent.mkdir(parents=True)
    fake.write_text(f"#!{sys.executable}\n" + FAKE_SPECIFY.read_text(encoding="utf-8"), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _epic(epic_id, slug, depends_on=(), touches=None, extra=""):
    return {
        "id": epic_id,
        "slug": slug,
        "title": f"Epic {slug}",
        "goal": f"Build {slug}. {extra}".strip(),
        "acceptance": [f"{slug} works"],
        "depends_on": list(depends_on),
        "touches": touches if touches is not None else [f"src/{slug}.txt", f"tests/test_{slug}.py"],
    }


@pytest.fixture
def make_project(tmp_path):
    if os.name == "nt":
        pytest.skip("fake specify relies on a POSIX shebang")
    fake = _fake_specify(tmp_path)

    def build(epics, **config):
        root = init_repo(tmp_path / "repo")
        install_extension(root, _config(fake, **config))
        state = root / ".specify" / "autopilot"
        state.mkdir(parents=True, exist_ok=True)
        (state / "epics.json").write_text(json.dumps({"schema": 1, "epics": epics}), encoding="utf-8")
        (state / "program.md").write_text(PROGRAM, encoding="utf-8")
        return root

    return build


PROGRAM = """# Demo Reports

## Architecture
src/ holds one module per feature.

## Conventions
Timestamps are ISO-8601.
"""


def _cli(root: Path, *args: str):
    result = run_cli(root, *args)
    return result.returncode, json.loads(result.stdout.strip().splitlines()[-1]), result.stderr


def _plan(root: Path) -> str:
    """plan-check, then the product owner's approval (recorded only: the test drives)."""
    code, out, stderr = _cli(root, "plan-check")
    assert code == 0, (out, stderr)
    code, approved, stderr = _cli(root, "approve", "plan", "--no-act")
    assert code == 0 and approved["verdict"] == "approved", (approved, stderr)
    return out["run_id"]


def test_preflight_requires_configured_and_green_gate(make_project, tmp_path):
    root = make_project([_epic("E1", "alpha")])
    # Untracked .specify files are allowed: the scheduler copies them into worktrees.
    code, out, _ = _cli(root, "preflight", "--baseline")
    assert code == 0, out
    (root / "requirements.md").write_text("uncommitted\n", encoding="utf-8")
    code, out, _ = _cli(root, "preflight")
    assert code == 1 and "requirements.md" in out["problems"][0]
    (root / "requirements.md").unlink()
    config_path = root / ".specify" / "extensions" / "autopilot" / "autopilot-config.yml"
    config_path.write_text(config_path.read_text().replace(f"{Path(sys.executable).as_posix()} -c pass", ""), encoding="utf-8")
    code, out, _ = _cli(root, "preflight")
    assert code == 1 and "no gate checks configured" in out["problems"][0]


def test_plan_check_rejects_bad_plan_and_renders_dag(make_project):
    root = make_project([_epic("E1", "alpha", ["E2"]), _epic("E2", "beta", ["E1"])])
    code, out, _ = _cli(root, "plan-check")
    assert code == 1 and "cycle" in out["problems"][0]

    plan = root / ".specify" / "autopilot" / "epics.json"
    plan.write_text(json.dumps({"epics": [
        _epic("E1", "core", touches=["src/**"]),
        _epic("E2", "addon", touches=["src/addon/**"]),
    ]}), encoding="utf-8")
    code, out, _ = _cli(root, "plan-check")
    assert code == 0 and out["waves"] == [["E1"], ["E2"]]
    dag_md = (root / ".specify" / "autopilot" / "dag.md").read_text(encoding="utf-8")
    assert "E1 (added)" in dag_md and "specs/001-core" in dag_md and "```mermaid" in dag_md


def test_full_run_merges_dag_in_order_and_lands(make_project):
    root = make_project([
        _epic("E1", "alpha"),
        _epic("E2", "beta"),
        _epic("E3", "gamma", ["E1", "E2"], extra="REVIEW-ONCE"),
    ])
    _plan(root)
    code, summary, stderr = _cli(root, "drive")
    assert code == 0, stderr
    assert summary["counts"] == {"merged": 3}, summary
    assert summary["finished"] is True

    state = json.loads((root / ".specify" / "autopilot" / "state.json").read_text())
    assert state["epics"]["E3"]["iterations"] == 2  # review sent it round once
    events = [json.loads(line) for line in (root / ".specify" / "autopilot" / "log.jsonl").read_text().splitlines()]
    merged_order = [e["epic"] for e in events if e["event"] == "merged"]
    assert merged_order.index("E3") == 2
    starts = [e["epic"] for e in events if e["event"] == "start"]
    assert set(starts[:2]) == {"E1", "E2"}  # independent epics launched together

    integration = state["integration_branch"]
    files = git(root, "ls-tree", "-r", "--name-only", integration).splitlines()
    for name in ("alpha", "beta", "gamma"):
        assert f"src/{name}.txt" in files and f"tests/test_{name}.py" in files
    assert not any(path.startswith(".specify/autopilot") or path.startswith(".specify/workflows/runs") for path in files)
    # Nothing touched the base branch yet.
    assert "src/alpha.txt" not in git(root, "ls-tree", "-r", "--name-only", "main")

    code, out, _ = _cli(root, "report")
    assert code == 0 and "Approving merges" in (root / out["report"]).read_text(encoding="utf-8")
    git(root, "add", ".specify")
    git(root, "commit", "-q", "-m", "install autopilot")
    code, out, _ = _cli(root, "land")
    assert code == 1 and "not approved" in out["reason"]
    code, out, _ = _cli(root, "approve", "land")
    landed = out["land"]
    assert code == 0 and landed["landed"] is True
    assert "src/gamma.txt" in git(root, "ls-tree", "-r", "--name-only", "main")


def test_stuck_epic_escalates_and_blocks_dependents(make_project):
    root = make_project([
        _epic("E1", "alpha", extra="MISSING-TEST"),
        _epic("E2", "beta"),
        _epic("E3", "gamma", ["E1"]),
    ])
    _plan(root)
    code, summary, stderr = _cli(root, "drive")
    assert code == 0, stderr
    statuses = {e["id"]: e["status"] for e in summary["epics"]}
    assert statuses == {"E1": "escalated", "E2": "merged", "E3": "blocked"}
    _, out, _ = _cli(root, "report")
    report = (root / out["report"]).read_text(encoding="utf-8")
    assert "## Needs a human" in report and "identical gate failure" in report

    code, out, _ = _cli(root, "reset", "E1")
    assert code == 0 and out["reset"] == ["E1", "E3"]
    state = json.loads((root / ".specify" / "autopilot" / "state.json").read_text())
    assert state["epics"]["E1"]["status"] == "pending" and state["epics"]["E1"]["fresh_build"] is True


def test_merge_conflict_is_escalated_not_forced(make_project):
    root = make_project([
        _epic("E1", "alpha", touches=["src/alpha.txt", "shared.txt"], extra="SHARED:one"),
        # Lies about its touches, so the scheduler runs it in parallel with E1.
        _epic("E2", "beta", touches=["src/beta.txt"], extra="SHARED:two"),
    ])
    _plan(root)
    code, summary, stderr = _cli(root, "drive")
    assert code == 0, stderr
    statuses = sorted(e["status"] for e in summary["epics"])
    assert statuses == ["escalated", "merged"], summary
    escalated = next(e for e in summary["epics"] if e["status"] == "escalated")
    assert "conflict" in escalated["reason"]
    integration = json.loads((root / ".specify" / "autopilot" / "state.json").read_text())["integration_branch"]
    assert git(root, "status", "--porcelain") .count("UU") == 0
    assert git(root, "show", f"{integration}:shared.txt") in ("one", "two")


def test_budget_halts_new_pipes(make_project, tmp_path):
    root = make_project([_epic("E1", "alpha"), _epic("E2", "beta")], max_agent_calls="17", max_parallel="1")
    _plan(root)
    calls = []

    def fake_runner(project_root, epic, config, integration):
        calls.append(epic["id"])
        return {"pipe": {"status": "failed", "reason": "x", "iteration": 1, "agent_calls": 5}, "exit_code": 1}

    code, summary = driver.drive(root, load_config(root), pipe_runner=fake_runner)
    # Worst case per pipe is 5 + 4 x 3 = 17: only the first fits the budget.
    assert calls == ["E1"]
    assert summary["halted"].startswith("agent-call budget")
    assert {e["id"]: e["status"] for e in summary["epics"]} == {"E1": "failed", "E2": "pending"}


def test_stop_file_prevents_new_pipes(make_project):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    (root / ".specify" / "autopilot" / "STOP").write_text("", encoding="utf-8")
    code, summary = driver.drive(root, load_config(root), pipe_runner=lambda *a: pytest.fail("must not start"))
    assert summary["halted"] == "STOP file present" and summary["epics"][0]["status"] == "pending"


def test_status_stages_follow_the_run(make_project):
    root = make_project([_epic("E1", "alpha")])
    (root / ".specify" / "autopilot" / "epics.json").unlink()
    assert _cli(root, "status")[1]["stage"] == "no-plan"
    (root / ".specify" / "autopilot" / "epics.json").write_text(json.dumps({"epics": [_epic("E1", "alpha")]}))
    assert _cli(root, "status")[1]["stage"] == "planned-unchecked"
    assert _cli(root, "plan-check")[0] == 0
    assert _cli(root, "status")[1]["stage"] == "awaiting-plan-approval"
    assert _cli(root, "drive")[0] == 1  # not approved yet
    assert _cli(root, "approve", "plan", "--no-act")[0] == 0
    assert _cli(root, "drive")[0] == 0
    status = _cli(root, "status")[1]
    assert status["stage"] == "awaiting-land" and status["drive_running"] is False
    assert any(e["event"] == "merged" for e in status["recent_events"])
    assert _cli(root, "stop")[1] == {"stop_requested": True}
    assert _cli(root, "status")[1]["stop_requested"] is True
    assert _cli(root, "unstop")[1] == {"stop_requested": False}


def test_only_one_drive_runs_at_a_time(make_project):
    from autopilot_core.util import ProcessLock

    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    lock_path = root / ".specify" / "autopilot" / "drive.lock"
    holder = ProcessLock(lock_path)
    holder.acquire()
    try:
        code, out, _ = _cli(root, "drive")
        assert code == 1 and "already running" in out["error"]
        code, out, _ = _cli(root, "drive", "--detach")
        assert code == 0 and out["already_running"] is True and out["pid"] == os.getpid()
        assert _cli(root, "status")[1]["stage"] == "driving"
    finally:
        holder.release()
    # A lock file left behind by a crashed holder is not held by anyone.
    lock_path.write_text(json.dumps({"pid": 2**22 + 12345, "started_at": "then"}), encoding="utf-8")
    assert driver.drive_owner(root) is None
    assert _cli(root, "drive")[0] == 0


def test_simultaneous_drives_never_both_run(make_project):
    import threading

    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def slow_runner(project_root, epic, config, integration):
        import time as _time

        _time.sleep(1.0)
        return {"pipe": {"status": "failed", "reason": "x", "iteration": 1, "agent_calls": 5}, "exit_code": 1}

    def attempt():
        barrier.wait()
        try:
            driver.drive(root, load_config(root), pipe_runner=slow_runner)
            results.append("ran")
        except driver.DriverError as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(r == "ran" for r in results) == [False, True], results


def test_detached_drive_runs_in_the_background(make_project):
    import time as _time

    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    code, out, _ = _cli(root, "drive", "--detach")
    assert code == 0 and out["started"] is True and out["output"].endswith("drive.out")
    deadline = _time.time() + 120
    while _time.time() < deadline and _cli(root, "status")[1]["stage"] in ("driving", "awaiting-plan-approval"):
        _time.sleep(0.5)
    status = _cli(root, "status")[1]
    assert status["stage"] == "awaiting-land", status
    assert status["epics"][0]["status"] == "merged"


def test_infrastructure_failure_is_retried_up_to_max_attempts(make_project):
    root = make_project([_epic("E1", "alpha")])
    config_path = root / ".specify" / "extensions" / "autopilot" / "autopilot-config.yml"
    config_path.write_text(config_path.read_text().replace("timeout_minutes: 10", "timeout_minutes: 10\n  max_attempts: 2"), encoding="utf-8")
    _plan(root)
    attempts = []

    def flaky(project_root, epic, config, integration):
        attempts.append(epic["attempts"])
        return {"pipe": None, "exit_code": 1, "error": "agent CLI crashed"}

    _, summary = driver.drive(root, load_config(root), pipe_runner=flaky)
    assert attempts == [1, 2]
    assert summary["epics"][0]["status"] == "failed"


# ── the team: context, questions, decisions ──────────────────────────────────


def test_plan_check_requires_the_team_handbook(make_project):
    root = make_project([_epic("E1", "alpha")])
    (root / ".specify" / "autopilot" / "program.md").unlink()
    code, out, _ = _cli(root, "plan-check")
    assert code == 1 and "team handbook" in out["problems"][0]


def test_developers_work_with_team_context_and_share_decisions(make_project):
    root = make_project([
        {**_epic("E1", "alpha", extra="SPEC-MARKER ASK-LEAD"), "provides": ["alpha.export() returning CSV text"]},
        _epic("E3", "gamma"),
        _epic("E2", "beta", ["E1"]),
    ])
    _plan(root)
    code, summary, stderr = _cli(root, "drive")
    assert code == 0, stderr
    assert summary["counts"] == {"merged": 3}, summary
    state = json.loads((root / ".specify" / "autopilot" / "state.json").read_text())
    worktree = {i: Path(e["worktree"]) for i, e in state["epics"].items()}

    # Every developer had the handbook, its brief and the decision log before its first step.
    for epic_id in ("E1", "E2", "E3"):
        seen = next((worktree[epic_id] / ".specify" / "workflows" / "runs").glob("*/context-seen.txt")).read_text()
        assert seen.split() == ["decisions.md", "epic-context.md", "program.md"], (epic_id, seen)

    # The brief places the epic in the team.
    e3 = (worktree["E3"] / ".specify" / "autopilot" / "context" / "epic-context.md").read_text()
    assert "Demo Reports" in e3 and "**E1: Epic alpha**" in e3.split("## Teammates working in parallel")[1]
    e2 = (worktree["E2"] / ".specify" / "autopilot" / "context" / "epic-context.md").read_text()
    already_built = e2.split("## Already built")[1].split("##")[0]
    assert "E1: Epic alpha" in already_built and "alpha.export() returning CSV text" in already_built
    e1 = (worktree["E1"] / ".specify" / "autopilot" / "context" / "epic-context.md").read_text()
    assert "E2: Epic beta" in e1.split("## Waiting on your epic")[1]

    # The spec question and the developer's question were answered by the lead and published.
    decisions = json.loads(json.dumps([json.loads(p.read_text()) for p in (root / ".specify" / "autopilot" / "decisions").glob("*.json")]))
    assert len(decisions) == 2 and {d["epic"] for d in decisions} == {"E1"}
    spec = (worktree["E1"] / "specs" / "001-alpha" / "spec.md").read_text()
    assert "[NEEDS CLARIFICATION" not in spec and "**Decided (D-001-S" in spec
    assert "Apply team decision D-001-Q1" in (worktree["E1"] / "specs" / "001-alpha" / "tasks.md").read_text()
    # A later developer sees the team's decisions.
    assert "ISO-8601 UTC" in (worktree["E2"] / ".specify" / "autopilot" / "context" / "decisions.md").read_text()
    # The answers merged with the work; the context and questions did not.
    files = git(root, "ls-tree", "-r", "--name-only", state["integration_branch"]).splitlines()
    assert "specs/001-alpha/spec.md" in files and not any(".specify/autopilot" in f for f in files)


def test_product_owner_question_pauses_only_that_developer(make_project):
    root = make_project([_epic("E1", "alpha", extra="ASK-HUMAN"), _epic("E2", "beta")])
    _plan(root)
    _, summary, stderr = _cli(root, "drive")
    assert {e["id"]: e["status"] for e in summary["epics"]} == {"E1": "awaiting-human", "E2": "merged"}, stderr

    status = _cli(root, "status")[1]
    assert status["stage"] == "awaiting-human"
    [question] = status["questions_for_human"]
    assert question["epic"] == "E1" and question["question"] == "Which export format?"
    assert question["options"] == ["CSV", "XLSX"] and question["lead_recommendation"] == "CSV"

    code, out, _ = _cli(root, "answer", question["id"], "--text", "")
    assert code == 1
    code, out, _ = _cli(root, "answer", question["id"], "--text", "CSV only for now")
    assert code == 0 and out["still_open"] == [] and out["drive_running"] is False

    code, summary, stderr = _cli(root, "drive")
    assert summary["counts"] == {"merged": 2}, (summary, stderr)
    [decision] = [json.loads(p.read_text()) for p in (root / ".specify" / "autopilot" / "decisions").glob("*.json")]
    assert decision["decided_by"] == "product owner" and decision["answer"] == "CSV only for now"
    events = [json.loads(line)["event"] for line in (root / ".specify" / "autopilot" / "log.jsonl").read_text().splitlines()]
    assert "question" in events and "answered" in events


# ── review fixes: approvals, fresh resources, halts, budget, landing ─────────


def test_rejected_plan_blocks_drive_until_replanned(make_project):
    root = make_project([_epic("E1", "alpha")])
    code, out, _ = _cli(root, "plan-check")
    first_run = out["run_id"]
    code, out, _ = _cli(root, "approve", "plan", "--reject", "--note", "split E1")
    assert code == 0 and out["verdict"] == "rejected"
    status = _cli(root, "status")[1]
    assert status["stage"] == "plan-rejected" and status["approvals"]["plan"]["note"] == "split E1"
    code, out, _ = _cli(root, "drive")
    assert code == 1 and "rejected" in out["error"]
    # An approval naming another run never counts.
    code, out, _ = _cli(root, "approve", "plan", "--run", "r0000-dead")
    assert code == 1 and "not the current run" in out["error"]
    code, out, _ = _cli(root, "plan-check", "--reset")
    assert code == 0 and out["run_id"] != first_run
    assert _cli(root, "status")[1]["stage"] == "awaiting-plan-approval"


def test_replanning_never_reuses_an_earlier_runs_branches(make_project):
    root = make_project([_epic("E1", "alpha")])
    first = _plan(root)
    assert _cli(root, "drive")[0] == 0
    state = json.loads((root / ".specify" / "autopilot" / "state.json").read_text())
    old_branch, old_integration = state["epics"]["E1"]["branch"], state["integration_branch"]
    assert first in old_branch and first in old_integration
    code, out, _ = _cli(root, "plan-check", "--reset")
    assert code == 0, out
    state = json.loads((root / ".specify" / "autopilot" / "state.json").read_text())
    assert state["run_id"] != first
    assert state["epics"]["E1"]["branch"] != old_branch and state["integration_branch"] != old_integration
    assert not Path(state["epics"]["E1"]["worktree"]).exists()


def test_a_landed_run_must_be_archived_before_the_next_plan(make_project):
    root = make_project([_epic("E1", "alpha")])
    run = _plan(root)
    assert _cli(root, "drive")[0] == 0
    git(root, "add", ".specify")
    git(root, "commit", "-q", "-m", "install autopilot")
    assert _cli(root, "approve", "land")[0] == 0
    code, out, _ = _cli(root, "plan-check", "--reset")
    assert code == 1 and "new-run" in out["problems"][0]
    code, out, _ = _cli(root, "new-run")
    assert code == 0 and "epics.json" in out["files"]
    archive = root / ".specify" / "autopilot" / "runs" / run
    assert (archive / "state.json").is_file() and (archive / "program.md").is_file()
    assert _cli(root, "status")[1]["stage"] == "no-plan"


def test_land_verifies_the_combination_when_the_base_moved(make_project):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    assert _cli(root, "drive")[0] == 0
    git(root, "add", ".specify")
    git(root, "commit", "-q", "-m", "install autopilot")
    (root / "later.txt").write_text("landed on main meanwhile\n", encoding="utf-8")
    git(root, "add", "later.txt")
    git(root, "commit", "-q", "-m", "work on main during the run")
    code, out, stderr = _cli(root, "approve", "land")
    assert code == 0 and out["land"]["landed"] is True, (out, stderr)
    files = git(root, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "later.txt" in files and "src/alpha.txt" in files
    # main fast-forwarded to the verified merge; the scratch worktree is gone.
    assert git(root, "rev-parse", "HEAD") == out["land"]["commit"]
    assert "_land" not in git(root, "worktree", "list")


def test_withdrawn_stop_does_not_end_the_run(make_project):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    assert _cli(root, "stop")[0] == 0
    code, summary, _ = _cli(root, "drive")
    assert summary["halted"] == "STOP file present"
    assert _cli(root, "status")[1]["stage"] == "finished-nothing-merged"
    assert _cli(root, "unstop")[0] == 0
    status = _cli(root, "status")[1]
    assert status["stage"] == "interrupted" and status["halted"] == ""
    code, summary, _ = _cli(root, "drive")
    assert summary["counts"] == {"merged": 1} and summary["halted"] == ""


def test_budget_waits_for_running_pipes_before_halting(make_project):
    # Worst case per pipe is 17: two do not fit 25 together, but the first uses 6.
    root = make_project([_epic("E1", "alpha"), _epic("E2", "beta")], max_agent_calls="25", max_parallel="2")
    _plan(root)
    calls = []

    def frugal(project_root, epic, config, integration):
        calls.append(epic["id"])
        return {"pipe": {"status": "failed", "reason": "x", "iteration": 1, "agent_calls": 6}, "exit_code": 1}

    _, summary = driver.drive(root, load_config(root), pipe_runner=frugal)
    assert calls == ["E1", "E2"] and summary["halted"] == ""


def test_every_attempt_is_charged_to_the_budget(make_project):
    root = make_project([_epic("E1", "alpha")])
    config_path = root / ".specify" / "extensions" / "autopilot" / "autopilot-config.yml"
    config_path.write_text(config_path.read_text().replace("timeout_minutes: 10", "timeout_minutes: 10\n  max_attempts: 3"), encoding="utf-8")
    _plan(root)
    _, summary = driver.drive(root, load_config(root), pipe_runner=lambda *a: {"pipe": None, "exit_code": 1, "error": "crash"})
    assert summary["agent_calls_used"] == 3 * driver.UNRECORDED_ATTEMPT_CALLS


def _recorder(tmp_path: Path) -> Path:
    script = tmp_path / "bin" / "record-specify"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, pathlib\n"
        "out = pathlib.Path(os.environ['RECORD_TO'])\n"
        "out.write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'init_dir': os.environ.get('SPECIFY_INIT_DIR'), 'config': os.environ.get('AUTOPILOT_CONFIG')}))\n"
        "print(json.dumps({'run_id': 'x', 'status': 'failed'}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_pipes_run_in_their_worktree_with_the_conductors_config(make_project, tmp_path, monkeypatch):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    config = load_config(root)
    config["run"]["specify_command"] = str(_recorder(tmp_path))
    record = tmp_path / "record.json"
    monkeypatch.setenv("RECORD_TO", str(record))
    monkeypatch.setenv("SPECIFY_INIT_DIR", str(root))  # inherited from the caller: must be overridden
    state = driver.load_state(root)
    epic = state["epics"]["E1"]
    driver.ensure_integration(root, state, config)
    driver.prepare_epic_worktree(root, state, epic, config)
    # A checkpoint left "running" by a killed worker cannot be resumed: start a new run.
    runs = Path(epic["worktree"]) / ".specify" / "workflows" / "runs" / "dead"
    runs.mkdir(parents=True)
    (runs / "state.json").write_text(json.dumps({"run_id": "dead", "workflow_id": "autopilot-epic", "status": "running"}))
    driver.run_pipe(root, epic, config, "")
    seen = json.loads(record.read_text())
    assert seen["argv"][:3] == ["workflow", "run", "autopilot-epic"]
    assert Path(seen["init_dir"]).resolve() == Path(epic["worktree"]).resolve()
    assert json.loads(Path(seen["config"]).read_text())["run"]["specify_command"] == config["run"]["specify_command"]
    # A paused or failed run is resumed.
    (runs / "state.json").write_text(json.dumps({"run_id": "dead", "workflow_id": "autopilot-epic", "status": "failed"}))
    driver.run_pipe(root, epic, config, "")
    assert json.loads(record.read_text())["argv"][:3] == ["workflow", "resume", "dead"]


def test_failed_worktree_setup_is_retried(make_project, tmp_path):
    root = make_project([_epic("E1", "alpha")])
    _plan(root)
    flag = tmp_path / "deps-available"
    config = load_config(root)
    config["run"]["setup"] = f"test -f '{flag.as_posix()}'"
    state = driver.load_state(root)
    with pytest.raises(driver.DriverError):
        driver.ensure_integration(root, state, config)
    flag.write_text("", encoding="utf-8")
    path = driver.ensure_integration(root, state, config)
    assert (path / ".specify" / "autopilot" / driver.SETUP_MARKER).is_file()
