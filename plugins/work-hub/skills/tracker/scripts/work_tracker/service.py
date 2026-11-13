"""Opt-in macOS login scheduling with durable run status."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import connected, sources
from .store import Store, TrackerError, now, timestamp


@contextmanager
def run_lock(store: Store):
    # A process crash releases the lock immediately, including during a check.
    import fcntl

    store.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = store.path.parent / f".{store.path.name}.{store.workspace}.worker.lock"
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR, 0o600), "wb") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def tick(store: Store, *, interval: int, desktop: bool, refresh: bool, force=False) -> dict:
    if os.name != "posix":
        raise TrackerError("Use the terminal worker on this platform.")
    with run_lock(store) as locked:
        if not locked:
            return {"running": True}
        return run_check(store, interval, desktop, refresh, force)


def run_check(store: Store, interval: int, desktop: bool, refresh: bool, force: bool) -> dict:
    started = now()
    with store.db(write=True) as db:
        state = store.setting(db, "service", "health") or {}
        previous = state.get("started_at")
        missed = max(0, int((started - dt.datetime.fromisoformat(previous)).total_seconds()
                            // interval) - 1) if previous else 0
        state.update(started_at=timestamp(started), interval_seconds=interval,
                     missed_last_run=missed, missed_total=state.get("missed_total", 0) + missed)
        store.set(db, "service", "health", state)
    try:
        checked = sources.poll(store, force=force)
        errors = [row["error"] for row in checked if row.get("error")]
        if refresh:
            workflow = connected.workflow_module()
            config = workflow.load_config(Path.cwd())
            snapshot = workflow.collect(config)
            connected.sync(store, workflow, config, snapshot)
            for source in ("jira", "github", "gitlab"):
                if snapshot["source_status"][source]["state"] in ("error", "partial"):
                    errors.append(f"{source} intake was incomplete.")
        if desktop:
            cursor = state.get("desktop_cursor", 0)
            for notice in reversed(store.notifications(seen=True)):
                if notice["sequence"] <= cursor:
                    continue
                if not notice["seen"] and not sources.desktop_notification(notice["message"]):
                    errors.append("Desktop notification could not be delivered.")
                    break
                state["desktop_cursor"] = notice["sequence"]
        if not errors:
            state["last_success_at"] = timestamp()
        state["errors"] = errors[:10]
    except (TrackerError, ValueError, OSError, sqlite3.Error) as exc:
        state["errors"] = [str(exc)]
    finally:
        state["finished_at"] = timestamp()
        with store.db(write=True) as db:
            store.set(db, "service", "health", state)
    return state


def agent_path(store: Store) -> Path:
    identity = hashlib.sha256(str(store.path.resolve()).encode()).hexdigest()[:12]
    name = f"com.workhub.tracker.{identity}.{store.workspace}.plist"
    return Path.home() / "Library/LaunchAgents" / name


def agent_definition(store: Store, interval: int, desktop: bool, refresh: bool, cwd: Path) -> dict:
    entry = Path(__file__).resolve().parents[1] / "tracker.py"
    args = [sys.executable, str(entry), "--workspace", store.workspace, "service", "tick",
            "--interval", str(interval)]
    if desktop:
        args.append("--desktop")
    if refresh:
        args.append("--connected")
    return {
        "Label": agent_path(store).stem, "ProgramArguments": args, "StartInterval": interval,
        "RunAtLoad": True, "WorkingDirectory": str(cwd.resolve()),
        "EnvironmentVariables": {"PERSONAL_WORKFLOW_HOME": str(store.path.parent.parent.resolve())},
    }


def launchctl(*args, check=True):
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise TrackerError(result.stderr.strip() or "launchctl could not load the Work Hub job.")
    return result.returncode == 0


def manage(store: Store, action: str, *, interval=300, desktop=False, refresh=False) -> dict:
    path = agent_path(store)
    if action == "status":
        with store.db() as db:
            state = store.setting(db, "service", "health") or {}
        state["installed"] = sys.platform == "darwin" and path.is_file()
        if state["installed"]:
            state["loaded"] = launchctl("print", f"gui/{os.getuid()}/{path.stem}", check=False)
        if state.get("started_at"):
            seconds = (now() - dt.datetime.fromisoformat(state["started_at"])).total_seconds()
            state["currently_missed"] = max(0, int(seconds // state["interval_seconds"]))
        return state
    if sys.platform != "darwin":
        raise TrackerError("Login scheduling is available on macOS. Run the worker on this platform.")
    domain = f"gui/{os.getuid()}"
    if action == "disable":
        if path.is_file():
            launchctl("bootout", domain, str(path), check=False)
            path.unlink()
        return {"installed": False}
    if not 60 <= interval <= 86400:
        raise TrackerError("Use an interval from 60 to 86400 seconds.")
    path.parent.mkdir(exist_ok=True, parents=True)
    if path.exists():
        raise TrackerError("Disable the existing Work Hub schedule before enabling a new one.")
    definition = agent_definition(store, interval, desktop, refresh, Path.cwd())
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            plistlib.dump(definition, output)
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    try:
        launchctl("bootstrap", domain, str(path))
    except (TrackerError, OSError, subprocess.TimeoutExpired):
        path.unlink(missing_ok=True)
        raise
    return {"installed": True, "interval_seconds": interval, "desktop": desktop, "connected": refresh}
