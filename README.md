# Lunatap

**An AI agent that tests iOS apps.** Describe a task in plain language. Lunatap drives an iOS simulator or a real iPhone, step by step, and reports what happened.

```bash
uv run lunatap run "Open Settings, go to General > About, and report the iOS version" \
  --platform ios --standalone --device-serial <SIMULATOR-OR-DEVICE-UDID> --profile flash
```

Lunatap is a fork of [Artemis](https://github.com/google/artemis) by Google, narrowed to iOS. See [NOTICE](NOTICE) for attribution. It is an independent project, not affiliated with or endorsed by Google.

> **Status: early (0.1.0).** iOS only: the Android support inherited from Artemis has been removed. Environment variables use the `LUNATAP_` prefix; the old `ARTEMIS_` names still work. The config file is still `config/artemis.jsonc`. See [Roadmap](#roadmap).

## What it does

- **Simulators on Xcode 15 and later.** On Xcode 27 it uses Apple's own device tools; on Xcode 15–26 it uses WebDriverAgent.
- **Real iPhones and iPads** over the CoreDevice tunnel, using a WebDriverAgent runner signed with your Apple team (a free personal team works).
- **Two ways to run:**
  - **Flash** is a fast observe→act loop, a few seconds per step.
  - **Pro** plans the task, checks each action before it runs, and verifies checkpoints.
- **Taps what is actually visible.** Elements hidden behind iOS 26 floating bars or scrolled out of view are not offered to the agent, and swipes avoid the system edges.
- **Screen recording and replay** of every run, on simulators and devices.
- **Interfaces:**
  - a command line (`lunatap`);
  - an MCP server for Claude Code, Cursor and other AI IDEs;
  - a Python SDK;
  - a web console.

## Requirements

- macOS with Xcode 15 or later.
- Python 3.12 or later and [uv](https://docs.astral.sh/uv/).
- A model: Gemini by default, or OpenAI and any OpenAI-compatible server. See [Models](#models). Anthropic models also work.

## Quick start

```bash
git clone https://github.com/parvej-flight/lunatap.git && cd lunatap
uv sync --dev
bash scripts/setup_ios_env.sh                 # checks Xcode, simulators and devices
bash scripts/setup_ios_wda_simulator.sh       # Xcode 15–26 only: builds WebDriverAgent once
cp .env.example .env                          # add your model API key
uv run lunatap run "Open Settings and inspect the General screen" \
  --platform ios --standalone --device-serial <SIMULATOR-UDID> --profile flash
```

For real devices, WebDriverAgent signing, the simulator backends and troubleshooting, see the [iOS guide](docs/ios.md).

## Models

Gemini is the default. One line in `.env` switches every agent, including the background summarizers, to another provider:

| Setup | `.env` |
|---|---|
| OpenAI | `OPENAI_API_KEY=sk-…` and `LUNATAP_LLM_PRESET=openai` (gpt-5.4-mini, gpt-5.4-nano as fallback) |
| Any OpenAI-compatible server (OpenRouter, LM Studio, vLLM, LiteLLM, …) | `LUNATAP_LLM_PRESET=openai-compatible`, `OPENAI_BASE_URL=…`, `LUNATAP_LLM_MODEL=…`, and `OPENAI_API_KEY` if the server needs one |
| Ollama on this machine | `LUNATAP_LLM_PRESET=local-ollama` (a vision model with tool calling, such as `qwen3-vl`) |

`LUNATAP_LLM_MODEL` and `LUNATAP_LLM_FALLBACK_MODEL` override any preset's model. You don't need a Google key: nodes that the stock config pins to Gemini fall back to your preset's model. Settings that name a single model, such as `step_summarizer.model` in `config/artemis.jsonc`, also accept `"provider/model"`, for example `"openai/gpt-5.4-nano"`.

Some features are Gemini-only. Google Search grounding is skipped on other providers. The video analyzer's native video understanding is replaced by analysis of extracted keyframes. Audio analysis has not been verified on other providers. Whichever model you use has to accept images and support tool calling.

## Use it from an AI IDE (MCP)

```bash
uv run lunatap mcp --install claude    # or: cursor, codex, windsurf, antigravity, all
```

Then ask your IDE agent things like *"Install the latest build on the booted simulator, sign in with the test account, and check the profile screen for layout problems."*

## Development

```bash
make test        # unit tests (set GOOGLE_API_KEY=dummy-for-tests if you have no key)
make lint        # ruff + quality ratchet
make typecheck   # pyright
```

The architecture overview is in [CLAUDE.md](CLAUDE.md).

## Roadmap

1. ~~**Remove Android.**~~ Done: drivers, ADB tooling, the helper APK, logcat tools and Android prompts are gone.
2. **Finish the rename.** The config filename, the data directories and the MCP server name, with aliases for the old names. `LUNATAP_` environment variables are done.
3. **iOS logs.** Unified logging (`log stream`) for the Diagnoser, which lost logcat analysis with Android.
4. **Visual check before each tap.** Before tapping, read the screen with Apple Vision to confirm the target is really visible.
5. **One-command device setup.** WebDriverAgent signing, the 7-day re-sign for free teams, and keeping the device trusted.
6. **Save a run as a native XCUITest.**
7. **Simulator pools and a CI template** for macOS runners.

## Credits

- Built on [Artemis](https://github.com/google/artemis) by Google LLC (Apache 2.0).
- The initial iOS support comes from [google/artemis#175](https://github.com/google/artemis/pull/175) by banggaoo.
- Includes code from [Mobile-Use](https://github.com/minitap-ai/mobile-use) by Minitap, Inc. (Apache 2.0).

## License

[Apache License 2.0](LICENSE).
