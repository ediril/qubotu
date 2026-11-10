# Running Qubotu

```sh
qubotu open [--port N]             # open Qubotu in your browser
qubotu serve [--open] [--port N]   # run in the foreground
qubotu service install             # start at login (macOS); restarts it if already installed
qubotu service uninstall           # stop starting at login
qubotu --version
```

Agents run only while Qubotu is running and your Mac is awake. Scheduled runs missed during sleep collapse into one run on wake.

Your data lives in `~/.qubotu/` (set `QUBOTU_HOME` to move it, and `QUBOTU_PORT` to change the port):
- `qubotu.db`: agents, chats, outputs, and memory.
- `agents/<id>/`: each agent's working folder, including its `NOTES.md`.
- `models/`: the local embedding model.
- `browser/sites/<id>/`: the Chrome profile holding each site's login.
- `daemon.log`: the log when running as a login service.

## Troubleshooting
- **Runs fail with a Codex error:** run `codex login`, then check **Settings** in Qubotu.
- **The login service can't find `codex`:** the service keeps the `PATH` from when it was installed. After installing or moving Codex, run `qubotu service install` again.
- **"Qubotu is already running":** another Qubotu, often the login service, is using the same data folder. Use that one, or stop it with `qubotu service uninstall`. If you start `qubotu serve` first, the login service waits and takes over when you stop it.
- **An agent can't run a tool installed in your home folder,** such as node through nvm or Python through uv: agents can't read your home folder. Tools in `/usr`, `/opt/homebrew`, and other system locations work.
- **Anything else:** check `~/.qubotu/daemon.log`.
