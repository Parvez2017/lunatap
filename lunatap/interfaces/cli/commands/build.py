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

"""``lunatap build``: build an Xcode app for a simulator, install it and launch it."""

from __future__ import annotations

import asyncio
from typing import Annotated

from rich.console import Console
import typer

from lunatap.harness import build as builder

console = Console()
WATCH_POLL_SECONDS = 1.0
WATCH_SETTLE_SECONDS = 0.6


async def _build_once(
    project: builder.Project, scheme: str, sim: dict, configuration: str, launch: bool
) -> bool:
    udid = str(sim["udid"])
    console.print(f"[bold]Building[/bold] {project.path.name} ({scheme}) for {sim.get('name')}…")
    result = await builder.build(project, scheme, udid, configuration)
    if not result.ok or result.app_path is None or result.bundle_id is None:
        console.print(f"[red]{result.describe()}[/red]")
        return False
    console.print(f"[green]{result.describe()}[/green]")
    console.print(await builder.install_and_launch(udid, result.app_path, result.bundle_id, launch))
    return True


async def _main(
    path: str,
    scheme: str | None,
    device_serial: str | None,
    configuration: str,
    launch: bool,
    watch: bool,
) -> bool:
    project = builder.find_project(path)
    sim = await builder.resolve_simulator(device_serial)
    await builder.ensure_booted(str(sim["udid"]))
    chosen = await builder.choose_scheme(project, scheme)
    ok = await _build_once(project, chosen, sim, configuration, launch)
    if not watch:
        return ok
    root = project.path.parent
    console.print(f"Watching {root} for changes. Ctrl+C to stop.")
    before = await asyncio.to_thread(builder.source_snapshot, root)
    while True:
        await asyncio.sleep(WATCH_POLL_SECONDS)
        now = await asyncio.to_thread(builder.source_snapshot, root)
        changed = builder.changed_files(before, now)
        if not changed:
            continue
        # Let an editor finish writing (save-all, formatters) before building.
        await asyncio.sleep(WATCH_SETTLE_SECONDS)
        before = await asyncio.to_thread(builder.source_snapshot, root)
        names = ", ".join(c.rsplit("/", 1)[-1] for c in changed[:3])
        console.print(f"\nChanged: {names}{' …' if len(changed) > 3 else ''}")
        await _build_once(project, chosen, sim, configuration, launch)


def build_command(
    path: Annotated[str, typer.Argument(help="App folder, .xcodeproj or .xcworkspace.")] = ".",
    scheme: Annotated[str | None, typer.Option("--scheme", "-s", help="Scheme to build.")] = None,
    device_serial: Annotated[
        str | None, typer.Option("--device-serial", "-d", help="Simulator UDID.")
    ] = None,
    configuration: Annotated[str, typer.Option("--configuration", "-c")] = "Debug",
    launch: Annotated[
        bool, typer.Option("--launch/--no-launch", help="Launch after install.")
    ] = True,
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Rebuild when a source file changes.")
    ] = False,
) -> None:
    """Build the app for a simulator, install it and launch it; --watch rebuilds on save."""
    try:
        ok = asyncio.run(_main(path, scheme, device_serial, configuration, launch, watch))
    except builder.BuildError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    if not ok:
        raise typer.Exit(1)
