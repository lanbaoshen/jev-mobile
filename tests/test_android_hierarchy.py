from datetime import datetime, timezone

import pytest

from jev_mobile import (
    Bounds,
    ElementCapability,
    HierarchyParseError,
    StaleSnapshotError,
    parse_android_hierarchy,
)


ANDROID_HIERARCHY = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example" clickable="false" enabled="true"
        bounds="[0,0][1080,2400]">
    <node index="0" text="Continue" resource-id="com.example:id/continue_button"
          class="android.widget.Button" package="com.example" clickable="true"
          enabled="true" visible-to-user="true" content-desc="Continue setup"
          bounds="[40,1800][1040,1940]" />
    <node index="1" text="" resource-id="com.example:id/email"
          class="android.widget.EditText" package="com.example" clickable="true"
            enabled="true" focusable="true" focused="false" password="false"
            bounds="[40,1500][1040,1640]" />
  </node>
</hierarchy>
"""


def test_parse_android_hierarchy_normalizes_elements() -> None:
    captured_at = datetime(2026, 9, 29, tzinfo=timezone.utc)

    snapshot = parse_android_hierarchy(
        ANDROID_HIERARCHY,
        revision="revision-1",
        captured_at=captured_at,
    )

    assert snapshot.revision == "revision-1"
    assert snapshot.captured_at == captured_at
    assert snapshot.root_ids == ("android:0.0", "android:0.1")
    assert len(snapshot.elements) == 2

    button = snapshot.resolve("android:0.0")
    assert button.parent_id is None
    assert button.label == "Continue"
    assert button.bounds == Bounds(40, 1800, 1040, 1940)
    assert button.bounds.center == (540, 1870)
    assert button.actionable is True
    assert button.capabilities == frozenset({ElementCapability.CLICK})
    assert snapshot.resolve(button.ref) is button

    email = snapshot.resolve("android:0.1")
    assert email.editable is True
    assert email.label == "email"
    assert email.capabilities == frozenset(
        {ElementCapability.CLICK, ElementCapability.INPUT_TEXT}
    )
    assert email.has_input_focus is False
    assert email.requires_focus_before_input is True
    assert email.to_action_data()["requiresFocusBeforeInput"] is True
    assert snapshot.actionable_elements == snapshot.elements

    unfiltered = parse_android_hierarchy(
        ANDROID_HIERARCHY, include_non_actionable=True
    )
    assert unfiltered.root_ids == ("android:0",)
    assert len(unfiltered.elements) == 3


def test_focused_input_is_ready_for_text() -> None:
    hierarchy = ANDROID_HIERARCHY.replace('focused="false"', 'focused="true"')

    email = parse_android_hierarchy(hierarchy).resolve("android:0.1")

    assert email.has_input_focus is True
    assert email.requires_focus_before_input is False
    assert email.to_action_data()["hasInputFocus"] is True


def test_static_android_text_is_observed_but_not_actionable() -> None:
        hierarchy = """\
        <hierarchy>
            <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
                <node class="android.widget.TextView" text="Battery usage: 42%"
                            bounds="[40,200][1040,300]" />
            </node>
        </hierarchy>
        """

        snapshot = parse_android_hierarchy(hierarchy)

        static_text = snapshot.resolve("android:0.0")
        assert static_text.text == "Battery usage: 42%"
        assert static_text.meaningful is True
        assert static_text.actionable is False
        assert snapshot.actionable_elements == ()


def test_offscreen_android_node_with_inverted_bounds_is_not_actionable() -> None:
    hierarchy = """\
    <hierarchy>
      <node class="android.widget.Button" text="Off screen" clickable="true"
            visible-to-user="true" bounds="[0,2387][1080,2337]" />
    </hierarchy>
    """

    assert parse_android_hierarchy(hierarchy).elements == ()

    element = parse_android_hierarchy(
        hierarchy, include_non_actionable=True
    ).resolve("android:0")
    assert element.bounds is None
    assert element.visible is False
    assert element.actionable is False


    def test_clickable_android_container_uses_visible_descendant_text() -> None:
        hierarchy = """\
        <hierarchy>
        <node class="android.widget.LinearLayout" clickable="true"
            bounds="[0,200][1080,400]">
          <node class="android.widget.TextView" text="Connected devices"
              bounds="[100,220][900,280]" />
          <node class="android.widget.TextView" text="Bluetooth, pairing"
              bounds="[100,290][900,350]" />
          <node class="android.widget.TextView" text="Hidden detail"
              visible-to-user="false" bounds="[100,350][900,380]" />
          <node class="android.widget.TextView" text="secret"
              password="true" bounds="[100,350][900,380]" />
        </node>
        </hierarchy>
        """

        snapshot = parse_android_hierarchy(hierarchy)

        assert len(snapshot.elements) == 1
        assert snapshot.elements[0].label == "Connected devices | Bluetooth, pairing"


def test_actionable_tree_preserves_scroll_relationships() -> None:
        hierarchy = """\
        <hierarchy>
    <node class="android.widget.FrameLayout" clickable="true"
        bounds="[0,0][1080,2400]">
                <node class="android.widget.ScrollView" scrollable="true"
                            bounds="[0,0][1080,2200]">
                    <node class="android.widget.LinearLayout" bounds="[0,0][1080,2200]">
                        <node class="android.widget.Button" text="Submit" clickable="true"
                                    bounds="[40,1800][1040,1940]" />
                    </node>
                </node>
            </node>
        </hierarchy>
        """

        snapshot = parse_android_hierarchy(hierarchy)

        scroll = snapshot.resolve("android:0.0")
        button = snapshot.resolve("android:0.0.0.0")
        assert snapshot.root_ids == (scroll.element_id,)
        assert scroll.capabilities == frozenset({ElementCapability.SCROLL})
        assert scroll.child_ids == (button.element_id,)
        assert button.parent_id == scroll.element_id
        assert button.supports(ElementCapability.CLICK)

        unfiltered = parse_android_hierarchy(hierarchy, include_non_actionable=True)
        empty_wrapper = unfiltered.resolve("android:0")
        assert empty_wrapper.actionable is True
        assert empty_wrapper.meaningful is False


def test_element_reference_cannot_resolve_against_a_new_snapshot() -> None:
    first_snapshot = parse_android_hierarchy(ANDROID_HIERARCHY)
    second_snapshot = parse_android_hierarchy(ANDROID_HIERARCHY)

    reference = first_snapshot.resolve("android:0.0").ref

    with pytest.raises(StaleSnapshotError):
        second_snapshot.resolve(reference)


@pytest.mark.parametrize(
    "payload",
    [
        "<hierarchy><node></hierarchy>",
        '<hierarchy><node bounds="not-bounds" /></hierarchy>',
        "<node />",
    ],
)
def test_parse_android_hierarchy_rejects_malformed_input(payload: str) -> None:
    with pytest.raises(HierarchyParseError):
        parse_android_hierarchy(payload)
