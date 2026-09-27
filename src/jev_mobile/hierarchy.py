from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from .exceptions import HierarchyParseError
from .models import Bounds, Platform, UIElement, UISnapshot

_ANDROID_BOUNDS = re.compile(
    r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]"
)

_HARMONY_CHILD_KEYS = ("children", "childNodes")


def parse_android_hierarchy(
    payload: str | bytes,
    *,
    revision: str | None = None,
    captured_at: datetime | None = None,
    include_non_actionable: bool = False,
) -> UISnapshot:
    """Parse Android XML into a snapshot, excluding inert elements by default."""
    try:
        hierarchy = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, ValueError, TypeError) as error:
        raise HierarchyParseError("invalid Android hierarchy XML") from error

    if _local_name(hierarchy.tag) != "hierarchy":
        raise HierarchyParseError("Android hierarchy root must be <hierarchy>")

    snapshot_revision = revision or uuid4().hex
    elements: list[UIElement] = []
    root_nodes = tuple(
        child for child in hierarchy if _local_name(child.tag) == "node"
    )

    for root_index, root_node in enumerate(root_nodes):
        _visit_android_node(
            root_node,
            path=(root_index,),
            parent_id=None,
            revision=snapshot_revision,
            elements=elements,
        )

    root_ids = tuple(_android_element_id((index,)) for index in range(len(root_nodes)))
    snapshot = UISnapshot(
        revision=snapshot_revision,
        platform=Platform.ANDROID,
        captured_at=captured_at or datetime.now(timezone.utc),
        elements=tuple(elements),
        root_ids=root_ids,
        viewport_bounds=_viewport_bounds(elements, root_ids),
    )
    return snapshot if include_non_actionable else snapshot.only_meaningful()


