from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from json import JSONDecodeError
from typing import Any

from .actions import HandoffHandler, UITools
from .apps import MAX_SELECTABLE_APPS, LaunchableApp, is_valid_app_id
from .dumpers import HarmonyOSHierarchyDumper
from .exceptions import DeviceActionError, DeviceActionTimeoutError, HierarchyDumpError
from .models import CurrentApp, Platform, UISnapshot
from .transport import CommandResult, CommandRunner, run_command

_ABILITY_RECORD = re.compile(
    r"^\s*AbilityRecord ID\s+#\d+.*?(?=^\s*AbilityRecord ID\s+#\d+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_FOREGROUND_ABILITY_STATE = re.compile(
    r"^\s*(?:AbilityRecord ID\s+#\d+\s+)?state\s+#FOREGROUND\b",
    re.MULTILINE,
)

LaunchableAppProvider = Callable[[], Sequence[LaunchableApp]]
CurrentAppProvider = Callable[[], CurrentApp]


@dataclass(slots=True)
class HarmonyOSDeviceAdapter:
    hdc_path: str = "hdc"
    connect_key: str | None = None
    timeout_seconds: float = 15.0
    runner: CommandRunner = field(default=run_command, repr=False)
    launchable_app_provider: LaunchableAppProvider | None = field(
        default=None, repr=False
    )
    current_app_provider: CurrentAppProvider | None = field(default=None, repr=False)
    platform: Platform = field(default=Platform.HARMONYOS, init=False)
    requires_app_entry: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not self.hdc_path.strip():
            raise ValueError("hdc_path must not be empty")
        if self.connect_key is not None and not self.connect_key.strip():
            raise ValueError("connect_key must not be empty")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120")

    def observe(self) -> UISnapshot:
        dumper = HarmonyOSHierarchyDumper(
            hdc_path=self.hdc_path,
            connect_key=self.connect_key,
            timeout_seconds=self.timeout_seconds,
            runner=self.runner,
        )
        current_app = self.current_app_provider or self.current_app
        for _ in range(2):
            before = current_app()
            snapshot = dumper.dump()
            after = current_app()
            if before == after:
                return replace(snapshot, current_app=after)
        raise HierarchyDumpError("foreground app changed while observing the UI")

    def current_app(self) -> CurrentApp:
        result = self._run_shell("get current app", "aa", "dump", "-a")
        return _parse_current_app(result.stdout)

    def list_launchable_apps(self) -> tuple[LaunchableApp, ...]:
        if self.launchable_app_provider is not None:
            apps = tuple(self.launchable_app_provider())
        else:
            labels = _parse_installed_app_labels(
                self._run_shell(
                    "list installed apps", "bm", "dump", "-a", "-l"
                ).stdout
            )
            discovered: list[LaunchableApp] = []
            for app_id, label in labels:
                app = _parse_launchable_app(
                    self._run_shell(
                        "inspect installed app", "bm", "dump", "-n", app_id
                    ).stdout,
                    app_id=app_id,
                    label=label,
                )
                if app is not None:
                    discovered.append(app)
            apps = tuple(discovered)
        if any(app.entry is None for app in apps):
            raise DeviceActionError(
                "HarmonyOS launchable apps must include an ability name"
            )
        if len(apps) > MAX_SELECTABLE_APPS:
            raise DeviceActionError(
                "HarmonyOS launchable app catalog exceeds the Choice limit; "
                "configure an explicit app allowlist"
            )
        return apps

    def start_app(self, app_id: str, entry: str | None) -> None:
        if entry is None:
            raise DeviceActionError("start app requires an ability name")
        self._execute_shell(
            "start app",
            "aa",
            "start",
            "-b",
            app_id,
            "-a",
            entry,
        )

    def click(self, x: int, y: int) -> None:
        self._execute("click", "click", str(x), str(y))

    def long_click(self, x: int, y: int, duration_ms: int) -> None:
        self._execute("long click", "longClick", str(x), str(y))

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> None:
        distance = max(abs(end_x - start_x), abs(end_y - start_y))
        velocity = max(200, min(40_000, round(distance * 1_000 / duration_ms)))
        self._execute(
            "swipe",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(velocity),
        )

    def type_text(self, text: str) -> None:
        self._execute("type text", "text", shlex.quote(text))

    def _execute(self, operation: str, *arguments: str) -> None:
        self._execute_shell(operation, "uitest", "uiInput", *arguments)

    def _execute_shell(self, operation: str, *arguments: str) -> None:
        self._run_shell(operation, *arguments)

    def _run_shell(self, operation: str, *arguments: str) -> CommandResult:
        command = [*self._hdc_prefix(), "shell", *arguments]
        try:
            result = self.runner(command, self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise DeviceActionTimeoutError(f"{operation} timed out") from error
        except (FileNotFoundError, OSError) as error:
            raise DeviceActionError(f"failed to start {operation}") from error
        if result.returncode != 0:
            raise DeviceActionError(
                f"{operation} failed with exit code {result.returncode}"
            )
        return result

    def _hdc_prefix(self) -> list[str]:
        prefix = [self.hdc_path]
        if self.connect_key is not None:
            prefix.extend(("-t", self.connect_key))
        return prefix


class HarmonyOSUITools(UITools):
    def __init__(
        self,
        *,
        hdc_path: str = "hdc",
        connect_key: str | None = None,
        timeout_seconds: float = 15.0,
        runner: CommandRunner = run_command,
        handoff_handler: HandoffHandler | None = None,
        launchable_app_provider: LaunchableAppProvider | None = None,
        current_app_provider: CurrentAppProvider | None = None,
    ) -> None:
        super().__init__(
            HarmonyOSDeviceAdapter(
                hdc_path=hdc_path,
                connect_key=connect_key,
                timeout_seconds=timeout_seconds,
                runner=runner,
                launchable_app_provider=launchable_app_provider,
                current_app_provider=current_app_provider,
            ),
            handoff_handler=handoff_handler,
        )


def _parse_current_app(output: str) -> CurrentApp:
    for match in _ABILITY_RECORD.finditer(output):
        record = match.group(0)
        if _FOREGROUND_ABILITY_STATE.search(record) is None:
            continue
        ability_type = _record_field(record, "ability type")
        if ability_type not in {"PAGE", "UIEXTENSION"}:
            continue
        bundle_name = _record_field(record, "bundle name")
        ability_name = _record_field(record, "main name")
        if bundle_name and ability_name:
            return CurrentApp(bundle_name, ability_name)
    raise DeviceActionError("could not determine the current HarmonyOS app")


def _record_field(record: str, name: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(name)}\s+\[([^\]]+)\]",
        record,
        re.MULTILINE,
    )
    return match.group(1).strip() if match is not None else None


