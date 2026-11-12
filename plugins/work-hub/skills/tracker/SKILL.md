---
name: tracker
description: Track standalone work and personal commitments, capture tasks from messages or notes, manage projects and habits, view calendars and dashboards, monitor changes, or review time and progress.
argument-hint: "[what to track or view]"
allowed-tools: Read, Bash
---

# Work Hub tracker

Use the bundled CLI for local tracker actions:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/tracker/scripts/tracker.py" --help
```

It works as soon as the plugin is loaded. It uses the private Work Hub profile, with `work` as the default workspace. Use `--workspace personal` before the command when the writer is tracking personal items. Use existing MCP connections for source content when available. The local tracker requires Python and no connector setup.

## Capture and find work

- Use `add` for standalone tasks, projects, milestones, checklists, and habits. Capture the writer's wording, a useful next action, and any known due date. Ask when a date or owner materially changes the item and the source is ambiguous.
- For email, Slack, meeting, or Markdown commitments, read only the requested source or connected surface. Confirm the commitment and its owner from the evidence. Capture short titles and exact source permalinks using `add --source ... --url ...` or the `capture` JSON import. Quote shell arguments safely. Keep message bodies, transcripts, and credentials out of the tracker. An external source is evidence for the task.
- Use `/work-hub:personal-workflow` to refresh Jira, GitHub, and GitLab, then use `track <ID-OR-URL> --brief-id <BRIEF-ID>` to retain a connected item and track its source status. Local status is controlled by the writer. Confirmed docs progress is shown alongside the connected ticket.
- Use `list --view inbox|today|waiting|upcoming|projects|all`, `list --q <QUERY>`, and `history <ID>`. Add `view save <NAME>` with filters to keep a reusable view. Use `export` for Markdown, JSON, CSV, or an iCalendar file.
- Use `status`, `edit`, `snooze`, and `link` to maintain items. For meaningful duplicates, reuse a canonical item and attach the other source. A repeat capture of the same permalink reuses its saved item. Use a stable `--reference` or JSON `reference` for separate commitments that share a source link.

## Plan and monitor

- Group work with `--parent <ID>`, connect blockers with `depend <ID> <BLOCKER-ID>`, and save reusable steps with `template save` and `template use`. `list --view projects` includes completion and waiting counts.
- Use `--repeat` on tasks or habits with a due date. Record each occurrence as done or skipped so its history stays available. Dates, reminders, and `--scheduled` time blocks use the system timezone unless the writer supplies an offset.
- Add a calendar with `calendar add <NAME> <FILE-OR-ICS-URL>` and use `agenda` to see the next appointments and planned work together. A subscribed calendar is read-only. Export task dates or time blocks with `export --format ics`.
- Use `watch add` for web text, RSS/Atom feeds, public GitHub releases or PR merges, or a local Radar signal bank. Use `watch pause`, `watch resume`, and `watch list` to inspect last checks. A first successful observation sets the baseline. Existing connected accounts support private GitHub and GitLab items through `track` and connected refreshes.
- Use `check` to run due checks once. Use `run --desktop` in a separate terminal for between-session reminders and watches. It stays running until interrupted. `--connected` also refreshes connected sources using that terminal's working directory and credentials. `notifications list` shows durable alerts.
- Use `time start`, `time stop`, or `time log` for actual effort and `--estimate` for planned minutes. Use `review` to summarize completions, time, waits, carryovers, and habit check-ins.
- Start `dashboard` when the writer wants a visual inbox, board, agenda, or weekly review. The dashboard command prints its local URL. The terminal process serves it until stopped.