def parse_harmonyos_hierarchy(
    payload: str | bytes,
    *,
    revision: str | None = None,
    captured_at: datetime | None = None,
    include_non_actionable: bool = False,
) -> UISnapshot:
    """Parse HarmonyOS JSON into a snapshot, excluding inert elements by default."""
    try:
        hierarchy = json.loads(payload)
    except (JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise HierarchyParseError("invalid HarmonyOS hierarchy JSON") from error

    root_nodes = _harmony_root_nodes(hierarchy)
    snapshot_revision = revision or uuid4().hex
    elements: list[UIElement] = []

    for root_index, root_node in enumerate(root_nodes):
        _visit_harmony_node(
            root_node,
            path=(root_index,),
            parent_id=None,
            revision=snapshot_revision,
            elements=elements,
        )

    root_ids = tuple(
        _harmony_element_id((index,)) for index in range(len(root_nodes))
    )
    snapshot = UISnapshot(
        revision=snapshot_revision,
        platform=Platform.HARMONYOS,
        captured_at=captured_at or datetime.now(timezone.utc),
        elements=tuple(elements),
        root_ids=root_ids,
        viewport_bounds=_viewport_bounds(elements, root_ids),
    )
    return snapshot if include_non_actionable else snapshot.only_meaningful()


def _viewport_bounds(
    elements: list[UIElement], root_ids: tuple[str, ...]
) -> Bounds | None:
    bounds = tuple(
        element.bounds
        for element in elements
        if element.element_id in root_ids
        and element.bounds is not None
        and element.bounds.width > 0
        and element.bounds.height > 0
    )
    return max(bounds, key=lambda item: item.width * item.height, default=None)


def _visit_android_node(
    node: Element,
    *,
    path: tuple[int, ...],
    parent_id: str | None,
    revision: str,
    elements: list[UIElement],
) -> None:
    element_id = _android_element_id(path)
    child_nodes = tuple(child for child in node if _local_name(child.tag) == "node")
    child_ids = tuple(
        _android_element_id((*path, child_index))
        for child_index in range(len(child_nodes))
    )
    class_name = _optional_text(node.get("class"))

    try:
        bounds = _parse_android_bounds(node.get("bounds"))
        enabled = _parse_bool(node.get("enabled"), default=True)
        visible = _parse_bool(node.get("visible-to-user"), default=True)
        clickable = _parse_bool(node.get("clickable"))
        long_clickable = _parse_bool(node.get("long-clickable"))
        focusable = _parse_bool(node.get("focusable"))
        focused = _parse_bool(node.get("focused"))
        scrollable = _parse_bool(node.get("scrollable"))
        checkable = _parse_bool(node.get("checkable"))
        checked = _parse_bool(node.get("checked"))
        selected = _parse_bool(node.get("selected"))
        password = _parse_bool(node.get("password"))
    except ValueError as error:
        raise HierarchyParseError(
            f"invalid attributes for Android element {element_id}"
        ) from error

    visible = visible and bounds is not None
    text = _optional_text(node.get("text"))
    content_description = _optional_text(node.get("content-desc"))
    if (
        text is None
        and content_description is None
        and (clickable or long_clickable or checkable)
    ):
        text = _android_descendant_text(node)

    elements.append(
        UIElement(
            element_id=element_id,
            snapshot_revision=revision,
            platform=Platform.ANDROID,
            parent_id=parent_id,
            child_ids=child_ids,
            class_name=class_name,
            resource_id=_optional_text(node.get("resource-id")),
            package_name=_optional_text(node.get("package")),
            text=text,
            content_description=content_description,
            bounds=bounds,
            enabled=enabled,
            visible=visible,
            clickable=clickable,
            long_clickable=long_clickable,
            focusable=focusable,
            focused=focused,
            scrollable=scrollable,
            checkable=checkable,
            checked=checked,
            selected=selected,
            editable=_is_android_editable(class_name),
            password=password,
        )
    )

    for child_index, child_node in enumerate(child_nodes):
        _visit_android_node(
            child_node,
            path=(*path, child_index),
            parent_id=element_id,
            revision=revision,
            elements=elements,
        )


def _visit_harmony_node(
    node: Mapping[str, Any],
    *,
    path: tuple[int, ...],
    parent_id: str | None,
    revision: str,
    elements: list[UIElement],
) -> None:
    element_id = _harmony_element_id(path)
    attributes = _harmony_attributes(node, element_id)
    child_nodes = _harmony_children(node, element_id)
    child_ids = tuple(
        _harmony_element_id((*path, child_index))
        for child_index in range(len(child_nodes))
    )
    class_name = _optional_text(_attribute(attributes, "type", "class", "className"))

    try:
        bounds = _parse_harmony_bounds(_attribute(attributes, "bounds", "rect"))
        enabled = _parse_bool(_attribute(attributes, "enabled"), default=True)
        visible = _parse_bool(
            _attribute(attributes, "visible", "visibleToUser"), default=True
        )
        clickable = _parse_bool(_attribute(attributes, "clickable"))
        long_clickable = _parse_bool(
            _attribute(attributes, "longClickable", "long-clickable")
        )
        focusable = _parse_bool(_attribute(attributes, "focusable"))
        focused = _parse_bool(_attribute(attributes, "focused"))
        scrollable = _parse_bool(_attribute(attributes, "scrollable"))
        checkable = _parse_bool(_attribute(attributes, "checkable"))
        checked = _parse_bool(_attribute(attributes, "checked"))
        selected = _parse_bool(_attribute(attributes, "selected"))
        editable = _parse_bool(_attribute(attributes, "editable")) or (
            _is_harmony_editable(class_name)
        )
        password = _parse_bool(_attribute(attributes, "password"))
    except ValueError as error:
        raise HierarchyParseError(
            f"invalid attributes for HarmonyOS element {element_id}"
        ) from error

    resource_id = _optional_text(
        _attribute(attributes, "id", "resourceId", "key", "accessibilityId")
    )
    elements.append(
        UIElement(
            element_id=element_id,
            snapshot_revision=revision,
            platform=Platform.HARMONYOS,
            parent_id=parent_id,
            child_ids=child_ids,
            class_name=class_name,
            resource_id=resource_id,
            package_name=_optional_text(
                _attribute(attributes, "bundleName", "bundle", "package")
            ),
            text=_optional_text(_attribute(attributes, "text", "content")),
            content_description=_optional_text(
                _attribute(
                    attributes,
                    "contentDescription",
                    "description",
                    "hint",
                )
            ),
            bounds=bounds,
            enabled=enabled,
            visible=visible,
            clickable=clickable,
            long_clickable=long_clickable,
            focusable=focusable,
            focused=focused,
            scrollable=scrollable,
            checkable=checkable,
            checked=checked,
            selected=selected,
            editable=editable,
            password=password,
        )
    )

    for child_index, child_node in enumerate(child_nodes):
        _visit_harmony_node(
            child_node,
            path=(*path, child_index),
            parent_id=element_id,
            revision=revision,
            elements=elements,
        )


def _android_element_id(path: tuple[int, ...]) -> str:
    return f"android:{'.'.join(str(index) for index in path)}"


def _harmony_element_id(path: tuple[int, ...]) -> str:
    return f"harmonyos:{'.'.join(str(index) for index in path)}"


def _parse_android_bounds(value: str | None) -> Bounds | None:
    if not value:
        return None
    match = _ANDROID_BOUNDS.fullmatch(value)
    if match is None:
        raise ValueError("invalid Android bounds")
    left, top, right, bottom = (int(coordinate) for coordinate in match.groups())
    if right < left or bottom < top:
        return None
    return Bounds(left, top, right, bottom)


def _android_descendant_text(node: Element) -> str | None:
    labels: list[str] = []

    def collect(descendant: Element) -> None:
        if _local_name(descendant.tag) != "node":
            return
        if descendant.get("password", "").strip().lower() == "true":
            return
        if descendant.get("visible-to-user", "").strip().lower() == "false":
            return
        if any(
            descendant.get(attribute, "").strip().lower() == "true"
            for attribute in (
                "clickable",
                "long-clickable",
                "scrollable",
                "checkable",
            )
        ) or _is_android_editable(_optional_text(descendant.get("class"))):
            return
        for attribute in ("text", "content-desc"):
            label = _optional_text(descendant.get(attribute))
            if label is not None and label not in labels:
                labels.append(label)
        for child in descendant:
            collect(child)

    for child in node:
        collect(child)
    return " | ".join(labels) or None


def _parse_harmony_bounds(value: Any) -> Bounds | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        android_match = _ANDROID_BOUNDS.fullmatch(value)
        if android_match is not None:
            return Bounds(*(int(coordinate) for coordinate in android_match.groups()))
        try:
            value = json.loads(value)
        except JSONDecodeError as error:
            raise ValueError("invalid HarmonyOS bounds") from error

    if isinstance(value, Mapping):
        coordinates = tuple(value.get(name) for name in ("left", "top", "right", "bottom"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        coordinates = tuple(value)
    else:
        raise ValueError("invalid HarmonyOS bounds")

    if len(coordinates) != 4 or any(coordinate is None for coordinate in coordinates):
        raise ValueError("HarmonyOS bounds require left, top, right, and bottom")
    return Bounds(*(_parse_coordinate(coordinate) for coordinate in coordinates))


def _parse_coordinate(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("coordinate must be an integer")
    try:
        coordinate = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("coordinate must be an integer") from error
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("coordinate must be an integer")
    return coordinate


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    return stripped or None


def _is_android_editable(class_name: str | None) -> bool:
    return class_name is not None and class_name.rsplit(".", maxsplit=1)[-1] in {
        "EditText",
        "AutoCompleteTextView",
        "MultiAutoCompleteTextView",
    }


def _is_harmony_editable(class_name: str | None) -> bool:
    return class_name is not None and class_name.rsplit(".", maxsplit=1)[-1] in {
        "Search",
        "TextArea",
        "TextInput",
    }


def _harmony_root_nodes(hierarchy: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(hierarchy, list):
        roots = hierarchy
    elif isinstance(hierarchy, Mapping):
        if "attributes" in hierarchy or any(
            key in hierarchy for key in ("type", "class", "className")
        ):
            roots = [hierarchy]
        elif isinstance(hierarchy.get("root"), Mapping):
            roots = [hierarchy["root"]]
        else:
            roots = next(
                (
                    hierarchy[key]
                    for key in ("roots", "windows", *_HARMONY_CHILD_KEYS)
                    if key in hierarchy
                ),
                None,
            )
            if roots is None:
                raise HierarchyParseError(
                    "HarmonyOS hierarchy does not contain component nodes"
                )
    else:
        raise HierarchyParseError("HarmonyOS hierarchy root must be an object or array")

    if not isinstance(roots, list) or not all(
        isinstance(root, Mapping) for root in roots
    ):
        raise HierarchyParseError("HarmonyOS hierarchy roots must be objects")
    return tuple(roots)


def _harmony_attributes(
    node: Mapping[str, Any], element_id: str
) -> Mapping[str, Any]:
    nested = node.get("attributes", {})
    if not isinstance(nested, Mapping):
        raise HierarchyParseError(
            f"attributes must be an object for HarmonyOS element {element_id}"
        )
    direct = {
        key: value
        for key, value in node.items()
        if key != "attributes" and key not in _HARMONY_CHILD_KEYS
    }
    return {**direct, **nested}


def _harmony_children(
    node: Mapping[str, Any], element_id: str
) -> tuple[Mapping[str, Any], ...]:
    children: Any = []
    for key in _HARMONY_CHILD_KEYS:
        if key in node:
            children = node[key]
            break
    if not isinstance(children, list) or not all(
        isinstance(child, Mapping) for child in children
    ):
        raise HierarchyParseError(
            f"children must be an array for HarmonyOS element {element_id}"
        )
    return tuple(children)


def _attribute(attributes: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in attributes:
            return attributes[name]
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]
