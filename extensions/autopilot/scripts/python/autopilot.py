#!/usr/bin/env python3
"""Spec Kit autopilot: deterministic conductor, scheduler and gate.

Usage (from the project root, or an epic worktree for the pipe commands):

  Conductor (main checkout)
    autopilot.py preflight [--requirements DIR] [--baseline]
    autopilot.py plan-check [--reset]
    autopilot.py approve plan|land [--reject] [--note TEXT] [--run RUN_ID] [--no-act]
                                 the product owner's verdict; approving the plan
                                 starts a detached drive, approving land lands
    autopilot.py drive [--integration KEY] [--detach]   (needs an approved plan)
    autopilot.py status          stage, live drive, epic table, recent events
    autopilot.py stop | unstop   ask the drive to start no new pipes / undo
    autopilot.py report
    autopilot.py land            (needs an approved final merge)
    autopilot.py new-run         archive a finished run before planning the next
    autopilot.py reset EPIC_ID
    autopilot.py answer QUESTION_ID --text "..."   the product owner answers

  Team chat (Slack or Discord; see chat in autopilot-config.yml)
    autopilot.py chat check [--hello]     credentials, permissions, owner ID; post a hello
    autopilot.py chat bridge [--detach] [--once]   mirror threads, take the owner's replies
    autopilot.py chat stop | status
    autopilot.py chat replay --confirm    allow a newly connected app to post the history

  Pipe (inside one epic's checkout; used by the autopilot-epic workflow)
    autopilot.py consult --stage spec|design|build  file questions for the lead
    autopilot.py record --stage spec|design|build   publish the lead's answers
    autopilot.py check-spec
    autopilot.py gate
    autopilot.py decide
    autopilot.py pipe-result

  Setup
    autopilot.py checks          run the configured gate checks once

Every command prints one JSON object on stdout. Exit status is 0 on success;
1 means the step must stop the workflow (a failed precondition or a pipe that
did not pass). Progress goes to stderr and .specify/autopilot/log.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Running from an epic worktree must not leave __pycache__ files next to the
# copied extension (they would look like new work to the gate).
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from autopilot_core import bridge, chat, driver, gate, team  # noqa: E402
from autopilot_core.connector import ConnectorError  # noqa: E402
from autopilot_core.config import ConfigError, load_config  # noqa: E402
from autopilot_core.spec import FeatureError  # noqa: E402
from autopilot_core.util import GitError, repo_root  # noqa: E402


def _emit(code: int, payload: dict) -> int:
    print(json.dumps(payload, indent=None, sort_keys=False))
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autopilot.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--requirements", default=os.environ.get("AUTOPILOT_REQUIREMENTS", ""))
    pre.add_argument("--baseline", action="store_true", help="also run the gate checks on HEAD")
    plan = sub.add_parser("plan-check")
    plan.add_argument("--reset", action="store_true")
    drv = sub.add_parser("drive")
    drv.add_argument("--integration", default=os.environ.get("AUTOPILOT_INTEGRATION", ""))
    drv.add_argument("--detach", action="store_true", help="start in the background and return immediately")
    for name in ("status", "report", "land", "new-run", "stop", "unstop", "check-spec", "gate", "decide",
                 "pipe-result", "checks"):
        sub.add_parser(name)
    appr = sub.add_parser("approve", help="record the product owner's verdict on a gate")
    appr.add_argument("gate", choices=["plan", "land"])
    appr.add_argument("--reject", action="store_true")
    appr.add_argument("--note", default="")
    appr.add_argument("--run", default="", help="the run ID being approved (must be the current run)")
    appr.add_argument("--via", default="agent chat")
    appr.add_argument("--no-act", action="store_true", help="only record: do not start the drive / land")
    reset = sub.add_parser("reset")
    reset.add_argument("epic_id")
    for name in ("consult", "record"):
        step = sub.add_parser(name)
        step.add_argument("--stage", default="build", choices=["spec", "design", "build"])
    talk = sub.add_parser("chat", help="team chat: check, bridge, stop, replay, status")
    talk.add_argument("action", choices=["check", "bridge", "stop", "replay", "status"])
    talk.add_argument("--detach", action="store_true", help="bridge: start in the background")
    talk.add_argument("--once", action="store_true", help="bridge: one tick, then exit")
    talk.add_argument("--hello", action="store_true", help="check: also post a hello thread")
    talk.add_argument("--confirm", action="store_true", help="replay: confirm posting the journal's history")
    ans = sub.add_parser("answer", help="record the product owner's answer to a question")
    ans.add_argument("question_id")
    ans.add_argument("--text", required=True)
    return parser


def _chat(root: Path, config: dict, args: argparse.Namespace) -> tuple[int, dict]:
    if args.action == "check":
        return bridge.check(root, config, hello=args.hello)
    if args.action == "bridge":
        if args.detach:
            return 0, bridge.ensure_running(root, config)
        if config["chat"]["provider"] == "none":
            return 1, {"ok": False, "error": "chat.provider is none"}
        return bridge.serve(root, config, once=args.once)
    if args.action == "stop":
        return 0, bridge.stop(root)
    if args.action == "replay":
        if not args.confirm:
            return 1, {"ok": False, "error": "replaying posts the whole journal history to the chat app; "
                       "run again with --confirm once the product owner agreed"}
        return 0, bridge.confirm_replay(root, config)
    return 0, chat.health(root, config)


def _journal_answer(root: Path, result: dict, question_id: str, text: str) -> None:
    """An answer given in the agent chat also belongs in the epic's thread."""
    state = driver.load_state(root) if (root / ".specify" / "autopilot" / "state.json").is_file() else None
    if not state or not result.get("epic"):
        return
    thread = chat.epic_thread(driver.run_id(state), result["epic"])
    chat.safe_post(root, thread, "owner", f"{question_id}: {text.strip()}", kind="answer",
                   meta={"question": question_id, "epic": result["epic"]})


