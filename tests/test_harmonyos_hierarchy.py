import json

import pytest

from jev_mobile import (
    Bounds,
    ElementCapability,
    HierarchyParseError,
    parse_harmonyos_hierarchy,
)


HARMONYOS_HIERARCHY = {
    "attributes": {
        "id": "root",
        "type": "Column",
        "bundleName": "com.example",
        "enabled": True,
        "bounds": {"left": 0, "top": 0, "right": 720, "bottom": 1280},
    },
    "children": [
        {
            "attributes": {
                "id": "login",
                "type": "Button",
                "text": "Sign in",
                "clickable": "true",
                "bounds": "[40,800][680,900]",
            },
            "children": [],
        },
        {
            "attributes": {
                "id": "email",
                "type": "TextInput",
                "hint": "Email address",
                "clickable": True,
                "bounds": '{"left":40,"top":600,"right":680,"bottom":700}',
            },
            "children": [],
        },
        {
            "attributes": {
                "id": "feed",
                "type": "Scroll",
                "scrollable": True,
                "bounds": [0, 900, 720, 1280],
            },
            "children": [],
        },
    ],
}


def test_parse_harmonyos_hierarchy_normalizes_elements() -> None:
    snapshot = parse_harmonyos_hierarchy(
        json.dumps(HARMONYOS_HIERARCHY), revision="revision-2"
    )

    assert snapshot.root_ids == (
        "harmonyos:0.0",
        "harmonyos:0.1",
        "harmonyos:0.2",
    )
    assert len(snapshot.elements) == 3

    button = snapshot.resolve("harmonyos:0.0")
    assert button.class_name == "Button"
    assert button.resource_id == "login"
    assert button.label == "Sign in"
    assert button.bounds == Bounds(40, 800, 680, 900)
    assert button.actionable is True
    assert button.capabilities == frozenset({ElementCapability.CLICK})

    email = snapshot.resolve("harmonyos:0.1")
    assert email.editable is True
    assert email.label == "Email address"
    assert email.capabilities == frozenset(
        {ElementCapability.CLICK, ElementCapability.INPUT_TEXT}
    )

    scroll = snapshot.resolve("harmonyos:0.2")
    assert scroll.capabilities == frozenset({ElementCapability.SCROLL})
    assert snapshot.actionable_elements == snapshot.elements

    unfiltered = parse_harmonyos_hierarchy(
        json.dumps(HARMONYOS_HIERARCHY), include_non_actionable=True
    )
    assert unfiltered.root_ids == ("harmonyos:0",)
    assert len(unfiltered.elements) == 4


def test_parse_harmonyos_hierarchy_defaults_blank_boolean_attributes() -> None:
    hierarchy = {
        "attributes": {
            "id": "root",
            "type": "Column",
            "enabled": "",
            "visible": " ",
            "clickable": "",
            "bounds": [0, 0, 720, 1280],
        },
        "children": [],
    }

    snapshot = parse_harmonyos_hierarchy(
        json.dumps(hierarchy), include_non_actionable=True
    )

    root = snapshot.resolve("harmonyos:0")
    assert root.enabled is True
    assert root.visible is True
    assert root.clickable is False


def test_static_harmonyos_text_is_observed_but_not_actionable() -> None:
    hierarchy = {
        "attributes": {
            "id": "root",
            "type": "Column",
            "bounds": [0, 0, 720, 1280],
        },
        "children": [
            {
                "attributes": {
                    "id": "battery-usage",
                    "type": "Text",
                    "text": "Battery usage: 42%",
                    "bounds": [40, 200, 680, 300],
                },
                "children": [],
            }
        ],
    }

    snapshot = parse_harmonyos_hierarchy(json.dumps(hierarchy))

    static_text = snapshot.resolve("harmonyos:0.0")
    assert static_text.text == "Battery usage: 42%"
    assert static_text.meaningful is True
    assert static_text.actionable is False
    assert snapshot.actionable_elements == ()


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{}",
        '{"attributes": [], "children": []}',
        '{"attributes": {"bounds": {"left": 0}}, "children": []}',
        '{"attributes": {"bounds": [{}, 0, 1, 1]}, "children": []}',
        '{"attributes": {}, "children": {}}',
    ],
)
def test_parse_harmonyos_hierarchy_rejects_malformed_input(payload: str) -> None:
    with pytest.raises(HierarchyParseError):
        parse_harmonyos_hierarchy(payload)
