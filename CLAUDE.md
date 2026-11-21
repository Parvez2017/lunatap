# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Lunatap is an iOS-only fork of google/artemis: multimodal LLM agents that drive iOS simulators and iPhones from natural-language tasks. It is exposed as a CLI, a web console, an MCP server for AI IDEs, and a Python SDK. Python 3.12+, managed with `uv`. See `docs/ios.md` and the roadmap in `README.md`.

**Fork status.** iOS is the only platform; Android support was removed in Phase 1. Don't reintroduce platform branches: `normalize_device_platform` rejects anything but `ios`. Environment variables: document and use `LUNATAP_*`. Code still reads the inherited `ARTEMIS_*` names, and `lunatap/config/env_aliases.py` copies each `LUNATAP_X` onto `ARTEMIS_X` at import (`LUNATAP_LLM_*` are new-only and read directly). `config/artemis.jsonc`, `~/.artemis`, the MCP server key `artemis` and the `artemis-client` package keep their old names until the planned rename. The `upstream` remote is google/artemis: cherry-pick its platform-neutral agent improvements, rewriting `artemis/` paths to `lunatap/`.

## Commands

```bash
make install            # uv sync --dev
make test               # hermetic suite: tests/unit, tests/tools, packages/artemis-client/tests
make lint               # ruff format --check + ruff check + scripts/quality_ratchet.py
make format             # ruff format + ruff check --fix
make typecheck          # pyright --project pyright-core.json
make doctor             # system/device/toolchain diagnostics

uv run pytest tests/unit/drivers/test_ios_overlays.py -q            # one file
uv run pytest tests/unit/drivers/test_ios_overlays.py::test_name    # one test
```

- Many tests under `tests/unit/agents` build a real `ChatGoogleGenerativeAI` and fail without a key. Run the suite as `GOOGLE_API_KEY=dummy-for-tests uv run pytest`.
- pytest `addopts` exclude the `integration`, `e2e`, `cloud` and `manual` markers by default. Use `make test-integration`, `make test-device` or `make test-all` for those. Any test that needs external state must carry one of these markers and must still be collectable when that state is absent. `--strict-markers` is on.
- `scripts/quality_ratchet.py` fails CI when the count of broad `except` handlers or `type: ignore` comments goes above `.quality-baseline.json`. Don't add either casually. Lower the baseline when you remove some.
- Run a task: `uv run lunatap run "<task>" --profile flash|pro [--platform ios --standalone --device-serial <UDID>]`.
- Web console: `uv run lunatap ui` (port 8000). The Angular frontend is in `apps/showcase_ui` (`make build-ui`); the FastAPI backend is in `apps/admin_console`.

## Configuration

- Model and agent config is `config/artemis.jsonc` (JSONC). Pydantic validates it in `lunatap/config/agent.py` and `lunatap/config/llm.py`.
- Any config file can be overridden with a `LUNATAP_<FILENAME_UPPER>` env var. For example, `LUNATAP_ARTEMIS_JSONC=/path/alt.jsonc` swaps in a different config file; `LUNATAP_LLM_PRESET=openai` switches every agent to OpenAI (presets live in that file) (see `lunatap/config/paths.py:get_config_path`).
- Runtime settings and credentials come from `.env` (`lunatap/config/settings.py`). Gemini is the default provider. OpenAI and OpenAI-compatible servers work for every agent via presets (`lunatap/config/llm.py`); settings that name a single model resolve through `get_llm_for_model` in `lunatap/services/llm.py`. Don't add direct Gemini calls (`get_google_llm`, `genai.Client`) without a non-Google path.

## Architecture

The layers, from the top:

- **Interfaces.** `lunatap/interfaces/cli` (Typer, entry point `lunatap`), `lunatap/interfaces/sdk`, `mcp_server/` (the MCP server for IDEs, `python -m mcp_server`; `mcp_server/rules.md` is the agent-facing usage guide), `apps/admin_console` (web API), and `packages/artemis-client` (a zero-dependency remote client).
- **Agents.** There are two execution profiles:
  - **Flash** (`lunatap/agents/flash/runner.py`): one model in an observe→act loop, with no graph.
  - **Pro** (`lunatap/graph/graph.py`): a LangGraph multi-agent graph. The Planner keeps a Markdown plan; the Operator acts; a pre-execution Safety Net checks each action against the UI tree and pixels; the Checker verifies checkpoints.
  - Agent prompts are `.md` files next to each agent in `lunatap/agents/*`.
  - Both profiles share compressed history, in which screenshots are folded into summaries (`lunatap/memory`, `lunatap/agents/summarizer`).
- **Actions / MCP tools** (`lunatap/mcp/`). The `action_*` modules define the tools agents call. `lunatap/mcp/actuators/` maps those calls to a backend in a **0–1000 normalized coordinate space**; pixel conversion stays private to the actuator. `device.py` (`DeviceActuator`) is the controller-backed base; `ios.py` (`IosActuator`) is the default for real devices and `mock.py` serves tests. Agents choose elements **by index** from `ui_elements`; each element's `hit_point` is the pixel the tap lands on.
- **Drivers** (`lunatap/drivers/`). Drivers implement `BaseDeviceDriver` (`base.py`): `get_screen_data()` returns a `ScreenData` (screenshot plus flattened `ui_elements`), and the driver supplies tap, swipe, input, app lifecycle and recording. `factory.py` picks the driver class (iOS, or `mock` in tests).
  - **iOS:** `ios/`, chosen by `ios_driver_class()`:
    - `PhysicalIosDriver` for paired devices: `devicectl`, plus a WebDriverAgent (WDA) runner over the CoreDevice tunnel.
    - `XcodeSimulatorDriver` for simulators on Xcode 27+, using Xcode's `mcpbridge` tools.
    - `WdaSimulatorDriver` for simulators on Xcode 15–26, using WDA plus `simctl`.
    - `LUNATAP_IOS_SIM_BACKEND=auto|xcode|wda` picks the simulator backend.
    - The shared WDA client and the tree→element parser are in `ios/wda.py`. The parser drops elements that are hidden or covered by floating bars (Toolbar, TabBar, NavigationBar, Keyboard).
    - Prebuilt WDA lives under `~/.artemis/wda/{simulator,device}`; build it with `scripts/setup_ios_wda_simulator.sh` and `scripts/setup_ios_env.sh`.
- **Runtime** (`lunatap/runtime/`): the daemon/server lifecycle, the iOS device pool and device locks, and the trace store. Traces and recordings go to `traces/`, which is gitignored.

## Constraints

- `third_party/mobile_use/` is vendored Apache-2.0 code. `lunatap` and `third_party` may import each other only through **public** names; `tests/unit/test_third_party_boundary.py` enforces this. Many modules import the logger from `third_party.mobile_use.utils.logger`.
- Every source file starts with the Apache 2.0 "Copyright 2026 Google LLC" header.
- Every file changed from upstream carries the line "Modified by the Lunatap Authors in 2026 (forked from google/artemis)." under its licence header (Apache 2.0 §4(b)); add it to any upstream file you change. Keep `NOTICE` and `third_party/mobile_use/NOTICE`.
