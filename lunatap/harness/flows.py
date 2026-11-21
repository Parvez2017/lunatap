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

"""Saved flows: record a harness session, replay it on the next build.

A flow is a JSON file of the calls an exploratory session made (``act``,
``do_step``, ``wait_for``, ``assert``) with targets stored as text or step
wording, never as a bare element index, so a small layout change does not
break it. Replaying stops at the first step or check that fails and reports
the screen at that point. A folder of flows is the first CI target
(``lunatap flow run <dir>`` exits non-zero on the first failing check).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lunatap.harness.session import HarnessSession, Observation

FLOWS_DIR_ENV = "LUNATAP_FLOWS_DIR"
KINDS = ("act", "do_step", "wait_for", "assert")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def flows_dir() -> Path:
    """Where flows are saved: ``$LUNATAP_FLOWS_DIR``, else ``flows/`` next to Lunatap's traces dir.

    Never the caller's working directory: the MCP server runs inside the app
    repository under test, and flows are Lunatap's files.
    """
    override = os.environ.get(FLOWS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    from lunatap.config.paths import get_default_traces_path

    return get_default_traces_path().parent / "flows"


def flow_path(name: str) -> Path:
    """The file for ``name``: an existing path as given, else ``<flows dir>/<name>.json``."""
    candidate = Path(name).expanduser()
    if candidate.suffix == ".json" or candidate.is_absolute() or candidate.exists():
        return candidate
    return flows_dir() / f"{_NAME_RE.sub('-', name.strip()).strip('-') or 'flow'}.json"


@dataclass
class FlowStep:
    kind: str  # act, do_step, wait_for, assert
    params: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind == "act":
            action = self.params.get("action")
            bits = [
                f"{key}={value!r}"
                for key, value in self.params.items()
                if key not in ("action", "screen") and value is not None
            ]
            line = f"act {action}" + (f" ({', '.join(bits)})" if bits else "")
            if isinstance(self.params.get("target"), list):
                line += " [replays by coordinates: fragile]"
            return line + self._expected_screen()
        if self.kind == "do_step":
            return f"do_step {self.params.get('step')!r}" + self._expected_screen()
        if self.kind == "wait_for":
            return f"wait_for {self.params.get('text')!r} to {self.params.get('state', 'appear')}"
        return f"assert {self.params.get('text')!r} {self.params.get('state', 'present')}"

    def _expected_screen(self) -> str:
        screen = self.params.get("screen")
        return f" -> '{screen}'" if screen else ""


@dataclass
class Flow:
    name: str
    steps: list[FlowStep] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    device: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "created": self.created,
                "device": self.device,
                "steps": [{"kind": s.kind, **s.params} for s in self.steps],
            },
            indent=2,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> Flow:
        data = json.loads(text)
        steps = []
        for raw in data.get("steps") or []:
            if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
                raise ValueError(f"flow step must have kind in {KINDS}: {raw!r}")
            params = {k: v for k, v in raw.items() if k != "kind"}
            steps.append(FlowStep(str(raw["kind"]), params))
        return cls(
            name=str(data.get("name") or "flow"),
            steps=steps,
            created=str(data.get("created") or ""),
            device=data.get("device"),
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or flow_path(self.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, name_or_path: str | Path) -> Flow:
        path = flow_path(str(name_or_path))
        if not path.is_file():
            raise FileNotFoundError(f"No flow at {path} (saved flows: {list_flows()})")
        return cls.from_json(path.read_text(encoding="utf-8"))

    def describe(self) -> str:
        lines = [f"Flow '{self.name}': {len(self.steps)} step(s)"]
        lines += [f"  {n}. {step.describe()}" for n, step in enumerate(self.steps, 1)]
        return "\n".join(lines)


def list_flows() -> list[str]:
    directory = flows_dir()
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


class FlowRecorder:
    """Collects the calls of a session while recording is on."""

    def __init__(self, name: str, device: str | None = None) -> None:
        self.flow = Flow(name=name, device=device)
        # Checks and waits that failed while recording: a saved flow must
        # replay green on the build it was recorded on.
        self.skipped: list[str] = []

    def record(self, kind: str, **params: Any) -> None:
        clean = {k: v for k, v in params.items() if v is not None}
        self.flow.steps.append(FlowStep(kind, clean))


@dataclass
class FlowResult:
    name: str
    passed: bool
    steps_run: int
    total: int
    seconds: float
    failed_step: FlowStep | None = None
    message: str = ""
    observation: Observation | None = None

    def describe(self) -> str:
        head = (
            f"{'PASS' if self.passed else 'FAIL'} flow '{self.name}': "
            f"{self.steps_run}/{self.total} step(s) in {self.seconds:.1f}s"
        )
        if self.failed_step is not None:
            head += (
                f"\nFailed at step {self.steps_run}: {self.failed_step.describe()} — {self.message}"
            )
        elif self.message:
            head += f"\n{self.message}"
        if self.observation is not None:
            head += "\n" + self.observation.describe()
        return head


async def run_flow(session: HarnessSession, flow: Flow) -> FlowResult:
    """Replay ``flow`` on ``session``; stop at the first failing step or check."""
    started = time.monotonic()
    was_recording = session.recorder
    session.recorder = None  # replaying must not re-record
    try:
        if session.last_observation is None:
            await session.observe()
        for number, step in enumerate(flow.steps, 1):
            ok, message, observation = await _run_step(session, step)
            if not ok:
                return FlowResult(
                    flow.name,
                    False,
                    number,
                    len(flow.steps),
                    time.monotonic() - started,
                    step,
                    message,
                    observation,
                )
        return FlowResult(
            flow.name, True, len(flow.steps), len(flow.steps), time.monotonic() - started
        )
    finally:
        session.recorder = was_recording


async def _run_step(session: HarnessSession, step: FlowStep) -> tuple[bool, str, Any]:
    params = dict(step.params)
    expected_screen = params.pop("screen", None)
    if step.kind == "act":
        action = str(params.pop("action", "tap"))
        outcome = await session.act(action, **params)
        if (
            not outcome.ok
            and outcome.code == "bad_target"
            and isinstance(params.get("target"), str)
        ):
            # The label moved or is now ambiguous: let the step decider find it.
            fallback = await session.do_step(f"Tap {params['target']}")
            if fallback.status in ("done", "tapped"):
                return _landed(
                    expected_screen,
                    f"{outcome.message} Recovered via do_step.",
                    fallback.observation,
                )
            return (
                False,
                f"{outcome.message} do_step fallback: {fallback.reason}",
                fallback.observation,
            )
        if not outcome.ok:
            return False, outcome.message, outcome.observation
        return _landed(expected_screen, outcome.message, outcome.observation)
    if step.kind == "do_step":
        result = await session.do_step(str(params.get("step") or ""))
        if result.status not in ("done", "tapped"):
            return False, f"{result.status}: {result.reason}", result.observation
        return _landed(expected_screen, f"{result.status}: {result.reason}", result.observation)
    if step.kind == "wait_for":
        outcome = await session.wait_for(
            str(params.get("text") or ""),
            timeout_ms=int(params.get("timeout_ms") or 10000),
            state=str(params.get("state") or "appear"),
        )
        return outcome.ok, outcome.message, outcome.observation
    if step.kind == "assert":
        outcome = await session.assert_text(
            str(params.get("text") or ""),
            state=str(params.get("state") or "present"),
            timeout_ms=int(params.get("timeout_ms") or 5000),
        )
        return outcome.ok, outcome.message, outcome.observation
    return False, f"unknown step kind {step.kind!r}", None


def _landed(expected: Any, message: str, observation: Any) -> tuple[bool, str, Any]:
    """Pass only when the screen the recording reached is the one reached now."""
    if not expected or observation is None:
        return True, message, observation
    got = observation.title
    if got == expected:
        return True, message, observation
    return False, f"landed on the wrong screen: expected '{expected}', got '{got}'", observation


def flows_in(path: str | Path) -> list[Path]:
    """The flow files a CLI argument names: one file, or every ``*.json`` in a folder."""
    target = Path(path).expanduser()
    if target.is_dir():
        return sorted(target.glob("*.json"))
    if target.is_file():
        return [target]
    resolved = flow_path(str(path))
    return [resolved] if resolved.is_file() else []
