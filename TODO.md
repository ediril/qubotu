# TODO

Planned work, in priority order. Tags say what each item touches. Items tagged `maybe` may never be built. Details get worked out when an item is picked up.

1. **"Needs you to log in"** `browser`: An agent that hits a login wall asks the user to log in to that site.

2. **Tool inventory** `tools`: A list of all tools, built-in and custom, where the user chooses which ones each agent may use. Sane defaults, so most agents need no changes.

3. **Custom tools** `tools`: A "new tool" workflow for adding custom code as a tool agents can use, such as reading an X list. Site-specific logic lives in tools, not in Qubotu's code.

4. **Sources in shared memory** `memory`: Bring files, folders, and web links into shared memory. Each source is scanned and imported, and rescanned when it changes, updating its memories instead of duplicating them.

5. **Projects** `agents`: Create a project, add agents to it, and talk with them together in the project's room, with a lead agent that merges their work.

6. **Claude Code runtime** `runtimes`: A second runtime that drives the `claude` CLI, so a Claude subscription can be used. Check Anthropic's terms first.

7. **Letting a site act** `browser`: Allow posting and other changes on a logged-in site, instead of read-only.

8. **Supermemory** `memory`: A possible replacement for the built-in memory. The user only ever sees the memory screen, whatever runs underneath.

9. **Native wrapper** `ui`: A desktop app (likely Tauri) with a menu-bar status icon. The daemon keeps running when the window closes.

10. **Run in the cloud** `maybe`: Run Qubotu on a VM so agents keep working when the laptop is off. Rakazo's approach is a good starting point.

11. **Docker isolation** `maybe`: Optionally run agents in containers instead of under the Codex permission profile.
