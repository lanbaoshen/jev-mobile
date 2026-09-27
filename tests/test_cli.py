from __future__ import annotations

import pytest

from jev_mobile import HandoffRequest, LaunchableApp
from jev_mobile.agent import AgentLoopConfig
from jev_mobile.cli import (
    _build_config,
    _build_tools,
    _handle_handoff,
    build_parser,
    main,
)


def test_cli_parses_harmonyos_run_configuration() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "Open Settings and inspect Battery",
            "--platform",
            "harmonyos",
            "--device",
            "127.0.0.1:5557",
            "--app",
            "com.huawei.hmos.settings/com.huawei.hmos.settings.MainAbility=Settings",
            "--action-confidence",
            "0.2",
            "--argument-confidence",
            "0.5",
            "--finish-confidence",
            "0.95",
            "--no-handoff",
        ]
    )

    assert args.goal == "Open Settings and inspect Battery"
    assert args.device == "127.0.0.1:5557"
    assert args.app == [
        LaunchableApp(
            "com.huawei.hmos.settings",
            "com.huawei.hmos.settings.MainAbility",
            "Settings",
        )
    ]
    assert args.action_confidence == 0.2
    assert args.argument_confidence == 0.5
    assert args.finish_confidence == 0.95
    assert args.no_handoff is True

    config = _build_config(args)
    assert config.action_confidence == 0.2
    assert config.argument_confidence == 0.5
    assert config.finish_confidence == 0.95


def test_cli_confidence_defaults_match_agent_config() -> None:
    args = build_parser().parse_args(
        ["run", "Inspect Battery", "--platform", "android"]
    )
    config = AgentLoopConfig()

    assert args.action_confidence == config.action_confidence == 0.0
    assert args.argument_confidence == config.argument_confidence == 0.0
    assert args.finish_confidence == config.finish_confidence == 0.0


def test_cli_prints_handoff_reason_and_instruction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt))

    _handle_handoff(
        HandoffRequest(
            instruction="Enter the required text on the device.",
            reason="Jev cannot generate text.",
        )
    )

    output = capsys.readouterr().out
    assert "HANDOFF  Jev cannot generate text." in output
    assert "ACTION   Enter the required text on the device." in output
    assert prompts == ["Press Enter after completing the action on the device: "]


def test_cli_builds_harmonyos_app_allowlist() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "Open Settings",
            "--platform",
            "harmonyos",
            "--app",
            "com.example.settings/SettingsAbility=Settings",
        ]
    )

    catalog = _build_tools(args).available_apps()

    assert catalog.apps == (
        LaunchableApp("com.example.settings", "SettingsAbility", "Settings"),
    )


def test_cli_rejects_invalid_probability() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(
            [
                "run",
                "Inspect Battery",
                "--platform",
                "harmonyos",
                "--finish-confidence",
                "1.1",
            ]
        )

    assert error.value.code == 2


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_cli_reports_missing_typesafe_key_before_device_access(
    api_key: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if api_key is None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TYPESAFE_API_KEY", api_key)
    monkeypatch.setattr(
        "jev_mobile.cli._build_tools",
        lambda args: pytest.fail("device tools must not be created without auth"),
    )

    exit_code = main(
        ["run", "Inspect Battery", "--platform", "harmonyos", "--no-handoff"]
    )

    error = capsys.readouterr().err
    assert exit_code == 2
    assert "TypeSafe authentication is not configured" in error
    assert "export TYPESAFE_API_KEY='<your TypeSafe API key>'" in error
    assert "https://console.typesafe.ai/" in error
