from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest

from jev_mobile import (
    AgentAction,
    AgentAppDecision,
    AgentDecision,
    AgentLoop,
    AgentLoopConfig,
    AgentRunStatus,
    AgentTask,
    AgentTextInput,
    CurrentApp,
    HandoffResponse,
    LaunchableApp,
    Platform,
    SwipeDirection,
    TypeSafeDecisionProvider,
    UITools,
    parse_android_hierarchy,
)
from jev_mobile.agent import NO_ACTION_MATCH, AgentStep
from jev_mobile.exceptions import AgentDecisionError
from jev_mobile.models import UISnapshot


def make_snapshot(revision: str) -> UISnapshot:
    snapshot = parse_android_hierarchy(
        """\
        <hierarchy>
          <node class="android.widget.FrameLayout" package="com.example.app"
                bounds="[0,0][200,400]">
            <node class="android.widget.Button" package="com.example.app"
                  text="Continue" clickable="true" bounds="[20,20][120,80]" />
          </node>
        </hierarchy>
        """,
        revision=revision,
    )
    return replace(
        snapshot,
        current_app=CurrentApp("com.example.app", ".MainActivity", "Example"),
    )


def make_text_input_snapshot(revision: str) -> UISnapshot:
    snapshot = parse_android_hierarchy(
        """\
        <hierarchy>
          <node class="android.widget.FrameLayout" package="com.example.app"
                bounds="[0,0][200,400]">
            <node class="android.widget.EditText" package="com.example.app"
                  resource-id="query" clickable="true" focusable="true"
                  focused="true" bounds="[20,20][180,80]" />
          </node>
        </hierarchy>
        """,
        revision=revision,
    )
    return replace(
        snapshot,
        current_app=CurrentApp("com.example.app", ".MainActivity", "Example"),
    )


@dataclass
class FakeAdapter:
    snapshots: list[UISnapshot]
    events: list[tuple[object, ...]] = field(default_factory=list)
    platform: Platform = Platform.ANDROID
    requires_app_entry: bool = False

    def observe(self) -> UISnapshot:
        return self.snapshots.pop(0)

    def list_launchable_apps(self) -> tuple[LaunchableApp, ...]:
        return (LaunchableApp("com.example.other", ".MainActivity", "Other"),)

    def start_app(self, app_id: str, entry: str | None) -> None:
        self.events.append(("start_app", app_id, entry))

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))

    def long_click(self, x: int, y: int, duration_ms: int) -> None:
        self.events.append(("long_click", x, y, duration_ms))

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> None:
        self.events.append(("swipe", start_x, start_y, end_x, end_y, duration_ms))

    def type_text(self, text: str) -> None:
        self.events.append(("type_text", text))


class ScriptedProvider:
    def __init__(
        self,
        decisions: list[AgentDecision],
        *,
        app_choice: str | None = None,
    ) -> None:
        self.decisions = decisions
        self.app_choice = app_choice

    def decide(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        history: tuple[AgentStep, ...],
    ) -> AgentDecision:
        return self.decisions.pop(0)

    def select_app(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        catalog: object,
        history: tuple[AgentStep, ...],
    ) -> AgentAppDecision:
        assert self.app_choice is not None
        return AgentAppDecision(
            catalog_revision=catalog.revision,
            choice=self.app_choice,
            confidence=0.99,
        )

class FailingProvider(ScriptedProvider):
    def decide(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        history: tuple[AgentStep, ...],
    ) -> AgentDecision:
        raise AgentDecisionError("decision service unavailable")


def test_agent_finishes_only_from_confident_model_choice() -> None:
    snapshot = make_snapshot("one")
    tools = UITools(FakeAdapter([snapshot]))
    provider = ScriptedProvider(
        [AgentDecision("one", AgentAction.FINISH, confidence=0.95)]
    )

    result = AgentLoop(tools, provider).run(AgentTask("Open Example"))

    assert result.status is AgentRunStatus.FINISHED
    assert result.history[-1].action == "finish"


def test_low_confidence_finish_never_finishes_session() -> None:
    snapshot = make_snapshot("one")
    handoffs: list[object] = []
    tools = UITools(
        FakeAdapter([snapshot]),
        handoff_handler=lambda request: (
            handoffs.append(request) or HandoffResponse("done")
        ),
    )
    provider = ScriptedProvider(
        [AgentDecision("one", AgentAction.FINISH, confidence=0.80)]
    )
    config = AgentLoopConfig(
        max_steps=1,
        finish_confidence=0.90,
        uncertain_retries=0,
    )

    result = AgentLoop(tools, provider, config=config).run(AgentTask("Open Example"))

    assert result.status is AgentRunStatus.NEEDS_HANDOFF
    assert tools.state.value == "active"
    assert handoffs == []


