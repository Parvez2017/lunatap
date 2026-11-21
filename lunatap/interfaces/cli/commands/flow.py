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

"""``lunatap flow``: replay saved harness flows, the first CI target."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from rich.console import Console
import typer

from lunatap.harness.flows import Flow, flows_dir, flows_in, list_flows, run_flow

flow_app = typer.Typer(help="Record-and-replay flows from harness sessions.")
console = Console()


async def _open_session(device_serial: str | None):
    """A connected harness session (kept separate so tests can swap it)."""
    from lunatap.harness.session import HarnessSession
    from lunatap.mcp.device_controllers import get_controller

    controller = await asyncio.to_thread(
        get_controller, device_serial=device_serial, target_platform="ios"
    )
    session = HarnessSession(controller)
    await session.start()
    return session


async def _run_all(paths: list[Path], device_serial: str | None) -> list:
    session = await _open_session(device_serial)
    results = []
    try:
        for path in paths:
            flow = Flow.load(path)
            result = await run_flow(session, flow)
            results.append(result)
            console.print(result.describe().split("\n", 1)[0])
            if not result.passed:
                console.print(result.describe())
    finally:
        await session.close()
    return results


@flow_app.command("run")
def run_command(
    target: Annotated[str, typer.Argument(help="A flow name, a .json file, or a folder of flows.")],
    device_serial: Annotated[
        str | None, typer.Option("--device-serial", "-d", help="Simulator or device UDID.")
    ] = None,
) -> None:
    """Replay one flow or every flow in a folder; exit 1 on the first failing flow."""
    paths = flows_in(target)
    if not paths:
        console.print(f"[red]No flows at {target!r}[/red] (flows dir: {flows_dir()})")
        raise typer.Exit(code=2)
    results = asyncio.run(_run_all(paths, device_serial))
    failed = [r for r in results if not r.passed]
    console.print(
        f"{len(results) - len(failed)}/{len(results)} flow(s) passed"
        + (f"; failed: {', '.join(r.name for r in failed)}" if failed else "")
    )
    if failed:
        raise typer.Exit(code=1)


@flow_app.command("list")
def list_command() -> None:
    """List the saved flows."""
    names = list_flows()
    console.print(f"Flows in {flows_dir()}: " + (", ".join(names) if names else "none"))
