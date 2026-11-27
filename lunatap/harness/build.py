# Copyright 2026 The Lunatap Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build an Xcode app for a simulator, install it and launch it.

The loop an agent needs after editing code: ``xcodebuild`` for the chosen
simulator, compile errors back as ``file:line: message``, then the fresh
``.app`` installed and launched, so the next observation shows the change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import time
from typing import Literal

from lunatap.drivers.ios.discovery import list_ios_simulators

#: Where builds go, one folder per project, outside the user's tree.
BUILD_ROOT_ENV = "LUNATAP_BUILD_DIR"
DEFAULT_BUILD_ROOT = Path("~/Library/Caches/lunatap/DerivedData").expanduser()
BUILD_TIMEOUT = 30 * 60.0
_LIST_TIMEOUT = 120.0
_SIMCTL_TIMEOUT = 120.0
#: Folders never searched for projects or watched for changes.
_SKIP_DIRS = {".git", "build", "DerivedData", "Pods", "Carthage", "node_modules", ".build"}
#: Files whose change triggers a rebuild in watch mode.
_SOURCE_SUFFIXES = {
    ".swift", ".m", ".mm", ".h", ".c", ".cpp", ".storyboard", ".xib", ".plist",
    ".strings", ".xcstrings", ".json", ".pbxproj", ".entitlements", ".metal",
}  # fmt: skip
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>/[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)? (?P<kind>error|warning): (?P<msg>.+)$"
)
_PLAIN_ERROR_RE = re.compile(r"^(?:error|xcodebuild: error|.*: error): (?P<msg>.+)$")


class BuildError(RuntimeError):
    """A build that could not start: no project, no scheme, no simulator."""


@dataclass(frozen=True)
class Project:
    kind: Literal["workspace", "project"]
    path: Path

    @property
    def name(self) -> str:
        return self.path.stem

    def args(self) -> list[str]:
        return [f"-{self.kind}", str(self.path)]


@dataclass
class BuildResult:
    ok: bool
    seconds: float
    scheme: str
    project: Project
    log_path: Path
    errors: list[str] = field(default_factory=list)
    warnings: int = 0
    app_path: Path | None = None
    bundle_id: str | None = None

    def describe(self, max_errors: int = 20) -> str:
        where = f"{self.project.path.name}, scheme {self.scheme}"
        if self.ok:
            extra = f", {self.warnings} warning(s)" if self.warnings else ""
            return (
                f"Built {where} in {self.seconds:.0f}s{extra}: {self.app_path} ({self.bundle_id})."
            )
        shown = self.errors[:max_errors]
        more = len(self.errors) - len(shown)
        lines = [f"BUILD FAILED: {where} after {self.seconds:.0f}s, {len(self.errors)} error(s):"]
        lines += [f"  {error}" for error in shown]
        if more > 0:
            lines.append(f"  … and {more} more")
        lines.append(f"Full log: {self.log_path}")
        return "\n".join(lines)


# --- Finding the project and scheme ---------------------------------------


def find_project(path: str | Path) -> Project:
    """The workspace or project at ``path``, or the one inside that folder.

    A workspace wins over a project (CocoaPods and multi-project setups
    build through it); an ``.xcodeproj``'s own inner workspace never counts.
    """
    target = Path(path).expanduser().resolve()
    if target.suffix == ".xcworkspace" and target.is_dir():
        return Project("workspace", target)
    if target.suffix == ".xcodeproj" and target.is_dir():
        return Project("project", target)
    if not target.is_dir():
        raise BuildError(f"{target} is not a folder, an .xcodeproj or an .xcworkspace.")
    for depth in (0, 1, 2):
        workspaces = _children(target, ".xcworkspace", depth)
        projects = _children(target, ".xcodeproj", depth)
        if len(workspaces) == 1:
            return Project("workspace", workspaces[0])
        if len(workspaces) > 1:
            raise BuildError(_pick_one("workspaces", workspaces))
        if len(projects) == 1:
            return Project("project", projects[0])
        if len(projects) > 1:
            raise BuildError(_pick_one("projects", projects))
    raise BuildError(f"No .xcodeproj or .xcworkspace in {target} (searched 2 levels down).")