def test_safe_click_executes_then_reobserves_and_finishes() -> None:
    first = make_snapshot("one")
    second = make_snapshot("two")
    adapter = FakeAdapter([first, second])
    tools = UITools(adapter)
    provider = ScriptedProvider(
        [
            AgentDecision(
                "one",
                AgentAction.CLICK,
                confidence=0.9,
                target=first.elements[0].ref,
                target_confidence=0.9,
            ),
            AgentDecision("two", AgentAction.FINISH, confidence=0.95),
        ]
    )

    result = AgentLoop(tools, provider).run(AgentTask("Continue and finish"))

    assert result.status is AgentRunStatus.FINISHED
    assert adapter.events == [("click", 70, 50)]


def test_agent_observers_receive_each_decision_and_step() -> None:
    first = make_snapshot("one")
    second = make_snapshot("two")
    decisions: list[tuple[AgentAction, str]] = []
    steps: list[AgentStep] = []
    provider = ScriptedProvider(
        [
            AgentDecision(
                "one",
                AgentAction.CLICK,
                confidence=0.9,
                target=first.elements[0].ref,
                target_confidence=0.9,
            ),
            AgentDecision("two", AgentAction.FINISH, confidence=0.95),
        ]
    )

    result = AgentLoop(
        UITools(
            FakeAdapter([first, second]),
            clock=iter((10.0, 10.4)).__next__,
        ),
        provider,
        on_decision=lambda decision, snapshot: decisions.append(
            (decision.action, snapshot.revision)
        ),
        on_step=steps.append,
    ).run(AgentTask("Continue and finish"))

    assert result.status is AgentRunStatus.FINISHED
    assert decisions == [
        (AgentAction.CLICK, "one"),
        (AgentAction.FINISH, "two"),
    ]
    assert [step.action for step in steps] == ["click", "finish"]
    assert steps[0].duration_seconds == pytest.approx(0.4)
    assert steps[1].duration_seconds == 0.0


def test_typesafe_context_contains_current_app_and_visible_packages() -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            calls.append({"state": state, "questions": questions})
            return SimpleNamespace(
                choices={
                    "next_action": SimpleNamespace(
                        choice="finish", confidence=0.99
                    )
                }
            )

    provider = TypeSafeDecisionProvider(FakeClient())  # type: ignore[arg-type]

    decision = provider.decide(AgentTask("Open Example"), make_snapshot("one"), ())

    state = calls[0]["state"]
    assert isinstance(state, dict)
    observation = state["observation"]
    assert observation["currentApp"] == {
        "appId": "com.example.app",
        "entry": ".MainActivity",
        "label": "Example",
    }
    assert observation["visiblePackages"] == ["com.example.app"]
    assert decision.action is AgentAction.FINISH


def test_typesafe_observes_static_text_without_offering_it_as_a_target() -> None:
    calls: list[dict[str, object]] = []
    snapshot = parse_android_hierarchy(
        """\
        <hierarchy>
          <node class="android.widget.FrameLayout" package="com.example.app"
                bounds="[0,0][200,400]">
            <node class="android.widget.TextView" package="com.example.app"
                  text="Battery usage: 42%" clickable="true" enabled="false"
                  bounds="[20,20][180,80]" />
            <node class="android.widget.Button" package="com.example.app"
                  text="Continue" clickable="true" bounds="[20,100][180,160]" />
          </node>
        </hierarchy>
        """,
        revision="one",
    )

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            calls.append({"state": state, "questions": questions})
            return SimpleNamespace(
                choices={
                    "next_action": SimpleNamespace(
                        choice="finish", confidence=0.99
                    )
                }
            )

    TypeSafeDecisionProvider(FakeClient()).decide(  # type: ignore[arg-type]
        AgentTask("View battery usage"), snapshot, ()
    )

    state = calls[0]["state"]
    assert isinstance(state, dict)
    observation = state["observation"]
    assert observation["elements"][0]["text"] == "Battery usage: 42%"

    questions = calls[0]["questions"]
    assert isinstance(questions, dict)
    click_targets = questions["click_target"].criteria
    assert set(click_targets) == {"element_1", NO_ACTION_MATCH}


def test_typesafe_offers_handoff_when_required_text_was_not_supplied() -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            calls.append({"state": state, "questions": questions})
            return SimpleNamespace(
                choices={
                    "next_action": SimpleNamespace(
                        choice="handoff_text_input", confidence=0.99
                    )
                }
            )

    decision = TypeSafeDecisionProvider(FakeClient()).decide(  # type: ignore[arg-type]
        AgentTask("Enter a search query"), make_text_input_snapshot("one"), ()
    )

    questions = calls[0]["questions"]
    assert isinstance(questions, dict)
    assert "handoff_text_input" in questions["next_action"].criteria
    assert "text_target" not in questions
    assert "text_input" not in questions
    assert decision.action is AgentAction.HANDOFF_TEXT_INPUT


