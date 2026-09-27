from collections.abc import Sequence

from jev_mobile import (
    CurrentApp,
    HandoffResponse,
    HarmonyOSDeviceAdapter,
    HarmonyOSUITools,
    LaunchableApp,
)
from jev_mobile.exceptions import DeviceActionError
from jev_mobile.harmonyos import _parse_current_app, _parse_launchable_app
from jev_mobile.transport import CommandResult
import pytest


def test_harmonyos_adapter_emits_bounded_hdc_commands() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], timeout_seconds: float) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0)

    adapter = HarmonyOSDeviceAdapter(connect_key="device-1", runner=runner)
    adapter.click(10, 20)
    adapter.long_click(10, 20, 800)
    adapter.swipe(10, 100, 10, 20, 400)
    adapter.type_text("hello world")

    assert commands == [
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "uitest",
            "uiInput",
            "click",
            "10",
            "20",
        ),
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "uitest",
            "uiInput",
            "longClick",
            "10",
            "20",
        ),
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "uitest",
            "uiInput",
            "swipe",
            "10",
            "100",
            "10",
            "20",
            "200",
        ),
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "uitest",
            "uiInput",
            "text",
            "'hello world'",
        ),
    ]


def test_harmonyos_tools_accept_user_handoff_handler() -> None:
    tools = HarmonyOSUITools(
        handoff_handler=lambda request: HandoffResponse()
    )

    result = tools.handoff("Complete confirmation", reason="User action required")

    assert result.success is True


def test_harmonyos_adapter_starts_explicit_ability() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], timeout_seconds: float) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0)

    adapter = HarmonyOSDeviceAdapter(connect_key="device-1", runner=runner)

    adapter.start_app("com.example.app", "EntryAbility")

    assert commands == [
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "aa",
            "start",
            "-b",
            "com.example.app",
            "-a",
            "EntryAbility",
        )
    ]


def test_harmonyos_tools_build_catalog_from_explicit_provider() -> None:
    app = LaunchableApp("com.example.app", "EntryAbility", "Example")
    tools = HarmonyOSUITools(launchable_app_provider=lambda: [app])

    catalog = tools.available_apps()

    assert catalog.apps == (app,)
    assert catalog.reference(app.app_id).app_id == app.app_id


def test_harmonyos_adapter_discovers_installed_launchable_apps() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], timeout_seconds: float) -> CommandResult:
        commands.append(tuple(command))
        app_id = command[-1]
        if tuple(command[-3:]) == ("dump", "-a", "-l"):
            return CommandResult(
                0,
                '[{"bundleName":"com.example.settings","label":"Settings"},'
                '{"bundleName":"com.example.service","label":"Service"}]',
            )
        if app_id == "com.example.settings":
            return CommandResult(
                0,
                "com.example.settings:\n" + _bundle_metadata(
                    "com.example.settings", "SettingsAbility"
                ),
            )
        return CommandResult(
            0,
            "com.example.service:\n" + _bundle_metadata(
                "com.example.service", "ServiceAbility", launcher=False
            ),
        )

    apps = HarmonyOSDeviceAdapter(
        connect_key="device-1", runner=runner
    ).list_launchable_apps()

    assert apps == (
        LaunchableApp("com.example.settings", "SettingsAbility", "Settings"),
    )
    assert commands == [
        ("hdc", "-t", "device-1", "shell", "bm", "dump", "-a", "-l"),
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "bm",
            "dump",
            "-n",
            "com.example.service",
        ),
        (
            "hdc",
            "-t",
            "device-1",
            "shell",
            "bm",
            "dump",
            "-n",
            "com.example.settings",
        ),
    ]


def test_harmonyos_launcher_parser_prefers_module_main_element() -> None:
    output = _bundle_metadata(
        "com.example.contacts",
        "MainAbility",
        additional_launcher="EntryAbility",
    )

    app = _parse_launchable_app(
        output,
        app_id="com.example.contacts",
        label="Contacts",
    )

    assert app == LaunchableApp("com.example.contacts", "MainAbility", "Contacts")


