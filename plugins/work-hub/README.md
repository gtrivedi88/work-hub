# Work Hub plugin

This Claude Code plugin includes a standalone tracker, local dashboard, and connected work intake for technical writers. Use `/work-hub:tracker` for local tasks, habits, projects, watches, and time tracking. It collects read-only task signals from Jira, GitLab, and GitHub; uses connected Slack and AEM MCP tools when available; and hands a chosen Jira documentation issue to an already installed docs-orchestrator skill in draft mode. Local ticket history carries draft progress, next actions, and dependency follow-ups between briefs. Its SessionStart hook creates missing Markdown templates under `~/.claude/work-hub` without replacing the writer's edits. It reads the same project `.env` or `~/.env` as docs-tools and creates no credential file. See the repository README for installation and setup.

Install from the GitHub marketplace with `claude plugin marketplace add gtrivedi88/work-hub` followed by `claude plugin install work-hub@gtrivedi88-work-hub`.
