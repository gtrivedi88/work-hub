# Work Hub for Claude Code

Work Hub is a personal tracker and a Claude Code plugin for technical writers. It builds a personal work brief from Jira, GitLab, and GitHub, uses connected Slack and AEM MCP tools when available, and hands a selected documentation ticket to an **already installed** `docs-tools:docs-orchestrator`. Work Hub does not contain or reinstall docs-orchestrator.

## Install once

You need Claude Code, Python 3.9+, and access to the sources you want to include. Install your organization's docs-tools plugin separately if you want the docs-orchestrator handoff.

```bash
claude plugin marketplace add gtrivedi88/work-hub
claude plugin install work-hub@gtrivedi88-work-hub
```

Or, if you have cloned this repository and want to test that checkout:

```bash
claude --plugin-dir ./plugins/work-hub
```

Start a **new Claude Code session** in any project after installation. Its SessionStart hook copies starter Markdown files into `~/.claude/work-hub/` and adds one `@work-hub/CLAUDE.md` import to `~/.claude/CLAUDE.md`. Existing files are never replaced. Updates to the plugin can add new starter files without erasing your edits. The files in `~/.claude` are yours, not part of the shared repository. The hook does not create a credential file.

## Track work and personal commitments

Use `/work-hub:tracker` to capture a task, follow a PR, check your agenda, or open the dashboard. Local features work with Python and the installed plugin. You can use the same tracker directly from a checkout:

```bash
python3 plugins/work-hub/skills/tracker/scripts/tracker.py --help
python3 plugins/work-hub/skills/tracker/scripts/tracker.py dashboard
```

Use `--workspace personal` before a command to work in a separate inbox. The default workspace is `work`. The dashboard opens a local browser view and prints its URL in the terminal. Open an item to edit dates, next actions, reminders, tags, and color or to manage dependencies. Use **Saved views** to keep filters and **Watches** to add, check, pause, or resume a watch. Status labels identify overdue, waiting, and completed work alongside the color cues. Keep the dashboard process running while you use it.

| Track | Commands |
| --- | --- |
| Tasks and commitments | `add` for a quick entry, `capture` for a reviewed JSON batch, or `track` for a connected Jira, GitHub, or GitLab item. Capture from email, Slack, or meeting notes through a connected MCP tool or with the source permalink. |
| Inbox and history | `list --view today`, `list --q`, `view save`, `history`, and `export` in JSON, CSV, Markdown, or iCalendar. |
| Projects and releases | `add --kind project`, `--kind milestone`, `--parent`, `depend`, and `template save` / `template use` for reusable checklists. |
| Habits and reminders | `add --kind habit --due YYYY-MM-DD --repeat weekdays`, `status ID done` or `skipped`, `--remind`, and `snooze`. Completing or skipping an occurrence creates the next one. Habit dates that elapsed without a check-in appear as missed, and the current occurrence remains available. You can mark a missed date done or skipped from its history or the All view. |
| Calendar and time | `calendar add NAME FILE-OR-URL`, `agenda`, `--scheduled`, `--estimate`, `time start`, `time stop`, `time log`, and `review`. |
| Watches | `watch add TITLE TARGET --type page\|feed\|release\|review\|radar`, `watch pause`, `watch resume`, and `watch list`. Use `--contains` for a keyword on supported watch types. |

For scheduled checks between Claude sessions, leave `tracker.py run --desktop` running in a terminal. It checks due reminders and watches, records notifications locally, and attempts desktop delivery on macOS or Linux. Add `--connected` to refresh your signed-in Jira, GitHub, and GitLab items as well. `check` runs a single check, and `notifications list` shows unread alerts. A watch's first successful check establishes its baseline. GitHub release and PR watches use public URLs. For private reviews, use `track` on a review from your connected brief. A Radar watch reads the local `signal-bank.jsonl` file or a directory containing it.

On macOS, opt in to background checks with `tracker.py service enable --desktop`. The login agent runs reminders and watches at the selected interval, with a five-minute default. Use `service status` or the dashboard's **Watches** view to check recent runs and missed intervals. Use `service disable` to remove the schedule. If you update the plugin, disable and enable the job again so it uses the new installed script. On other platforms, run the terminal worker.

Back up the tracker, Jira history, and personal Markdown with `tracker.py backup create /path/to/work-hub.zip`. Restore and validate into a new directory with `tracker.py backup restore /path/to/work-hub.zip /path/to/new-profile`, then point `PERSONAL_WORKFLOW_HOME` at that directory. The archive contains private work data. It excludes credential files and the transient intake snapshot.

Calendar sources can be local `.ics` files or HTTP(S) iCalendar feeds. The agenda handles all-day and timed events and common daily, weekly, monthly, and yearly recurrence rules. It reports unsupported rules and source errors. Use iCalendar export to transfer Work Hub deadlines and time blocks into another calendar.

## Use your existing `.env`

