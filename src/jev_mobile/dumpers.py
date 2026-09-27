from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .exceptions import HierarchyDumpError
from .hierarchy import parse_android_hierarchy, parse_harmonyos_hierarchy
from .models import UISnapshot
from .transport import CommandResult, CommandRunner, run_command


@dataclass(slots=True)
class AndroidHierarchyDumper:
    adb_path: str = "adb"
    serial: str | None = None
    timeout_seconds: float = 15.0
    runner: CommandRunner = field(default=run_command, repr=False)

    def __post_init__(self) -> None:
        _validate_configuration(self.adb_path, self.serial, self.timeout_seconds)

    def dump(self) -> UISnapshot:
        command = [*self._adb_prefix(), "exec-out", "uiautomator", "dump", "/dev/tty"]
        result = _run_dump_command(
            self.runner, command, self.timeout_seconds, "Android hierarchy dump"
        )
        _require_success(result, "Android hierarchy dump")
        return parse_android_hierarchy(_extract_android_xml(result.stdout))

    def _adb_prefix(self) -> list[str]:
        prefix = [self.adb_path]
        if self.serial is not None:
            prefix.extend(("-s", self.serial))
        return prefix


@dataclass(slots=True)
class HarmonyOSHierarchyDumper:
    hdc_path: str = "hdc"
    connect_key: str | None = None
    timeout_seconds: float = 15.0
    runner: CommandRunner = field(default=run_command, repr=False)

    def __post_init__(self) -> None:
        _validate_configuration(self.hdc_path, self.connect_key, self.timeout_seconds)

    def dump(self) -> UISnapshot:
        filename = f"jev-mobile-{uuid4().hex}.json"
        remote_path = f"/data/local/tmp/{filename}"
        prefix = self._hdc_prefix()

        try:
            dump_result = _run_dump_command(
                self.runner,
                [*prefix, "shell", "uitest", "dumpLayout", "-p", remote_path],
                self.timeout_seconds,
                "HarmonyOS hierarchy dump",
            )
            _require_success(dump_result, "HarmonyOS hierarchy dump")

            with tempfile.TemporaryDirectory(prefix="jev-mobile-") as directory:
                local_path = Path(directory, filename)
                receive_result = _run_dump_command(
                    self.runner,
                    [*prefix, "file", "recv", remote_path, str(local_path)],
                    self.timeout_seconds,
                    "HarmonyOS hierarchy receive",
                )
                _require_success(receive_result, "HarmonyOS hierarchy receive")
                try:
                    payload = local_path.read_bytes()
                except OSError as error:
                    raise HierarchyDumpError(
                        "HarmonyOS hierarchy file was not received"
                    ) from error
        finally:
            self.runner(
                [*prefix, "shell", "rm", "-f", remote_path],
                self.timeout_seconds,
            )

        return parse_harmonyos_hierarchy(payload)

    def _hdc_prefix(self) -> list[str]:
        prefix = [self.hdc_path]
        if self.connect_key is not None:
            prefix.extend(("-t", self.connect_key))
        return prefix


def _validate_configuration(
    executable: str, device_selector: str | None, timeout_seconds: float
) -> None:
    if not executable.strip():
        raise ValueError("device tool path must not be empty")
    if device_selector is not None and not device_selector.strip():
        raise ValueError("device selector must not be empty")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("timeout_seconds must be between 0 and 120")


def _require_success(result: CommandResult, operation: str) -> None:
    if result.returncode != 0:
        raise HierarchyDumpError(
            f"{operation} failed with exit code {result.returncode}"
        )


def _run_dump_command(
    runner: CommandRunner,
    command: list[str],
    timeout_seconds: float,
    operation: str,
) -> CommandResult:
    try:
        return runner(command, timeout_seconds)
    except FileNotFoundError as error:
        raise HierarchyDumpError(f"device tool not found: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise HierarchyDumpError(f"{operation} timed out") from error
    except OSError as error:
        raise HierarchyDumpError(f"failed to start {operation}") from error


def _extract_android_xml(output: str) -> str:
    start = output.find("<hierarchy")
    end = output.rfind("</hierarchy>")
    if start < 0 or end < start:
        raise HierarchyDumpError("Android hierarchy command returned no XML")
    return output[start : end + len("</hierarchy>")]
