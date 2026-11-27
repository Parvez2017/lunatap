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

"""Build from source: project and scheme choice, error parsing, the .app, watching."""

import plistlib

import pytest

from lunatap.harness import build as builder
from lunatap.harness.build import BuildError, Project

SIM = {
    "udid": "D4499A27-A89A-4C42-96FE-2538DA8B5A61",
    "name": "iPhone 17 Pro Max",
    "state": "Booted",
}
OTHER = {"udid": "EC683597-CDC9-4A7F-94F9-A2C8F3708D82", "name": "iPhone 17e", "state": "Booted"}


def mkdirs(root, *names):
    for name in names:
        (root / name).mkdir(parents=True)


# --- find_project -----------------------------------------------------------


def test_a_workspace_wins_over_a_project(tmp_path):
    mkdirs(tmp_path, "App.xcodeproj/project.xcworkspace", "App.xcworkspace")
    found = builder.find_project(tmp_path)
    assert found == Project("workspace", (tmp_path / "App.xcworkspace").resolve())


def test_a_projects_inner_workspace_does_not_count(tmp_path):
    mkdirs(tmp_path, "App.xcodeproj/project.xcworkspace")
    assert builder.find_project(tmp_path).kind == "project"


def test_a_project_one_level_down_is_found(tmp_path):
    """e.g. a monorepo's apps/ios folder holding the project."""
    mkdirs(tmp_path, "ios/App.xcodeproj", "node_modules/x/Other.xcodeproj")
    found = builder.find_project(tmp_path)
    assert found.path.name == "App.xcodeproj"


def test_two_projects_ask_which(tmp_path):
    mkdirs(tmp_path, "A.xcodeproj", "B.xcodeproj")
    with pytest.raises(BuildError, match="Several projects"):
        builder.find_project(tmp_path)


def test_no_project_is_explained(tmp_path):
    with pytest.raises(BuildError, match="No .xcodeproj"):
        builder.find_project(tmp_path)


def test_a_project_path_is_used_as_given(tmp_path):
    mkdirs(tmp_path, "App.xcodeproj", "App.xcworkspace")
    assert builder.find_project(tmp_path / "App.xcodeproj").kind == "project"


# --- choose_scheme ----------------------------------------------------------


@pytest.fixture
def schemes(monkeypatch):
    found = []

    async def fake(project):
        return list(found)

    monkeypatch.setattr(builder, "list_schemes", fake)
    return found


@pytest.mark.asyncio
async def test_the_only_scheme_is_chosen(schemes):
    schemes += ["App"]
    assert (
        await builder.choose_scheme(Project("project", builder.Path("/x/App.xcodeproj")), None)
        == "App"
    )


@pytest.mark.asyncio
async def test_the_scheme_named_after_the_project_is_chosen(schemes):
    schemes += ["AppTests", "App", "Widget"]
    assert (
        await builder.choose_scheme(Project("project", builder.Path("/x/App.xcodeproj")), None)
        == "App"
    )


@pytest.mark.asyncio
async def test_several_schemes_ask_which(schemes):
    schemes += ["Staging", "Production"]
    with pytest.raises(BuildError, match="Staging, Production"):
        await builder.choose_scheme(Project("project", builder.Path("/x/App.xcodeproj")), None)


@pytest.mark.asyncio
async def test_an_unknown_scheme_lists_the_real_ones(schemes):
    schemes += ["App"]
    with pytest.raises(BuildError, match="schemes: App"):
        await builder.choose_scheme(Project("project", builder.Path("/x/App.xcodeproj")), "Nope")


# --- resolve_simulator ------------------------------------------------------


def simulators(monkeypatch, sims):
    async def fake():
        return sims

    monkeypatch.setattr(builder, "list_ios_simulators", fake)


@pytest.mark.asyncio
async def test_the_only_booted_simulator_is_used(monkeypatch):
    simulators(monkeypatch, [SIM, {**OTHER, "state": "Shutdown"}])
    assert (await builder.resolve_simulator(None))["udid"] == SIM["udid"]


