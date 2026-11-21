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

# Installs what Lunatap needs on a Mac: Xcode (checked, not installed), uv,
# and a system ffmpeg (for ffprobe; the Python env already bundles ffmpeg).
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

if [ "$(uname -s)" != "Darwin" ]; then
    echo -e "${YELLOW}Lunatap drives iOS simulators and devices, which needs macOS with Xcode.${NC}"
    exit 1
fi

if xcrun simctl help >/dev/null 2>&1; then
    echo -e "${GREEN}✓ $(xcodebuild -version 2>/dev/null | head -n 1)${NC}"
else
    echo -e "${YELLOW}Xcode not found. Install it from the App Store, open it once, then run:${NC}"
    echo "  sudo xcode-select -s /Applications/Xcode.app"
fi

if ! command -v brew >/dev/null 2>&1; then
    echo -e "${YELLOW}Homebrew not found; install it from https://brew.sh to add ffmpeg.${NC}"
elif ! command -v ffprobe >/dev/null 2>&1; then
    echo -e "${CYAN}Installing ffmpeg (provides ffprobe)...${NC}"
    HOMEBREW_NO_AUTO_UPDATE=1 brew install ffmpeg
else
    echo -e "${GREEN}✓ ffmpeg/ffprobe${NC}"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo -e "${CYAN}Installing uv...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
else
    echo -e "${GREEN}✓ uv $(uv --version | cut -d' ' -f2)${NC}"
fi

echo -e "${GREEN}Done. Next: uv sync --dev, then bash scripts/setup_ios_wda_simulator.sh (Xcode 15-26).${NC}"