def test_harmonyos_launcher_parser_skips_ambiguous_bundle() -> None:
    output = _bundle_metadata(
        "com.example.tools",
        "MissingMainAbility",
        launcher_name="FirstAbility",
        additional_launcher="SecondAbility",
    )

    assert _parse_launchable_app(
        output,
        app_id="com.example.tools",
        label="Tools",
    ) is None


def test_harmonyos_adapter_accepts_current_app_provider() -> None:
    app = CurrentApp("com.example.app", "EntryAbility", "Example")
    adapter = HarmonyOSDeviceAdapter(current_app_provider=lambda: app)

    assert adapter.current_app_provider is not None
    assert adapter.current_app_provider() == app


def test_harmonyos_adapter_discovers_foreground_page() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], timeout_seconds: float) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(
            0,
            """\
      AbilityRecord ID #47
        main name [com.huawei.hmos.settings.MainAbility]
        bundle name [com.huawei.hmos.settings]
        ability type [PAGE]
        state #FOREGROUND  start time [1727295]
""",
        )

    app = HarmonyOSDeviceAdapter(connect_key="device-1", runner=runner).current_app()

    assert app == CurrentApp(
        "com.huawei.hmos.settings",
        "com.huawei.hmos.settings.MainAbility",
    )
    assert commands == [("hdc", "-t", "device-1", "shell", "aa", "dump", "-a")]


def test_harmonyos_foreground_parser_ignores_services_and_background_pages() -> None:
    output = """\
      AbilityRecord ID #1
        main name [BackgroundAbility]
        bundle name [com.example.background]
        ability type [PAGE]
        state #BACKGROUND
      AbilityRecord ID #2
        main name [ServiceAbility]
        bundle name [com.example.service]
        ability type [SERVICE]
        state #FOREGROUND
      AbilityRecord ID #3
        main name [PanelAbility]
        bundle name [com.example.systemui]
        ability type [UIEXTENSION]
        state #FOREGROUND
"""

    assert _parse_current_app(output) == CurrentApp(
        "com.example.systemui", "PanelAbility"
    )


def test_harmonyos_foreground_parser_accepts_state_on_record_header() -> None:
    output = """\
      AbilityRecord ID #24   state #FOREGROUND   start time [31767]
        main name [EngineServiceAbility]
        bundle name [com.ohos.sceneboard]
        ability type [UIEXTENSION]
        app state #FOREGROUND
"""

    assert _parse_current_app(output) == CurrentApp(
        "com.ohos.sceneboard", "EngineServiceAbility"
    )


def test_harmonyos_foreground_parser_rejects_missing_ui_app() -> None:
    with pytest.raises(DeviceActionError, match="current HarmonyOS app"):
        _parse_current_app("")


def _bundle_metadata(
    app_id: str,
    main_element: str,
    *,
    launcher_name: str | None = None,
    additional_launcher: str | None = None,
    launcher: bool = True,
) -> str:
    import json

    def ability(name: str, *, is_launcher: bool) -> dict[str, object]:
        return {
            "name": name,
            "type": 1,
            "enabled": True,
            "skills": (
                [
                    {
                        "actions": ["action.system.home"],
                        "entities": ["entity.system.home"],
                    }
                ]
                if is_launcher
                else []
            ),
        }

    abilities = [ability(launcher_name or main_element, is_launcher=launcher)]
    if additional_launcher is not None:
        abilities.append(ability(additional_launcher, is_launcher=True))
    return json.dumps(
        {
            "name": app_id,
            "entryModuleName": "entry",
            "hapModuleInfos": [
                {
                    "moduleName": "entry",
                    "mainElementName": main_element,
                    "abilityInfos": abilities,
                }
            ],
        }
    )
