from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from threading import Event, Thread

import pytest

from jev_mobile import (
    AndroidDeviceAdapter,
    AndroidUITools,
    AppRef,
    CurrentApp,
    ElementCapability,
    HandoffResponse,
    LaunchableApp,
    Platform,
    SwipeDirection,
    ToolControl,
    ToolFailureCode,
    ToolName,
    ToolSessionState,
    UITools,
    parse_android_hierarchy,
)
from jev_mobile.exceptions import (
    DeviceActionError,
    DeviceActionTimeoutError,
    ToolSessionError,
)
from jev_mobile.models import UISnapshot
from jev_mobile.transport import CommandResult


def make_snapshot(*, revision: str, focused: bool = False) -> UISnapshot:
    return parse_android_hierarchy(
        f"""\
        <hierarchy>
          <node class="android.widget.FrameLayout" bounds="[0,0][200,400]">
            <node class="android.widget.Button" text="Submit" clickable="true"
                  long-clickable="true" bounds="[20,20][120,80]" />
            <node class="android.widget.ScrollView" resource-id="feed"
                  scrollable="true" bounds="[0,100][200,400]" />
            <node class="android.widget.EditText" resource-id="query"
                  clickable="true" focusable="true" focused="{str(focused).lower()}"
                  bounds="[20,100][180,160]" />
          </node>
        </hierarchy>
        """,
        revision=revision,
    )


@dataclass
class FakeAdapter:
    snapshots: list[UISnapshot]
    platform: Platform = Platform.ANDROID
    requires_app_entry: bool = False
    events: list[tuple[object, ...]] = field(default_factory=list)
    timeout_next_action: bool = False
    apps: tuple[LaunchableApp, ...] = (
        LaunchableApp("com.example.app", ".MainActivity", "Example"),
    )

    def observe(self) -> UISnapshot:
        return self.snapshots.pop(0)

    def list_launchable_apps(self) -> tuple[LaunchableApp, ...]:
        return self.apps

    def start_app(self, app_id: str, entry: str | None) -> None:
        self._record("start_app", app_id, entry)

    def _record(self, *event: object) -> None:
        if self.timeout_next_action:
            self.timeout_next_action = False
            raise DeviceActionTimeoutError("timeout")
        self.events.append(event)

    def click(self, x: int, y: int) -> None:
        self._record("click", x, y)

    def long_click(self, x: int, y: int, duration_ms: int) -> None:
        self._record("long_click", x, y, duration_ms)

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> None:
        self._record("swipe", start_x, start_y, end_x, end_y, duration_ms)

    def type_text(self, text: str) -> None:
        self._record("type_text", text)


def test_click_and_long_click_use_element_center_and_invalidate_snapshot() -> None:
    adapter = FakeAdapter(
        [make_snapshot(revision="one"), make_snapshot(revision="two")]
    )
    tools = UITools(adapter)

    first = tools.observe().resolve("android:0.0")
    tools.available_apps()
    click_result = tools.click(first.ref)

    assert click_result.success is True
    assert click_result.snapshot_invalidated is True
    assert adapter.events == [("click", 70, 50)]
    assert tools.current_snapshot is None
    assert tools.current_app_catalog is None

    second = tools.observe().resolve("android:0.0")
    long_click_result = tools.long_click(second.ref, duration_ms=900)

    assert long_click_result.success is True
    assert adapter.events[-1] == ("long_click", 70, 50, 900)


def test_wait_sleeps_and_invalidates_observed_state() -> None:
    durations: list[float] = []
    clock_values = iter((10.0, 12.5))
    tools = UITools(
        FakeAdapter([make_snapshot(revision="one")]),
        sleeper=durations.append,
        clock=lambda: next(clock_values),
    )
    tools.observe()
    tools.available_apps()

    result = tools.wait(2.5)

    assert result.success is True
    assert result.action is ToolName.WAIT
    assert result.snapshot_invalidated is True
    assert result.duration_seconds == 2.5
    assert durations == [2.5]
    assert tools.current_snapshot is None
    assert tools.current_app_catalog is None


