from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .exceptions import ElementNotFoundError, StaleSnapshotError


class Platform(str, Enum):
    ANDROID = "android"
    HARMONYOS = "harmonyos"


@dataclass(frozen=True, slots=True)
class CurrentApp:
    app_id: str
    entry: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.app_id.strip() or len(self.app_id) > 255:
            raise ValueError("current app id must be non-empty and at most 255 characters")
        if self.entry is not None and (
            not self.entry.strip() or len(self.entry) > 255
        ):
            raise ValueError("current app entry must be non-empty and at most 255 characters")
        if self.label is not None and (
            not self.label.strip() or len(self.label) > 200
        ):
            raise ValueError("current app label must be non-empty and at most 200 characters")

    def to_action_data(self) -> dict[str, str | None]:
        return {
            "appId": self.app_id,
            "entry": self.entry,
            "label": self.label,
        }


class ElementCapability(str, Enum):
    CLICK = "click"
    LONG_CLICK = "long_click"
    SCROLL = "scroll"
    INPUT_TEXT = "input_text"
    TOGGLE = "toggle"


@dataclass(frozen=True, slots=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("bounds must have non-negative width and height")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (
            self.left + self.width // 2,
            self.top + self.height // 2,
        )


@dataclass(frozen=True, slots=True)
class ElementRef:
    snapshot_revision: str
    element_id: str


@dataclass(frozen=True, slots=True)
class UIElement:
    element_id: str
    snapshot_revision: str
    platform: Platform
    parent_id: str | None
    child_ids: tuple[str, ...]
    class_name: str | None = None
    resource_id: str | None = None
    package_name: str | None = None
    text: str | None = None
    content_description: str | None = None
    bounds: Bounds | None = None
    enabled: bool = True
    visible: bool = True
    clickable: bool = False
    long_clickable: bool = False
    focusable: bool = False
    focused: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    editable: bool = False
    password: bool = False

    @property
    def ref(self) -> ElementRef:
        return ElementRef(self.snapshot_revision, self.element_id)

    @property
    def label(self) -> str | None:
        if self.text:
            return self.text
        if self.content_description:
            return self.content_description
        if self.resource_id:
            return self.resource_id.rsplit("/", maxsplit=1)[-1]
        return None

    @property
    def capabilities(self) -> frozenset[ElementCapability]:
        capabilities: set[ElementCapability] = set()
        if self.clickable:
            capabilities.add(ElementCapability.CLICK)
        if self.long_clickable:
            capabilities.add(ElementCapability.LONG_CLICK)
        if self.scrollable:
            capabilities.add(ElementCapability.SCROLL)
        if self.editable:
            capabilities.add(ElementCapability.INPUT_TEXT)
        if self.checkable:
            capabilities.add(ElementCapability.TOGGLE)
        return frozenset(capabilities)

    @property
    def actionable(self) -> bool:
        return self.visible and self.enabled and bool(self.capabilities)

    @property
    def meaningful(self) -> bool:
        has_observable_text = not self.password and (
            self.text is not None or self.content_description is not None
        )
        return self.visible and (
            has_observable_text
            or (
                self.actionable
                and (
                    self.label is not None
                    or self.scrollable
                    or self.editable
                    or self.checkable
                )
            )
        )

    @property
    def has_input_focus(self) -> bool:
        return self.actionable and self.editable and self.focused

    @property
    def requires_focus_before_input(self) -> bool:
        return self.actionable and self.editable and not self.focused

    def supports(self, capability: ElementCapability) -> bool:
        return capability in self.capabilities

    def to_action_data(self) -> dict[str, object]:
        bounds = None
        if self.bounds is not None:
            bounds = {
                "left": self.bounds.left,
                "top": self.bounds.top,
                "right": self.bounds.right,
                "bottom": self.bounds.bottom,
            }

        return {
            "text": None if self.password else self.text,
            "resourceId": self.resource_id,
            "contentDescription": self.content_description,
            "className": self.class_name,
            "packageName": self.package_name,
            "bounds": bounds,
            "capabilities": sorted(
                capability.value for capability in self.capabilities
            ),
            "enabled": self.enabled,
            "checked": self.checked,
            "selected": self.selected,
            "focusable": self.focusable,
            "focused": self.focused,
            "hasInputFocus": self.has_input_focus,
            "requiresFocusBeforeInput": self.requires_focus_before_input,
        }


@dataclass(frozen=True, slots=True)
class UISnapshot:
    revision: str
    platform: Platform
    captured_at: datetime
    elements: tuple[UIElement, ...]
    root_ids: tuple[str, ...]
    viewport_bounds: Bounds | None = None
    current_app: CurrentApp | None = None

    def __post_init__(self) -> None:
        if not self.revision:
            raise ValueError("snapshot revision must not be empty")

        elements_by_id = {element.element_id: element for element in self.elements}
        if len(elements_by_id) != len(self.elements):
            raise ValueError("snapshot element ids must be unique")

        for element in self.elements:
            if element.snapshot_revision != self.revision:
                raise ValueError("element belongs to another snapshot revision")
            if element.platform is not self.platform:
                raise ValueError("element belongs to another platform")
            if element.parent_id is not None and element.parent_id not in elements_by_id:
                raise ValueError(f"unknown parent element: {element.parent_id}")
            if any(child_id not in elements_by_id for child_id in element.child_ids):
                raise ValueError(f"unknown child element in: {element.element_id}")

        for root_id in self.root_ids:
            root = elements_by_id.get(root_id)
            if root is None:
                raise ValueError(f"unknown root element: {root_id}")
            if root.parent_id is not None:
                raise ValueError(f"root element has a parent: {root_id}")

    def resolve(self, reference: ElementRef | str) -> UIElement:
        if isinstance(reference, ElementRef):
            if reference.snapshot_revision != self.revision:
                raise StaleSnapshotError(
                    f"element reference is from revision {reference.snapshot_revision!r}, "
                    f"current revision is {self.revision!r}"
                )
            element_id = reference.element_id
        else:
            element_id = reference

        for element in self.elements:
            if element.element_id == element_id:
                return element
        raise ElementNotFoundError(f"element not found: {element_id}")

    @property
    def actionable_elements(self) -> tuple[UIElement, ...]:
        return tuple(element for element in self.elements if element.actionable)

    @property
    def meaningful_elements(self) -> tuple[UIElement, ...]:
        return tuple(element for element in self.elements if element.meaningful)

    @property
    def visible_packages(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    element.package_name
                    for element in self.elements
                    if element.visible and element.package_name is not None
                }
            )
        )

    def only_actionable(self) -> UISnapshot:
        return self._retain(self.actionable_elements)

    def only_meaningful(self) -> UISnapshot:
        return self._retain(self.meaningful_elements)

    def _retain(self, retained: tuple[UIElement, ...]) -> UISnapshot:
        if len(retained) == len(self.elements):
            return self

        elements_by_id = {element.element_id: element for element in self.elements}
        retained_ids = {element.element_id for element in retained}
        parent_ids: dict[str, str | None] = {}
        children_by_parent: dict[str, list[str]] = {
            element.element_id: [] for element in retained
        }
        root_ids: list[str] = []

        for element in retained:
            parent_id = element.parent_id
            while parent_id is not None and parent_id not in retained_ids:
                parent_id = elements_by_id[parent_id].parent_id
            parent_ids[element.element_id] = parent_id
            if parent_id is None:
                root_ids.append(element.element_id)
            else:
                children_by_parent[parent_id].append(element.element_id)

        return UISnapshot(
            revision=self.revision,
            platform=self.platform,
            captured_at=self.captured_at,
            elements=tuple(
                replace(
                    element,
                    parent_id=parent_ids[element.element_id],
                    child_ids=tuple(children_by_parent[element.element_id]),
                )
                for element in retained
            ),
            root_ids=tuple(root_ids),
            viewport_bounds=self.viewport_bounds,
            current_app=self.current_app,
        )
