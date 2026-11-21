# lunatap (npm)

Installs [Lunatap](https://github.com/Parvez2017/lunatap), an AI agent that tests iOS apps, and registers its MCP server with your AI IDE.

```bash
npx lunatap                      # install or update, for every IDE found on this Mac
npx lunatap install --ide cursor # just one IDE
npx lunatap doctor               # any other command runs the installed lunatap CLI
```

Needs macOS with Xcode 15 or later. Lunatap itself is Python; this package runs the same installer as
`curl -fsSL https://raw.githubusercontent.com/Parvez2017/lunatap/main/install.sh | bash`,
which installs into `~/.lunatap` (or `$LUNATAP_HOME`).