def test_typesafe_swipe_direction_is_selected_by_outer_action_choice() -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            calls.append({"state": state, "questions": questions})
            return SimpleNamespace(
                choices={
                    "next_action": SimpleNamespace(
                        choice="swipe_up", confidence=0.9
                    )
                }
            )

    decision = TypeSafeDecisionProvider(FakeClient()).decide(  # type: ignore[arg-type]
        AgentTask("Find Bluetooth"), make_snapshot("one"), ()
    )

    questions = calls[0]["questions"]
    assert isinstance(questions, dict)
    assert "swipe_target" not in questions
    assert "swipe_direction" not in questions
    next_action = questions["next_action"]
    assert {"swipe_up", "swipe_down", "swipe_left", "swipe_right"} <= set(
        next_action.criteria
    )
    assert decision.action is AgentAction.SWIPE
    assert decision.direction is SwipeDirection.UP
    assert decision.target is None


def test_agent_swipes_without_an_element_target() -> None:
    first = make_snapshot("one")
    second = make_snapshot("two")
    adapter = FakeAdapter([first, second])
    provider = ScriptedProvider(
        [
            AgentDecision(
                "one",
                AgentAction.SWIPE,
                confidence=0.3,
                direction=SwipeDirection.UP,
            ),
            AgentDecision("two", AgentAction.FINISH, confidence=0.95),
        ]
    )

    result = AgentLoop(
        UITools(adapter),
        provider,
        config=AgentLoopConfig(
            action_confidence=0.2,
            argument_confidence=0.95,
        ),
    ).run(AgentTask("Find Bluetooth"))

    assert result.status is AgentRunStatus.FINISHED
    assert adapter.events == [("swipe", 100, 300, 100, 100, 400)]


def test_typesafe_response_observer_receives_api_response() -> None:
    response = SimpleNamespace(
        choices={
            "next_action": SimpleNamespace(choice="finish", confidence=0.99)
        }
    )
    responses: list[object] = []

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            return response

    provider = TypeSafeDecisionProvider(  # type: ignore[arg-type]
        FakeClient(),
        on_response=responses.append,  # type: ignore[arg-type]
    )

    provider.decide(AgentTask("Open Example"), make_snapshot("one"), ())

    assert responses == [response]


def test_typesafe_call_observer_reports_preparation_and_api_time() -> None:
    response = SimpleNamespace(
        choices={
            "next_action": SimpleNamespace(choice="finish", confidence=0.99)
        }
    )
    clock_values = iter((10.0, 10.25, 10.25, 11.75))
    calls: list[object] = []

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            return response

    provider = TypeSafeDecisionProvider(  # type: ignore[arg-type]
        FakeClient(),
        on_call=calls.append,  # type: ignore[arg-type]
        clock=lambda: next(clock_values),
    )

    provider.decide(AgentTask("Open Example"), make_snapshot("one"), ())

    assert len(calls) == 1
    call = calls[0]
    assert call.response is response
    assert call.preparation_seconds == 0.25
    assert call.api_seconds == 1.5


def test_sensitive_text_value_is_not_sent_to_typesafe() -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def system_one(self, *, state: object, questions: object) -> object:
            calls.append({"state": state, "questions": questions})
            return SimpleNamespace(
                choices={
                    "next_action": SimpleNamespace(
                        choice="finish", confidence=0.99
                    )
                }
            )

    task = AgentTask(
        "Sign in",
        text_inputs=(
            AgentTextInput(
                "password", "very-secret", "Account password", sensitive=True
            ),
        ),
    )

    TypeSafeDecisionProvider(FakeClient()).decide(  # type: ignore[arg-type]
        task, make_snapshot("one"), ()
    )

    assert "very-secret" not in repr(calls[0])


def test_agent_starts_only_app_selected_from_current_catalog() -> None:
    first = make_snapshot("one")
    second = make_snapshot("two")
    adapter = FakeAdapter([first, second])
    provider = ScriptedProvider(
        [
            AgentDecision("one", AgentAction.START_APP, confidence=0.9),
            AgentDecision("two", AgentAction.FINISH, confidence=0.95),
        ],
        app_choice="com.example.other",
    )

    result = AgentLoop(UITools(adapter), provider).run(AgentTask("Open Other"))

    assert result.status is AgentRunStatus.FINISHED
    assert adapter.events == [
        ("start_app", "com.example.other", ".MainActivity")
    ]


