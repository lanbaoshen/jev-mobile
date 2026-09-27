from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from time import perf_counter, sleep
from typing import Protocol
from uuid import uuid4

from .apps import AppCatalog, AppRef, LaunchableApp
from .exceptions import (
    AppNotFoundError,
    DeviceActionError,
    DeviceActionTimeoutError,
    ElementNotFoundError,
    HandoffError,
    StaleAppCatalogError,
    StaleSnapshotError,
    ToolSessionError,
)
from .models import Bounds, ElementCapability, ElementRef, Platform, UIElement, UISnapshot


class ToolName(str, Enum):
    START_APP = "start_app"
    CLICK = "click"
    LONG_CLICK = "long_click"
    SWIPE = "swipe"
    TYPE_TEXT = "type_text"
    WAIT = "wait"
    HANDOFF = "handoff"
    FINISH = "finish"


class SwipeDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class ToolFailureCode(str, Enum):
    STALE_SNAPSHOT = "stale_snapshot"
    STALE_ELEMENT = "stale_element"
    ELEMENT_NOT_FOUND = "element_not_found"
    UNSUPPORTED_ACTION = "unsupported_action"
    MISSING_BOUNDS = "missing_bounds"
    INVALID_ARGUMENT = "invalid_argument"
    FOCUS_REQUIRED = "focus_required"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    HANDOFF_UNAVAILABLE = "handoff_unavailable"
    HANDOFF_NOT_COMPLETED = "handoff_not_completed"
    WAITING_FOR_USER = "waiting_for_user"
    SESSION_FINISHED = "session_finished"
    STALE_APP_CATALOG = "stale_app_catalog"
    APP_NOT_FOUND = "app_not_found"


class ToolControl(str, Enum):
    CONTINUE = "continue"
    WAIT_FOR_USER = "wait_for_user"
    FINISH = "finish"


class ToolSessionState(str, Enum):
    ACTIVE = "active"
    WAITING_FOR_USER = "waiting_for_user"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    instruction: str
    reason: str


@dataclass(frozen=True, slots=True)
class HandoffResponse:
    message: str | None = None


class HandoffHandler(Protocol):
    def __call__(self, request: HandoffRequest) -> HandoffResponse:
        """Block without a timeout and return only after the user responds."""
        ...


@dataclass(frozen=True, slots=True)
class ToolFailure:
    code: ToolFailureCode
    message: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    action: ToolName
    success: bool
    snapshot_invalidated: bool
    control: ToolControl = ToolControl.CONTINUE
    failure: ToolFailure | None = None
    output: str | None = None
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("duration_seconds must be finite and non-negative")

    @classmethod
    def succeeded(
        cls,
        action: ToolName,
        *,
        control: ToolControl = ToolControl.CONTINUE,
        output: str | None = None,
        duration_seconds: float = 0.0,
    ) -> ToolResult:
        return cls(
            action=action,
            success=True,
            snapshot_invalidated=True,
            control=control,
            output=output,
            duration_seconds=duration_seconds,
        )

    @classmethod
    def failed(
        cls,
        action: ToolName,
        code: ToolFailureCode,
        message: str,
        *,
        snapshot_invalidated: bool = False,
        control: ToolControl = ToolControl.CONTINUE,
        duration_seconds: float = 0.0,
    ) -> ToolResult:
        return cls(
            action=action,
            success=False,
            snapshot_invalidated=snapshot_invalidated,
            control=control,
            failure=ToolFailure(code=code, message=message),
            duration_seconds=duration_seconds,
        )


class DeviceAdapter(Protocol):
    platform: Platform
    requires_app_entry: bool

    def observe(self) -> UISnapshot: ...

    def list_launchable_apps(self) -> tuple[LaunchableApp, ...]: ...

    def start_app(self, app_id: str, entry: str | None) -> None: ...

    def click(self, x: int, y: int) -> None: ...

    def long_click(self, x: int, y: int, duration_ms: int) -> None: ...

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> None: ...

    def type_text(self, text: str) -> None: ...