def _children(root: Path, suffix: str, depth: int) -> list[Path]:
    found: list[Path] = []
    level = [root]
    for _ in range(depth):
        level = [
            child
            for folder in level
            for child in sorted(folder.iterdir())
            if child.is_dir() and child.name not in _SKIP_DIRS and child.suffix not in (".xcodeproj", ".xcworkspace")
            and not child.name.startswith(".")
        ]  # fmt: skip
    for folder in level:
        found += [c for c in sorted(folder.iterdir()) if c.suffix == suffix and c.is_dir()]
    return found


def _pick_one(kind: str, paths: list[Path]) -> str:
    names = ", ".join(str(p) for p in paths)
    return f"Several {kind} found ({names}); pass the one to build as the path."


async def list_schemes(project: Project) -> list[str]:
    code, out = await _run("xcodebuild", "-list", "-json", *project.args(), timeout=_LIST_TIMEOUT)
    if code != 0:
        raise BuildError(f"xcodebuild -list failed for {project.path}:\n{_tail(out)}")
    start = out.find("{")
    try:
        data = json.loads(out[start:]) if start >= 0 else {}
    except json.JSONDecodeError as exc:
        raise BuildError(f"Could not read the schemes of {project.path}: {exc}") from exc
    return list((data.get(project.kind) or {}).get("schemes") or [])


async def choose_scheme(project: Project, scheme: str | None) -> str:
    schemes = await list_schemes(project)
    if scheme:
        if schemes and scheme not in schemes:
            raise BuildError(
                f"No scheme {scheme!r} in {project.path.name}; schemes: {', '.join(schemes)}."
            )
        return scheme
    if len(schemes) == 1:
        return schemes[0]
    if project.name in schemes:
        return project.name
    if not schemes:
        raise BuildError(
            f"{project.path.name} has no shared schemes; open it in Xcode once or pass scheme=."
        )
    raise BuildError(
        f"{project.path.name} has several schemes ({', '.join(schemes)}); pass scheme=."
    )


# --- Simulator -------------------------------------------------------------


async def resolve_simulator(device_serial: str | None) -> dict:
    """The simulator to build for: the one named, else the only booted one."""
    simulators = await list_ios_simulators() or []
    if device_serial:
        wanted = device_serial.strip().upper()
        for sim in simulators:
            if str(sim.get("udid", "")).upper() == wanted:
                return sim
        raise BuildError(
            f"{device_serial} is not a simulator on this Mac. Building for a real iPhone "
            "is not supported yet; build for a simulator, or build and install it in Xcode."
        )
    booted = [sim for sim in simulators if sim.get("state") == "Booted"]
    if len(booted) == 1:
        return booted[0]
    if not booted:
        raise BuildError(
            "No simulator is booted: boot one (open -a Simulator) or pass device_serial."
        )
    names = ", ".join(f"{s.get('name')} ({s.get('udid')})" for s in booted)
    raise BuildError(f"Several simulators are booted ({names}); pass device_serial.")


async def ensure_booted(udid: str) -> None:
    code, out = await _run("xcrun", "simctl", "bootstatus", udid, "-b", timeout=_SIMCTL_TIMEOUT)
    if code != 0:
        raise BuildError(f"Could not boot simulator {udid}:\n{_tail(out)}")


# --- Build -------------------------------------------------------------------


def derived_data_for(project: Project) -> Path:
    root = Path(os.environ.get(BUILD_ROOT_ENV) or DEFAULT_BUILD_ROOT).expanduser()
    digest = hashlib.sha1(str(project.path).encode()).hexdigest()[:8]
    return root / f"{project.name}-{digest}"