def test_unmatched_app_selection_returns_without_invoking_handoff() -> None:
    first = make_snapshot("one")
    handoffs: list[object] = []
    tools = UITools(
        FakeAdapter([first]),
        handoff_handler=lambda request: (
            handoffs.append(request) or HandoffResponse("opened")
        ),
    )
    provider = ScriptedProvider(
        [AgentDecision("one", AgentAction.START_APP, confidence=0.9)],
        app_choice="__no_match__",
    )

    result = AgentLoop(tools, provider).run(AgentTask("Open Missing"))

    assert result.status is AgentRunStatus.NEEDS_HANDOFF
    assert result.message == "no confident launchable app matches the task"
    assert handoffs == []


def test_agent_rejects_decision_from_another_snapshot() -> None:
    tools = UITools(FakeAdapter([make_snapshot("current")]))
    provider = ScriptedProvider(
        [AgentDecision("old", AgentAction.FINISH, confidence=1.0)]
    )

    result = AgentLoop(tools, provider).run(AgentTask("Open Example"))

    assert result.status is AgentRunStatus.FAILED
    assert "stale" in result.message


def test_agent_returns_structured_failure_for_decision_error() -> None:
    tools = UITools(FakeAdapter([make_snapshot("one")]))

    result = AgentLoop(tools, FailingProvider([])).run(AgentTask("Open Example"))

    assert result.status is AgentRunStatus.FAILED
    assert result.message == "decision service unavailable"


def test_agent_requires_current_app_before_model_decision() -> None:
    snapshot = parse_android_hierarchy(
        '<hierarchy><node text="Open" clickable="true" bounds="[0,0][20,20]" /></hierarchy>',
        revision="one",
    )
    provider = ScriptedProvider(
        [AgentDecision("one", AgentAction.FINISH, confidence=1.0)]
    )

    result = AgentLoop(UITools(FakeAdapter([snapshot])), provider).run(
        AgentTask("Open Example")
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.message == "current app information is unavailable"
    assert len(provider.decisions) == 1


def test_selected_click_executes_without_implicit_handoff() -> None:
    first = make_snapshot("one")
    second = make_snapshot("two")
    adapter = FakeAdapter([first, second])
    handoffs: list[object] = []
    tools = UITools(
        adapter,
        handoff_handler=lambda request: (
            handoffs.append(request) or HandoffResponse("done")
        ),
    )
    provider = ScriptedProvider(
        [
            AgentDecision(
                "one",
                AgentAction.CLICK,
                confidence=0.9,
                target=first.elements[0].ref,
                target_confidence=0.9,
            ),
            AgentDecision("two", AgentAction.FINISH, confidence=0.95),
        ]
    )

    result = AgentLoop(tools, provider).run(AgentTask("Submit an order"))

    assert result.status is AgentRunStatus.FINISHED
    assert adapter.events == [("click", 70, 50)]
    assert handoffs == []


def test_missing_text_input_hands_off_with_actionable_instruction() -> None:
    first = make_text_input_snapshot("one")
    second = make_snapshot("two")
    adapter = FakeAdapter([first, second])
    handoffs: list[object] = []
    tools = UITools(
        adapter,
        handoff_handler=lambda request: (
            handoffs.append(request) or HandoffResponse("entered")
        ),
    )
    provider = ScriptedProvider(
        [
            AgentDecision(
                "one", AgentAction.HANDOFF_TEXT_INPUT, confidence=0.99
            ),
            AgentDecision("two", AgentAction.FINISH, confidence=0.99),
        ]
    )

    result = AgentLoop(tools, provider).run(AgentTask("Enter a search query"))

    assert result.status is AgentRunStatus.FINISHED
    assert adapter.events == []
    assert len(handoffs) == 1
    request = handoffs[0]
    assert "Enter the required text" in request.instruction
    assert "Jev cannot generate" in request.reason


def test_model_selected_handoff_does_not_execute_device_action() -> None:
    first = make_snapshot("one")
    second = make_snapshot("two")
    adapter = FakeAdapter([first, second])
    handoffs: list[object] = []
    tools = UITools(
        adapter,
        handoff_handler=lambda request: (
            handoffs.append(request) or HandoffResponse("done")
        ),
    )
    provider = ScriptedProvider(
        [
            AgentDecision("one", AgentAction.HANDOFF, confidence=0.99),
            AgentDecision("two", AgentAction.FINISH, confidence=0.99),
        ]
    )

    result = AgentLoop(tools, provider).run(AgentTask("Submit an order"))

    assert result.status is AgentRunStatus.FINISHED
    assert adapter.events == []
    assert len(handoffs) == 1