def _parse_installed_app_labels(output: str) -> tuple[tuple[str, str | None], ...]:
    payload = _parse_bundle_json(output, "installed app labels")
    if not isinstance(payload, list):
        raise DeviceActionError("installed app labels are not a JSON array")

    labels: dict[str, str | None] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            raise DeviceActionError("installed app label entry is malformed")
        app_id = item.get("bundleName")
        if not is_valid_app_id(app_id):
            raise DeviceActionError("installed app label has an invalid bundle name")
        label = item.get("label")
        labels[app_id] = (
            label.strip()
            if isinstance(label, str) and label.strip() and len(label.strip()) <= 200
            else None
        )
    return tuple(sorted(labels.items()))


def _parse_launchable_app(
    output: str,
    *,
    app_id: str,
    label: str | None,
) -> LaunchableApp | None:
    payload = _parse_bundle_json(output, f"bundle metadata for {app_id}")
    if not isinstance(payload, Mapping) or payload.get("name") != app_id:
        raise DeviceActionError(f"bundle metadata does not match {app_id}")

    entry_module = payload.get("entryModuleName") or payload.get("mainEntry")
    modules = payload.get("hapModuleInfos")
    if not isinstance(modules, list):
        raise DeviceActionError(f"bundle metadata has no modules: {app_id}")

    candidates: list[str] = []
    preferred: list[str] = []
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        module_name = module.get("moduleName") or module.get("name")
        if entry_module and module_name != entry_module:
            continue
        main_element = module.get("mainElementName")
        abilities = module.get("abilityInfos")
        if not isinstance(abilities, list):
            continue
        for ability in abilities:
            if not isinstance(ability, Mapping) or not _is_launcher_ability(ability):
                continue
            name = ability.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            candidates.append(name)
            if name == main_element:
                preferred.append(name)

    selected = preferred[0] if len(preferred) == 1 else None
    if selected is None and len(candidates) == 1:
        selected = candidates[0]
    if selected is None:
        return None
    try:
        return LaunchableApp(app_id, selected, label)
    except ValueError as error:
        raise DeviceActionError(f"bundle has an invalid launcher: {app_id}") from error


def _is_launcher_ability(ability: Mapping[str, Any]) -> bool:
    if ability.get("type") != 1 or ability.get("enabled", True) is not True:
        return False
    skills = ability.get("skills")
    if not isinstance(skills, list):
        return False
    for skill in skills:
        if not isinstance(skill, Mapping):
            continue
        actions = skill.get("actions")
        entities = skill.get("entities")
        if (
            isinstance(actions, list)
            and "action.system.home" in actions
            and isinstance(entities, list)
            and "entity.system.home" in entities
        ):
            return True
    return False


def _parse_bundle_json(output: str, description: str) -> object:
    stripped = output.strip()
    if not stripped:
        raise DeviceActionError(f"{description} output is empty")
    if stripped[0] not in "[{":
        _, separator, stripped = stripped.partition("\n")
        stripped = stripped.lstrip()
        if not separator or not stripped or stripped[0] not in "[{":
            raise DeviceActionError(f"{description} output has no JSON payload")
    try:
        return json.loads(stripped)
    except JSONDecodeError as error:
        raise DeviceActionError(f"{description} output is invalid JSON") from error
