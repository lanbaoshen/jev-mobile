from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from time import perf_counter
from typing import Protocol

from typesafe_sdk import Choice, SystemOneResponse, TypeSafeClient, TypeSafeError

from .actions import SwipeDirection, ToolResult, UITools
from .apps import AppCatalog, build_start_app_choice, select_app_reference
from .exceptions import AgentDecisionError, AppNotFoundError, JevMobileError
from .models import ElementCapability, ElementRef, UIElement, UISnapshot

NO_ACTION_MATCH = "__no_match__"
MAX_ELEMENT_CHOICES = 254
SWIPE_ACTION_CHOICES = {
    f"swipe_{direction.value}": direction for direction in SwipeDirection
}


class AgentAction(str, Enum):
    START_APP = "start_app"
    CLICK = "click"
    LONG_CLICK = "long_click"
    SWIPE = "swipe"
    TYPE_TEXT = "type_text"
    HANDOFF_TEXT_INPUT = "handoff_text_input"
    WAIT = "wait"
    HANDOFF = "handoff"
    FINISH = "finish"
    NO_MATCH = NO_ACTION_MATCH


class AgentRunStatus(str, Enum):
    FINISHED = "finished"
    STEP_LIMIT = "step_limit"
    NEEDS_HANDOFF = "needs_handoff"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentTextInput:
    key: str
    value: str
    description: str
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.key.strip() or len(self.key) > 100:
            raise ValueError("text input key must be non-empty and at most 100 characters")
        if not self.value or len(self.value) > 1_000 or "\x00" in self.value:
            raise ValueError("text input value must be non-empty and at most 1000 characters")
        if not self.description.strip() or len(self.description) > 500:
            raise ValueError(
                "text input description must be non-empty and at most 500 characters"
            )


@dataclass(frozen=True, slots=True)
class AgentTask:
    goal: str
    text_inputs: tuple[AgentTextInput, ...] = ()

    def __post_init__(self) -> None:
        if not self.goal.strip() or len(self.goal) > 1_000 or "\x00" in self.goal:
            raise ValueError("goal must be non-empty and at most 1000 characters")
        keys = {item.key for item in self.text_inputs}
        if len(keys) != len(self.text_inputs):
            raise ValueError("text input keys must be unique")
        if len(self.text_inputs) > MAX_ELEMENT_CHOICES:
            raise ValueError("too many text inputs for one Choice question")

    def input_by_key(self, key: str) -> AgentTextInput:
        for item in self.text_inputs:
            if item.key == key:
                return item
        raise AgentDecisionError(f"unknown text input: {key}")