def test_wait_defaults_to_one_second() -> None:
    durations: list[float] = []
    tools = UITools(FakeAdapter([]), sleeper=durations.append)

    result = tools.wait()

    assert result.success is True
    assert durations == [1.0]


@pytest.mark.parametrize(
    "seconds", [0, 30.1, float("inf"), float("nan"), 10**1000, True]
)
def test_wait_rejects_invalid_duration(seconds: object) -> None:
    durations: list[float] = []
    tools = UITools(FakeAdapter([]), sleeper=durations.append)

    result = tools.wait(seconds)  # type: ignore[arg-type]

    assert result.failure is not None
    assert result.failure.code is ToolFailureCode.INVALID_ARGUMENT
    assert result.snapshot_invalidated is False
    assert durations == []


def test_start_app_validates_target_and_invalidates_snapshot() -> None:
    adapter = FakeAdapter([make_snapshot(revision="one")])
    tools = UITools(adapter)
    tools.observe()
    catalog = tools.available_apps()

    invalid = tools.start_app(AppRef("old-revision", "com.example.app"))

    assert invalid.failure is not None
    assert invalid.failure.code is ToolFailureCode.STALE_APP_CATALOG
    assert adapter.events == []
    assert tools.current_snapshot is not None

    result = tools.start_app(catalog.reference("com.example.app"))

    assert result.success is True
    assert result.action.value == "start_app"
    assert result.snapshot_invalidated is True
    assert adapter.events == [("start_app", "com.example.app", ".MainActivity")]
    assert tools.current_snapshot is None


def test_start_app_rejects_free_app_identifier() -> None:
    adapter = FakeAdapter([])
    tools = UITools(adapter)

    result = tools.start_app("com.example.app")  # type: ignore[arg-type]

    assert result.failure is not None
    assert result.failure.code is ToolFailureCode.INVALID_ARGUMENT
    assert adapter.events == []


def test_swipe_uses_outer_bounds_and_fixed_half_screen_distance() -> None:
    adapter = FakeAdapter([make_snapshot(revision="one")])
    tools = UITools(adapter)

    tools.observe()
    result = tools.swipe(SwipeDirection.UP, duration_ms=500)

    assert result.success is True
    assert adapter.events == [("swipe", 100, 300, 100, 100, 500)]


def test_type_text_requires_focus_then_accepts_focused_input() -> None:
    adapter = FakeAdapter(
        [
            make_snapshot(revision="one", focused=False),
            make_snapshot(revision="two", focused=True),
        ]
    )
    tools = UITools(adapter)

    unfocused = tools.observe().resolve("android:0.2")
    focus_required = tools.type_text(unfocused.ref, "hello")

    assert focus_required.failure is not None
    assert focus_required.failure.code is ToolFailureCode.FOCUS_REQUIRED
    assert tools.current_snapshot is not None
    assert adapter.events == []

    focused = tools.observe().resolve("android:0.2")
    assert focused.supports(ElementCapability.INPUT_TEXT)
    result = tools.type_text(focused.ref, "hello")

    assert result.success is True
    assert adapter.events == [("type_text", "hello")]
    assert tools.current_snapshot is None


def test_old_reference_and_timeout_return_structured_failures() -> None:
    adapter = FakeAdapter(
        [make_snapshot(revision="one"), make_snapshot(revision="two")]
    )
    tools = UITools(adapter)

    old_reference = tools.observe().resolve("android:0.0").ref
    current = tools.observe().resolve("android:0.0")

    stale = tools.click(old_reference)
    assert stale.failure is not None
    assert stale.failure.code is ToolFailureCode.STALE_ELEMENT

    adapter.timeout_next_action = True
    timed_out = tools.click(current.ref)
    assert timed_out.failure is not None
    assert timed_out.failure.code is ToolFailureCode.TIMEOUT
    assert timed_out.snapshot_invalidated is True
    assert tools.current_snapshot is None


