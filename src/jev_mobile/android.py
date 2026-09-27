from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field, replace

from .actions import HandoffHandler, UITools
from .apps import LaunchableApp
from .dumpers import AndroidHierarchyDumper
from .exceptions import DeviceActionError, DeviceActionTimeoutError, HierarchyDumpError
from .models import CurrentApp, Platform, UISnapshot
from .transport import CommandResult, CommandRunner, run_command

_LAUNCHER_COMPONENT = re.compile(
    r"(?P<app>[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)/"
    r"(?P<entry>\.?[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*)"
)
_RESUMED_ACTIVITY = re.compile(
    r"(?:mResumedActivity|topResumedActivity|ResumedActivity|mFocusedApp)"
    r"\s*[:=]\s*ActivityRecord\{[^}]*\s"
    r"(?P<app>[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)/"
    r"(?P<entry>\.?[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*)"
    r"(?:\s|})"
)
_DEVICE_NOT_FOUND = re.compile(
    r"device\s+['\"](?P<serial>[^'\"]+)['\"]\s+not found", re.IGNORECASE
)


@dataclass(slots=True)
class AndroidDeviceAdapter:
    adb_path: str = "adb"
    serial: str | None = None
    timeout_seconds: float = 15.0
    runner: CommandRunner = field(default=run_command, repr=False)
    platform: Platform = field(default=Platform.ANDROID, init=False)
    requires_app_entry: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.adb_path.strip():
            raise ValueError("adb_path must not be empty")
        if self.serial is not None and not self.serial.strip():
            raise ValueError("serial must not be empty")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120")

    def observe(self) -> UISnapshot:
        dumper = AndroidHierarchyDumper(
            adb_path=self.adb_path,
            serial=self.serial,
            timeout_seconds=self.timeout_seconds,
            runner=self.runner,
        )
        for _ in range(2):
            before = self.current_app()
            snapshot = dumper.dump()
            after = self.current_app()
            if before == after:
                return replace(snapshot, current_app=after)
        raise HierarchyDumpError("foreground app changed while observing the UI")

    def current_app(self) -> CurrentApp:
        parse_error: DeviceActionError | None = None
        for _ in range(3):
            result = self._run(
                "get current app", "shell", "dumpsys", "activity", "activities"
            )
            try:
                return _parse_current_app(result.stdout)
            except DeviceActionError as error:
                parse_error = error
        assert parse_error is not None
        raise parse_error

    def list_launchable_apps(self) -> tuple[LaunchableApp, ...]:
        result = self._run(
            "list launchable apps",
            "shell",
            "cmd",
            "package",
            "query-activities",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
        )
        return _parse_launchable_apps(result.stdout)

    def start_app(self, app_id: str, entry: str | None) -> None:
        command = [
            "shell",
            "am",
            "start",
            "-W",
        ]
        if entry is None:
            command.extend(
                (
                    "-a",
                    "android.intent.action.MAIN",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "-p",
                    app_id,
                )
            )
        else:
            command.extend(("-n", f"{app_id}/{entry}"))
        self._execute("start app", *command)

    def click(self, x: int, y: int) -> None:
        self._execute("click", "shell", "input", "tap", str(x), str(y))

    def long_click(self, x: int, y: int, duration_ms: int) -> None:
        self._execute(
            "long click",
            "shell",
            "input",
            "touchscreen",
            "swipe",
            str(x),
            str(y),
            str(x),
            str(y),
            str(duration_ms),
        )

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> None:
        self._execute(
            "swipe",
            "shell",
            "input",
            "touchscreen",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        )

    def type_text(self, text: str) -> None:
        encoded = shlex.quote(text.replace(" ", "%s"))
        self._execute("type text", "shell", "input", "text", encoded)

    def _execute(self, operation: str, *arguments: str) -> None:
        self._run(operation, *arguments)

    def _run(self, operation: str, *arguments: str) -> CommandResult:
        command = [*self._adb_prefix(), *arguments]
        try:
            result = self.runner(command, self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise DeviceActionTimeoutError(f"{operation} timed out") from error
        except (FileNotFoundError, OSError) as error:
            raise DeviceActionError(f"failed to start {operation}") from error
        if result.returncode != 0:
            missing_device = _DEVICE_NOT_FOUND.search(result.stderr)
            if missing_device is not None:
                serial = missing_device.group("serial")
                raise DeviceActionError(
                    f"Android device {serial!r} was not found; "
                    "check --device against 'adb devices'"
                )
            raise DeviceActionError(
                f"{operation} failed with exit code {result.returncode}"
            )
        return result

    def _adb_prefix(self) -> list[str]:
        prefix = [self.adb_path]
        if self.serial is not None:
            prefix.extend(("-s", self.serial))
        return prefix


class AndroidUITools(UITools):
    def __init__(
        self,
        *,
        adb_path: str = "adb",
        serial: str | None = None,
        timeout_seconds: float = 15.0,
        runner: CommandRunner = run_command,
        handoff_handler: HandoffHandler | None = None,
    ) -> None:
        super().__init__(
            AndroidDeviceAdapter(
                adb_path=adb_path,
                serial=serial,
                timeout_seconds=timeout_seconds,
                runner=runner,
            ),
            handoff_handler=handoff_handler,
        )


def _parse_launchable_apps(output: str) -> tuple[LaunchableApp, ...]:
    selected: dict[str, tuple[LaunchableApp, bool]] = {}
    is_default = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Activity #"):
            is_default = False
            continue
        if "isDefault=true" in stripped:
            is_default = True
            continue
        match = _LAUNCHER_COMPONENT.fullmatch(stripped)
        if match is None:
            continue
        app = LaunchableApp(
            app_id=match.group("app"),
            entry=match.group("entry"),
        )
        current = selected.get(app.app_id)
        if current is None or (is_default and not current[1]):
            selected[app.app_id] = (app, is_default)
    return tuple(app for app, _ in selected.values())


def _parse_current_app(output: str) -> CurrentApp:
    match = _RESUMED_ACTIVITY.search(output)
    if match is None:
        raise DeviceActionError("could not determine the current Android app")
    return CurrentApp(app_id=match.group("app"), entry=match.group("entry"))
