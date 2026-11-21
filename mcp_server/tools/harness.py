# Copyright 2026 The Lunatap Authors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MCP tools for harness mode: the IDE agent drives the device step by step."""

import asyncio

from lunatap.harness.session import DeviceBusy, HarnessSession
from lunatap.mcp.device_controllers import get_controller as _get_controller
from mcp_server.base import mcp

_sessions: dict[str, HarnessSession] = {}
_lock = asyncio.Lock()
# Device and session failures a tool reports as text; anything else is a bug
# and surfaces as an MCP error.
_ERRORS = (OSError, RuntimeError, ValueError, TimeoutError)


def _key(device_serial: str | None) -> str:
    return (device_serial or "booted").strip()


def _canonical(device_serial: str | None) -> str:
    """The device UDID behind any serial a caller may pass.

    'booted', a simulator name, a phone name or its CoreDevice identifier all
    name the same device as its UDID; the session and the lock are keyed by
    the UDID so the phone is never "busy, held by its own session".
    """
    from lunatap.drivers.ios.discovery import (
        device_matches_identifier,
        find_physical_ios_device_sync,
        list_ios_simulators_sync,
    )

    key = _key(device_serial)
    try:
        simulators = list_ios_simulators_sync() or []
        if key.lower() == "booted":
            booted = [d for d in simulators if d.get("state") == "Booted" and d.get("udid")]
            return str(booted[0]["udid"]) if len(booted) == 1 else key
        for device in simulators:
            if device_matches_identifier(device, key) and device.get("udid"):
                return str(device["udid"])
        physical = find_physical_ios_device_sync(key)
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return key
    return str(physical["udid"]) if physical and physical.get("udid") else key


def _explain(exc: BaseException) -> str:
    """The error as text, plus the way out when this server is in its own way."""
    message = f"Error: {exc}"
    if "isSimulator=True" in message:
        held = [key for key, session in _sessions.items() if session.is_simulator]
        if held:
            calls = " or ".join(f"mobile_release_device('{key}')" for key in held)
            message += f" This server holds that simulator: call {calls}, then retry."
    return message


async def _get_session(device_serial: str | None) -> HarnessSession:
    """The live session for the device, started on first use."""
    key = await asyncio.to_thread(_canonical, device_serial)
    async with _lock:
        session = _sessions.get(key)
        if session is not None:
            return session
        controller = await asyncio.to_thread(
            _get_controller, device_serial=device_serial, target_platform="ios"
        )
        session = HarnessSession(controller)
        await session.start()
        _sessions[key] = session
        return session


@mcp.tool()
async def mobile_observe(
    device_serial: str | None = None, screenshot: bool = False, detect: str | None = None
) -> str:
    """Look at the iOS screen: the numbered element list, with the page title.

    Harness mode: YOU plan and decide, Lunatap only observes, acts and checks
    (no model of its own; nothing to wait for). Handoff mode is
    `mobile_run_task`, which runs Lunatap's autonomous agent instead.

    Each line is `[index] Class 'text' bounds`; pick elements by index or text
    in `mobile_act` / `mobile_do_step`. Ask for `screenshot=true` only when the
    list is thin (custom-drawn views, games) — it returns a file path.
    `detect="markers"` adds the map pins the tree does not list as `Marker`
    elements you can tap by index: round pins of any colour, each with a
    confidence and a kind (pin, icon with a symbol inside, or one
    "cluster of N+" for touching pins). It says when the map is
    satellite imagery, where it is less sure; check the screenshot then.

    The session holds the device (simulator or paired iPhone) until
    `mobile_release_device`; a handoff task cannot run on it meanwhile.
    """
    try:
        session = await _get_session(device_serial)
        return (await session.observe(screenshot=screenshot, detect=detect)).describe()
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_act(
    action: str,
    target: int | str | list[int] | None = None,
    text: str | None = None,
    direction: str | None = None,
    key: str | None = None,
    app: str | None = None,
    url: str | None = None,
    duration_ms: int | None = None,
    device_serial: str | None = None,
) -> str:
    """One device action, then the new screen (so no separate observe call).

    Actions: `tap` / `long_press` (target = element index or text from
    `mobile_observe`, or [x, y] screenshot pixels), `type` (text; optional
    target field), `swipe` (direction up/down/left/right), `press_key` (HOME,
    BACK, ENTER, ...), `open_app` / `stop_app` (app = name or bundle id),
    `wait` (duration_ms), `open_link` (url), `alert` (text = the alert button
    to press, e.g. "Allow While Using App"; omit for the default button).
    Alert buttons also appear in the element list as `AlertButton`, so a tap
    on one works too.

    A tap is refused with `screen_changed` when the pixels around the target
    moved since your last observation; the reply then carries the current
    screen, so choose again. Ambiguous text targets list the candidates.
    """
    try:
        session = await _get_session(device_serial)
        outcome = await session.act(
            action,
            target=target,
            text=text,
            direction=direction,
            key=key,
            app=app,
            url=url,
            duration_ms=duration_ms,
        )
        return outcome.describe()
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_do_step(step: str, device_serial: str | None = None) -> str:
    """Carry out one plan step ("Open General", "Tap Save") without deciding yourself.

    Lunatap matches the step to a control by text, else asks Clef (a cheap
    decision model), taps it, and checks the next screen names the step.
    Status `done`: verified. `tapped`: tapped, not verified; look at the
    screen. `undecided`: nothing confident; use `mobile_observe` and
    `mobile_act`. `blocked`: the tap was refused (reason given).
    """
    try:
        session = await _get_session(device_serial)
        return (await session.do_step(step)).describe()
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_wait_for(
    text: str, timeout_ms: int = 10000, state: str = "appear", device_serial: str | None = None
) -> str:
    """Wait until text appears (or, with state='disappear', goes) on the screen.

    On timeout the reply carries the current screen, so you can see what
    was there instead (a sheet covering the page, an alert, ...).
    """
    try:
        session = await _get_session(device_serial)
        return (await session.wait_for(text, timeout_ms=timeout_ms, state=state)).describe()
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return f"Error: {exc}"


