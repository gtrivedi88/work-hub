"""Tracker command line."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import agenda, connected, sources, views
from .store import KINDS, SOURCES, STATES, Store, TrackerError


def fields(parser, *, capture=False):
    if capture:
        parser.add_argument("--kind", choices=KINDS[:-1], default="task")
        parser.add_argument("--source", choices=SOURCES, default="manual")
        parser.add_argument("--url")
        parser.add_argument("--reference", help="Stable action ID when a source link contains several tasks")
        parser.add_argument("--parent")
        parser.add_argument("--repeat")
    else:
        parser.add_argument("--title")
    for field in ("description", "next-action", "due", "scheduled", "remind"):
        parser.add_argument("--" + field)
    parser.add_argument("--tag", action="append", dest="tags")
    parser.add_argument("--estimate", type=int, metavar="MINUTES")


def filters(parser):
    parser.add_argument("--view")
    parser.add_argument("--q")
    parser.add_argument("--status", choices=(*STATES, "all"))
    parser.add_argument("--kind", choices=KINDS)
    parser.add_argument("--source", choices=SOURCES)
    parser.add_argument("--tag")
    parser.add_argument("--parent")
    parser.add_argument("--due", choices=("today", "overdue", "upcoming"))
    parser.add_argument("--layout", choices=("list", "board"))
    parser.add_argument("--waiting", action="store_true", default=None)
    parser.add_argument("--snoozed", action="store_true", default=None)


def group(commands, name, description):
    return commands.add_parser(name, help=description).add_subparsers(dest="action", required=True)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.environ.get("WORK_HUB_WORKSPACE", "work"))
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Capture a standalone task, project, or habit")
    add.add_argument("title")
    fields(add, capture=True)
    edit = commands.add_parser("edit", help="Edit task details; an empty string clears an optional field")
    edit.add_argument("id")
    fields(edit)
    capture = commands.add_parser("capture", help="Import reviewed commitments from JSON")
    capture.add_argument("file", help="JSON path or - for stdin")
    track = commands.add_parser("track", help="Save an item from the latest connected brief")
    track.add_argument("reference")
    track.add_argument("--brief-id")
    state = commands.add_parser("status")
    state.add_argument("id")
    state.add_argument("state", choices=STATES)
    snooze = commands.add_parser("snooze")
    snooze.add_argument("id")
    snooze.add_argument("until", help="Date or ISO time; an empty string clears snooze")
    commands.add_parser("history").add_argument("id")
    link = commands.add_parser("link")
    link.add_argument("id")
    link.add_argument("url")
    for name in ("depend", "undepend"):
        dependency = commands.add_parser(name)
        dependency.add_argument("id")
        dependency.add_argument("target")
    listing = commands.add_parser("list", help="Search and filter the persistent inbox")
    filters(listing)
    export = commands.add_parser("export")
    export.add_argument("--format", choices=("json", "csv", "markdown", "ics"), required=True)
    filters(export)
    saved = group(commands, "view", "Manage saved filters")
    save = saved.add_parser("save")
    save.add_argument("name")
    filters(save)
    saved.add_parser("list")
    saved.add_parser("remove").add_argument("name")
    templates = group(commands, "template", "Manage and apply reusable checklists")
    template = templates.add_parser("save")
    template.add_argument("name")
    template.add_argument("--step", action="append", required=True)
    use = templates.add_parser("use")
    use.add_argument("name")
    use.add_argument("title")
    use.add_argument("--parent")
    use.add_argument("--due")
    templates.add_parser("list")
    templates.add_parser("remove").add_argument("name")
    calendars = group(commands, "calendar", "Connect iCalendar files or feeds")
    calendar = calendars.add_parser("add")
    calendar.add_argument("name")
    calendar.add_argument("source")
    calendars.add_parser("list")
    calendars.add_parser("remove").add_argument("name")
    schedule = commands.add_parser("agenda", help="Show calendar commitments and scheduled work")
    schedule.add_argument("--start")
    schedule.add_argument("--days", type=int, default=7)
    watches = group(commands, "watch", "Track changes to web pages, feeds, releases, PRs, and Radar")
    watch = watches.add_parser("add")
    watch.add_argument("title")
    watch.add_argument("target")
    watch.add_argument("--type", choices=sources.WATCH_TYPES, default="page")
    watch.add_argument("--contains")
    watch.add_argument("--every", type=int, default=60, metavar="MINUTES")
    watch.add_argument("--parent")
    watches.add_parser("list")
    for action in ("pause", "resume"):
        watches.add_parser(action).add_argument("id")
    checks = commands.add_parser("check", help="Run due watches and reminders once")
    checks.add_argument("--force", action="store_true")
    checks.add_argument("--connected", action="store_true")
    worker = commands.add_parser("run", help="Run scheduled checks between Claude sessions")
    worker.add_argument("--interval", type=int, default=60, metavar="SECONDS")
    worker.add_argument("--desktop", action="store_true")
    worker.add_argument("--connected", action="store_true")
    notifications = group(commands, "notifications", "Inspect or dismiss notifications")
    notifications.add_parser("list").add_argument("--all", action="store_true")
    notifications.add_parser("dismiss").add_argument("sequence", type=int, nargs="?")
    timer = group(commands, "time", "Start, stop, or log time on an item")
    timer.add_parser("start").add_argument("id")
    timer.add_parser("stop")
    log = timer.add_parser("log")
    log.add_argument("id")
    log.add_argument("minutes", type=int)
    log.add_argument("--when")
    commands.add_parser("review", help="Review outcomes, habits, and time").add_argument("--since")
    dashboard = commands.add_parser("dashboard", help="Open a local dashboard")
    dashboard.add_argument("--port", type=int, default=0)
    dashboard.add_argument("--no-open", action="store_true")
    return parser


def values(args, names):
    return {key: getattr(args, key) for key in names if getattr(args, key, None) is not None}


def selected_filters(args):
    return values(args, ("q", "status", "kind", "source", "tag", "parent", "due",
                         "layout", "waiting", "snoozed"))


def check(store, args, *, force=False):
    checked = sources.poll(store, force=force)
    if args.connected:
        workflow = connected.workflow_module()
        config = workflow.load_config(Path.cwd())
        connected.sync(store, workflow, config, workflow.collect(config))
    return checked


def run_worker(store, args):
    if args.interval < 1:
        raise TrackerError("Use a positive interval in seconds.")
    delivered = {row["sequence"] for row in store.notifications(seen=True)}
    while True:
        try:
            check(store, args)
        except (TrackerError, OSError, sqlite3.Error, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr, flush=True)
        for row in reversed(store.notifications()):
            if row["sequence"] in delivered:
                continue
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if args.desktop:
                sources.desktop_notification(row["message"])
            delivered.add(row["sequence"])
        time.sleep(args.interval)


def dispatch(store, args):
    cmd = args.command
    action = getattr(args, "action", None)
    common = ("description", "next_action", "due", "scheduled", "remind", "tags", "estimate")
    if cmd == "add":
        return store.add(args.title, **values(args, (*common, "kind", "source", "url", "reference", "parent", "repeat")))
    if cmd == "edit":
        return store.edit(args.id, **values(args, (*common, "title")))
    if cmd == "capture":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        return connected.capture_many(store, json.loads(text))
    if cmd == "track":
        return connected.track(store, args.reference, args.brief_id)
    if cmd == "status":
        return store.set_status(args.id, args.state)
    if cmd == "snooze":
        return store.edit(args.id, snooze=args.until)
    if cmd == "history":
        return store.history(args.id)
    if cmd == "link":
        return store.attach(args.id, args.url)
    if cmd in ("depend", "undepend"):
        (store.depend if cmd == "depend" else store.undepend)(args.id, args.target)
        return {"id": args.id, "dependency": args.target, "linked": cmd == "depend"}
    if cmd in ("list", "export"):
        rows = views.query(store, args.view, **selected_filters(args))
        if cmd == "list":
            return rows
        return agenda.calendar_export(store, rows) if args.format == "ics" else views.export(rows, args.format)
    if cmd in ("view", "template", "calendar"):
        if action == "list":
            return {**views.BUILTIN, **store.settings(cmd)} if cmd == "view" else store.settings(cmd)
        if action == "remove":
            if cmd == "view" and args.name in views.BUILTIN:
                raise TrackerError("Built-in views cannot be removed.")
            store.remove_setting(cmd, args.name)
        elif cmd == "view":
            if args.view:
                raise TrackerError("Save the filters directly instead of naming an existing view.")
            views.save_view(store, args.name, selected_filters(args))
        elif cmd == "calendar":
            agenda.add_calendar(store, args.name, args.source)
        elif action == "use":
            return store.apply_template(args.name, args.title, args.parent, args.due)
        else:
            store.template(args.name, args.step)
        return {"name": args.name, "action": action}
    if cmd == "agenda":
        return agenda.agenda(store, args.start, args.days)
    if cmd == "watch":
        if action == "add":
            return sources.add_watch(store, args.title, args.target, kind=args.type,
                                     contains=args.contains, every=args.every, parent=args.parent)
        if action == "list":
            return views.query(store, "all", kind="watch")
        if store.history(args.id)["item"]["kind"] != "watch":
            raise TrackerError("Select a watch.")
        return store.set_status(args.id, "waiting" if action == "pause" else "todo")
    if cmd == "check":
        return {"checked": check(store, args, force=args.force), "notifications": store.notifications()}
    if cmd == "run":
        return run_worker(store, args)
    if cmd == "notifications":
        if action == "list":
            return store.notifications(seen=args.all)
        store.dismiss(args.sequence)
        return {"dismissed": args.sequence or "all"}
    if cmd == "time":
        if action == "stop":
            return store.stop_timer()
        if action == "start":
            store.start_timer(args.id)
        else:
            store.log_time(args.id, args.minutes, args.when)
        return {"item": args.id, "action": action}
    if cmd == "review":
        return views.review(store, args.since)
    if cmd == "dashboard":
        from .web import serve
        return serve(store, args.port, open_browser=not args.no_open)
    raise TrackerError("Unknown tracker command.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = dispatch(Store(args.workspace), args)
        if args.command == "export":
            sys.stdout.write(result)
        elif result is not None:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 0
    except (TrackerError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
