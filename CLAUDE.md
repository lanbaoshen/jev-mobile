# jev-mobile Guidelines

## Purpose

`jev-mobile` is a Python 3.10+ package for operating Android devices through ADB and HarmonyOS devices through HDC. It converts UI hierarchies into platform-neutral elements, lets Jev select bounded typed actions, and executes them through device tools.

Keep reusable code under `src/jev_mobile`; do not embed assumptions about one app, device, locale, or account.

## Architecture

- Keep Android/ADB and HarmonyOS/HDC details behind platform adapters.
- Parse hierarchy formats structurally and normalize actionable nodes into shared typed elements.
- Let Python own the control loop, retries, stopping rules, and side effects. Jev only returns typed judgments over explicit candidates.
- Validate tool arguments and resolve targets against the latest UI snapshot before dispatching to an adapter.

## UI and Jev Rules

- Treat each hierarchy capture as an immutable, revisioned snapshot. Invalidate it after any action that may change the UI.
- Never resolve an element reference from a stale snapshot. Prefer semantic actions; use checked screen coordinates only as a fallback.
- Follow the live TypeSafe documentation and `.agents/skills/typesafe-ai/SKILL.md`.
- Use `Choice` for closed selections, `Noul` for yes/no probabilities, and `Score` for ordered degrees.
- Send only relevant structured state, ask narrow questions, include a no-match/no-op outcome, and combine results in Python.
- Use explicit confidence thresholds. Ambiguous or low-confidence decisions must re-observe, retry within a fixed bound, hand off, or stop.

## Tools and Safety

- Keep tools small, typed, and capability-based. Never expose unrestricted shell execution.
- Validate device connection, snapshot freshness, target capabilities, argument ranges, timeouts, and retry limits.
- Return structured failures for stale elements, unsupported actions, invalid arguments, transport errors, and timeouts.
- For destructive or externally consequential actions, call a human-handoff tool. The human performs the action; resume only after the tool reports completion. Never bypass this flow.
- Never log API keys, device credentials, tokens, or sensitive UI content.

## Python and Tests

- Type public APIs and domain models, keep the exported API small, avoid import-time side effects, and raise package-specific exceptions.
- Unit tests must not require a physical device or live TypeSafe API.
- Use sanitized Android and HarmonyOS hierarchy fixtures, fake adapters, and mocked Jev responses.
- Cover malformed hierarchies, ambiguous or missing labels, stale elements, low confidence, no-match, retries, and service failures.
- Keep real-device and live-API tests explicit and opt-in.

## uv Workflow

- Use `uv` exclusively: `uv sync`, `uv add`, `uv add --dev`, and `uv run`. Never edit `uv.lock` manually.
- Keep build metadata in `pyproject.toml` and maintain support for every declared Python version.
- Before release, run configured checks and tests, run `uv build`, and verify the wheel and source distribution in a clean environment.