@pytest.mark.asyncio
async def test_two_booted_simulators_ask_which(monkeypatch):
    simulators(monkeypatch, [SIM, OTHER])
    with pytest.raises(BuildError, match="Several simulators"):
        await builder.resolve_simulator(None)


@pytest.mark.asyncio
async def test_a_real_iphone_is_refused_for_now(monkeypatch):
    simulators(monkeypatch, [SIM])
    with pytest.raises(BuildError, match="real iPhone"):
        await builder.resolve_simulator("00008140-00000000000000AB")


@pytest.mark.asyncio
async def test_a_named_simulator_matches_case_insensitively(monkeypatch):
    simulators(monkeypatch, [SIM, OTHER])
    assert (await builder.resolve_simulator(OTHER["udid"].lower()))["name"] == "iPhone 17e"


# --- parse_diagnostics and built_app ---------------------------------------

LOG = """
CompileSwift normal arm64 /src/App/ContentView.swift
/src/App/ContentView.swift:17:44: error: cannot convert value of type 'String' to expected argument type 'Int'
        Button("Tap me") { taps += "one" }
                                   ^
/src/App/ContentView.swift:17:44: error: cannot convert value of type 'String' to expected argument type 'Int'
/src/App/Model.swift:3:9: warning: variable 'x' was never used
/src/App/Model.swift:3:9: warning: variable 'x' was never used
error: Signing for "App" requires a development team.
** BUILD FAILED **
"""


def test_errors_are_unique_file_line_messages_and_warnings_are_counted():
    errors, warnings = builder.parse_diagnostics(LOG)
    assert errors == [
        "/src/App/ContentView.swift:17: cannot convert value of type 'String' to expected argument type 'Int'",
        'Signing for "App" requires a development team.',
    ]
    assert warnings == 1


def test_built_app_reads_the_bundle_id(tmp_path):
    app = tmp_path / "Build/Products/Debug-iphonesimulator/App.app"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.App"}))
    assert builder.built_app(tmp_path, "Debug") == (app, "com.example.App")
    assert builder.built_app(tmp_path, "Release") is None


def test_a_failed_build_describes_its_errors_and_log(tmp_path):
    result = builder.BuildResult(
        ok=False, seconds=4.2, scheme="App", project=Project("project", tmp_path / "App.xcodeproj"),
        log_path=tmp_path / "last-build.log", errors=["/a.swift:1: boom"] * 3,
    )  # fmt: skip
    text = result.describe(max_errors=2)
    assert text.startswith("BUILD FAILED") and "3 error(s)" in text
    assert "… and 1 more" in text and "last-build.log" in text


def test_derived_data_is_per_project_and_outside_the_tree(monkeypatch, tmp_path):
    monkeypatch.setenv(builder.BUILD_ROOT_ENV, str(tmp_path))
    a = builder.derived_data_for(Project("project", tmp_path / "a/App.xcodeproj"))
    b = builder.derived_data_for(Project("project", tmp_path / "b/App.xcodeproj"))
    assert a != b and a.parent == tmp_path and a.name.startswith("App-")


# --- watch ------------------------------------------------------------------


def test_watch_sees_source_changes_but_not_build_output(tmp_path):
    mkdirs(tmp_path, "App", "build", ".git")
    source = tmp_path / "App/View.swift"
    source.write_text("a")
    (tmp_path / "build/Generated.swift").write_text("x")
    (tmp_path / "App/notes.txt").write_text("x")
    before = builder.source_snapshot(tmp_path)
    assert list(before) == [str(source)]
    import os

    os.utime(source, (1, 1))
    (tmp_path / "App/New.swift").write_text("b")
    after = builder.source_snapshot(tmp_path)
    assert builder.changed_files(before, after) == [str(tmp_path / "App/New.swift"), str(source)]
