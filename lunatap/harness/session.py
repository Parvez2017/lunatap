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

"""One device, driven by an outside agent, with the judgement done in code.

A :class:`HarnessSession` holds a connected device for as long as the
harness (an IDE agent such as Claude Code) is working on it. It offers four
verbs: ``observe`` (the numbered element list the agents also see), ``act``
(one device action, refused when the screen moved since the observation it
was chosen from), ``do_step`` (a plan step such as "Open General" settled by
text match or Clef and verified against the next screen) and ``wait_for``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from lunatap.agents.fast_path import decider
from lunatap.agents.fast_path.step_done import page_title, step_reached
from lunatap.agents.validator.precondition_pixel import target_region_unchanged
from lunatap.config.paths import get_temp_dir
from lunatap.drivers.ios.hierarchy import layout_shift
from lunatap.drivers.ios.wda import WdaBusyError
from lunatap.harness.flows import Flow, FlowRecorder, FlowResult, flow_path, run_flow
from lunatap.harness.logs import LogCapture, log_command
from lunatap.harness.markers import (
    MarkerScan,
    control_rects,
    map_region,
    marker_elements,
    scan_markers,
    text_rects,
)
from lunatap.harness.network import NetCapture, net_command, setup_instructions
from lunatap.harness.walkthrough import (
    Walkthrough,
    recording_segments,
    render_recording,
    render_slideshow,
    step_caption,
)
from lunatap.mcp.action_types import ActionResult
from lunatap.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
from lunatap.runtime.device_target import IOS_LOCK_SCOPE
from lunatap.utils.ocr_api import is_ocr_configured, perform_ocr
from lunatap.utils.ocr_xml_fusion import (
    pixel_tree_shift,
    _crop_image_remove_status_bar,
    _detect_status_bar_height,
    _map_coordinates_back,
    fuse_ocr_with_xml,
)
from lunatap.utils.visualization import format_minimal_list_with_elements
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

ACTIONS = (
    "tap",
    "long_press",
    "type",
    "swipe",
    "press_key",
    "open_app",
    "stop_app",
    "wait",
    "open_link",
    "alert",
)
ALERT_BUTTON_CLASS = "AlertButton"
# Finger paths for a directional swipe, in 0-1000 screen coordinates.
_SWIPES = {
    "up": ((500, 700), (500, 300)),
    "down": ((500, 300), (500, 700)),
    "left": ((800, 500), (200, 500)),
    "right": ((200, 500), (800, 500)),
}
SETTLE_AFTER_ACTION_MS = 600
#: After a screen change the reply is re-read until a tree-only read agrees
#: with it (QA bug 32: read mid-transition, every element ~62/1000 too high).
SETTLE_CHECKS = 2
SETTLE_TOLERANCE = 0.015  # of the screen height


#: Mean difference (0-255) between two small grey thumbnails below which
#: the screen counts as still.
STILL_DIFF = 1.5


def _thumbnail(png: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(png)) as image:
        return np.asarray(image.convert("L").resize((48, 104)), dtype=np.float32)


def _thumb_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean())


class DeviceBusy(RuntimeError):
    """Another Lunatap run (a handoff task, another harness) holds the device."""


@dataclass
class Observation:
    """One look at the screen: what the harness chooses from."""

    elements: list[dict[str, Any]]
    text: str
    width: int
    height: int
    title: str | None
    screenshot_b64: str
    screenshot_path: str | None = None
    backend: str | None = None  # "WDA physical @ host:port", "WDA simulator", ...
    markers: MarkerScan | None = None  # what detect="markers" found
    # The driver's elements this was built from (pixel boxes), for comparing reads.
    raw: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Median offset (px) between the tree's labels and the same words found by
    # OCR in the screenshot; large when the tree was read mid-transition.
    pixel_shift: float | None = None
    taken_at: float = field(default_factory=time.monotonic)

    def describe(self) -> str:
        """One line per element: index, class, text and 0-1000 bounds."""
        head = f"Screen: {self.title or '(no title)'} — {len(self.elements)} elements, "
        head += f"{self.width}x{self.height} (bounds below are 0-1000 of the screen)"
        if self.backend:
            head += f" via {self.backend}"
        if self.markers is not None:
            scan = self.markers
            clusters = sum(1 for m in scan.markers if m.count > 1)
            head += (
                f"\nMarkers: {len(scan.markers)} found in the screenshot (~{scan.pins} pins"
                + (f", {clusters} cluster(s) of touching pins listed once" if clusters else "")
                + "), as Marker elements with a confidence; tap by index (a cluster's"
                " index taps one of its pins)"
            )
            if scan.imagery:
                head += (
                    "\nBackground looks like satellite imagery: confidences are less"
                    " reliable there; check the screenshot."
                )
        if self.screenshot_path:
            head += f"\nScreenshot: file://{self.screenshot_path}"
        lines = [head]
        for element in self.elements:
            lines.append(
                f"[{element.get('index')}] {element.get('class') or 'Element'}"
                f" '{str(element.get('text') or '').strip()}'{self._bounds_of(element)}"
            )
        return "\n".join(lines)

    def _bounds_of(self, element: dict[str, Any]) -> str:
        bounds = element.get("bounds")
        if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4):
            return ""
        left, top, right, bottom = bounds
        return (
            f" [{int(left) * 1000 // self.width},{int(top) * 1000 // self.height}]"
            f"[{int(right) * 1000 // self.width},{int(bottom) * 1000 // self.height}]"
        )

    def labels(self) -> list[str]:
        return [str(e.get("text") or "") for e in self.elements]


@dataclass
class ActOutcome:
    ok: bool
    message: str
    code: str = "ok"  # ok, bad_action, bad_target, screen_changed, failed
    observation: Observation | None = None
    screen_changed: bool | None = None
    step: int | None = None  # harness step number, the key for mobile_logs(since_step=...)
    log_lines: int | None = None  # device log lines written while this step ran
    net_requests: tuple[int, int] | None = None  # (requests, failed) while this step ran

    def describe(self) -> str:
        lines = [("OK: " if self.ok else f"Not done ({self.code}): ") + self.message]
        if self.screen_changed is not None:
            lines.append("Screen changed." if self.screen_changed else "Screen did not change.")
        if self.step is not None and self.log_lines is not None:
            lines.append(
                f"Logs: {self.log_lines} line(s) during step {self.step}; "
                f"mobile_logs(action='read', since_step={self.step})"
            )
        if self.step is not None and self.net_requests is not None:
            total, failed = self.net_requests
            lines.append(
                f"Network: {total} request(s) so far during step {self.step}"
                + (f", {failed} failed" if failed else "")
                + f"; mobile_net(action='read', since_step={self.step}) (calls still in"
                " flight are added to the step when they finish)"
            )
        if self.observation is not None:
            lines.append(self.observation.describe())
        return "\n".join(lines)


@dataclass
class StepOutcome:
    status: str  # done, tapped, undecided, blocked
    reason: str
    tapped: str | None
    observation: Observation | None

    def describe(self) -> str:
        head = f"Step {self.status}: {self.reason}"
        if self.tapped:
            head += f" (tapped '{self.tapped}')"
        if self.observation is not None:
            return f"{head}\n{self.observation.describe()}"
        return head


@dataclass(frozen=True)
class _StepOnly:
    """A step number without a log mark (network capture on, logs off)."""

    step: int


# Where typed text goes: a `type` with a text target prefers these.
_INPUT_CLASSES = frozenset({"TextField", "SecureTextField", "SearchField", "TextView"})

# Classes that only show something; when one shares a frame with a control,
# the control is the one to tap.
_PASSIVE_CLASSES = frozenset(
    {"StaticText", "StaticText [OCR]", "Text [OCR]", "Other", "Other [OCR]", "Image"}
)


def _frame(element: dict[str, Any]) -> tuple[int, int, int, int] | None:
    bounds = element.get("bounds")  # [left, top, right, bottom] pixels, from observe
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        return (bounds[0], bounds[1], bounds[2], bounds[3])
    return None


def _inside(inner: dict[str, Any], outer: dict[str, Any]) -> bool:
    a, b = _frame(inner), _frame(outer)
    if a is None or b is None:
        return False
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def _passive(element: dict[str, Any]) -> bool:
    return str(element.get("class") or "") in _PASSIVE_CLASSES


def _outermost(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every match that lies inside another match.

    A Button 'Map' and the StaticText 'Map' drawn inside it are one target,
    the Button. Matches that share the exact same frame keep the control
    and drop the passive one.
    """

    def covered(element: dict[str, Any]) -> bool:
        for other in elements:
            if other is element or not _inside(element, other):
                continue
            if not _inside(other, element) or (_passive(element) and not _passive(other)):
                return True
        return False

    return [e for e in elements if not covered(e)]


