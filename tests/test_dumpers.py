import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from jev_mobile import (
    AndroidHierarchyDumper,
    HarmonyOSHierarchyDumper,
    HierarchyDumpError,
    Platform,
)
from jev_mobile.dumpers import CommandResult

from .test_android_hierarchy import ANDROID_HIERARCHY
from .test_harmonyos_hierarchy import HARMONYOS_HIERARCHY


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float) -> CommandResult:
        self.commands.append(tuple(command))
        if "uiautomator" in command:
            return CommandResult(
                0,
                f"{ANDROID_HIERARCHY}\nUI hierarchy dumped to: /dev/tty\n",
            )
        if "recv" in command:
            Path(command[-1]).write_text(json.dumps(HARMONYOS_HIERARCHY))
        return CommandResult(0)


def test_android_dumper_captures_and_parses_fresh_snapshot() -> None:
    runner = FakeRunner()

    snapshot = AndroidHierarchyDumper(serial="device-1", runner=runner).dump()

    assert snapshot.platform is Platform.ANDROID
    assert runner.commands == [
        (
            "adb",
            "-s",
            "device-1",
            "exec-out",
            "uiautomator",
            "dump",
            "/dev/tty",
        )
    ]


def test_harmonyos_dumper_captures_receives_and_removes_remote_file() -> None:
    runner = FakeRunner()

    snapshot = HarmonyOSHierarchyDumper(connect_key="device-2", runner=runner).dump()

    assert snapshot.platform is Platform.HARMONYOS
    remote_path = runner.commands[0][-1]
    assert remote_path.startswith("/data/local/tmp/jev-mobile-")
    assert runner.commands[0][:-1] == (
        "hdc",
        "-t",
        "device-2",
        "shell",
        "uitest",
        "dumpLayout",
        "-p",
    )
    assert runner.commands[1][:-2] == (
        "hdc",
        "-t",
        "device-2",
        "file",
        "recv",
    )
    assert runner.commands[1][-2] == remote_path
    assert runner.commands[2] == (
        "hdc",
        "-t",
        "device-2",
        "shell",
        "rm",
        "-f",
        remote_path,
    )


def test_dumper_returns_structured_failure_without_command_output() -> None:
    def failing_runner(
        command: Sequence[str], timeout_seconds: float
    ) -> CommandResult:
        return CommandResult(1, stderr="sensitive device output")

    with pytest.raises(HierarchyDumpError, match="exit code 1") as error:
        AndroidHierarchyDumper(runner=failing_runner).dump()

    assert "sensitive device output" not in str(error.value)
