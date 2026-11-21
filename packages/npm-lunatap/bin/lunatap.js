#!/usr/bin/env node
// Copyright 2026 The Lunatap Authors
// Licensed under the Apache License, Version 2.0. See LICENSE in the repository.
//
//   npx lunatap                  install or update Lunatap and its MCP server
//   npx lunatap install --ide cursor
//   npx lunatap doctor           any other command runs the installed lunatap CLI

"use strict";
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const args = process.argv.slice(2);
const home = process.env.LUNATAP_HOME || path.join(os.homedir(), ".lunatap");
const cli = path.join(home, ".venv", "bin", "lunatap");

function run(cmd, cmdArgs) {
  const result = spawnSync(cmd, cmdArgs, { stdio: "inherit" });
  if (result.error) {
    console.error(`lunatap: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

if (process.platform !== "darwin") {
  console.error("lunatap: Lunatap needs macOS with Xcode (it drives iOS simulators and iPhones).");
  process.exit(1);
}

const INSTALL_FLAGS = new Set(["--ide", "--dir", "--branch", "--skip-wda", "--no-mcp"]);
const isCommand = args[0] === "install" || args[0] === "update";
if (args.length === 0 || isCommand || INSTALL_FLAGS.has(args[0])) {
  const script = path.join(__dirname, "..", "install.sh");
  const rest = isCommand ? args.slice(1) : args;
  run("bash", [script, ...rest]);
} else if (fs.existsSync(cli)) {
  run(cli, args);
} else {
  console.error(`lunatap: not installed yet. Run: npx lunatap install`);
  process.exit(1);
}