def subcommands() -> set[str]:
    action = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return set(action.choices)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        root = repo_root(Path.cwd())
        config = load_config(root)
        if args.command == "preflight":
            return _emit(*driver.preflight(root, config, args.requirements, args.baseline))
        if args.command == "plan-check":
            code, out = driver.plan_check(root, config, reset=args.reset)
            if code == 0:
                out["chat"] = bridge.ensure_running(root, config)  # gate 1 can be answered in chat
            return _emit(code, out)
        if args.command == "drive":
            if args.detach:
                code, out = driver.drive_detached(root, Path(__file__).resolve(), args.integration)
                out["chat"] = bridge.ensure_running(root, config)
                return _emit(code, out)
            return _emit(*driver.drive(root, config, integration=args.integration))
        if args.command == "status":
            return _emit(0, driver.run_status(root))
        if args.command == "stop":
            path = driver.stop_file(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stop requested\n", encoding="utf-8")
            return _emit(0, {"stop_requested": True})
        if args.command == "unstop":
            return _emit(0, driver.unstop(root))
        if args.command == "approve":
            code, out = driver.approve(
                root, config, args.gate, accept=not args.reject, via=args.via, note=args.note,
                expected_run=args.run, cli=Path(__file__).resolve(), act=not args.no_act,
            )
            if code == 0 and not args.no_act:
                out["chat"] = bridge.ensure_running(root, config)
            return _emit(code, out)
        if args.command == "chat":
            return _emit(*_chat(root, config, args))
        if args.command == "new-run":
            return _emit(*driver.new_run(root))
        if args.command == "report":
            return _emit(*driver.report(root, config))
        if args.command == "land":
            return _emit(*driver.land(root, config))
        if args.command == "reset":
            return _emit(*driver.reset_epic(root, config, args.epic_id))
        if args.command == "consult":
            return _emit(0, team.collect(root, config, args.stage))
        if args.command == "record":
            return _emit(0, team.record(root, config, args.stage))
        if args.command == "answer":
            code, out = driver.answer(root, args.question_id, args.text)
            if code == 0:
                _journal_answer(root, out, args.question_id, args.text)
            return _emit(code, out)
        if args.command == "check-spec":
            return _emit(*gate.check_spec(root, config))
        if args.command == "gate":
            return _emit(0, gate.run_gate(root, config))
        if args.command == "decide":
            return _emit(0, gate.decide(root, config))
        if args.command == "pipe-result":
            return _emit(*gate.pipe_result(root))
        if args.command == "checks":
            results = gate.checks_only(root, config, None)
            return _emit(0 if all(r["passed"] for r in results) else 1, {"checks": results})
    except (ConfigError, FeatureError, GitError, gate.GateError, driver.DriverError, team.TeamError,
            ConnectorError) as exc:
        print(f"[autopilot] error: {exc}", file=sys.stderr)
        return _emit(1, {"ok": False, "error": str(exc)})
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
