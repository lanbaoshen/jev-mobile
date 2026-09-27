from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from typesafe_sdk import TypeSafeClient

from .actions import HandoffRequest, HandoffResponse, UITools
from .agent import (
    AgentDecision,
    AgentLoop,
    AgentLoopConfig,
    AgentRunStatus,
    AgentStep,
    AgentTask,
    TypeSafeCall,
    TypeSafeDecisionProvider,
)
from .android import AndroidUITools
from .apps import LaunchableApp
from .exceptions import HandoffError
from .harmonyos import HarmonyOSUITools
from .models import UISnapshot

SEPARATOR = "=" * 56
SUB_SEPARATOR = "-" * 56
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"

_EXIT_CODES = {
    AgentRunStatus.FINISHED: 0,
    AgentRunStatus.FAILED: 1,
    AgentRunStatus.NEEDS_HANDOFF: 3,
    AgentRunStatus.STEP_LIMIT: 4,
}


@dataclass(slots=True)
class RunLogger:
    request_count: int = 0
    decision_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    preparation_seconds: float = 0.0
    api_seconds: float = 0.0
    action_seconds: float = 0.0

    def on_call(self, call: TypeSafeCall) -> None:
        response = call.response
        self.request_count += 1
        self.preparation_seconds += call.preparation_seconds
        self.api_seconds += call.api_seconds
        if "next_action" in response.choices:
            self.decision_count += 1
            answer = response.choices["next_action"]
            print(f"\n{SEPARATOR}", flush=True)
            print(f"STEP {self.decision_count:02d}", flush=True)
            print(SUB_SEPARATOR, flush=True)
            print(
                f"JEV      action={answer.choice} confidence={answer.confidence:.2f}",
                flush=True,
            )
        elif "start_app_target" in response.choices:
            answer = response.choices["start_app_target"]
            print(
                f"JEV      app={answer.choice} confidence={answer.confidence:.2f}",
                flush=True,
            )
        else:
            print("JEV      response received", flush=True)
        print(
            f"TIME     prepare={call.preparation_seconds * 1000:.1f}ms "
            f"jev_api={call.api_seconds * 1000:.1f}ms",
            flush=True,
        )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        print(
            f"USAGE    in={input_tokens} out={output_tokens} "
            f"total={self.input_tokens}/{self.output_tokens}",
            flush=True,
        )

    def on_decision(self, decision: AgentDecision, snapshot: UISnapshot) -> None:
        target = None
        if decision.target is not None:
            element = snapshot.resolve(decision.target)
            target = element.label or element.element_id
        parts = [f"action={decision.action.value}"]
        if target is not None:
            parts.append(f"target={target}")
        parts.append(f"confidence={decision.confidence:.2f}")
        if decision.target_confidence is not None:
            parts.append(f"target_confidence={decision.target_confidence:.2f}")
        if decision.direction is not None:
            parts.append(f"direction={decision.direction.value}")
        if decision.text_input_key is not None:
            parts.append(f"text_input={decision.text_input_key}")
        print(f"AGENT    {' '.join(parts)}", flush=True)

    def on_step(self, step: AgentStep) -> None:
        self.action_seconds += step.duration_seconds
        outcome = "ok" if step.success else "failed"
        detail = f" detail={step.detail}" if not step.success and step.detail else ""
        print(
            f"RESULT   action={step.action} status={outcome}{detail}",
            flush=True,
        )
        print(f"TIME     action={step.duration_seconds * 1000:.1f}ms", flush=True)

    def print_total_usage(self) -> None:
        print(SUB_SEPARATOR, flush=True)
        print(
            f"TOTAL    requests={self.request_count} "
            f"usage={self.input_tokens}/{self.output_tokens}",
            flush=True,
        )
        print(
            f"TIME     prepare={self.preparation_seconds * 1000:.1f}ms "
            f"jev_api={self.api_seconds * 1000:.1f}ms "
            f"action={self.action_seconds * 1000:.1f}ms",
            flush=True,
        )
        print(SEPARATOR, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-mobile",
        description="Run a bounded TypeSafe/Jev agent loop on a mobile device.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run a natural-language device task")
    run.add_argument("goal", help="task goal and explicit completion condition")
    run.add_argument(
        "--platform",
        choices=("android", "harmonyos"),
        required=True,
        help="device platform",
    )
    run.add_argument(
        "--device",
        help="ADB serial or HDC connect key; uses the tool default when omitted",
    )
    run.add_argument(
        "--app",
        action="append",
        default=[],
        metavar="BUNDLE/ABILITY[=LABEL]",
        type=_parse_app,
        help=(
            "override HarmonyOS app discovery with an allowlist entry; "
            "repeat as needed"
        ),
    )
    run.add_argument("--adb-path", default="adb", help="ADB executable path")
    run.add_argument("--hdc-path", default="hdc", help="HDC executable path")
    run.add_argument(
        "--timeout",
        default=15.0,
        type=_bounded_float("timeout", 0.0, 120.0, lower_inclusive=False),
        help="device command timeout in seconds (default: 15)",
    )
    run.add_argument(
        "--max-steps",
        default=30,
        type=_bounded_int("max steps", 1, 1_000),
        help="maximum agent steps (default: 30)",
    )
    run.add_argument(
        "--action-confidence",
        default=0.0,
        type=_probability,
        help="minimum next-action confidence (default: 0)",
    )
    run.add_argument(
        "--argument-confidence",
        default=0.0,
        type=_probability,
        help="minimum target or argument confidence (default: 0)",
    )
    run.add_argument(
        "--finish-confidence",
        default=0.0,
        type=_probability,
        help="minimum completion confidence (default: 0)",
    )
    run.add_argument(
        "--uncertain-retries",
        default=1,
        type=_bounded_int("uncertain retries", 0, 10),
        help="re-observations before handoff (default: 1)",
    )
    run.add_argument(
        "--reobserve-delay",
        default=1.0,
        type=_bounded_float("re-observe delay", 0.1, 30.0),
        help="seconds to wait before re-observing (default: 1)",
    )
    run.add_argument(
        "--no-handoff",
        action="store_true",
        help="return instead of prompting when user action is required",
    )
    run.set_defaults(handler=_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.platform == "android" and args.app:
        parser.error("--app is only supported with --platform harmonyos")
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


def _run(args: argparse.Namespace) -> int:
    if not _authentication_configured():
        print(
            "TypeSafe authentication is not configured.\n"
            f"Set {TYPESAFE_API_KEY_ENV} before running jev-mobile:\n"
            f"  export {TYPESAFE_API_KEY_ENV}='<your TypeSafe API key>'\n"
            "Create a key at https://console.typesafe.ai/",
            file=sys.stderr,
        )
        return 2

    tools = _build_tools(args)
    logger = RunLogger()
    config = _build_config(args)
    with TypeSafeClient() as client:
        result = AgentLoop(
            tools,
            TypeSafeDecisionProvider(client, on_call=logger.on_call),
            config=config,
            on_decision=logger.on_decision,
            on_step=logger.on_step,
        ).run(AgentTask(args.goal))

    print(f"\nFINAL    status={result.status.value}", flush=True)
    print(f"DETAIL   {result.message}", flush=True)
    logger.print_total_usage()
    return _EXIT_CODES[result.status]


def _authentication_configured() -> bool:
    api_key = os.environ.get(TYPESAFE_API_KEY_ENV)
    return api_key is not None and bool(api_key.strip())


def _build_config(args: argparse.Namespace) -> AgentLoopConfig:
    return AgentLoopConfig(
        max_steps=args.max_steps,
        action_confidence=args.action_confidence,
        argument_confidence=args.argument_confidence,
        finish_confidence=args.finish_confidence,
        uncertain_retries=args.uncertain_retries,
        reobserve_delay_seconds=args.reobserve_delay,
    )


def _build_tools(args: argparse.Namespace) -> UITools:
    handoff_handler = None if args.no_handoff else _handle_handoff
    if args.platform == "android":
        return AndroidUITools(
            adb_path=args.adb_path,
            serial=args.device,
            timeout_seconds=args.timeout,
            handoff_handler=handoff_handler,
        )

    apps = tuple(args.app)
    app_provider = (lambda: apps) if apps else None
    return HarmonyOSUITools(
        hdc_path=args.hdc_path,
        connect_key=args.device,
        timeout_seconds=args.timeout,
        handoff_handler=handoff_handler,
        launchable_app_provider=app_provider,
    )


def _handle_handoff(request: HandoffRequest) -> HandoffResponse:
    print(f"\n{SUB_SEPARATOR}", flush=True)
    print(f"HANDOFF  {request.reason}", flush=True)
    print(f"ACTION   {request.instruction}", flush=True)
    try:
        input("Press Enter after completing the action on the device: ")
    except EOFError as error:
        raise HandoffError("interactive input is unavailable") from error
    return HandoffResponse("User completed the requested action")


def _parse_app(value: str) -> LaunchableApp:
    identity, separator, label = value.partition("=")
    app_id, slash, entry = identity.partition("/")
    if not slash or not app_id or not entry:
        raise argparse.ArgumentTypeError(
            "app must use BUNDLE/ABILITY[=LABEL]"
        )
    if separator and not label.strip():
        raise argparse.ArgumentTypeError("app label must not be empty")
    try:
        return LaunchableApp(app_id, entry, label.strip() if separator else None)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _probability(value: str) -> float:
    return _bounded_float("probability", 0.0, 1.0)(value)


def _bounded_float(
    name: str,
    minimum: float,
    maximum: float,
    *,
    lower_inclusive: bool = True,
):
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{name} must be a number") from error
        lower_valid = parsed >= minimum if lower_inclusive else parsed > minimum
        if not lower_valid or parsed > maximum:
            operator = ">=" if lower_inclusive else ">"
            raise argparse.ArgumentTypeError(
                f"{name} must be {operator} {minimum} and <= {maximum}"
            )
        return parsed

    return parse


def _bounded_int(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


if __name__ == "__main__":
    raise SystemExit(main())
