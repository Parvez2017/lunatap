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

# Read-only prerequisite checks. This script does not install tools, boot
# devices, select Xcode, or change Xcode's MCP permissions.
set -euo pipefail

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "iOS support requires macOS."

command -v xcodebuild >/dev/null 2>&1 || fail "Install Xcode (27+ native, or 15+ via WebDriverAgent)."
command -v xcrun >/dev/null 2>&1 || fail "Xcode's xcrun is unavailable."

if ! version_output=$(xcodebuild -version); then
    fail "Cannot read the selected Xcode version. Check xcode-select -p."
fi
version=$(printf '%s\n' "$version_output" | awk '/^Xcode / {print $2; exit}')
[[ "$version" =~ ^([0-9]+)(\.[0-9]+)*$ ]] || fail "Cannot parse Xcode version: $version"
major=${BASH_REMATCH[1]}
(( major >= 15 )) || fail "Xcode 15 or newer is required (selected: $version)."
# Xcode 27+ drives simulators natively through mcpbridge; older Xcode uses
# WebDriverAgent (LUNATAP_IOS_SIM_BACKEND=auto picks this automatically).
native=0
(( major >= 27 )) && native=1
backend="${LUNATAP_IOS_SIM_BACKEND:-${ARTEMIS_IOS_SIM_BACKEND:-auto}}"
[[ "$backend" == "xcode" ]] && native=1
[[ "$backend" == "wda" ]] && native=0

if ! simctl_path=$(xcrun --find simctl); then
    fail "The selected Xcode installation does not provide simctl."
fi
if (( native )); then
    if ! bridge_path=$(xcrun --find mcpbridge); then
        fail "The selected Xcode installation does not provide mcpbridge."
    fi
    printf 'Xcode %s (native simulator backend)\nsimctl: %s\nmcpbridge: %s\n\n' \
        "$version" "$simctl_path" "$bridge_path"
else
    printf 'Xcode %s (WebDriverAgent simulator backend)\nsimctl: %s\n' "$version" "$simctl_path"
    wda_xctestrun="${LUNATAP_IOS_WDA_XCTESTRUN:-${ARTEMIS_IOS_WDA_XCTESTRUN:-}}"
    if [[ -z "$wda_xctestrun" ]]; then
        wda_xctestrun=$(find "$HOME/.artemis/wda/simulator" -name '*iphonesimulator*.xctestrun' 2>/dev/null | head -n 1 || true)
    fi
    if [[ -n "$wda_xctestrun" ]]; then
        printf 'WebDriverAgent (simulator): %s\n\n' "$wda_xctestrun"
    else
        printf 'WebDriverAgent (simulator): not built yet. Run: bash scripts/setup_ios_wda_simulator.sh\n\n'
    fi
fi

if ! devices=$(xcrun simctl list devices available); then
    fail "Cannot query CoreSimulator. Run this check in a local terminal with access to Xcode's simulator services."
fi
printf '%s\n\n' "$devices"
if ! printf '%s\n' "$devices" | awk '
    /^-- iOS / { ios = 1; next }
    /^-- / { ios = 0 }
    ios && /\([[:xdigit:]-]+\) \((Booted|Shutdown)\)/ { found = 1 }
    END { exit !found }
'; then
    fail "No available iOS simulator found. Install an iOS runtime and create a simulator in Xcode."
fi

if (( native )); then
    printf 'Xcode MCP server status:\n'
    if ! xcrun mcp-server status; then
        printf 'Could not read MCP server status. Check Xcode MCP access manually.\n' >&2
    fi
fi

if ! devicectl_path=$(xcrun --find devicectl); then
    fail "The selected Xcode installation does not provide devicectl (needed for physical devices)."
fi
printf 'devicectl: %s\n\n' "$devicectl_path"

printf 'Physical devices (devicectl inventory):\n'
if physical=$(xcrun devicectl list devices 2>/dev/null); then
    printf '%s\n' "$physical" | awk 'NR > 2 && NF'
    # The table shows the connection state (connected / available (paired)),
    # not the word "paired" for every paired device.
    if ! printf '%s\n' "$physical" | grep -Eq 'connected|paired'; then
        printf 'No paired physical devices found. Pair over USB and tap Trust to use hardware.\n'
    fi
else
    printf 'Could not query CoreDevice. Physical-device runs need a paired, connected device.\n' >&2
fi

printf '\nToolchain and simulator inventory checks passed.\n'
if (( native )); then
    printf 'Before running Artemis, review and grant Xcode MCP access for your agent.\n'
    printf 'This check does not verify that device-interaction permission is granted.\n'
fi
printf 'Physical devices additionally need a WebDriverAgent runner installed; see docs/ios.md.\n'
printf 'See docs/ios.md and https://developer.apple.com/documentation/xcode/giving-external-agents-access-to-xcode\n'