Work Hub detects and reads the same `.env` locations as docs-tools: the active project’s `.env` and `~/.env`. Installing docs-orchestrator alone does not create a `.env`. The project file overrides the global file; process environment overrides both. Keep using the credentials you already configured for docs-orchestrator. Work Hub never asks for a token in chat. If your existing file is in one repository, start Claude Code in that repository; if you want those credentials available across projects, put them in `~/.env`. Never commit credential files. Work Hub no longer creates its own `.env`; for users of version 0.1.0, it accepts that legacy file only as a lower-priority fallback while they consolidate credentials.

```dotenv
JIRA_URL=https://your-team.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=...
GITHUB_TOKEN=...
```

The example shows the supported keys; add only any missing keys to your **existing** file. If neither file exists, the connected brief reports that no compatible file was found and prints the exact recommended `~/.env` path, the project alternative, and the keys for each service. Create the file if you want to connect those services. Use `KEY=value` assignments, protect the file with `chmod 600 ~/.env` (or the project path), and never commit it. `JIRA_URL` and `GITLAB_URL` tell Work Hub which hosts to query. `GITHUB_API_URL` is optional for GitHub Enterprise. Slack and AEM use the MCP servers already connected to Claude Code; Work Hub does not request their tokens.

If an existing file uses `email:`, `jira_token:`, or `gitlab_token:` lines, Work Hub can read those as a legacy convenience and reports the file and line in its brief. Change them to `JIRA_EMAIL=`, `JIRA_API_TOKEN=`, and `GITLAB_TOKEN=` in that **same file** before invoking docs-orchestrator, because docs-tools expects standard `.env` assignments. Work Hub's `config` check reports each key's source and warns if an inherited process value overrides an updated file. Open a new terminal and Claude Code session after changing a shell-loaded `.env`; the existing session keeps its old environment. The report lists paths and key names, never credential values.

## Use it in one Claude session

```text
/work-hub:personal-workflow
```

Claude checks which sources are healthy, groups related signals, presents priorities with evidence and uncertainty, and asks you which task to take. Choose a live Jira issue. Work Hub then follows its linked issue graph through the installed Jira reader and starts `docs-tools:docs-orchestrator` in local draft mode. Before the draft starts, the current Claude Code directory must be the documentation repository for that ticket. If you chose work from another repository, use `/cd <path-to-docs-repo>` inside the same session. The installed orchestrator ignores `--draft` when combined with `--docs-repo-path`, so Work Hub does not use that combination. The orchestrator's installation location does not matter; Claude Code resolves its installed skill by name. The brief can still run if docs-tools is absent; the draft handoff will say what is missing.

Your normal corrections and clear preferences can update `persona.md`, a project file, a stage checklist, or memory through `/work-hub:personalize-workflow`. The plugin nudges this on corrections. The personal files are deliberately small and should evolve as you work.

### Pick up where you left off

Work Hub keeps a private history for each Jira ticket under the authenticated Jira account. On the next brief it rechecks selected tickets even when they are absent from the open list. It shows the last local draft stage, a saved next action, and verified changes to Jira and confirmed GitHub PRs or GitLab MRs. Selecting the same ticket again keeps the saved stage and next action.

For example, you can ask Work Hub to remember that a draft is ready for a flag-name check and wait for the implementation MR to merge. On a later brief, Work Hub can verify the merge directly and bring the draft back with the saved next action. You can set a check date for a wait as well. If a dependency cannot be verified, the brief reports its status as unknown. A closed review without a merge is shown separately.

For Jira options, Work Hub records why it recommended the work, along with any explicit deferral and later action. It can report whether you picked up or drafted the work, and can record a verified docs merge or publication when there is a source link. Jira completion, implementation review status, and the documentation outcome each have their own evidence.

Work Hub compares intake snapshots only when the same account and source had complete coverage in both runs. The first successful run after a change of account or an upgrade from an unscoped snapshot establishes a baseline.

## What it collects

The bundled Python script reads assigned, watched, and reported Jira issues across projects; GitLab assigned and review MRs, issues, and pending to-dos; and GitHub assigned, mentioned, created, and review-requested issues and PRs. It checks authentication before reporting an empty source, caps each query, and labels incomplete coverage. It does not write to these services. Claude may add Slack and AEM context through installed MCP connections. Its score is a clue; Claude's final triage distinguishes urgency, impact, dependencies, and ease of the next action.

## Upgrade and privacy

`claude plugin marketplace update gtrivedi88-work-hub` refreshes the catalog; update the installed plugin through Claude Code's plugin manager. Your `~/.claude/work-hub` Markdown files and existing `.env` stay local. The general tracker stores items, views, watches, and time logs in `~/.claude/work-hub/cache/tracker.sqlite3`. Work Hub stores the latest intake in `~/.claude/work-hub/cache/latest-snapshot.json` and work history in `work-history.sqlite3` in the same private directory. If you have a legacy `tracked-jira.json`, the first successful verification of a matching issue on the original Jira instance imports its saved stage. Remove the snapshot to reset intake comparisons. To reset work history, remove the database and any legacy tracking file. Review the repository code before installing, especially the SessionStart hook that writes to your home directory.

## Development

```bash
claude plugin validate .
claude plugin validate ./plugins/work-hub
python3 -m pytest -q tests
```