@mcp.tool()
async def mobile_assert(
    text: str,
    state: str = "present",
    timeout_ms: int = 5000,
    device_serial: str | None = None,
) -> str:
    """Check the screen: PASS when `text` is present (or, with state='absent', gone).

    Waits up to `timeout_ms`. On FAIL the reply carries the current screen.
    Checks are recorded into the flow when `mobile_flow(action='record')` is on,
    so a replay on the next build can fail the same way.
    """
    try:
        session = await _get_session(device_serial)
        outcome = await session.assert_text(text, state=state, timeout_ms=timeout_ms)
        reply = ("PASS: " if outcome.ok else "FAIL: ") + outcome.message
        if outcome.observation is not None:
            reply += "\n" + outcome.observation.describe()
        return reply
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_flow(
    action: str,
    name: str | None = None,
    device_serial: str | None = None,
) -> str:
    """Saved flows: record this session, save it, replay it on the next build.

    `record` (name) starts recording every act / do_step / wait_for / assert;
    `save` writes `<flows dir>/<name>.json` (targets are stored as text or
    step wording, never as indexes); `run` (name or path) replays it and
    reports the first failing step or check with the screen; `list` and
    `show` as named. Flows are saved under `$LUNATAP_FLOWS_DIR`, else `flows/` next to
    Lunatap's traces dir (never the app repo); `record` prints the path.
    """
    from lunatap.harness.flows import Flow, flows_dir, list_flows, run_flow

    try:
        action = (action or "").strip().lower()
        if action == "list":
            names = list_flows()
            return f"Flows in {flows_dir()}: " + (", ".join(names) if names else "none")
        if action == "show":
            if not name:
                return "Error: 'show' needs a flow name."
            return Flow.load(name).describe()
        session = await _get_session(device_serial)
        if action == "record":
            if not name:
                return "Error: 'record' needs a flow name."
            return "OK: " + session.start_recording(name)
        if action == "save":
            flow, path, skipped = session.stop_recording(save=True)
            reply = f"OK: saved {path}\n{flow.describe()}"
            if skipped:
                reply += "\nNot saved (failed while recording): " + "; ".join(skipped)
            return reply
        if action == "run":
            if not name:
                return "Error: 'run' needs a flow name or path."
            flow = Flow.load(name)
            return (await run_flow(session, flow)).describe()
        return f"Error: unknown action '{action}' (record, save, run, list, show)."
    except FileNotFoundError as exc:
        return f"Error: {exc}"
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_logs(
    action: str = "read",
    process: str | None = None,
    bundle_id: str | None = None,
    since_step: int | None = None,
    last_seconds: float | None = None,
    grep: str | None = None,
    exclude: str | None = None,
    max_lines: int = 200,
    mode: str = "stream",
    device_serial: str | None = None,
) -> str:
    """Capture the device log for the app under test, lined up with your steps.

    `action="start"` (give `bundle_id` and/or `process`, the app's process
    name) streams the log in the background: simulators through `log stream`,
    physical devices through pymobiledevice3 when installed, or with
    `mode="launch"` for a dev-signed build (relaunches the app with its
    console attached; TestFlight builds cannot). `action="read"` returns
    lines: `since_step=N` gives the lines written during harness step N
    (every `mobile_act` reply names its step), `last_seconds` the newest
    ones, `grep` a regex filter, `exclude` a regex for more noise to drop
    (accessory heartbeats and iAP chatter are dropped by default;
    `exclude=""` shows every line). `action="status"` / `"stop"` as named.
    """
    try:
        session = await _get_session(device_serial)
        action = (action or "read").strip().lower()
        if action == "start":
            return "OK: " + await session.start_logs(
                bundle_id=bundle_id, process=process, mode=mode
            )
        if action == "stop":
            status = await session.stop_logs()
            return "OK: stopped. " + status if status else "Nothing to stop: no log capture."
        capture = session.logs
        if capture is None:
            return (
                "No log capture running: call mobile_logs(action='start', "
                "bundle_id=<app bundle id>) first."
            )
        if action == "status":
            return capture.status()
        if action != "read":
            return f"Error: unknown action '{action}' (start, read, status, stop)."
        lines = capture.read(
            since_step=since_step,
            last_seconds=last_seconds,
            grep=grep,
            exclude=exclude,
            max_lines=max_lines,
        )
        head = f"{len(lines)} line(s)"
        if since_step is not None:
            head += f" during step {since_step}"
        if grep:
            head += f" matching /{grep}/"
        return head + (":\n" + "\n".join(lines) if lines else ". " + capture.status())
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_net(
    action: str = "read",
    port: int = 8080,
    since_step: int | None = None,
    last_seconds: float | None = None,
    grep: str | None = None,
    failed_only: bool = False,
    host: str | None = None,
    summary: bool = False,
    max_lines: int = 100,
    device_serial: str | None = None,
) -> str:
    """Capture the app's network calls through mitmproxy, lined up with your steps.

    `action="start"` runs mitmdump on `port` and returns the one-time device
    setup (Wi-Fi proxy + trusted CA: a person's job on a phone; two
    `networksetup` commands and a simctl keychain call for a simulator).
    Every `mobile_act` reply then names the requests made during its step;
    `action="read"` with `since_step=N`, `last_seconds`, `grep` (on the URL)
    or `failed_only=true` returns them as `METHOD url -> status (ms)` with the
    body excerpt of failed responses. `host` (a regex on the host) keeps the
    app's own calls and drops other apps' traffic; `summary=true` gives one
    line per endpoint (ids collapsed: count, statuses, slowest), the quickest
    way to spot repeated or failing calls. `status` / `stop` as named; on a
    phone, turn its proxy off before `stop`. Needs mitmproxy installed
    (`uv tool install mitmproxy`).
    """
    try:
        session = await _get_session(device_serial)
        action = (action or "read").strip().lower()
        if action == "start":
            return "OK: " + await session.start_net(port=port)
        if action == "stop":
            status = await session.stop_net()
            return "OK: stopped. " + status if status else "Nothing to stop: no network capture."
        capture = session.net
        if capture is None:
            return "No network capture running: call mobile_net(action='start') first."
        if action == "status":
            return capture.status()
        if action != "read":
            return f"Error: unknown action '{action}' (start, read, status, stop)."
        lines = capture.read(
            since_step=since_step,
            last_seconds=last_seconds,
            grep=grep,
            host=host,
            failed_only=failed_only,
            summary=summary,
            max_lines=max_lines,
        )
        head = f"{len(lines)} endpoint(s)" if summary else f"{len(lines)} request(s)"
        if since_step is not None:
            head += f" during step {since_step}"
        if failed_only:
            head += " that failed"
        return head + (":\n" + "\n".join(lines) if lines else ". " + capture.status())
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_walkthrough(
    action: str,
    name: str | None = None,
    text: str | None = None,
    device_serial: str | None = None,
) -> str:
    """Record the app as a shareable walkthrough video.

    `action="start"` (`name`, e.g. "Create a job") records the screen. Every
    `mobile_act`, `mobile_do_step` and passing `mobile_assert` after it becomes a
    step: a screenshot and a caption ("Tap “Jobs”", or the plan step's own
    words). `action="caption"` with `text` rewords the latest step, for a
    clearer story ("Open the job list"). `wait`s and actions that leave the
    screen unchanged are not steps. `action="stop"` writes, in one folder:
    `walkthrough.mp4` (the recording with each step's caption in a band below
    the app, still stretches over ~6 s cut, H.264 at most 1920 px tall, ready
    to share), the step screenshots, and `steps.md`. The walkthrough's name is
    its opening caption. `action="status"` shows the progress.

    Use the simulator for smooth video; a physical iPhone records at about
    1-3 fps. Without a recording the video is a slideshow of the screenshots.
    Releasing the device or closing the session finishes a running walkthrough.
    """
    try:
        session = await _get_session(device_serial)
        verb = (action or "").strip().lower()
        if verb == "start":
            return await session.start_walkthrough(name or "walkthrough")
        if verb == "stop":
            return await session.stop_walkthrough()
        walk = session.walkthrough
        if verb == "status":
            return walk.status() if walk else "No walkthrough is running."
        if verb == "caption":
            if walk is None:
                return "Error: no walkthrough is running."
            if not text or not walk.recaption(text):
                return "Error: give `text`, after at least one step."
            return f"Step {walk.steps[-1].number} is now: {walk.steps[-1].caption}"
        return "Error: action must be start, caption, stop or status."
    except DeviceBusy as exc:
        return f"Error: device busy: {exc}"
    except _ERRORS as exc:
        return _explain(exc)


@mcp.tool()
async def mobile_release_device(device_serial: str | None = None) -> str:
    """End the harness session: disconnect and free the device for handoff tasks."""
    key = await asyncio.to_thread(_canonical, device_serial)
    session = _sessions.pop(key, None)
    if session is None:
        return f"Nothing to release: no harness session for '{key}'."
    try:
        await session.close()
    except _ERRORS as exc:
        return f"Error while releasing '{key}': {exc}"
    return f"Released '{key}'."