def test_android_adapter_emits_bounded_adb_commands() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0)

    adapter = AndroidDeviceAdapter(serial="device-1", runner=runner)
    adapter.click(10, 20)
    adapter.long_click(10, 20, 800)
    adapter.swipe(10, 100, 10, 20, 400)
    adapter.type_text("hello world;ok")

    assert commands == [
        ("adb", "-s", "device-1", "shell", "input", "tap", "10", "20"),
        (
            "adb",
            "-s",
            "device-1",
            "shell",
            "input",
            "touchscreen",
            "swipe",
            "10",
            "20",
            "10",
            "20",
            "800",
        ),
        (
            "adb",
            "-s",
            "device-1",
            "shell",
            "input",
            "touchscreen",
            "swipe",
            "10",
            "100",
            "10",
            "20",
            "400",
        ),
        (
            "adb",
            "-s",
            "device-1",
            "shell",
            "input",
            "text",
            "'hello%sworld;ok'",
        ),
    ]


def test_android_adapter_starts_default_or_explicit_activity() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0)

    adapter = AndroidDeviceAdapter(serial="device-1", runner=runner)
    adapter.start_app("com.example.app", None)
    adapter.start_app("com.example.app", ".MainActivity")

    assert commands == [
        (
            "adb",
            "-s",
            "device-1",
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            "com.example.app",
        ),
        (
            "adb",
            "-s",
            "device-1",
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            "com.example.app/.MainActivity",
        ),
    ]


def test_android_adapter_lists_enabled_launcher_apps_and_prefers_default() -> None:
    output = """\
        3 activities found:
            Activity #0:
                isDefault=false
                com.example.app/.SecondaryActivity
            Activity #1:
                isDefault=true
                com.example.app/.MainActivity
            Activity #2:
                isDefault=true
                com.example.other/com.example.other.HomeActivity
        """

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(0, stdout=output)

    apps = AndroidDeviceAdapter(runner=runner).list_launchable_apps()

    assert apps == (
        LaunchableApp("com.example.app", ".MainActivity"),
        LaunchableApp("com.example.other", "com.example.other.HomeActivity"),
    )


def test_android_adapter_reads_current_resumed_app() -> None:
    output = """\
        topResumedActivity=ActivityRecord{fe325c1 u0 com.example.app/.MainActivity t45}
    """

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(0, stdout=output)

    current_app = AndroidDeviceAdapter(runner=runner).current_app()

    assert current_app == CurrentApp("com.example.app", ".MainActivity")


def test_android_adapter_accepts_colon_resumed_app_format() -> None:
    output = """\
      mResumedActivity: ActivityRecord{abc u0 com.example.app/.MainActivity t1}
    """

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(0, stdout=output)

    assert AndroidDeviceAdapter(runner=runner).current_app() == CurrentApp(
        "com.example.app", ".MainActivity"
    )


def test_android_adapter_accepts_resumed_activity_format() -> None:
    output = """\
      ResumedActivity: ActivityRecord{abc u0 com.example.app/.MainActivity t1}
    """

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(0, stdout=output)

    assert AndroidDeviceAdapter(runner=runner).current_app() == CurrentApp(
        "com.example.app", ".MainActivity"
    )


def test_android_adapter_accepts_focused_app_fallback() -> None:
    output = """\
      mFocusedApp=ActivityRecord{abc u0 com.example.app/.MainActivity t1}
    """

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(0, stdout=output)

    assert AndroidDeviceAdapter(runner=runner).current_app() == CurrentApp(
        "com.example.app", ".MainActivity"
    )


def test_android_adapter_retries_transient_missing_resumed_app() -> None:
    outputs = iter(
        (
            "No resumed activity during transition",
            "topResumedActivity=ActivityRecord{abc u0 com.example.app/.Details t1}",
        )
    )

    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(0, stdout=next(outputs))

    assert AndroidDeviceAdapter(runner=runner).current_app() == CurrentApp(
        "com.example.app", ".Details"
    )