def _shared_control(
    found: list[dict[str, Any]], elements: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The one control every match sits in, when they all sit in the same one.

    Live (QA bug 25): a job row is a Button 'Test job, IN FIELD, ...' holding
    two StaticText 'Test job' labels; either tap opens the same row, so the
    row is the target, not an ambiguity.
    """

    def nearest_control(element: dict[str, Any]) -> dict[str, Any] | None:
        holders = [
            other
            for other in elements
            if other is not element
            and not _passive(other)
            and str(other.get("class") or "") != "Application"
            and _inside(element, other)
        ]
        if not holders:
            return None
        return min(holders, key=lambda e: _area(_frame(e)))

    controls = [nearest_control(e) for e in found]
    first = controls[0]
    if first is None or any(c is not first for c in controls):
        return None
    return first


def _area(frame: tuple[int, int, int, int] | None) -> int:
    if frame is None:
        return 0
    return max(0, frame[2] - frame[0]) * max(0, frame[3] - frame[1])


class HarnessSession:
    """A leased, connected device plus the last observation taken from it."""

    def __init__(self, controller: Any, actuator: Any = None, settle_ms: int | None = None):
        self._controller = controller
        self._actuator = actuator
        self._lease: Any = None
        self._last: Observation | None = None
        self._fast_path = decider.FastPath(harness=True)
        self._logs: LogCapture | None = None
        self._net: NetCapture | None = None
        self._step_no = 0
        self.recorder: FlowRecorder | None = None
        self._settle_ms = SETTLE_AFTER_ACTION_MS if settle_ms is None else settle_ms
        self.device_id: str | None = None
        self.walkthrough: Walkthrough | None = None

    # --- lifecycle ---

    async def start(self) -> None:
        driver = self._controller.driver
        resolved = await driver.resolve_device()
        self._controller.ctx.device.device_id = resolved
        self.device_id = resolved
        lease = DeviceExecutionLock(
            resolved,
            description="Lunatap harness session",
            ingress="mcp",
            concurrency_mode="per_device",
            lock_scope=IOS_LOCK_SCOPE,
        )
        try:
            await asyncio.to_thread(lease.acquire, blocking=False)
        except DeviceBusyError as exc:
            raise DeviceBusy(str(exc)) from exc
        self._lease = lease
        try:
            await driver.connect()
        except (OSError, RuntimeError, ValueError, TimeoutError, asyncio.CancelledError):
            lease.release()
            self._lease = None
            raise
        if self._actuator is None:
            from lunatap.mcp.actuators.ios import IosActuator

            self._actuator = IosActuator(self._controller.ctx, self._controller)

    async def close(self) -> None:
        if self.walkthrough is not None:
            try:
                await self.stop_walkthrough()
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                logger.warning(f"Harness walkthrough could not be finished on close: {exc}")
        await self.stop_logs()
        await self.stop_net()
        try:
            await self._controller.driver.disconnect()
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            logger.warning(f"Harness session disconnect failed: {exc}")
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    # --- observe ---

    async def observe(
        self, settle_ms: int = 0, screenshot: bool = False, detect: str | None = None
    ) -> Observation:
        """Look at the screen; ``detect="markers"`` adds vivid blobs as Marker elements."""
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
        data = await self._controller.get_screen_data()
        width, height = data.width or 1080, data.height or 2400
        device = getattr(self._controller.ctx, "device", None)
        if device is not None:
            device.device_width, device.device_height = width, height
        ocr_results = await self._ocr(data.base64, data.elements, height)
        fused = fuse_ocr_with_xml(data.elements, ocr_results)
        text, elements, _ = format_minimal_list_with_elements(fused, width, height)
        elements = await self._with_alert_buttons(elements)
        markers: MarkerScan | None = None
        if detect == "markers":
            markers = await asyncio.to_thread(
                scan_markers,
                base64.b64decode(data.base64),
                exclude=control_rects(elements),
                text=text_rects(elements),
                region=map_region(elements) or map_region(data.elements),
            )
            first = max((int(e.get("index") or 0) for e in elements), default=0) + 1
            elements = elements + marker_elements(markers.markers, first)
        path = None
        if screenshot:
            path = str(get_temp_dir("screenshots") / f"harness_{int(time.time())}.png")
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(data.base64))
        self._last = Observation(
            elements=elements,
            text=text,
            width=width,
            height=height,
            title=page_title(elements, width, height),
            screenshot_b64=data.base64,
            screenshot_path=path,
            backend=self.backend,
            markers=markers,
            raw=list(data.elements),
            pixel_shift=pixel_tree_shift(data.elements, ocr_results),
        )
        return self._last

    async def _with_alert_buttons(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add the alert's buttons as elements when an alert is on screen.

        An app's own UIAlertController often exposes no Button children in
        the tree; WebDriverAgent's alert endpoints still see them.
        """
        alert = next((e for e in elements if e.get("class") in ("Alert", "Sheet")), None)
        client = self._wda_client
        if alert is None or client is None:
            return elements
        try:
            buttons = await client.alert_buttons()
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            logger.debug(f"Harness: alert buttons unavailable: {exc}")
            return elements
        known = {str(e.get("text") or "").strip() for e in elements if e.get("class") == "Button"}
        index = max((int(e.get("index") or 0) for e in elements), default=0)
        added = list(elements)
        for label in buttons:
            if label in known:
                continue
            index += 1
            added.append(
                {
                    "index": index,
                    "center": list(alert.get("center") or alert.get("hit_point") or [0, 0]),
                    "hit_point": list(alert.get("hit_point") or alert.get("center") or [0, 0]),
                    "text": label,
                    "bounds": list(alert.get("bounds") or [0, 0, 0, 0]),
                    "class": ALERT_BUTTON_CLASS,
                    "is_ocr": False,
                }
            )
        return added

    @property
    def _wda_client(self) -> Any | None:
        """The WebDriverAgent client behind the driver, when there is one."""
        return getattr(self._controller.driver, "_wda", None)

    @property
    def backend(self) -> str:
        """Which backend drives the device, with the WDA endpoint when there is one."""
        driver = self._controller.driver
        name = type(driver).__name__
        url = getattr(getattr(driver, "_wda", None), "base_url", None)
        label = {
            "PhysicalIosDriver": "WDA physical",
            "WdaSimulatorDriver": "WDA simulator",
            "XcodeSimulatorDriver": "Xcode simulator",
        }.get(name, name)
        return f"{label} @ {url}" if url else label

    @property
    def is_simulator(self) -> bool:
        return type(self._controller.driver).__name__ != "PhysicalIosDriver"

    async def _ocr(self, screenshot_b64: str, elements: list, height: int) -> list:
        if not is_ocr_configured():
            return []
        try:
            status_bar = _detect_status_bar_height(elements, height)
            if status_bar > 0:
                cropped, _, _ = _crop_image_remove_status_bar(screenshot_b64, status_bar)
                return _map_coordinates_back(await perform_ocr(cropped), status_bar)
            return await perform_ocr(screenshot_b64)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning(f"Harness OCR skipped: {exc}")
            return []

    # --- act ---

    async def act(
        self,
        action: str,
        target: Any = None,
        text: str | None = None,
        direction: str | None = None,
        key: str | None = None,
        app: str | None = None,
        url: str | None = None,
        duration_ms: int | None = None,
        observe_after: bool = True,
    ) -> ActOutcome:
        """One device action; a fresh observation comes back with it."""
        began = time.monotonic()
        outcome = await self._act(
            action, target, text, direction, key, app, url, duration_ms, observe_after
        )
        # A wait, or an action that left the screen as it was, is no step to
        # show: live, "Wait" steps made 30 s of a still screen.
        shows = (action or "").strip().lower() != "wait" and outcome.screen_changed is not False
        if outcome.ok and self.walkthrough is not None and shows:
            caption = step_caption(
                (action or "").strip().lower(), target, text, direction, key, app, url
            )
            self._walk_step(caption, outcome.observation, began)
        return outcome

    async def _act(
        self,
        action: str,
        target: Any = None,
        text: str | None = None,
        direction: str | None = None,
        key: str | None = None,
        app: str | None = None,
        url: str | None = None,
        duration_ms: int | None = None,
        observe_after: bool = True,
    ) -> ActOutcome:
        """One device action; a fresh observation comes back with it."""
        action = (action or "").strip().lower()
        if action not in ACTIONS:
            return ActOutcome(
                False, f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.", "bad_action"
            )
        just_observed = self._is_text_target(target)
        if just_observed:
            # A text target is resolved against the screen as it is now: the
            # cached tree may be from before the app moved on (a wait_for, a
            # relaunch). Index targets keep the cached tree plus the pixel check.
            await self.observe()
        before = self._last
        point: tuple[int, int] | None = None
        mark = self._mark_step(f"{action} {target if target is not None else text or ''}".strip())
        # What a saved flow will replay: the label under the index, read now,
        # before the screen changes.
        recorded_target = self._recorded_target(target)
        if action == "alert" or (action == "tap" and self._is_alert_button(target)):
            outcome = await self._act_on_alert(before, text if action == "alert" else target)
            return self._with_logs(outcome, mark)
        needs_target = action in ("tap", "long_press") or (action == "type" and target is not None)
        if needs_target:
            resolved = self._resolve_target(target, prefer_input=action == "type")
            if isinstance(resolved, str):
                return ActOutcome(False, resolved, "bad_target")
            point, from_observation = resolved
            # A target resolved from a look taken by this very call needs no
            # second look: on a phone that is a full capture, seconds each.
            if from_observation and before is not None and not just_observed:
                refusal = await self._refuse_if_screen_changed(before, point)
                if refusal is not None:
                    return refusal
        try:
            result = await self._dispatch(
                action, point, text, direction, key, app, url, duration_ms
            )
        except ValueError as exc:
            return ActOutcome(False, str(exc), "bad_action")
        if not result.ok:
            return self._with_logs(ActOutcome(False, result.message, "failed"), mark)
        if not observe_after:
            self._record_act(action, recorded_target, text, direction, key, app, url, duration_ms)
            return self._with_logs(ActOutcome(True, result.message), mark)
        try:
            after = await self.observe(settle_ms=self._settle_ms)
        except WdaBusyError as busy:
            # The action happened; only the look afterwards hung. Say both,
            # so open_app / HOME stay usable as the way out of a stuck app.
            self._last = None
            self._record_act(action, recorded_target, text, direction, key, app, url, duration_ms)
            return self._with_logs(
                ActOutcome(True, f"{result.message} The screen could not be read after it: {busy}"),
                mark,
            )
        changed = before is None or (
            after.title != before.title or after.labels() != before.labels()
        )
        if changed:
            after = await self._settled(after)
        self._record_act(
            action, recorded_target, text, direction, key, app, url, duration_ms, after.title
        )
        return self._with_logs(
            ActOutcome(True, result.message, observation=after, screen_changed=changed), mark
        )

    # --- recording ---

    @property
    def last_observation(self) -> Observation | None:
        return self._last

    def _recorded_target(self, target: Any) -> Any:
        """``target`` as a flow stores it: the element's text, else pixels."""
        if target is None:
            return None
        element = self._element_for(target)
        label = str(element.get("text") or "").strip() if element is not None else ""
        if label:
            return label  # by text, never by index
        if isinstance(target, (list, tuple)):
            return [int(target[0]), int(target[1])]
        if element is not None:
            return list(element.get("hit_point") or element.get("center") or [])
        return target

    @staticmethod
    def _is_text_target(target: Any) -> bool:
        return isinstance(target, str) and bool(target.strip()) and not target.strip().isdigit()

    def _record_act(
        self, action, target, text, direction, key, app, url, duration_ms, screen=None
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record(
            "act",
            action=action,
            target=target,
            text=text,
            direction=direction,
            key=key,
            app=app,
            url=url,
            duration_ms=duration_ms,
            screen=screen,  # the title the replay must land on
        )

    def start_recording(self, name: str) -> str:
        self.recorder = FlowRecorder(name, device=self.device_id)
        return (
            f"Recording flow '{name}': every act, do_step, wait_for and assert from now on "
            f"(checks that fail are not saved). Will save to {flow_path(name)}."
        )

    def stop_recording(self, save: bool = True) -> tuple[Flow, str | None, list[str]]:
        """Stop recording; save the flow unless told not to.

        Returns ``(flow, path, skipped)``: ``skipped`` lists the checks that
        failed while recording and were therefore left out.
        """
        if self.recorder is None:
            raise RuntimeError("Not recording: call start_recording(name) first.")
        flow, skipped = self.recorder.flow, list(self.recorder.skipped)
        self.recorder = None
        path = str(flow.save()) if save else None
        return flow, path, skipped

    async def run_flow(self, name_or_path: str) -> FlowResult:
        return await run_flow(self, Flow.load(name_or_path))

    # --- checks ---

    async def assert_text(
        self, text: str, state: str = "present", timeout_ms: int = 5000
    ) -> ActOutcome:
        """PASS when ``text`` is (or, with ``absent``, is not) on screen within the timeout."""
        wanted = (state or "present").strip().lower()
        if wanted not in ("present", "absent"):
            return ActOutcome(False, "state must be 'present' or 'absent'.", "bad_action")
        result = await self._actuator.wait_for_text(
            text, "appear" if wanted == "present" else "disappear", timeout_ms
        )
        if result.ok:
            if self.recorder is not None:
                self.recorder.record("assert", text=text, state=wanted, timeout_ms=timeout_ms)
            self._last = None  # the screen was read by the wait, not by observe
            if self.walkthrough is not None:
                began = time.monotonic()
                seen = await self.observe()
                verb = "is shown" if wanted == "present" else "is gone"
                self._walk_step(f"Check: “{text}” {verb}", seen, began)
            return ActOutcome(True, f"'{text}' is {wanted} on screen.", "ok")
        if self.recorder is not None:
            self.recorder.skipped.append(f"assert '{text}' {wanted} (failed while recording)")
        after = await self.observe()
        return ActOutcome(
            False,
            f"'{text}' is not {wanted} on screen after {timeout_ms} ms (title: "
            f"{after.title or '(no title)'}).",
            "assert_failed",
            observation=after,
        )

    # --- walkthrough ---

    def _walk_step(self, caption: str, observation: Observation | None, began: float) -> None:
        walk = self.walkthrough
        if walk is None:
            return
        png = base64.b64decode(observation.screenshot_b64) if observation is not None else None
        walk.add(caption, observation.title if observation else None, png, began)

    async def start_walkthrough(self, name: str) -> str:
        """Record the screen and capture every following action as a captioned step."""
        if self.walkthrough is not None:
            raise RuntimeError(
                f"Walkthrough '{self.walkthrough.name}' is already running; stop it first."
            )
        walk = Walkthrough.create(name)
        try:
            await self._controller.driver.start_video_recording(walk.recording_dir)
            walk.recording = True
        except (OSError, RuntimeError, ValueError, TimeoutError, NotImplementedError) as exc:
            walk.recording_note = (str(exc).splitlines() or [repr(exc)])[0][:200]
            logger.warning(f"Walkthrough recording unavailable, steps only: {exc}")
        self.walkthrough = walk
        first = await self.observe()
        # The walkthrough's own name opens it: the screen's title was a
        # greeting ("Good afternoon, test") or OCR off a widget live.
        self._walk_step(walk.name, first, time.monotonic())
        return walk.status()

    async def _wait_until_still(
        self, since: float, min_seconds: float = 2.0, max_seconds: float = 8.0
    ) -> float:
        """When the screen stopped changing: two screenshots ~0.5 s apart agree.

        At least ``min_seconds`` after ``since``; gives up after ``max_seconds``.
        """
        deadline = time.monotonic() + max_seconds
        previous = None
        while time.monotonic() < deadline:
            try:
                shot = _thumbnail(base64.b64decode(await self._screenshot_b64()))
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                logger.debug(f"Walkthrough: could not check the final screen: {exc}")
                break
            if (
                previous is not None
                and _thumb_diff(previous, shot) < STILL_DIFF
                and time.monotonic() - since >= min_seconds
            ):
                break
            previous = shot
            await asyncio.sleep(0.5)
        return time.monotonic()

    async def stop_walkthrough(self) -> str:
        """Stop recording and write walkthrough.mp4, the screenshots and steps.md."""
        walk = self.walkthrough
        if walk is None:
            raise RuntimeError("No walkthrough is running; start one first.")
        self.walkthrough = None
        out = walk.directory / "walkthrough.mp4"
        video: Path | None = None
        notes: list[str] = []
        if walk.recording:
            # Hold the last step until its screen stops changing: live, the
            # video ended on a map still loading (black, pins only).
            since = walk.steps[-1].at if walk.steps else 0.0
            walk.settled_at = await self._wait_until_still(since)
            stopped_at = time.monotonic()
            try:
                await self._controller.driver.stop_video_recording()
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                notes.append(f"stopping the recording failed: {exc}")
            segments = recording_segments(walk.recording_dir)
            if segments:
                try:
                    video = await render_recording(walk, segments, stopped_at, out)
                except (OSError, RuntimeError, ValueError) as exc:
                    notes.append(f"the recording could not be rendered ({exc}); used a slideshow")
            else:
                notes.append("the recorder produced no video; used a slideshow")
        else:
            notes.append(f"no recording ({walk.recording_note}); used a slideshow")
        if video is None:
            try:
                video = await render_slideshow(walk, out)
            except (OSError, RuntimeError, ValueError) as exc:
                notes.append(f"no video: {exc}")
        index = walk.write_index(video)
        lines = [f"Walkthrough '{walk.name}': {len(walk.steps)} step(s)."]
        if video is not None:
            lines.append(f"Video: file://{video}")
        lines += [f"Steps: file://{index}", f"Folder: file://{walk.directory}"]
        lines += [f"Note: {n}" for n in notes]
        return "\n".join(lines)

    # --- logs ---

    def _mark_step(self, label: str):
        self._step_no += 1
        if self._net is not None:
            self._net.mark(self._step_no, label)
        if self._logs is None:
            return None if self._net is None else _StepOnly(self._step_no)
        return self._logs.mark(self._step_no, label)

    def _with_logs(self, outcome: ActOutcome, mark) -> ActOutcome:
        if mark is None:
            return outcome
        outcome.step = mark.step
        if self._logs is not None and hasattr(mark, "at"):
            outcome.log_lines = self._logs.count_since(mark.at)
        if self._net is not None:
            net_mark = next((m for m in self._net._marks if m.step == mark.step), None)
            if net_mark is not None:
                outcome.net_requests = self._net.count_since(net_mark.at)
        return outcome

    # --- network ---

    @property
    def net(self) -> NetCapture | None:
        return self._net

    async def start_net(self, port: int = 8080) -> str:
        """Start mitmdump for this session; returns the status and the device setup steps."""
        if self._net is not None:
            await self.stop_net()
        command, source = net_command(port)
        if command is None:
            raise RuntimeError(source)
        log_path = get_temp_dir("net") / f"harness_{self.device_id or 'booted'}.jsonl"
        capture = NetCapture(command, source=source, log_path=log_path)
        await capture.start()
        self._net = capture
        return (
            capture.status()
            + "\n"
            + setup_instructions(
                port, is_simulator=self.is_simulator, device_id=self.device_id or "booted"
            )
        )

    async def stop_net(self) -> str | None:
        if self._net is None:
            return None
        await self._net.stop()
        status = self._net.status()
        on_phone = not self.is_simulator
        self._net = None
        if on_phone:
            status += (
                " Turn the phone's Wi-Fi proxy off now (Settings > Wi-Fi > (i) > Configure"
                " Proxy > Off), or it has no internet."
            )
        return status

    @property
    def logs(self) -> LogCapture | None:
        return self._logs

    async def start_logs(
        self, *, bundle_id: str | None = None, process: str | None = None, mode: str = "stream"
    ) -> str:
        """Start the device log stream for this session; returns a status line."""
        if self._logs is not None:
            await self.stop_logs()
        command, source = log_command(
            self.device_id or "booted",
            is_simulator=self.is_simulator,
            bundle_id=bundle_id,
            process=process,
            mode=mode,
        )
        if command is None:
            raise RuntimeError(source)
        from lunatap.harness.logs import process_name

        # One file per capture: appending to one file per device kept
        # yesterday's lines at the top of today's evidence.
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"{time.time() % 1:.3f}"[1:]
        file_path = get_temp_dir("logs") / f"harness_{self.device_id or 'booted'}_{stamp}.log"
        capture = LogCapture(
            command, source=source, process=process_name(bundle_id, process), file_path=file_path
        )
        await capture.start()
        self._logs = capture
        if mode == "launch":
            self._last = None  # the app was relaunched; the cached tree is gone
        return capture.status()

    async def stop_logs(self) -> str | None:
        if self._logs is None:
            return None
        await self._logs.stop()
        status = self._logs.status()
        self._logs = None
        return status

    def _is_alert_button(self, target: Any) -> bool:
        element = self._element_for(target)
        return element is not None and element.get("class") == ALERT_BUTTON_CLASS

    def _element_for(self, target: Any) -> dict[str, Any] | None:
        """The last observation's element an index or exact text names, if any."""
        if self._last is None or isinstance(target, (list, tuple)):
            return None
        elements = self._last.elements
        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            return next((e for e in elements if e.get("index") == int(target)), None)
        if isinstance(target, str) and target.strip():
            wanted = target.strip().lower()
            exact = [e for e in elements if str(e.get("text") or "").strip().lower() == wanted]
            return exact[0] if len(exact) == 1 else None
        return None

    async def _act_on_alert(self, before: Observation | None, button: Any) -> ActOutcome:
        """Press a button on the alert on screen through WebDriverAgent."""
        client = self._wda_client
        if client is None:
            return ActOutcome(
                False, "Alerts need the WebDriverAgent backend (no alert endpoint here).", "failed"
            )
        label: str | None = None
        if button is not None:
            element = self._element_for(button)
            label = str(element.get("text")) if element is not None else str(button).strip()
        try:
            await client.alert_accept(label)
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            return ActOutcome(False, f"Alert button {label or '(default)'} failed: {exc}", "failed")
        after = await self.observe(settle_ms=self._settle_ms)
        changed = before is None or (
            after.title != before.title or after.labels() != before.labels()
        )
        message = f"Pressed alert button '{label}'." if label else "Accepted the alert."
        return ActOutcome(True, message, observation=after, screen_changed=changed)

    def _resolve_target(
        self, target: Any, prefer_input: bool = False
    ) -> tuple[tuple[int, int], bool] | str:
        """The pixel to act on and whether it came from the last observation, or an error."""
        if isinstance(target, (list, tuple)) and len(target) == 2:
            try:
                return (int(target[0]), int(target[1])), False
            except (TypeError, ValueError):
                return "A coordinate target must be two integers: [x, y] in screenshot pixels."
        if self._last is None:
            return "No observation yet: call observe first, then pick an element by index or text."
        elements = self._last.elements
        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            index = int(target)
            element = next((e for e in elements if e.get("index") == index), None)
            if element is None:
                return f"No element [{index}] on the last observation (1..{len(elements)})."
            return self._point_of(element), True
        if isinstance(target, str) and target.strip():
            wanted = target.strip().lower()
            exact = [e for e in elements if str(e.get("text") or "").strip().lower() == wanted]
            found = exact or [
                e for e in elements if wanted in str(e.get("text") or "").strip().lower()
            ]
            if not found:
                return f"No element with text '{target}' on the last observation."
            if len(found) > 1:
                # OCR only stands in for text the tree lacks; when the tree has
                # this text too, the tree's element is the target (live: Port
                # in Configure Proxy matched an OCR label and the TextField).
                tree_found = [e for e in found if not e.get("is_ocr")]
                found = tree_found or found
            if len(found) > 1 and prefer_input:
                inputs = [e for e in found if e.get("class") in _INPUT_CLASSES]
                found = inputs or found
            if len(found) > 1:
                found = _outermost(found)
            if len(found) > 1:
                control = _shared_control(found, elements)
                if control is not None:
                    found = [control]
            if len(found) > 1:
                listed = "; ".join(
                    f"[{e.get('index')}] {e.get('class')} '{e.get('text')}'" for e in found
                )
                return f"'{target}' matches several elements, pick one by index: {listed}."
            return self._point_of(found[0]), True
        return "Give a target: an element index, its text, or [x, y] pixels."

    @staticmethod
    def _point_of(element: dict[str, Any]) -> tuple[int, int]:
        point = element.get("hit_point") or element.get("center")
        return int(point[0]), int(point[1])

    async def _refuse_if_screen_changed(
        self, before: Observation, point: tuple[int, int]
    ) -> ActOutcome | None:
        try:
            live_b64 = await self._screenshot_b64()
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            # The device decides; a failed check is not a refusal.
            logger.warning(f"Harness pre-tap screenshot failed: {exc}")
            return None
        if target_region_unchanged(
            base64.b64decode(before.screenshot_b64), base64.b64decode(live_b64), point, point
        ):
            return None
        after = await self.observe()
        return ActOutcome(
            False,
            "The screen changed around the target since the last observation;"
            " choose again from the current one.",
            "screen_changed",
            observation=after,
        )

    async def _settled(self, after: Observation) -> Observation:
        """Re-read a just-changed screen until it is known to be still.

        Two checks, cheapest first. The screenshot is taken after the tree, so
        when OCR finds the tree's labels somewhere else the tree is stale; this
        costs nothing more. Then a tree-only read (no screenshot, no OCR) must
        agree with the reply. A map view can keep the tree still while the page
        slides in (QA, Part 10), so the first check catches what the second
        misses. Only a moving screen costs a full re-read.
        """
        read = getattr(self._controller.driver, "hierarchy_elements", None)
        tolerance = SETTLE_TOLERANCE * after.height
        for _ in range(SETTLE_CHECKS):
            if after.pixel_shift is not None and after.pixel_shift > tolerance:
                reason = f"tree and screenshot {after.pixel_shift:.0f} px apart"
            elif read is None or not after.raw:
                return after
            else:
                try:
                    fresh = await read()
                except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                    logger.debug(f"Harness settle check skipped: {exc}")
                    return after
                shift = layout_shift(after.raw, fresh) if fresh else 0.0
                if shift is not None and shift <= tolerance:
                    return after
                reason = f"tree moved {shift:.0f} px" if shift is not None else "tree unmatched"
            logger.info(f"Harness: the screen was still moving ({reason}); reading it again")
            await asyncio.sleep(0.3)
            after = await self.observe()
        return after

    async def _screenshot_b64(self) -> str:
        """Pixels only: WDA drivers skip the hierarchy read, the slow half of a capture."""
        screenshot_only = getattr(self._controller.driver, "screenshot_only", None)
        if screenshot_only is None:
            return await self._actuator.take_screenshot()
        return base64.b64encode(await screenshot_only()).decode("ascii")

    def _normalized(self, point: tuple[int, int]) -> tuple[int, int]:
        width = self._last.width if self._last else 1080
        height = self._last.height if self._last else 2400
        return (
            max(0, min(1000, round(point[0] * 1000 / max(1, width)))),
            max(0, min(1000, round(point[1] * 1000 / max(1, height)))),
        )

    async def _dispatch(
        self,
        action: str,
        point: tuple[int, int] | None,
        text: str | None,
        direction: str | None,
        key: str | None,
        app: str | None,
        url: str | None,
        duration_ms: int | None,
    ) -> ActionResult:
        actuator = self._actuator
        if action in ("tap", "long_press"):
            if point is None:
                raise ValueError(f"'{action}' needs a target.")
            nx, ny = self._normalized(point)
            if action == "tap":
                return await actuator.click(nx, ny)
            return await actuator.long_press(nx, ny, duration_ms=duration_ms or 1000)
        if action == "type":
            if text is None:
                raise ValueError("'type' needs text.")
            where = self._normalized(point) if point is not None else None
            return await actuator.input_text(text, where, clear_exist=False)
        if action == "swipe":
            path = _SWIPES.get((direction or "up").lower())
            if path is None:
                raise ValueError("'swipe' direction must be up, down, left or right.")
            return await actuator.swipe(path[0], path[1], duration_ms or 400)
        if action == "press_key":
            if not key:
                raise ValueError("'press_key' needs a key (HOME, BACK, ENTER, ...).")
            return await actuator.press_key(key)
        if action in ("open_app", "stop_app"):
            if not app:
                raise ValueError(f"'{action}' needs an app name or bundle id.")
            return await actuator.manage_app("launch" if action == "open_app" else "stop", app)
        if action == "wait":
            return await actuator.wait_for_delay(duration_ms or 1000)
        if action == "open_link":
            if not url:
                raise ValueError("'open_link' needs a url.")
            return await actuator.open_link(url)
        raise ValueError(f"Unknown action '{action}'.")

    # --- do_step ---

    async def do_step(self, step: str) -> StepOutcome:
        """Settle one plan step in code: text match, then Clef, then tap and verify."""
        started = time.monotonic()
        before = self._last if self._last is not None else await self.observe()
        decision, reason = await self._fast_path.judge(
            step, before.elements, before.screenshot_b64, before.width, before.height
        )
        if decision is None:
            return StepOutcome("undecided", reason, None, before)
        element = before.elements[decision.index - 1]
        tapped = str(element.get("text") or "")
        recorder, self.recorder = self.recorder, None  # the step, not its tap, is recorded
        try:
            outcome = await self.act("tap", target=element.get("index"))
        finally:
            self.recorder = recorder
        if not outcome.ok:
            return StepOutcome("blocked", outcome.message, None, outcome.observation)
        if self.walkthrough is not None:
            self.walkthrough.recaption(step, since=started)
        after = outcome.observation
        assert after is not None
        if self.recorder is not None:
            self.recorder.record("do_step", step=step, screen=after.title)
        reached = step_reached(
            step, tapped, before.elements, after.elements, after.width, after.height
        )
        if reached:
            return StepOutcome(
                "done", f"the screen now shows '{reached}' ({reason})", tapped, after
            )
        return StepOutcome(
            "tapped", f"tapped by {reason}; the new screen does not name the step", tapped, after
        )

    # --- wait_for ---

    async def wait_for(
        self, text: str, timeout_ms: int = 10000, state: str = "appear"
    ) -> ActOutcome:
        """Wait for ``text`` to appear or disappear; on timeout, say what is on screen."""
        result = await self._actuator.wait_for_text(text, state, timeout_ms)
        if result.ok:
            if self.recorder is not None:
                self.recorder.record("wait_for", text=text, state=state, timeout_ms=timeout_ms)
            self._last = None  # whatever was cached predates what the wait saw
            return ActOutcome(True, result.message, "ok")
        if self.recorder is not None:
            self.recorder.skipped.append(f"wait_for '{text}' {state} (timed out while recording)")
        after = await self.observe()
        return ActOutcome(False, result.message, "timeout", observation=after)