async def build(
    project: Project,
    scheme: str,
    udid: str,
    configuration: str = "Debug",
) -> BuildResult:
    """``xcodebuild build`` for one simulator, with the errors parsed out."""
    derived = derived_data_for(project)
    derived.mkdir(parents=True, exist_ok=True)
    log_path = derived / "last-build.log"
    argv = [
        "xcodebuild", *project.args(), "-scheme", scheme, "-configuration", configuration,
        "-destination", f"id={udid}", "-derivedDataPath", str(derived),
        "-skipPackagePluginValidation", "-skipMacroValidation", "build",
    ]  # fmt: skip
    started = time.monotonic()
    code, out = await _run(*argv, timeout=BUILD_TIMEOUT)
    seconds = time.monotonic() - started
    log_path.write_text(out, encoding="utf-8", errors="replace")
    errors, warnings = parse_diagnostics(out)
    result = BuildResult(
        ok=code == 0, seconds=seconds, scheme=scheme, project=project, log_path=log_path,
        errors=errors, warnings=warnings,
    )  # fmt: skip
    if not result.ok:
        if not result.errors:
            result.errors = [_tail(out, 15)]
        return result
    app = built_app(derived, configuration)
    if app is None:
        result.ok = False
        result.errors = [
            f"The build succeeded but no simulator .app is under {derived}/Build/Products."
        ]
        return result
    result.app_path, result.bundle_id = app
    return result


def parse_diagnostics(output: str) -> tuple[list[str], int]:
    """Unique compiler errors as ``file:line: message``, and the warning count."""
    errors: list[str] = []
    warnings: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        match = _DIAGNOSTIC_RE.match(line)
        if match:
            where = f"{match['file']}:{match['line']}"
            entry = f"{where}: {match['msg']}"
            if match["kind"] == "warning":
                warnings.add(entry)
            elif entry not in errors:
                errors.append(entry)
            continue
        plain = _PLAIN_ERROR_RE.match(line)
        if plain and "BUILD FAILED" not in line:
            entry = plain["msg"].strip()
            if entry and entry not in errors:
                errors.append(entry)
    return errors, len(warnings)


def built_app(derived: Path, configuration: str) -> tuple[Path, str] | None:
    """The newest simulator ``.app`` in the build products, and its bundle id."""
    products = derived / "Build" / "Products"
    apps = sorted(
        (p for p in products.glob(f"{configuration}-iphonesimulator/*.app") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for app in apps:
        info = app / "Info.plist"
        try:
            bundle_id = plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if bundle_id:
            return app, str(bundle_id)
    return None


async def install_and_launch(udid: str, app: Path, bundle_id: str, launch: bool = True) -> str:
    await ensure_booted(udid)
    code, out = await _run("xcrun", "simctl", "install", udid, str(app), timeout=_SIMCTL_TIMEOUT)
    if code != 0:
        raise BuildError(f"Installing {app.name} failed:\n{_tail(out)}")
    if not launch:
        return f"Installed {bundle_id}."
    await _run("xcrun", "simctl", "terminate", udid, bundle_id, timeout=_SIMCTL_TIMEOUT)
    code, out = await _run("xcrun", "simctl", "launch", udid, bundle_id, timeout=_SIMCTL_TIMEOUT)
    if code != 0:
        raise BuildError(f"Installed {bundle_id}, but launching it failed:\n{_tail(out)}")
    return f"Installed and launched {bundle_id}."


# --- Watch -------------------------------------------------------------------


def source_snapshot(root: Path) -> dict[str, float]:
    """Modification times of the files that matter for a rebuild."""
    snapshot: dict[str, float] = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if Path(name).suffix in _SOURCE_SUFFIXES:
                path = os.path.join(folder, name)
                try:
                    snapshot[path] = os.stat(path).st_mtime
                except OSError:
                    continue
    return snapshot


def changed_files(before: dict[str, float], after: dict[str, float]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


# --- Helpers -----------------------------------------------------------------


async def _run(*argv: str, timeout: float) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"{argv[0]} timed out after {timeout:.0f}s"
    return process.returncode or 0, out.decode(errors="replace")


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