def test_android_adapter_reports_missing_device() -> None:
    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        return CommandResult(
            1,
            stderr="adb: device 'emualtor-5554' not found\n",
        )

    with pytest.raises(
        DeviceActionError,
        match=(
            "Android device 'emualtor-5554' was not found; "
            "check --device against 'adb devices'"
        ),
    ):
        AndroidDeviceAdapter(serial="emualtor-5554", runner=runner).current_app()


def test_android_adapter_converts_timeout_without_leaking_command() -> None:
    def runner(command: list[str], timeout_seconds: float) -> CommandResult:
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    adapter = AndroidDeviceAdapter(runner=runner)
    tools = UITools(adapter)
    tools._snapshot = make_snapshot(revision="one")
    button = tools.current_snapshot.resolve("android:0.0")

    result = tools.click(button.ref)

    assert result.failure is not None
    assert result.failure.code is ToolFailureCode.TIMEOUT
    assert "adb" not in result.failure.message


def test_completed_handoff_invalidates_snapshot_and_resumes_session() -> None:
    requests: list[object] = []

    def handoff_handler(request: object) -> HandoffResponse:
        requests.append(request)
        return HandoffResponse(message="User confirmed completion")

    tools = UITools(
        FakeAdapter([make_snapshot(revision="one")]),
        handoff_handler=handoff_handler,
    )
    tools.observe()

    result = tools.handoff(
        "Confirm the purchase on the device",
        reason="This action creates an external order",
    )

    assert result.success is True
    assert result.action.value == "handoff"
    assert result.control is ToolControl.CONTINUE
    assert result.output == "User confirmed completion"
    assert result.snapshot_invalidated is True
    assert tools.state is ToolSessionState.ACTIVE
    assert tools.current_snapshot is None
    assert len(requests) == 1


def test_handoff_pauses_until_user_returns() -> None:
    handler_started = Event()
    user_returned = Event()
    results: list[object] = []

    def handoff_handler(request: object) -> HandoffResponse:
        handler_started.set()
        user_returned.wait()
        return HandoffResponse(message="User returned")

    tools = UITools(
        FakeAdapter([make_snapshot(revision="one")]),
        handoff_handler=handoff_handler,
    )
    button = tools.observe().resolve("android:0.0")

    worker = Thread(
        target=lambda: results.append(
            tools.handoff(
                "Approve the destructive action",
                reason="The action cannot be undone",
            )
        ),
        daemon=True,
    )
    worker.start()

    try:
        assert handler_started.wait(timeout=1)
        assert worker.is_alive()
        assert results == []
        assert tools.state is ToolSessionState.WAITING_FOR_USER

        blocked = tools.click(button.ref)
        assert blocked.failure is not None
        assert blocked.failure.code is ToolFailureCode.WAITING_FOR_USER
        assert blocked.control is ToolControl.WAIT_FOR_USER

        blocked_finish = tools.finish("Stop while waiting")
        assert blocked_finish.failure is not None
        assert blocked_finish.failure.code is ToolFailureCode.WAITING_FOR_USER
        assert tools.state is ToolSessionState.WAITING_FOR_USER
    finally:
        user_returned.set()

    worker.join(timeout=1)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].output == "User returned"
    assert tools.state is ToolSessionState.ACTIVE


def test_finish_ends_session_and_prevents_observe_or_actions() -> None:
    adapter = FakeAdapter([make_snapshot(revision="one")])
    tools = UITools(adapter)
    button = tools.observe().resolve("android:0.0")

    result = tools.finish("Task completed successfully")
    blocked = tools.click(button.ref)

    assert result.success is True
    assert result.action.value == "finish"
    assert result.control is ToolControl.FINISH
    assert result.output == "Task completed successfully"
    assert tools.current_snapshot is None
    assert blocked.failure is not None
    assert blocked.failure.code is ToolFailureCode.SESSION_FINISHED
    with pytest.raises(ToolSessionError):
        tools.observe()


def test_android_tools_accept_user_handoff_handler() -> None:
    tools = AndroidUITools(
        handoff_handler=lambda request: HandoffResponse()
    )

    result = tools.handoff("Complete confirmation", reason="User action required")

    assert result.success is True
