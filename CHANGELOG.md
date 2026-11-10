# Changelog

Versions are `<major>.<work-day>.<increment>` (see [docs/development.md](docs/development.md)), so each work day below is one minor version.

## Unreleased

- **Blocked agents stand out.** When an agent can't go on without you, a red bar at the top of every page says so, with the action right there. The agent shows **blocked** in the sidebar, and the browser tab's title counts blocked requests. Findings and other things that can wait stay in the Shared tab.
- **Agents can ask you to log in.** An agent that hits a login wall asks you to log in to that site. One click opens the Chrome window, or lets the agent use a login you already have, and the agent carries on once you're done.
- **Agents mark questions as blocking** only when they can't go on without the answer. Other questions wait in the Shared tab.
- **Agents can't use Qubotu's controls.** Qubotu now answers only browsers holding its key, which `qubotu open` hands over once per browser. Before, an agent's shell could call the UI and, for example, grant itself a site's login.
- **Logged-in sites are stricter.** No POST, PUT, or DELETE to any host (a site's API often lives on another domain), no WebSockets, no logout links, no pages from other sites after a redirect, and no service workers, even from the login window. Agents can't browse local or private addresses.
- **Logins resolve per site.** Closing a site's login window, from Settings or from a request, answers every agent waiting on that site. Only agents you approved, or that already had the site, get it, and only once the window closes, so declining or a failed window grants nothing. A request for `mobile.x.com` uses your `x.com` login.
- **Blocked shows next to run state,** so a blocked agent that's running or queued still shows **Stop run**. The red bar quotes the agent's reason as its own words, and names the site a new login would open. Unticking a site closes the agent's tabs on it.

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
