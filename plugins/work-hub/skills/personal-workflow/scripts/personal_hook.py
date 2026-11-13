#!/usr/bin/env python3
"""Nudge personal learning after a correction without storing prompt text."""

from __future__ import annotations

import json
import os
import re
import sys

from personal_workflow import initialize, profile_root


CORRECTION = re.compile(
    r"\b(?:that(?:'s| is) wrong|you missed|incorrect|correction|actually|"
    r"please correct|i prefer|we prefer|we use|for this project|from now on|"
    r"always|never|remember this)\b",
    re.IGNORECASE,
)


def handle(mode: str, event: dict) -> dict | None:
    if mode == "start":
        result = initialize()
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Work Hub is available. Personal context: "
                    + result["profile_root"]
                    + ". Load the relevant personal files for work requests. "
                    "Use /work-hub:personal-workflow for a cross-system brief."
                ),
            }
        }
    root = profile_root()
    if not root.is_dir():
        return None
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(event.get("session_id") or "session"))[
        :80
    ]
    directory = root / "hooks"
    flag = directory / session
    if mode == "prompt":
        if CORRECTION.search(str(event.get("prompt") or "")):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            fd = os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        return None
    if mode == "stop" and flag.exists() and not event.get("stop_hook_active"):
        flag.unlink()
        return {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": (
                    "A correction or durable preference was expressed. Before finishing, "
                    "check whether it should change future behavior. If so, invoke "
                    "work-hub:personalize-workflow, verify the evidence, make a small "
                    "local Markdown edit, and name the changed file. Do not store raw "
                    "prompt text, credentials, or uncertain claims as active rules."
                ),
            }
        }
    return None


def main(argv: list[str] | None = None) -> int:
    mode = (argv or sys.argv[1:])[0] if (argv or sys.argv[1:]) else ""
    if mode not in ("start", "prompt", "stop"):
        print(json.dumps({"error": "Expected start, prompt, or stop"}))
        return 1
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        event = {}
    result = handle(mode, event)
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