class UITools:
    def __init__(
        self,
        adapter: DeviceAdapter,
        *,
        handoff_handler: HandoffHandler | None = None,
        sleeper: Callable[[float], None] = sleep,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._adapter = adapter
        self._handoff_handler = handoff_handler
        self._sleeper = sleeper
        self._clock = clock
        self._snapshot: UISnapshot | None = None
        self._app_catalog: AppCatalog | None = None
        self._state = ToolSessionState.ACTIVE

    @property
    def current_snapshot(self) -> UISnapshot | None:
        return self._snapshot

    @property
    def state(self) -> ToolSessionState:
        return self._state

    @property
    def current_app_catalog(self) -> AppCatalog | None:
        return self._app_catalog

    @property
    def handoff_available(self) -> bool:
        return self._handoff_handler is not None

    def observe(self) -> UISnapshot:
        if self._state is not ToolSessionState.ACTIVE:
            raise ToolSessionError(
                f"cannot observe while tool session is {self._state.value}"
            )
        snapshot = self._adapter.observe()
        if snapshot.platform is not self._adapter.platform:
            raise ValueError("adapter returned a snapshot for another platform")
        self._snapshot = snapshot
        return snapshot

    def available_apps(self) -> AppCatalog:
        if self._state is not ToolSessionState.ACTIVE:
            raise ToolSessionError(
                f"cannot list apps while tool session is {self._state.value}"
            )
        self._app_catalog = None
        catalog = AppCatalog(
            revision=uuid4().hex,
            platform=self._adapter.platform,
            captured_at=datetime.now(timezone.utc),
            apps=self._adapter.list_launchable_apps(),
        )
        self._app_catalog = catalog
        return catalog

    def start_app(self, target: AppRef) -> ToolResult:
        action = ToolName.START_APP
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        if not isinstance(target, AppRef):
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "target must be an AppRef from the current app catalog",
            )
        if self._app_catalog is None:
            return ToolResult.failed(
                action,
                ToolFailureCode.STALE_APP_CATALOG,
                "no current app catalog; list available apps before starting one",
            )
        try:
            app = self._app_catalog.resolve(target)
        except StaleAppCatalogError:
            return ToolResult.failed(
                action,
                ToolFailureCode.STALE_APP_CATALOG,
                "app target belongs to an older catalog",
            )
        except AppNotFoundError:
            return ToolResult.failed(
                action,
                ToolFailureCode.APP_NOT_FOUND,
                "app is not present in the current launchable catalog",
            )
        if self._adapter.requires_app_entry and app.entry is None:
            return ToolResult.failed(
                action,
                ToolFailureCode.APP_NOT_FOUND,
                "app has no launchable entry for this platform",
            )
        self._app_catalog = None
        return self._dispatch(action, lambda: self._adapter.start_app(app.app_id, app.entry))

    def click(self, target: ElementRef) -> ToolResult:
        action = ToolName.CLICK
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        resolved = self._resolve_target(target, ElementCapability.CLICK, action)
        if isinstance(resolved, ToolResult):
            return resolved
        point = self._center(resolved, action)
        if isinstance(point, ToolResult):
            return point
        return self._dispatch(action, lambda: self._adapter.click(*point))

    def long_click(
        self, target: ElementRef, *, duration_ms: int = 800
    ) -> ToolResult:
        action = ToolName.LONG_CLICK
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        if not 500 <= duration_ms <= 10_000:
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "duration_ms must be between 500 and 10000",
            )
        resolved = self._resolve_target(
            target, ElementCapability.LONG_CLICK, action
        )
        if isinstance(resolved, ToolResult):
            return resolved
        point = self._center(resolved, action)
        if isinstance(point, ToolResult):
            return point
        return self._dispatch(
            action, lambda: self._adapter.long_click(*point, duration_ms)
        )

    def swipe(
        self,
        direction: SwipeDirection | str,
        *,
        duration_ms: int = 400,
    ) -> ToolResult:
        action = ToolName.SWIPE
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        try:
            parsed_direction = SwipeDirection(direction)
        except (TypeError, ValueError):
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "direction must be up, down, left, or right",
            )
        if not 100 <= duration_ms <= 5_000:
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "duration_ms must be between 100 and 5000",
            )
        if self._snapshot is None:
            return ToolResult.failed(
                action,
                ToolFailureCode.STALE_SNAPSHOT,
                "no current snapshot; observe the UI before acting",
            )
        viewport_bounds = self._snapshot.viewport_bounds
        if (
            viewport_bounds is None
            or viewport_bounds.width <= 0
            or viewport_bounds.height <= 0
        ):
            return ToolResult.failed(
                action,
                ToolFailureCode.MISSING_BOUNDS,
                "current UI has no usable outer bounds",
            )
        coordinates = self._swipe_coordinates(
            viewport_bounds, parsed_direction, action
        )
        if isinstance(coordinates, ToolResult):
            return coordinates
        return self._dispatch(
            action, lambda: self._adapter.swipe(*coordinates, duration_ms)
        )

    def type_text(self, target: ElementRef, text: str) -> ToolResult:
        action = ToolName.TYPE_TEXT
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        if not isinstance(text, str) or not text:
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "text must be a non-empty string",
            )
        if len(text) > 1_000 or "\x00" in text:
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "text must not contain NUL and must be at most 1000 characters",
            )

        resolved = self._resolve_target(
            target, ElementCapability.INPUT_TEXT, action
        )
        if isinstance(resolved, ToolResult):
            return resolved
        if resolved.requires_focus_before_input:
            return ToolResult.failed(
                action,
                ToolFailureCode.FOCUS_REQUIRED,
                "input is not focused; click it and observe the UI again",
            )
        return self._dispatch(action, lambda: self._adapter.type_text(text))

    def wait(self, seconds: float = 1.0) -> ToolResult:
        action = ToolName.WAIT
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 0.1 <= seconds <= 30
        ):
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "seconds must be a finite number between 0.1 and 30",
            )

        self._snapshot = None
        self._app_catalog = None
        started = self._clock()
        self._sleeper(float(seconds))
        return ToolResult.succeeded(
            action,
            duration_seconds=self._clock() - started,
        )

    def handoff(self, instruction: str, *, reason: str) -> ToolResult:
        action = ToolName.HANDOFF
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        invalid = self._validate_message(instruction, "instruction", action)
        if invalid is not None:
            return invalid
        invalid = self._validate_message(reason, "reason", action)
        if invalid is not None:
            return invalid

        self._snapshot = None
        self._app_catalog = None
        self._state = ToolSessionState.WAITING_FOR_USER
        if self._handoff_handler is None:
            return ToolResult.failed(
                action,
                ToolFailureCode.HANDOFF_UNAVAILABLE,
                "no user handoff handler is configured",
                snapshot_invalidated=True,
                control=ToolControl.WAIT_FOR_USER,
            )

        started = self._clock()
        try:
            response = self._handoff_handler(
                HandoffRequest(instruction=instruction, reason=reason)
            )
        except HandoffError:
            return ToolResult.failed(
                action,
                ToolFailureCode.HANDOFF_NOT_COMPLETED,
                "user handoff did not complete",
                snapshot_invalidated=True,
                control=ToolControl.WAIT_FOR_USER,
                duration_seconds=self._clock() - started,
            )
        if not isinstance(response, HandoffResponse):
            return ToolResult.failed(
                action,
                ToolFailureCode.HANDOFF_NOT_COMPLETED,
                "user handoff handler returned an invalid response",
                snapshot_invalidated=True,
                control=ToolControl.WAIT_FOR_USER,
                duration_seconds=self._clock() - started,
            )

        self._state = ToolSessionState.ACTIVE
        return ToolResult.succeeded(
            action,
            output=response.message,
            duration_seconds=self._clock() - started,
        )

    def finish(self, summary: str) -> ToolResult:
        action = ToolName.FINISH
        blocked = self._blocked_result(action)
        if blocked is not None:
            return blocked
        invalid = self._validate_message(summary, "summary", action)
        if invalid is not None:
            return invalid

        self._snapshot = None
        self._app_catalog = None
        self._state = ToolSessionState.FINISHED
        return ToolResult.succeeded(
            action,
            control=ToolControl.FINISH,
            output=summary,
        )

    @staticmethod
    def _validate_message(
        value: str, field_name: str, action: ToolName
    ) -> ToolResult | None:
        if not isinstance(value, str) or not value.strip():
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                f"{field_name} must be a non-empty string",
            )
        if len(value) > 2_000 or "\x00" in value:
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                f"{field_name} must not contain NUL and must be at most 2000 characters",
            )
        return None

    def _blocked_result(self, action: ToolName) -> ToolResult | None:
        if self._state is ToolSessionState.ACTIVE:
            return None
        if self._state is ToolSessionState.WAITING_FOR_USER:
            return ToolResult.failed(
                action,
                ToolFailureCode.WAITING_FOR_USER,
                "tool session is waiting for the user handoff to complete",
                control=ToolControl.WAIT_FOR_USER,
            )
        return ToolResult.failed(
            action,
            ToolFailureCode.SESSION_FINISHED,
            "tool session has finished",
            control=ToolControl.FINISH,
        )

    def _resolve_target(
        self,
        target: ElementRef,
        capability: ElementCapability,
        action: ToolName,
    ) -> UIElement | ToolResult:
        if not isinstance(target, ElementRef):
            return ToolResult.failed(
                action,
                ToolFailureCode.INVALID_ARGUMENT,
                "target must be an ElementRef from the current snapshot",
            )
        if self._snapshot is None:
            return ToolResult.failed(
                action,
                ToolFailureCode.STALE_SNAPSHOT,
                "no current snapshot; observe the UI before acting",
            )
        try:
            element = self._snapshot.resolve(target)
        except StaleSnapshotError:
            return ToolResult.failed(
                action,
                ToolFailureCode.STALE_ELEMENT,
                "target belongs to an older UI snapshot",
            )
        except ElementNotFoundError:
            return ToolResult.failed(
                action,
                ToolFailureCode.ELEMENT_NOT_FOUND,
                "target is not present in the current UI snapshot",
            )
        if not element.supports(capability):
            return ToolResult.failed(
                action,
                ToolFailureCode.UNSUPPORTED_ACTION,
                f"target does not support {capability.value}",
            )
        return element

    @staticmethod
    def _center(
        element: UIElement, action: ToolName
    ) -> tuple[int, int] | ToolResult:
        if (
            element.bounds is None
            or element.bounds.width <= 0
            or element.bounds.height <= 0
        ):
            return ToolResult.failed(
                action,
                ToolFailureCode.MISSING_BOUNDS,
                "target has no usable screen bounds",
            )
        return element.bounds.center

    @staticmethod
    def _swipe_coordinates(
        bounds: Bounds,
        direction: SwipeDirection,
        action: ToolName,
    ) -> tuple[int, int, int, int] | ToolResult:
        center_x, center_y = bounds.center
        travel_x = bounds.width // 4
        travel_y = bounds.height // 4

        if direction in {SwipeDirection.LEFT, SwipeDirection.RIGHT} and travel_x < 1:
            return ToolResult.failed(
                action,
                ToolFailureCode.MISSING_BOUNDS,
                "target is too narrow to swipe horizontally",
            )
        if direction in {SwipeDirection.UP, SwipeDirection.DOWN} and travel_y < 1:
            return ToolResult.failed(
                action,
                ToolFailureCode.MISSING_BOUNDS,
                "target is too short to swipe vertically",
            )

        if direction is SwipeDirection.UP:
            return center_x, center_y + travel_y, center_x, center_y - travel_y
        if direction is SwipeDirection.DOWN:
            return center_x, center_y - travel_y, center_x, center_y + travel_y
        if direction is SwipeDirection.LEFT:
            return center_x + travel_x, center_y, center_x - travel_x, center_y
        return center_x - travel_x, center_y, center_x + travel_x, center_y

    def _dispatch(self, action: ToolName, operation: Callable[[], None]) -> ToolResult:
        started = self._clock()
        try:
            operation()
        except DeviceActionTimeoutError:
            self._snapshot = None
            self._app_catalog = None
            return ToolResult.failed(
                action,
                ToolFailureCode.TIMEOUT,
                "device action timed out",
                snapshot_invalidated=True,
                duration_seconds=self._clock() - started,
            )
        except DeviceActionError:
            self._snapshot = None
            self._app_catalog = None
            return ToolResult.failed(
                action,
                ToolFailureCode.TRANSPORT_ERROR,
                "device action failed",
                snapshot_invalidated=True,
                duration_seconds=self._clock() - started,
            )

        self._snapshot = None
        self._app_catalog = None
        return ToolResult.succeeded(
            action,
            duration_seconds=self._clock() - started,
        )
