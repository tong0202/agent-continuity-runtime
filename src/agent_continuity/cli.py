from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

from .adapter_demo import run_adapter_demo
from .demo import DEMO_WORKFLOW_ID, run_demo
from .runtime import WorkflowEngine
from .spec import run_spec, spec_status, validate_spec


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _package_version() -> str:
    try:
        return version("agent-continuity-runtime")
    except PackageNotFoundError:
        return "0+unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-continuity")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "adapter-demo", "status", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--home", type=Path, required=True)
        if name == "demo":
            command.add_argument("--crash-once", action="store_true")
    for name in ("run-spec", "spec-status"):
        command = commands.add_parser(name)
        command.add_argument("--spec", type=Path, required=True)
        command.add_argument("--home", type=Path, required=True)
        if name == "run-spec":
            command.add_argument("--allow-command", action="store_true")
            command.add_argument("--allow-http", action="store_true")
    validate = commands.add_parser("validate-spec")
    validate.add_argument("--spec", type=Path, required=True)
    return parser


def _run() -> int:
    args = build_parser().parse_args()
    if args.command == "demo":
        _print(run_demo(args.home, crash_once=args.crash_once))
        return 0
    if args.command == "adapter-demo":
        _print(run_adapter_demo(args.home))
        return 0
    if args.command == "run-spec":
        _print(
            run_spec(
                args.spec,
                args.home,
                allow_command=args.allow_command,
                allow_http=args.allow_http,
            )
        )
        return 0
    if args.command == "spec-status":
        _print(spec_status(args.spec, args.home))
        return 0
    if args.command == "validate-spec":
        _print(validate_spec(args.spec))
        return 0
    engine = WorkflowEngine(args.home)
    if args.command == "status":
        _print(engine.status(DEMO_WORKFLOW_ID))
        return 0
    verification = engine.ledger.verify()
    _print(
        {
            "valid": verification.valid,
            "event_count": verification.event_count,
            "tail_hash": verification.tail_hash,
            "errors": list(verification.errors),
        }
    )
    return 0 if verification.valid else 2


def main() -> int:
    try:
        return _run()
    except (KeyError, PermissionError, RuntimeError, ValueError) as exc:
        _print(
            {
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 2
