# jev-mobile v1.0.0

Released September 29, 2026.

The first stable release of `jev-mobile`: a bounded, inspectable natural-language agent for controlling Android and HarmonyOS devices with TypeSafe/Jev.

## Highlights

- Android support through ADB and HarmonyOS support through HDC.
- A shared UI model for actionable elements and visible text.
- Typed actions for launching apps, clicking, long-clicking, swiping, typing, waiting, handing off, and finishing.
- Revisioned snapshots that prevent actions on stale elements.
- Configurable confidence thresholds, retries, step limits, and human handoff.
- No unrestricted shell access exposed to the model.
- Unit tests that require neither a physical device nor live TypeSafe requests.

## Quick Start

Requires Python 3.10+, `uv`, a TypeSafe API key, and a device available through `adb` or `hdc`.

```sh
export TYPESAFE_API_KEY='<your TypeSafe API key>'

uvx jev-mobile run \
  "Open Settings and show battery information. Finish only when the battery details are visible." \
  --platform android \
  --device emulator-5554
```

Run `uvx jev-mobile run --help` for all options.

## Notes

- Include an observable completion condition in each task for reliable results.
- Jev does not generate free-form text. When required text is unavailable, the CLI requests human handoff.
