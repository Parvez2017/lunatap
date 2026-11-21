#!/usr/bin/env bash
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

# Build WebDriverAgent once for iOS simulators (no code signing needed).
#
# Produces a WebDriverAgentRunner .xctestrun under ~/.artemis/wda/simulator that
# WdaSimulatorDriver discovers automatically and hosts with
# 'xcodebuild test-without-building'. Re-run after switching Xcode versions.
#
# Usage: bash scripts/setup_ios_wda_simulator.sh [WDA_VERSION]
#   WDA_VERSION  appium/WebDriverAgent tag (default below). Env overrides:
#   LUNATAP_WDA_HOME  install root (default ~/.artemis/wda)
set -euo pipefail

WDA_VERSION="${1:-v16.15.3}"
WDA_HOME="${LUNATAP_WDA_HOME:-${ARTEMIS_WDA_HOME:-$HOME/.artemis/wda}}"
SRC_DIR="$WDA_HOME/src/WebDriverAgent-$WDA_VERSION"
OUT_DIR="$WDA_HOME/simulator"

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "iOS simulators require macOS."
command -v xcodebuild >/dev/null 2>&1 || fail "Install Xcode (15 or newer)."
command -v git >/dev/null 2>&1 || fail "git is required."

version=$(xcodebuild -version | awk '/^Xcode / {print $2; exit}')
[[ "$version" =~ ^([0-9]+) ]] || fail "Cannot parse Xcode version: $version"
(( BASH_REMATCH[1] >= 15 )) || fail "Xcode 15 or newer is required (selected: $version)."
xcrun simctl list runtimes | grep -q 'iOS' || fail "No iOS simulator runtime installed (Xcode > Settings > Components)."

if [[ ! -d "$SRC_DIR/.git" ]]; then
    printf 'Cloning appium/WebDriverAgent %s ...\n' "$WDA_VERSION"
    mkdir -p "$(dirname "$SRC_DIR")"
    git clone --quiet --depth 1 --branch "$WDA_VERSION" \
        https://github.com/appium/WebDriverAgent.git "$SRC_DIR"
fi

printf 'Building WebDriverAgentRunner for iOS Simulator with Xcode %s (takes a few minutes) ...\n' "$version"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
LOG="$WDA_HOME/build-simulator.log"
if ! xcodebuild build-for-testing \
    -project "$SRC_DIR/WebDriverAgent.xcodeproj" \
    -scheme WebDriverAgentRunner \
    -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath "$OUT_DIR/DerivedData" \
    CODE_SIGNING_ALLOWED=NO >"$LOG" 2>&1; then
    tail -n 30 "$LOG" >&2
    fail "WebDriverAgent build failed; full log: $LOG"
fi

xctestrun=$(find "$OUT_DIR/DerivedData/Build/Products" -maxdepth 1 -name '*iphonesimulator*.xctestrun' | head -n 1)
[[ -n "$xctestrun" ]] || fail "Build succeeded but no simulator .xctestrun was produced; see $LOG"

cat <<EOF
WebDriverAgent for simulators is ready:
  $xctestrun

Artemis finds it automatically. To run a task on a simulator:
  xcrun simctl list devices available
  uv run lunatap run "Open Settings and inspect General" \\
      --platform ios --standalone --device-serial <SIMULATOR-UDID> --profile flash

Options: LUNATAP_IOS_WDA_PORT=<port> to run several simulators at once;
LUNATAP_IOS_SIM_BACKEND=wda|xcode to force a backend (auto picks by Xcode version).
EOF
