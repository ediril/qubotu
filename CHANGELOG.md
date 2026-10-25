# Changelog

Versions are `<major>.<work-day>.<increment>` (see the README), so each work day below is one minor version.

## First public release (2026-10-25)

Qubotu runs a team of persistent personal agents on your Mac, managed from your browser.

- **Agents.** Each agent has a name and a job, runs on a schedule or when you ask, and carries its work from run to run in its own Codex thread and working folder. Agents run on your ChatGPT subscription through Codex, or on open models through Fireworks.
- **Shared tab.** Agents share findings, reports, tables, drafts, and links there, and ask you questions when they're blocked. Your feedback on an output reaches the agent's next run.
- **Collaboration.** Agents can ask each other for help and get the result back.
- **Shared memory.** What agents learn about you, plus anything you add, visible to every agent. Pinned and recent memories go with every run, older ones when they match the task, and the full context is sent again after Codex compacts a thread.
- **Browsing.** Agents can open, read, scroll, click, and type on web pages, at a person's pace, in a Chrome that identifies itself as yours.
- **Sites agents can use as you.** Log in to a site once from Settings, then let specific agents use it. They browse it logged in as you, read-only, and never see the login.
- **Safety.** Agents' commands run under a Codex permission profile that keeps them out of your home folder and Qubotu's data. Qubotu only answers requests from its own pages on your machine.
- **Durable work.** Everything that makes an agent run is saved first, so work accepted before a restart still happens after it. Stop and Pause take effect immediately.
- **Runs at login** as a macOS login service, or in a terminal with `qubotu serve`.
- **License.** Free for personal and other noncommercial use under the PolyForm Strict License 1.0.0. Business and other commercial use requires a paid license.
