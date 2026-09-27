from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from typesafe_sdk import Choice

from .exceptions import AppNotFoundError, StaleAppCatalogError
from .models import Platform

NO_APP_MATCH = "__no_match__"
MAX_SELECTABLE_APPS = 254

_APP_ID_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
)
_APP_ENTRY_PATTERN = re.compile(
    r"\.?[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*"
)


def is_valid_app_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 255
        and _APP_ID_PATTERN.fullmatch(value) is not None
    )


def is_valid_app_entry(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 255
        and _APP_ENTRY_PATTERN.fullmatch(value) is not None
    )


@dataclass(frozen=True, slots=True)
class LaunchableApp:
    app_id: str
    entry: str | None
    label: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_app_id(self.app_id):
            raise ValueError("app_id must be a valid package or bundle identifier")
        if self.entry is not None and not is_valid_app_entry(self.entry):
            raise ValueError("entry must be a valid activity or ability name")
        if self.label is not None and (not self.label.strip() or len(self.label) > 200):
            raise ValueError("label must be non-empty and at most 200 characters")


@dataclass(frozen=True, slots=True)
class AppRef:
    catalog_revision: str
    app_id: str


@dataclass(frozen=True, slots=True)
class AppCatalog:
    revision: str
    platform: Platform
    captured_at: datetime
    apps: tuple[LaunchableApp, ...]

    def __post_init__(self) -> None:
        if not self.revision:
            raise ValueError("app catalog revision must not be empty")
        if len(self.apps) > MAX_SELECTABLE_APPS:
            raise ValueError(
                f"app catalog exceeds the {MAX_SELECTABLE_APPS}-app Choice limit"
            )
        app_ids = {app.app_id for app in self.apps}
        if len(app_ids) != len(self.apps):
            raise ValueError("app catalog app ids must be unique")

    def reference(self, app_id: str) -> AppRef:
        if not any(app.app_id == app_id for app in self.apps):
            raise AppNotFoundError(f"app is not launchable: {app_id}")
        return AppRef(catalog_revision=self.revision, app_id=app_id)

    def resolve(self, reference: AppRef) -> LaunchableApp:
        if reference.catalog_revision != self.revision:
            raise StaleAppCatalogError(
                f"app reference is from revision {reference.catalog_revision!r}, "
                f"current revision is {self.revision!r}"
            )
        for app in self.apps:
            if app.app_id == reference.app_id:
                return app
        raise AppNotFoundError(f"app is not launchable: {reference.app_id}")


def build_start_app_choice(catalog: AppCatalog) -> Choice:
    criteria: dict[str, object] = {
        app.app_id: {
            "name": app.label or app.app_id,
            "app_id": app.app_id,
            "entry": app.entry,
        }
        for app in catalog.apps
    }
    criteria[NO_APP_MATCH] = {
        "when": "No available app matches the user's requested app"
    }
    return Choice(
        instructions={
            "question": "Which currently available app should be opened?",
            "constraint": "Choose only an app that matches the user's request; otherwise choose no match.",
        },
        criteria=criteria,
    )


def select_app_reference(
    catalog: AppCatalog,
    choice: str,
    confidence: float,
    *,
    minimum_confidence: float = 0.0,
) -> AppRef | None:
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if not 0 <= minimum_confidence <= 1:
        raise ValueError("minimum_confidence must be between 0 and 1")
    if choice == NO_APP_MATCH or confidence < minimum_confidence:
        return None
    return catalog.reference(choice)
