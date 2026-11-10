---
name: personalize-workflow
description: Use when a writer corrects Claude, states a durable preference, reveals a verified project rule, or repeatedly changes a documentation checklist during normal work.
allowed-tools: Read, Glob, Grep, Edit, Write, Bash
---

# Personalize the workflow

Update the writer's local context from useful evidence in the current conversation. Do this as part of normal work; the writer does not need to say “remember this.” All files are under `~/.claude/work-hub/`. Do not edit plugin templates in the installed cache.

## Decide whether to save

Save a rule only when it will change a future answer, handoff, check, or task ranking. Good evidence includes an explicit preference or correction, a source-backed project convention, or the same mistake recurring across tasks. Do not save a single task's status, guesses from source titles, transient release dates, a raw transcript, or credentials. If a finding is promising but unverified, save it as a candidate and keep it out of active instructions.

## Route the finding

Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/personal-workflow/scripts/personal_workflow.py" target <kind> [name]` to get a safe local path. Use these kinds:

| Finding | Kind | File |
| --- | --- | --- |
| Stable preference that applies across projects | `preference` | `persona.md` |
| Verified convention for one project | `project <name>` | `projects/<name>.md` |
| Repeatable acceptance check for a stage | `checklist <stage>` | `checklists/<stage>.md` |
| Generalizable mistake and correction | `lesson` | `memory/lessons.md` |
| Plausible but unverified observation | `candidate` | `memory/candidates.md` |

Read the target and its index before editing. If creating a project or checklist file, copy the matching `_template.md` and add an index row with its trigger and owning evidence. Keep `MEMORY.md` as a small routing index, not a transcript. Avoid changing the global `~/.claude/CLAUDE.md` except for the Work Hub import installed by the bootstrap.

## Edit and verify

Write one concise, actionable rule. Include a source, date, or concrete reason when the rule depends on external facts. If the new finding narrows or supersedes an old rule, update the old rule rather than adding a contradiction. Then read back the edited section and tell the writer which local file changed and what future behavior it changes. Do not publish the personal file to GitHub.

If an explicit correction is already being handled in the current turn, fix the task first, then apply the learning. Do not interrupt the writer with a permission question for a small local correction.
