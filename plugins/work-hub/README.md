# Work Hub plugin

This Claude Code plugin adds personal work intake and evolving local context for technical writers. It collects read-only task signals from Jira, GitLab, and GitHub; uses connected Slack and AEM MCP tools when available; and hands a chosen Jira documentation issue to an already installed docs-orchestrator skill in draft mode. Its SessionStart hook creates missing Markdown templates under `~/.claude/work-hub` without replacing the writer's edits. It reads the same project `.env` or `~/.env` as docs-tools and creates no credential file. See the repository README for installation and setup.

Install from the GitHub marketplace with `claude plugin marketplace add gtrivedi88/work-hub` followed by `claude plugin install work-hub@gtrivedi88-work-hub`.
