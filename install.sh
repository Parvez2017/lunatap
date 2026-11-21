#!/usr/bin/env bash
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
#
# One-command install of Lunatap and its MCP server:
#
#   curl -fsSL https://raw.githubusercontent.com/Parvez2017/lunatap/main/install.sh | bash
#
# Options (after `bash -s --` when piped):
#   --ide <name>   claude, cursor, codex, windsurf, vscode, cline, antigravity or all.
#                  Repeatable. Default: every IDE found on this Mac (Claude Code if none).
#   --dir <path>   Where to install. Default: $LUNATAP_HOME or ~/.lunatap.
#   --branch <b>   Git branch or tag to install. Default: main.
#   --skip-wda     Do not build WebDriverAgent for simulators (Xcode 15–26).
#   --no-mcp       Install Lunatap but do not register it with any IDE.
#
# Running it again updates an existing install.

# Everything is inside main(), so a partly downloaded script never runs.
main() {
  set -euo pipefail

  local repo="${LUNATAP_REPO:-https://github.com/Parvez2017/lunatap.git}"
  local dir="${LUNATAP_HOME:-$HOME/.lunatap}"
  local branch="main"
  local skip_wda=0 no_mcp=0
  local ides=()

  while [ $# -gt 0 ]; do
    case "$1" in
      --ide) ides+=("${2:?--ide needs a value}"); shift 2 ;;
      --dir) dir="${2:?--dir needs a value}"; shift 2 ;;
      --branch) branch="${2:?--branch needs a value}"; shift 2 ;;
      --skip-wda) skip_wda=1; shift ;;
      --no-mcp) no_mcp=1; shift ;;
      -h|--help) usage; return 0 ;;
      *) fail "unknown option: $1 (see --help)" ;;
    esac
  done

  say "Installing Lunatap into $dir"

  # 1. macOS with Xcode.
  [ "$(uname -s)" = "Darwin" ] || fail "Lunatap needs macOS (it drives iOS through Xcode)."
  command -v xcrun >/dev/null && xcrun --find simctl >/dev/null 2>&1 \
    || fail "Xcode is not installed or not selected. Install Xcode from the App Store, then run: sudo xcode-select -s /Applications/Xcode.app"
  command -v git >/dev/null || fail "git is missing. Run: xcode-select --install"
  local xcode_major
  xcode_major="$(xcodebuild -version 2>/dev/null | awk 'NR==1 {split($2, v, "."); print v[1]}')"
  [ -n "$xcode_major" ] && [ "$xcode_major" -ge 15 ] \
    || fail "Lunatap needs Xcode 15 or later (found: ${xcode_major:-none})."
  ok "Xcode $xcode_major"

  # 2. uv, which also provides Python 3.12+.
  if ! command -v uv >/dev/null; then
    say "Installing uv (Python package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null || fail "uv installed but is not on PATH. Open a new terminal and run this again."
  fi
  ok "uv $(uv --version | awk '{print $2}')"

  # 3. Get or update the code.
  if [ -d "$dir/.git" ]; then
    say "Updating the existing install"
    git -C "$dir" fetch --quiet origin "$branch"
    git -C "$dir" checkout --quiet "$branch"
    git -C "$dir" pull --quiet --ff-only origin "$branch" \
      || fail "$dir has local changes; update it by hand or choose another --dir."
  elif [ -e "$dir" ]; then
    fail "$dir exists and is not a Lunatap checkout. Choose another --dir."
  else
    git clone --quiet --depth 1 --branch "$branch" "$repo" "$dir"
  fi
  ok "Code at $dir ($(git -C "$dir" rev-parse --short HEAD))"

  # 4. Python environment.
  say "Installing Python dependencies (first run takes a minute)"
  (cd "$dir" && uv sync --quiet --frozen --no-dev)
  ok "Python environment at $dir/.venv"

  # 5. A `lunatap` command on PATH.
  local bin_dir="${LUNATAP_BIN_DIR:-$HOME/.local/bin}"
  mkdir -p "$bin_dir"
  [ -x "$dir/.venv/bin/lunatap" ] || fail "the lunatap command was not built; see the uv output above."
  ln -sf "$dir/.venv/bin/lunatap" "$bin_dir/lunatap"
  ok "lunatap command at $bin_dir/lunatap"
  case ":$PATH:" in *":$bin_dir:"*) ;; *) warn "$bin_dir is not on your PATH. Add it: echo 'export PATH=\"$bin_dir:\$PATH\"' >> ~/.zshrc" ;; esac

  # 6. WebDriverAgent for simulators on Xcode 15–26 (Xcode 27 uses its own tools).
  if [ "$skip_wda" = 0 ] && [ "$xcode_major" -lt 27 ]; then
    if ls "$HOME/.artemis/wda/simulator/"*.xctestrun >/dev/null 2>&1; then
      ok "WebDriverAgent for simulators already built"
    else
      say "Building WebDriverAgent for simulators (one time, a few minutes)"
      if bash "$dir/scripts/setup_ios_wda_simulator.sh" >"$dir/wda-build.log" 2>&1; then
        ok "WebDriverAgent built"
      else
        warn "WebDriverAgent build failed; see $dir/wda-build.log. Retry with: bash $dir/scripts/setup_ios_wda_simulator.sh"
      fi
    fi
  fi

  # 7. Register the MCP server with the IDEs.
  if [ "$no_mcp" = 0 ]; then
    if [ ${#ides[@]} -eq 0 ]; then
      detect_ides
    fi
    local ide
    for ide in "${ides[@]}"; do
      if (cd "$dir" && "$dir/.venv/bin/lunatap" mcp --install "$ide" >/dev/null 2>&1); then
        ok "MCP server added to $ide"
      else
        warn "Could not add the MCP server to $ide. Run: cd $dir && uv run lunatap mcp --install $ide"
      fi
    done
  fi

  cat <<EOF

$(bold "Lunatap is installed.")

  Next:
  1. Restart your IDE (or reconnect its MCP servers) so it loads the Lunatap MCP server
     (listed as "artemis" for now).
  2. Boot a simulator:   open -a Simulator
  3. Ask your IDE agent: "Open Settings on the booted simulator and tell me the iOS version."

  Check the setup:  lunatap doctor
  Update later:     run this installer again
  Real iPhones and models: https://github.com/Parvez2017/lunatap#real-iphones

EOF
}

usage() {
  cat <<'EOF'
Install or update Lunatap and register its MCP server with your IDEs.

  --ide <name>   claude, cursor, codex, windsurf, vscode, cline, antigravity or all.
                 Repeatable. Default: every IDE found on this Mac (Claude Code if none).
  --dir <path>   Where to install. Default: $LUNATAP_HOME or ~/.lunatap.
  --branch <b>   Git branch or tag to install. Default: main.
  --skip-wda     Do not build WebDriverAgent for simulators (Xcode 15-26).
  --no-mcp       Install Lunatap but do not register it with any IDE.
EOF
}

detect_ides() {
  # Sets the caller's `ides` array to the IDEs found on this Mac.
  { command -v claude >/dev/null || [ -e "$HOME/.claude.json" ] || [ -d "/Applications/Claude.app" ]; } && ides+=(claude)
  { [ -d "$HOME/.cursor" ] || [ -d "/Applications/Cursor.app" ]; } && ides+=(cursor)
  { command -v codex >/dev/null || [ -d "$HOME/.codex" ]; } && ides+=(codex)
  { [ -d "$HOME/.codeium/windsurf" ] || [ -d "/Applications/Windsurf.app" ]; } && ides+=(windsurf)
  [ -d "/Applications/Visual Studio Code.app" ] && ides+=(vscode)
  [ ${#ides[@]} -gt 0 ] || ides=(claude)
  return 0
}

bold() { printf '\033[1m%s\033[0m' "$1"; }
say() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail() { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

main "$@"