@dataclass(frozen=True, slots=True)
class AgentDecision:
    snapshot_revision: str
    action: AgentAction
    confidence: float
    target: ElementRef | None = None
    target_confidence: float | None = None
    direction: SwipeDirection | None = None
    text_input_key: str | None = None
    text_input_confidence: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("confidence", self.confidence),
            ("target_confidence", self.target_confidence),
            ("text_input_confidence", self.text_input_confidence),
        ):
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class AgentAppDecision:
    catalog_revision: str
    choice: str
    confidence: float

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class AgentStep:
    index: int
    action: str
    success: bool
    detail: str | None = None
    duration_seconds: float = 0.0

    def to_state(self) -> dict[str, object]:
        return {
            "step": self.index,
            "action": self.action,
            "success": self.success,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentRunStatus
    history: tuple[AgentStep, ...]
    message: str


@dataclass(frozen=True, slots=True)
class TypeSafeCall:
    response: SystemOneResponse
    preparation_seconds: float
    api_seconds: float

    def __post_init__(self) -> None:
        if self.preparation_seconds < 0 or self.api_seconds < 0:
            raise ValueError("TypeSafe call durations must not be negative")


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_steps: int = 30
    action_confidence: float = 0.0
    argument_confidence: float = 0.0
    finish_confidence: float = 0.0
    uncertain_retries: int = 1
    reobserve_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= 1_000:
            raise ValueError("max_steps must be between 1 and 1000")
        for name, value in (
            ("action_confidence", self.action_confidence),
            ("argument_confidence", self.argument_confidence),
            ("finish_confidence", self.finish_confidence),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if not 0 <= self.uncertain_retries <= 10:
            raise ValueError("uncertain_retries must be between 0 and 10")
        if not 0.1 <= self.reobserve_delay_seconds <= 30:
            raise ValueError("reobserve_delay_seconds must be between 0.1 and 30")


class AgentDecisionProvider(Protocol):
    def decide(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        history: tuple[AgentStep, ...],
    ) -> AgentDecision: ...

    def select_app(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        catalog: AppCatalog,
        history: tuple[AgentStep, ...],
    ) -> AgentAppDecision: ...

class TypeSafeDecisionProvider:
    def __init__(
        self,
        client: TypeSafeClient,
        *,
        history_limit: int = 8,
        on_response: Callable[[SystemOneResponse], None] | None = None,
        on_call: Callable[[TypeSafeCall], None] | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if not 0 <= history_limit <= 50:
            raise ValueError("history_limit must be between 0 and 50")
        self._client = client
        self._history_limit = history_limit
        self._on_response = on_response
        self._on_call = on_call
        self._clock = clock

    def decide(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        history: tuple[AgentStep, ...],
    ) -> AgentDecision:
        preparation_started = self._clock()
        element_by_key = {
            f"element_{index}": element
            for index, element in enumerate(snapshot.meaningful_elements)
        }
        input_by_key = {
            f"input_{index}": item for index, item in enumerate(task.text_inputs)
        }
        questions = self._build_questions(element_by_key, input_by_key)
        state = self._build_state(task, snapshot, history, element_by_key)
        preparation_seconds = self._clock() - preparation_started
        response = self._system_one(
            state=state,
            questions=questions,
            preparation_seconds=preparation_seconds,
        )
        action_answer = response.choices["next_action"]
        direction = SWIPE_ACTION_CHOICES.get(action_answer.choice)
        if direction is not None:
            action = AgentAction.SWIPE
        else:
            try:
                action = AgentAction(action_answer.choice)
            except ValueError as error:
                raise AgentDecisionError(
                    f"unknown action choice: {action_answer.choice}"
                ) from error

        target = None
        target_confidence = None
        text_input_key = None
        text_input_confidence = None

        question_id = {
            AgentAction.CLICK: "click_target",
            AgentAction.LONG_CLICK: "long_click_target",
            AgentAction.TYPE_TEXT: "text_target",
        }.get(action)
        if question_id is not None:
            target_answer = response.choices[question_id]
            target_confidence = target_answer.confidence
            if target_answer.choice != NO_ACTION_MATCH:
                element = element_by_key.get(target_answer.choice)
                if element is None:
                    raise AgentDecisionError(
                        f"unknown element choice: {target_answer.choice}"
                    )
                target = element.ref

        if action is AgentAction.TYPE_TEXT:
            input_answer = response.choices["text_input"]
            text_input_confidence = input_answer.confidence
            if input_answer.choice != NO_ACTION_MATCH:
                text_input = input_by_key.get(input_answer.choice)
                if text_input is None:
                    raise AgentDecisionError(
                        f"unknown text input choice: {input_answer.choice}"
                    )
                text_input_key = text_input.key

        return AgentDecision(
            snapshot_revision=snapshot.revision,
            action=action,
            confidence=action_answer.confidence,
            target=target,
            target_confidence=target_confidence,
            direction=direction,
            text_input_key=text_input_key,
            text_input_confidence=text_input_confidence,
        )

    def select_app(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        catalog: AppCatalog,
        history: tuple[AgentStep, ...],
    ) -> AgentAppDecision:
        preparation_started = self._clock()
        state = self._build_state(task, snapshot, history)
        questions = {"start_app_target": build_start_app_choice(catalog)}
        preparation_seconds = self._clock() - preparation_started
        response = self._system_one(
            state=state,
            questions=questions,
            preparation_seconds=preparation_seconds,
        )
        answer = response.choices["start_app_target"]
        return AgentAppDecision(
            catalog_revision=catalog.revision,
            choice=answer.choice,
            confidence=answer.confidence,
        )

    def _system_one(
        self,
        *,
        state: dict[str, object],
        questions: dict[str, Choice],
        preparation_seconds: float,
    ) -> SystemOneResponse:
        api_started = self._clock()
        try:
            response = self._client.system_one(state=state, questions=questions)
        except TypeSafeError as error:
            raise AgentDecisionError("TypeSafe decision request failed") from error
        api_seconds = self._clock() - api_started
        if self._on_call is not None:
            self._on_call(
                TypeSafeCall(
                    response=response,
                    preparation_seconds=preparation_seconds,
                    api_seconds=api_seconds,
                )
            )
        if self._on_response is not None:
            self._on_response(response)
        return response

    def _build_questions(
        self,
        element_by_key: dict[str, UIElement],
        input_by_key: dict[str, AgentTextInput],
    ) -> dict[str, Choice]:
        criteria: dict[str, object] = {
            AgentAction.START_APP.value: {
                "when": "The task requires another app that is not the current foreground app",
                "not_for": "Navigating within the current app",
            },
            AgentAction.WAIT.value: "The UI is visibly loading or in a transient state",
            AgentAction.HANDOFF.value: {
                "when": "The user must provide missing information or personally perform a destructive or externally consequential action",
                "not_for": "Entering missing text into a visible editable field when handoff_text_input is available",
            },
            AgentAction.FINISH.value: {
                "when": "The current app and visible UI directly prove the user's goal is complete",
                "not_for": "A final action was merely attempted, the page is loading, no action is obvious, or completion is uncertain",
            },
            NO_ACTION_MATCH: "No safe next action can be determined; this does not mean the task is complete",
        }
        questions: dict[str, Choice] = {}

        capability_questions = (
            (AgentAction.CLICK, ElementCapability.CLICK, "click_target"),
            (AgentAction.LONG_CLICK, ElementCapability.LONG_CLICK, "long_click_target"),
        )
        for action, capability, question_id in capability_questions:
            candidates = {
                key: element
                for key, element in element_by_key.items()
                if element.actionable and element.supports(capability)
            }
            if candidates:
                criteria[action.value] = f"A visible element supports {action.value} and that action advances the task"
                questions[question_id] = self._element_choice(action, candidates)

        text_candidates = {
            key: element
            for key, element in element_by_key.items()
            if element.actionable
            and element.supports(ElementCapability.INPUT_TEXT)
        }
        if text_candidates and input_by_key:
            criteria[AgentAction.TYPE_TEXT.value] = "A provided text value should be entered into a visible editable field; click first when the field requires focus"
            questions["text_target"] = self._element_choice(
                AgentAction.TYPE_TEXT, text_candidates
            )
            input_criteria: dict[str, object] = {
                key: {
                    "description": item.description,
                    "value": None if item.sensitive else item.value,
                    "sensitive": item.sensitive,
                }
                for key, item in input_by_key.items()
            }
            input_criteria[NO_ACTION_MATCH] = "None of the provided values belongs in the selected field"
            questions["text_input"] = Choice(
                instructions="If the next action is type_text, which provided value belongs in the selected field?",
                criteria=input_criteria,
            )
        elif text_candidates:
            criteria[AgentAction.HANDOFF_TEXT_INPUT.value] = {
                "when": "The task requires entering text into a visible editable field, but no provided text value is available",
                "result": "Ask the user to enter the required text because Jev cannot generate text",
                "not_for": "The task can progress without entering text",
            }

        criteria.update(
            {
                "swipe_up": {
                    "gesture": "Move the finger from the lower half toward the upper half",
                    "result": "Reveal content below the currently visible content; move farther down a vertical list",
                },
                "swipe_down": {
                    "gesture": "Move the finger from the upper half toward the lower half",
                    "result": "Reveal content above the currently visible content; move toward the top of a vertical list",
                },
                "swipe_left": {
                    "gesture": "Move the finger from the right half toward the left half",
                    "result": "Reveal content to the right of the currently visible content",
                },
                "swipe_right": {
                    "gesture": "Move the finger from the left half toward the right half",
                    "result": "Reveal content to the left of the currently visible content",
                },
            }
        )

        questions["next_action"] = Choice(
            instructions={
                "question": "Which single safe action should happen next to accomplish `task.goal` from `observation`?",
                "constraint": "Choose finish only when currentApp and the visible UI directly establish completion. Choose no match instead of guessing.",
            },
            criteria=criteria,
        )
        return questions

    @staticmethod
    def _element_choice(
        action: AgentAction, candidates: dict[str, UIElement]
    ) -> Choice:
        if len(candidates) > MAX_ELEMENT_CHOICES:
            raise AgentDecisionError(
                f"too many {action.value} candidates for one Choice question"
            )
        criteria: dict[str, object] = {
            key: element.to_action_data() for key, element in candidates.items()
        }
        criteria[NO_ACTION_MATCH] = "No candidate is appropriate for this action"
        return Choice(
            instructions=f"If the next action is {action.value}, which visible element should it target?",
            criteria=criteria,
        )

    def _build_state(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        history: tuple[AgentStep, ...],
        element_by_key: dict[str, UIElement] | None = None,
    ) -> dict[str, object]:
        if element_by_key is None:
            element_by_key = {
                f"element_{index}": element
                for index, element in enumerate(snapshot.meaningful_elements)
            }
        current_app = (
            snapshot.current_app.to_action_data()
            if snapshot.current_app is not None
            else None
        )
        return {
            "task": {
                "goal": task.goal,
                "textInputs": [
                    {
                        "key": item.key,
                        "description": item.description,
                        "value": None if item.sensitive else item.value,
                        "sensitive": item.sensitive,
                    }
                    for item in task.text_inputs
                ],
            },
            "observation": {
                "platform": snapshot.platform.value,
                "currentApp": current_app,
                "visiblePackages": list(snapshot.visible_packages),
                "elements": [
                    {"key": key, **element.to_action_data()}
                    for key, element in element_by_key.items()
                ],
            },
            "recentActions": [
                step.to_state() for step in history[-self._history_limit :]
            ],
        }

class AgentLoop:
    def __init__(
        self,
        tools: UITools,
        decisions: AgentDecisionProvider,
        *,
        config: AgentLoopConfig = AgentLoopConfig(),
        on_decision: Callable[[AgentDecision, UISnapshot], None] | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> None:
        self._tools = tools
        self._decisions = decisions
        self._config = config
        self._on_decision = on_decision
        self._on_step = on_step

    def run(self, task: AgentTask) -> AgentRunResult:
        history: list[AgentStep] = []
        try:
            return self._run(task, history)
        except JevMobileError as error:
            return self._result(AgentRunStatus.FAILED, history, str(error))

    def _run(
        self, task: AgentTask, history: list[AgentStep]
    ) -> AgentRunResult:
        uncertain_attempts = 0

        for _ in range(self._config.max_steps):
            snapshot = self._tools.observe()
            if snapshot.current_app is None:
                return self._result(
                    AgentRunStatus.FAILED,
                    history,
                    "current app information is unavailable",
                )
            decision = self._decisions.decide(task, snapshot, tuple(history))
            if decision.snapshot_revision != snapshot.revision:
                return self._result(
                    AgentRunStatus.FAILED,
                    history,
                    "decision belongs to a stale UI snapshot",
                )
            if self._on_decision is not None:
                self._on_decision(decision, snapshot)

            uncertainty = self._uncertainty(decision)
            if uncertainty is not None:
                uncertain_attempts += 1
                if uncertain_attempts <= self._config.uncertain_retries:
                    wait_result = self._tools.wait(
                        self._config.reobserve_delay_seconds
                    )
                    self._record(history, wait_result, uncertainty)
                    if not wait_result.success:
                        return self._tool_failure(history, wait_result)
                    continue
                return self._result(
                    AgentRunStatus.NEEDS_HANDOFF, history, uncertainty
                )

            uncertain_attempts = 0
            if decision.action is AgentAction.FINISH:
                result = self._tools.finish(f"Completed task: {task.goal}")
                self._record(history, result)
                if not result.success:
                    return self._tool_failure(history, result)
                return self._result(
                    AgentRunStatus.FINISHED, history, result.output or "Task completed"
                )

            if decision.action is AgentAction.HANDOFF:
                result = self._handoff(
                    "Complete the required step on the device, then return.",
                    "The model determined that the next step requires the user.",
                )
                if result is None:
                    return self._result(
                        AgentRunStatus.NEEDS_HANDOFF,
                        history,
                        "the next step requires the user",
                    )
                self._record(history, result)
                if not result.success:
                    return self._tool_failure(history, result)
                continue

            if decision.action is AgentAction.HANDOFF_TEXT_INPUT:
                result = self._handoff(
                    "Enter the required text in the visible field on the device, then return.",
                    "Text input is required, but no text value was provided and Jev cannot generate one.",
                )
                if result is None:
                    return self._result(
                        AgentRunStatus.NEEDS_HANDOFF,
                        history,
                        "text input requires the user",
                    )
                self._record(history, result)
                if not result.success:
                    return self._tool_failure(history, result)
                continue

            if decision.action is AgentAction.START_APP:
                result = self._start_app(task, snapshot, decision, tuple(history))
                if result is None:
                    return self._result(
                        AgentRunStatus.NEEDS_HANDOFF,
                        history,
                        "no confident launchable app matches the task",
                    )
                self._record(history, result)
                if not result.success:
                    return self._tool_failure(history, result)
                continue

            result = self._execute(task, decision)
            self._record(history, result)
            if not result.success:
                return self._tool_failure(history, result)

        return self._result(
            AgentRunStatus.STEP_LIMIT,
            history,
            f"agent reached the {self._config.max_steps}-step limit",
        )

    def _uncertainty(self, decision: AgentDecision) -> str | None:
        if decision.action is AgentAction.NO_MATCH:
            return "the model found no safe next action"
        threshold = (
            self._config.finish_confidence
            if decision.action is AgentAction.FINISH
            else self._config.action_confidence
        )
        if decision.confidence < threshold:
            return f"{decision.action.value} confidence is below the threshold"
        if decision.action in {
            AgentAction.CLICK,
            AgentAction.LONG_CLICK,
            AgentAction.TYPE_TEXT,
        } and (
            decision.target is None
            or decision.target_confidence is None
            or decision.target_confidence < self._config.argument_confidence
        ):
            return f"{decision.action.value} target is missing or uncertain"
        if decision.action is AgentAction.SWIPE and (
            decision.direction is None
        ):
            return "swipe direction is missing"
        if decision.action is AgentAction.TYPE_TEXT and (
            decision.text_input_key is None
            or decision.text_input_confidence is None
            or decision.text_input_confidence < self._config.argument_confidence
        ):
            return "text input value is missing or uncertain"
        return None

    def _start_app(
        self,
        task: AgentTask,
        snapshot: UISnapshot,
        decision: AgentDecision,
        history: tuple[AgentStep, ...],
    ) -> ToolResult | None:
        catalog = self._tools.available_apps()
        app_decision = self._decisions.select_app(
            task, snapshot, catalog, history
        )
        if app_decision.catalog_revision != catalog.revision:
            raise AgentDecisionError("app decision belongs to a stale catalog")
        try:
            reference = select_app_reference(
                catalog,
                app_decision.choice,
                app_decision.confidence,
                minimum_confidence=self._config.argument_confidence,
            )
        except AppNotFoundError as error:
            raise AgentDecisionError("app choice is outside the current catalog") from error
        if reference is None:
            return None
        return self._tools.start_app(reference)

    def _execute(self, task: AgentTask, decision: AgentDecision) -> ToolResult:
        if decision.action is AgentAction.WAIT:
            return self._tools.wait(self._config.reobserve_delay_seconds)
        if decision.action is AgentAction.SWIPE:
            assert decision.direction is not None
            return self._tools.swipe(decision.direction)
        assert decision.target is not None
        if decision.action is AgentAction.CLICK:
            return self._tools.click(decision.target)
        if decision.action is AgentAction.LONG_CLICK:
            return self._tools.long_click(decision.target)
        if decision.action is AgentAction.TYPE_TEXT:
            assert decision.text_input_key is not None
            return self._tools.type_text(
                decision.target,
                task.input_by_key(decision.text_input_key).value,
            )
        raise AgentDecisionError(f"cannot execute action: {decision.action.value}")

    def _handoff(self, instruction: str, reason: str) -> ToolResult | None:
        if not self._tools.handoff_available:
            return None
        return self._tools.handoff(instruction, reason=reason)

    def _record(
        self,
        history: list[AgentStep],
        result: ToolResult,
        detail: str | None = None,
    ) -> None:
        if result.failure is not None:
            detail = result.failure.message
        elif result.output is not None:
            detail = result.output
        step = AgentStep(
            index=len(history) + 1,
            action=result.action.value,
            success=result.success,
            detail=detail,
            duration_seconds=result.duration_seconds,
        )
        history.append(step)
        if self._on_step is not None:
            self._on_step(step)

    @staticmethod
    def _result(
        status: AgentRunStatus,
        history: list[AgentStep],
        message: str,
    ) -> AgentRunResult:
        return AgentRunResult(status=status, history=tuple(history), message=message)

    def _tool_failure(
        self, history: list[AgentStep], result: ToolResult
    ) -> AgentRunResult:
        message = (
            result.failure.message
            if result.failure is not None
            else f"{result.action.value} failed"
        )
        return self._result(AgentRunStatus.FAILED, history, message)
