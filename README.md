# Work Hub for Claude Code

Work Hub is a companion plugin for technical writers. It builds a personal work brief from Jira, GitLab, and GitHub, uses connected Slack and AEM MCP tools when available, and hands a selected documentation ticket to an **already installed** `docs-tools:docs-orchestrator`. Work Hub does not contain or reinstall docs-orchestrator.

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

The example shows the supported keys; add only any missing keys to your **existing** file. If neither file exists, create `~/.env` for use across projects, or `<project>/.env` for that project only. Use `KEY=value` assignments, protect the file with `chmod 600 ~/.env` (or the project path), and never commit it. `JIRA_URL` and `GITLAB_URL` tell Work Hub which hosts to query. `GITHUB_API_URL` is optional for GitHub Enterprise. Slack and AEM use the MCP servers already connected to Claude Code; Work Hub does not request their tokens.

If an existing file uses `email:`, `jira_token:`, or `gitlab_token:` lines, Work Hub can read those as a legacy convenience and reports the file and line in its brief. Change them to `JIRA_EMAIL=`, `JIRA_API_TOKEN=`, and `GITLAB_TOKEN=` in that **same file** before invoking docs-orchestrator, because docs-tools expects standard `.env` assignments. Work Hub's `config` check reports each key's source and warns if an inherited process value overrides an updated file. Open a new terminal and Claude Code session after changing a shell-loaded `.env`; the existing session keeps its old environment. The report lists paths and key names, never credential values.

## Use it in one Claude session

```text
/work-hub:personal-workflow
```

Claude checks which sources are healthy, groups related signals, presents priorities with evidence and uncertainty, and asks you which task to take. Choose a live Jira issue. Work Hub then follows its linked issue graph through the installed Jira reader and starts `docs-tools:docs-orchestrator` in local draft mode. Before the draft starts, the current Claude Code directory must be the documentation repository for that ticket. If you chose work from another repository, use `/cd <path-to-docs-repo>` inside the same session. The installed orchestrator ignores `--draft` when combined with `--docs-repo-path`, so Work Hub does not use that combination. The orchestrator's installation location does not matter; Claude Code resolves its installed skill by name. The brief can still run if docs-tools is absent; the draft handoff will say what is missing.

Your normal corrections and clear preferences can update `persona.md`, a project file, a stage checklist, or memory through `/work-hub:personalize-workflow`. The plugin nudges this on corrections. The personal files are deliberately small and should evolve as you work.

## What it collects

The bundled Python script reads assigned, watched, and reported Jira issues across projects; GitLab assigned and review MRs, issues, and pending to-dos; and GitHub assigned, mentioned, created, and review-requested issues and PRs. It checks authentication before reporting an empty source, caps each query, and labels incomplete coverage. It does not write to these services. Claude may add Slack and AEM context through installed MCP connections. Its score is a clue; Claude's final triage distinguishes urgency, impact, dependencies, and ease of the next action.

## Upgrade and privacy

`claude plugin marketplace update gtrivedi88-work-hub` refreshes the catalog; update the installed plugin through Claude Code's plugin manager. Your `~/.claude/work-hub` Markdown files and existing `.env` stay local. Work Hub stores the latest intake snapshot under `~/.claude/work-hub/cache/` with private permissions. Delete that cache file whenever you want. Review the repository code before installing, especially the SessionStart hook that writes to your home directory.

## Development

```bash
claude plugin validate .
claude plugin validate ./plugins/work-hub
python3 -m pytest -q tests
```
