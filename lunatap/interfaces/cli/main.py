# Copyright 2026 Google LLC
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
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""ARTEMIS Unified Command Line Interface (CLI)."""

from typing import Annotated

from lunatap._version import __version__
from lunatap.interfaces.cli.commands.batch import batch_command
from lunatap.interfaces.cli.commands.doctor import doctor_command
from lunatap.interfaces.cli.commands.init import init_command
from lunatap.interfaces.cli.commands.mcp import mcp_command
from lunatap.interfaces.cli.commands.run import run_command
from lunatap.interfaces.cli.commands.server import server_app
from lunatap.interfaces.cli.commands.server_lifecycle import (
    restart_command,
    status_command,
    stop_command,
)
from lunatap.interfaces.cli.commands.build import build_command
from lunatap.interfaces.cli.commands.flow import flow_app
from lunatap.interfaces.cli.commands.trace import trace_app
from lunatap.interfaces.cli.commands.ui import ui_command
from third_party.mobile_use.utils.logger import get_logger
from rich.console import Console
from rich.panel import Panel
import typer

logger = get_logger(__name__)

app = typer.Typer(
    name="lunatap",
    help="🌙 Lunatap: an AI agent that tests iOS apps on simulators and iPhones.",
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=True,
)

# Register subcommands
app.command(name="ui", help="Launch the unified Showcase UI & Admin Console in your browser.")(
    ui_command
)
app.command(name="restart", help="Restart running Artemis Web UI & server.")(restart_command)
app.command(name="stop", help="Stop running Artemis Web UI & server.")(stop_command)
app.command(name="status", help="Display Artemis Web UI & server status.")(status_command)
app.command(name="run", help="Execute an autonomous task on a mobile device.")(run_command)
app.command(name="init", help="Interactive quickstart wizard to configure API keys & device.")(
    init_command
)
app.command(name="doctor", help="Check system prerequisites, device status, and configuration.")(
    doctor_command
)
app.command(name="batch", help="Execute a batch sequence of automation tasks.")(batch_command)
app.command(name="mcp", help="Start the Artemis Model Context Protocol (MCP) server.")(mcp_command)
app.add_typer(server_app, name="server", help="Cloud Run proxy and web dashboard server.")
app.add_typer(trace_app, name="trace", help="Inspect and query execution traces.")
app.command(
    name="build", help="Build an Xcode app for a simulator, install and launch it (--watch)."
)(build_command)
app.add_typer(flow_app, name="flow", help="Replay saved harness flows (record-and-replay QA).")


def version_callback(value: bool):
    if value:
        console = Console()
        console.print(
            Panel(
                f"[bold cyan]Lunatap[/bold cyan] v{__version__}\n"
                "[dim]AI testing agent for iOS[/dim]",
                title="🌙 Lunatap",
                expand=False,
            )
        )
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show Artemis version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
):
    """ARTEMIS Autonomous Mobile Agent CLI."""
    pass


def cli():
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    cli()
