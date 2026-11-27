# Lunatap

**An AI agent that tests iOS apps.** Point it at an iOS simulator or a real iPhone. Your AI IDE (Claude Code, Cursor, Codex) can then explore the app, reproduce bugs, check screens, and record flows that you replay on the next build. Or hand Lunatap a goal in plain English, and its own agent runs the task from start to finish.

```bash
uv run lunatap run "Open Settings, go to General > About, and report the iOS version" \
  --platform ios --standalone --device-serial <SIMULATOR-UDID>
```

> **Status: early (0.1.0).** iOS only, macOS only. Lunatap is a fork of [Artemis](https://github.com/google/artemis) by Google, narrowed to iOS. It is an independent project, not affiliated with or endorsed by Google. See [NOTICE](NOTICE).

## Contents

- [What you can do with it](#what-you-can-do-with-it)
- [Install](#install)
- [Examples](#examples)
  - [Build and run your app](#build-and-run-your-app)
  - [1. Explore an app from your AI IDE](#1-explore-an-app-from-your-ai-ide)
  - [2. Reproduce a bug with logs and network evidence](#2-reproduce-a-bug-with-logs-and-network-evidence)
  - [3. Record a flow and replay it in CI](#3-record-a-flow-and-replay-it-in-ci)
  - [4. Make a walkthrough video to share](#4-make-a-walkthrough-video-to-share)
  - [5. Tap pins on a map](#5-tap-pins-on-a-map)
  - [6. Run a whole task unattended](#6-run-a-whole-task-unattended)
  - [7. Use it from Python](#7-use-it-from-python)
- [Models](#models)
- [Real iPhones](#real-iphones)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Roadmap](#roadmap)

## What you can do with it

| You want to… | Use |
|---|---|
| Build your app from source and see the change on the simulator | `mobile_build` from your IDE, or `lunatap build --watch` to rebuild on every save |
| Explore an app, reproduce a bug, or check a screen while your IDE agent steers | **Harness tools** over MCP: `mobile_observe`, `mobile_act`, `mobile_assert`, … (no LLM key needed in Lunatap) |
| See why a step failed | `mobile_logs` (the app's unified log) and `mobile_net` (HTTP calls via mitmproxy), matched to each step |
| Retest the same path on every build | `mobile_flow` records it; `lunatap flow run` replays it and fails CI on the first broken step |
| Show someone how the app works | `mobile_walkthrough` turns the session into a captioned MP4 |
| Hand off a whole task | `lunatap run "<goal>"` or `mobile_run_task`: Lunatap's own agent plans, acts and verifies |

It works on:

- **Simulators**: Xcode 15–26 through WebDriverAgent, and Xcode 27 through Apple's own device tools.
- **Real iPhones and iPads** over the CoreDevice tunnel, through a WebDriverAgent runner you sign with your Apple team. A free personal team works.

The agent only taps what is actually visible. Elements hidden behind iOS 26 floating bars or scrolled off screen are not offered, and a tap is refused if the screen moved since the agent last looked.

## Install

On a Mac with Xcode 15 or later, run one command:

```bash
curl -fsSL https://raw.githubusercontent.com/Parvez2017/lunatap/main/install.sh | bash
```

or, with Node.js:

```bash
npx lunatap
```

The installer:

- installs [uv](https://docs.astral.sh/uv/) if you don't have it, which also brings Python 3.12;
- puts Lunatap in `~/.lunatap`;
- on Xcode 15–26, builds WebDriverAgent for simulators;
- adds the MCP server to every AI IDE it finds (Claude Code, Cursor, Codex, Windsurf, VS Code);
- adds a `lunatap` command.

Run the same command again to update. Restart your IDE afterwards so it loads the new server.

To install for one IDE only or in another folder, pass options:

```bash
curl -fsSL https://raw.githubusercontent.com/Parvez2017/lunatap/main/install.sh | bash -s -- --ide cursor --dir ~/tools/lunatap
npx lunatap install --ide claude --skip-wda
```

<details>
<summary>Manual install (for contributors)</summary>

```bash
git clone https://github.com/Parvez2017/lunatap.git && cd lunatap
uv sync --dev
bash scripts/setup_ios_env.sh               # checks Xcode, simulators and paired devices
bash scripts/setup_ios_wda_simulator.sh     # Xcode 15–26 only: builds WebDriverAgent once
uv run lunatap doctor                       # confirms everything is ready
uv run lunatap mcp --install claude         # or: cursor, codex, windsurf, vscode, cline, roo, antigravity, all
```

</details>

The harness tools need no model key: your IDE's agent is the brain. The autonomous agent ([example 6](#6-run-a-whole-task-unattended)) needs one in `~/.lunatap/.env`. See [Models](#models).

Find a simulator's UDID with `xcrun simctl list devices booted`. If only one simulator is booted, you can leave the UDID out everywhere.

## Examples

### Build and run your app

Point Lunatap at your app's folder and it builds the app for the simulator, installs it and launches it:

```bash
lunatap build ~/code/MyApp                  # finds the .xcworkspace or .xcodeproj and the scheme
lunatap build ~/code/MyApp --watch          # rebuild and relaunch on every save
lunatap build . --scheme Staging -d <UDID>  # pick the scheme and the simulator
```

From your IDE, the agent does the same with `mobile_build`, so it can fix code and check the result in one loop:

> *Add a "Reset" button under the counter, build the app and check that it sets the count back to 0.*

```python
mobile_build(path="~/code/MyApp")            # build → install → start logs → launch
# BUILD FAILED … ContentView.swift:17: cannot convert value of type 'String' to expected argument type 'Int'
mobile_build(path="~/code/MyApp")            # after the fix: built in 2s, relaunched
mobile_act(action="tap", target="Reset")
mobile_assert(text="Tapped 0 times")
```

Errors come back as `file:line: message`, and builds go to `~/Library/Caches/lunatap/DerivedData`, so Xcode's own build folder is untouched. To try it without your own app, use the example in [`examples/HelloLunatap`](examples/HelloLunatap). Building for a real iPhone isn't supported yet.

### 1. Explore an app from your AI IDE

Ask your IDE agent in plain language:

> *Open the Settings app on the booted simulator, go to General > About, and tell me the iOS version.*

The agent drives the device through the harness tools. A typical sequence of calls:

```python
mobile_observe()                               # numbered list of what's on screen + page title
mobile_act(action="open_app", app="Settings")
mobile_act(action="tap", target="General")     # by visible text…
mobile_act(action="tap", target=7)             # …or by element index from the last observation
mobile_assert(text="iOS Version", state="present")
mobile_release_device()                        # free the device when done
```

Every `mobile_act` reply already contains the new screen, so the agent rarely needs a separate observe. Other actions are `type`, `swipe`, `long_press`, `press_key` (`HOME`, `ENTER`, …), `open_link`, `wait`, and `alert` (presses a system alert button such as "Allow While Using App"). `mobile_do_step("Open General settings")` lets Lunatap resolve and verify a simple step on its own.

### 2. Reproduce a bug with logs and network evidence

> *Start capturing logs and network for com.example.shop, add an item to the cart and check out. Tell me what the server said when checkout failed.*

```python
mobile_logs(action="start", bundle_id="com.example.shop")
mobile_net(action="start")            # runs mitmproxy; the reply has the one-time phone proxy setup
mobile_act(action="open_app", app="com.example.shop")
mobile_act(action="tap", target="Add to cart")
mobile_act(action="tap", target="Checkout")       # the reply counts this step's log lines and requests
mobile_logs(action="read", since_step=3, grep="error|403")
mobile_net(action="read", since_step=3, failed_only=True)    # the failing call and the server's reason
mobile_net(action="read", host="api.example.com", summary=True)  # endpoints, with ids collapsed
```

Each step reports how many log lines and requests it caused, so a failing step comes with its own evidence. Network capture needs `brew install mitmproxy`. Setting the proxy and trusting the certificate on the phone is a one-time step for a person, which the agent relays to you.

### 3. Record a flow and replay it in CI

> *Record a flow called "login": sign in with the test account and check that the home screen says "Welcome back".*

```python
mobile_flow(action="record", name="login")
mobile_act(action="open_app", app="com.example.shop")
mobile_act(action="tap", target="Sign in")
mobile_act(action="type", target="Email", text="qa@example.com")
mobile_act(action="type", target="Password", text="••••••••")
mobile_act(action="tap", target="Continue")
mobile_assert(text="Welcome back")
mobile_flow(action="save")
```

Replay it later, from the IDE with `mobile_flow(action="run", name="login")`, or from a terminal or CI job:

```bash
uv run lunatap flow list
uv run lunatap flow run login --device-serial <UDID>
uv run lunatap flow run flows/                    # every flow in a folder; exits 1 on the first failure
```

A replay checks the screen titles it recorded. A step that lands on the wrong screen fails, and a check that failed while recording is not saved. Flows are JSON files kept in `flows/`; set `LUNATAP_FLOWS_DIR` to keep them in your app's repository.

### 4. Make a walkthrough video to share

> *Make a walkthrough of signing up and creating a first project.*

```python
mobile_walkthrough(action="start", name="Sign up and create a project")
mobile_act(action="tap", target="Get started")
mobile_walkthrough(action="caption", text="Start from the welcome screen")   # optional: reword the last step
mobile_act(action="type", target="Email", text="new@example.com")
mobile_act(action="tap", target="Create project")
mobile_walkthrough(action="stop")
```

`stop` returns a folder (under `walkthroughs/`, or `$LUNATAP_WALKTHROUGHS_DIR`) containing:

- `walkthrough.mp4`: the screen recording with a caption for each step, with idle stretches cut;
- a screenshot for each step;
- `steps.md`: the steps as text, for docs or a ticket.

Waits and steps that changed nothing are left out of the video, but any detour you take while recording is in it. Walkthroughs currently record simulators only.

### 5. Tap pins on a map

Map pins and other custom-drawn views are not in the accessibility tree. Ask for them explicitly:

```python
mobile_observe(detect="markers")
# adds Marker elements such as "grey pin (100%)", "green icon (90%)", "cluster of 57+ grey pins (76%)"
mobile_act(action="tap", target=41)                # tap one by its index
```

Each marker has a confidence and a kind:

- `pin`: a plain disc;
- `icon`: a symbol inside, such as a park or restaurant point of interest;
- `cluster of N+`: touching pins. `N+` is a lower bound, because pins hide under each other.

On satellite imagery the reply says so, and confidences deserve less trust there. Confirm with `mobile_observe(screenshot=True)`.

### 6. Run a whole task unattended

Give Lunatap's own agent a goal, and it plans, acts and reports without you steering it. This needs a model key in `.env` (see [Models](#models)).

```bash
# Flash: one model in a fast observe→act loop, a few seconds per step
uv run lunatap run "Turn on Dark Mode in Settings and confirm it is on" \
  --platform ios --standalone --device-serial <UDID> --profile flash

# Pro: keeps a written plan, checks each action before it runs, verifies checkpoints
uv run lunatap run "Create an account with a fresh email, then log out and log back in" \
  --platform ios --standalone --device-serial <UDID> --profile pro
```

From an IDE the same thing is `mobile_run_task(task_desc="…", model="Flash")`, which you follow with `mobile_manage_task`. Every run saves a trace with screenshots and a screen recording. Browse traces with `uv run lunatap ui`, a web console on port 8000, or `uv run lunatap trace`.

Use Flash for short, direct tasks. Use Pro when the task needs a persistent plan, verified checkpoints, or a written report.

### 7. Use it from Python

```python
import asyncio
from lunatap.sdk import Agent
from lunatap.sdk.builders import AgentConfigBuilder

async def main():
    agent = Agent(config=AgentConfigBuilder().for_ios_device("<SIMULATOR-UDID>").build())
    try:
        await agent.init()
        result = await agent.run_task(goal="Open Settings and report the iOS version", profile="flash")
        print(result)
    finally:
        await agent.clean()

asyncio.run(main())
```

To submit tasks to Lunatap running on another Mac, use the dependency-free client in [`packages/artemis-client`](packages/artemis-client).

## Models

Only the autonomous agent ([example 6](#6-run-a-whole-task-unattended)) and Lunatap's internal summarizers call a model. Copy `.env.example` to `.env` and choose one setup:

| Setup | `.env` |
|---|---|
| Gemini (default) | `GOOGLE_API_KEY=…` |
| OpenAI | `OPENAI_API_KEY=sk-…` and `LUNATAP_LLM_PRESET=openai` |
| Any OpenAI-compatible server (OpenRouter, LM Studio, vLLM, LiteLLM, …) | `LUNATAP_LLM_PRESET=openai-compatible`, `OPENAI_BASE_URL=…`, `LUNATAP_LLM_MODEL=…`, and `OPENAI_API_KEY` if the server needs one |
| Ollama on this machine | `LUNATAP_LLM_PRESET=local-ollama`, with a vision model that supports tool calling, such as `qwen3-vl` |
| Anthropic | `ANTHROPIC_API_KEY=…`, then set the models in `config/artemis.jsonc` |

- `LUNATAP_LLM_MODEL` and `LUNATAP_LLM_FALLBACK_MODEL` override any preset.
- Settings that name a single model also accept `"provider/model"`, for example `"openai/gpt-5.4-nano"`.
- Whichever model you use must accept images and support tool calling.
- Three features are tied to Gemini:
  - Google Search grounding is skipped on other providers.
  - On other providers, video analysis works from extracted keyframes.
  - Audio analysis hasn't been verified on other providers.

## Real iPhones

1. Pair the phone in Xcode and turn on Developer Mode.
2. Build and sign the WebDriverAgent runner with your team (see the [iOS guide](docs/ios.md#webdriveragent-setup-one-time-per-device)).
3. On the phone, trust the developer certificate under Settings > General > VPN & Device Management.
4. Pass the phone's UDID explicitly. Lunatap never picks a physical device on its own.

A free team's signature expires every 7 days. `mobile_diagnose` (or `uv run lunatap doctor`) detects this and prints the exact command to re-sign. The [iOS guide](docs/ios.md) covers the simulator backends, first-run Xcode approval and the limits.

## How it works

```
 AI IDE (Claude Code, Cursor, …)    CLI · Python SDK · web console
              │ MCP                              │
   harness tools: you steer           autonomous agent: Lunatap steers
   observe · act · assert ·           Flash: observe→act loop
   logs · net · flow · walkthrough    Pro: plan → safety check → act → verify
              └───────────────┬──────────────────┘
                    iOS drivers (lunatap/drivers/ios)
       WebDriverAgent (simulators on Xcode 15–26, real devices)
       Xcode device tools (simulators on Xcode 27)
```

- **Each observation** combines the accessibility tree with on-device OCR (Apple Vision) into one numbered element list. Before a tap, Lunatap checks that the target is still where it was.
- **Map markers** come from a shape detector that runs on the screenshot.
- **Every run** keeps a trace in `traces/`, with screenshots, actions and a screen recording.

The architecture notes for contributors are in [CLAUDE.md](CLAUDE.md).

## Troubleshooting

Run `uv run lunatap doctor`, or ask your IDE agent to call `mobile_diagnose`. Either one checks Python, the config, model keys, the device and WebDriverAgent signing, and lists the fixes in order. `mobile_diagnose(attempt_fix=true)` also clears stale device locks. Common cases are covered in the [iOS guide](docs/ios.md#troubleshooting).

## Development

```bash
make test        # hermetic unit tests: no device or model key needed
make lint        # ruff + quality ratchet
make typecheck   # pyright
make test-device # end-to-end tests on a booted simulator or paired device
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues privately as described in [SECURITY.md](SECURITY.md).

## Roadmap

**In 0.1**

- iOS-only codebase: the Android drivers, tooling and prompts are removed.
- Simulators (Xcode 15–27) and real iPhones and iPads.
- Harness MCP tools: observe/act with a pixel check before each tap, assertions, alerts, and per-step logs and network capture.
- Recorded flows with CLI replay, captioned walkthrough videos, and map-marker detection.
- An autonomous Flash or Pro agent on Gemini, OpenAI, OpenAI-compatible servers or Ollama.

**Next**

1. **Install without cloning.** Publish to PyPI so that `uvx lunatap mcp --install claude` works from anywhere.
2. **Device setup in one command.** Sign, install and trust WebDriverAgent on a phone; renew free-team signatures before the 7 days run out; configure the phone proxy for network capture.
3. **CI template.** A GitHub Actions workflow for macOS runners that boots a simulator, installs a build and replays `flows/`, with JUnit output and screenshots of failures.
4. **Export flows as XCUITest.** Turn a recorded flow into a native Swift UI test that you can keep in the app's repository.
5. **Walkthroughs on real devices,** plus vertical and GIF outputs for sharing.
6. **Finish the rename.** Retire the remaining Artemis names (`config/artemis.jsonc`, `~/.artemis`, `artemis-client`) behind aliases for the old ones.

**Later**

- Simulator pools for running flows in parallel.
- Better map markers: tell icons from pins on grey markers, and read clusters' own count badges.
- Visual diffs between builds: flag screens that changed since the last good run.

Ideas and pull requests are welcome. Open an [issue](https://github.com/Parvez2017/lunatap/issues).

## Credits

- Built on [Artemis](https://github.com/google/artemis) by Google LLC (Apache 2.0).
- The initial iOS support comes from [google/artemis#175](https://github.com/google/artemis/pull/175) by banggaoo.
- Includes code from [Mobile-Use](https://github.com/minitap-ai/mobile-use) by Minitap, Inc. (Apache 2.0).

## License

[Apache License 2.0](LICENSE).
