from datetime import datetime, timezone

import pytest

from jev_mobile import (
    NO_APP_MATCH,
    AppCatalog,
    AppNotFoundError,
    LaunchableApp,
    Platform,
    build_start_app_choice,
    select_app_reference,
)


def make_catalog() -> AppCatalog:
    return AppCatalog(
        revision="catalog-1",
        platform=Platform.ANDROID,
        captured_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        apps=(
            LaunchableApp(
                "com.google.android.youtube",
                ".app.honeycomb.Shell$HomeActivity",
                "YouTube",
            ),
            LaunchableApp("com.android.settings", ".Settings", "Settings"),
        ),
    )


def test_start_app_choice_contains_only_catalog_apps_and_no_match() -> None:
    catalog = make_catalog()

    question = build_start_app_choice(catalog)

    assert set(question.criteria) == {
        "com.google.android.youtube",
        "com.android.settings",
        NO_APP_MATCH,
    }
    assert question.criteria["com.google.android.youtube"] == {
        "name": "YouTube",
        "app_id": "com.google.android.youtube",
        "entry": ".app.honeycomb.Shell$HomeActivity",
    }


def test_app_choice_resolves_only_confident_catalog_match() -> None:
    catalog = make_catalog()

    selected = select_app_reference(
        catalog, "com.google.android.youtube", confidence=0.9
    )
    low_confidence = select_app_reference(
        catalog,
        "com.google.android.youtube",
        confidence=0.4,
        minimum_confidence=0.65,
    )
    zero_confidence = select_app_reference(
        catalog, "com.google.android.youtube", confidence=0.0
    )
    no_match = select_app_reference(catalog, NO_APP_MATCH, confidence=0.99)

    assert selected == catalog.reference("com.google.android.youtube")
    assert low_confidence is None
    assert zero_confidence == catalog.reference("com.google.android.youtube")
    assert no_match is None


def test_app_choice_rejects_value_outside_current_catalog() -> None:
    with pytest.raises(AppNotFoundError):
        select_app_reference(make_catalog(), "com.example.missing", confidence=1.0)
