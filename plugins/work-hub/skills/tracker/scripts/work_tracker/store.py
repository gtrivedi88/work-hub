"""Transactional tracker storage and operations."""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

KINDS = ("task", "habit", "project", "milestone", "checklist", "watch")
STATES = ("inbox", "todo", "doing", "waiting", "done", "skipped", "canceled")
CLOSED = {"done", "skipped", "canceled"}
SOURCES = ("manual", "email", "slack", "meeting", "markdown", "jira", "github", "gitlab", "web", "radar")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    workspace TEXT NOT NULL, id TEXT NOT NULL, origin TEXT, series TEXT, occurrence TEXT,
    data TEXT NOT NULL, PRIMARY KEY (workspace,id), UNIQUE (workspace,origin),
    UNIQUE (workspace,series,occurrence)
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY, workspace TEXT NOT NULL, item TEXT NOT NULL, at TEXT NOT NULL,
    kind TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_item ON events(workspace,item,sequence);
CREATE TABLE IF NOT EXISTS relations (
    workspace TEXT NOT NULL, item TEXT NOT NULL, target TEXT NOT NULL,
    PRIMARY KEY(workspace,item,target)
);
CREATE TABLE IF NOT EXISTS settings (
    workspace TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(workspace,kind,name)
);
CREATE TABLE IF NOT EXISTS notifications (
    sequence INTEGER PRIMARY KEY, workspace TEXT NOT NULL, item TEXT NOT NULL,
    identity TEXT NOT NULL, at TEXT NOT NULL, message TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(workspace,item,identity)
);
CREATE TABLE IF NOT EXISTS time_entries (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, item TEXT NOT NULL,
    started TEXT NOT NULL, ended TEXT, minutes INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS active_timer ON time_entries(workspace) WHERE ended IS NULL;
"""


class TrackerError(Exception):
    """An error suitable for the CLI or dashboard."""


def profile_root() -> Path:
    return Path(os.environ.get("PERSONAL_WORKFLOW_HOME", Path.home() / ".claude/work-hub"))


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value: dt.datetime | None = None) -> str:
    return (value or now()).astimezone(dt.timezone.utc).isoformat()


def local_today() -> dt.date:
    return dt.date.today()


def parse_date(value: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise TrackerError("Use a date in YYYY-MM-DD format.")
    return dt.date.fromisoformat(value)


def parse_time(value: str) -> dt.datetime:
    if len(value) == 10:
        return dt.datetime.combine(parse_date(value), dt.time(9)).astimezone()
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def validate_text(value: str, label: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TrackerError(f"{label} must be between 1 and {limit} characters.")
    return value.strip()


def web_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or any(ord(char) < 32 for char in value)):
        raise TrackerError("Use an HTTP or HTTPS source URL without embedded credentials.")
    return urlunsplit(parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()))


def recurrence(value: str) -> str:
    value = value.lower()
    if value in ("daily", "weekdays", "weekly", "monthly", "yearly"):
        return value
    if value.startswith("weekly:") and set(value[7:].split(",")) <= set(WEEKDAYS):
        return value
    raise TrackerError("Use daily, weekdays, weekly, weekly:mon,wed, monthly, or yearly.")


def next_occurrence(rule: str, current: dt.date, anchor: dt.date) -> dt.date:
    if rule == "daily":
        return current + dt.timedelta(days=1)
    if rule == "weekly":
        return current + dt.timedelta(days=7)
    if rule in ("monthly", "yearly"):
        months = current.year * 12 + current.month - 1 + (1 if rule == "monthly" else 12)
        year, month = divmod(months, 12)
        month += 1
        return dt.date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
    weekdays = WEEKDAYS[:5] if rule == "weekdays" else rule[7:].split(",")
    return next(
        day for offset in range(1, 8)
        if WEEKDAYS[(day := current + dt.timedelta(days=offset)).weekday()] in weekdays
    )


class Store:
    def __init__(self, workspace: str = "work", path: Path | None = None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", workspace):
            raise TrackerError("Use a workspace name such as work or personal.")
        self.workspace = workspace
        self.path = path or profile_root() / "cache/tracker.sqlite3"

    @contextmanager
    def db(self, *, write: bool = False):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(SCHEMA)
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def get(self, db, item_id: str) -> dict:
        row = db.execute(
            "SELECT data FROM items WHERE workspace=? AND id=?", (self.workspace, item_id)
        ).fetchone()
        if row is None:
            raise TrackerError(f"Unknown item {item_id} in {self.workspace}.")
        return json.loads(row["data"])

    def all(self, db) -> list[dict]:
        rows = db.execute("SELECT data FROM items WHERE workspace=?", (self.workspace,))
        return [json.loads(row["data"]) for row in rows]

    def save(self, db, item: dict):
        db.execute(
            """INSERT INTO items VALUES (?,?,?,?,?,?) ON CONFLICT(workspace,id)
               DO UPDATE SET data=excluded.data, origin=excluded.origin,
               series=excluded.series, occurrence=excluded.occurrence""",
            (self.workspace, item["id"], item.get("origin"), item.get("series"),
             item.get("occurrence"), json.dumps(item, ensure_ascii=False)),
        )

    def event(self, db, item: str, kind: str, data: dict | None = None, at: str | None = None):
        db.execute(
            "INSERT INTO events(workspace,item,at,kind,data) VALUES (?,?,?,?,?)",
            (self.workspace, item, at or timestamp(), kind, json.dumps(data or {}, ensure_ascii=False)),
        )

    def setting(self, db, kind: str, name: str) -> dict | None:
        row = db.execute(
            "SELECT data FROM settings WHERE workspace=? AND kind=? AND name=?",
            (self.workspace, kind, name),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def settings(self, kind: str) -> dict:
        with self.db() as db:
            rows = db.execute(
                "SELECT name,data FROM settings WHERE workspace=? AND kind=? ORDER BY name",
                (self.workspace, kind),
            )
            return {row["name"]: json.loads(row["data"]) for row in rows}

    def set(self, db, kind: str, name: str, data: dict):
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES (?,?,?,?)",
            (self.workspace, kind, validate_text(name, "Name", 80), json.dumps(data)),
        )

    def remove_setting(self, kind: str, name: str):
        with self.db(write=True) as db:
            db.execute(
                "DELETE FROM settings WHERE workspace=? AND kind=? AND name=?",
                (self.workspace, kind, name),
            )

    def insert(self, db, title: str, *, kind="task", source="manual", url=None,
               description=None, next_action=None, due=None, scheduled=None, remind=None,
               repeat=None, parent=None, tags=(), estimate=None, origin=None, connection=None,
               watch=None, reference=None) -> dict:
        if kind not in KINDS or source not in SOURCES:
            raise TrackerError("Unknown item type or source.")
        if (kind == "watch") != (watch is not None):
            raise TrackerError("Use watch add to create a watch.")
        url = web_url(url) if url else None
        if reference:
            if not url:
                raise TrackerError("A source reference needs a source URL.")
            reference = validate_text(reference, "Source reference", 120)
            origin = json.dumps([source, url, reference])
        origin = origin or url
        if origin:
            prior = db.execute(
                "SELECT data FROM items WHERE workspace=? AND origin=?", (self.workspace, origin)
            ).fetchone()
            if prior:
                return json.loads(prior["data"])
        if connection and url:
            prior = db.execute(
                "SELECT data FROM items WHERE workspace=? AND origin=?", (self.workspace, url)
            ).fetchone()
            if prior:
                captured = json.loads(prior["data"])
                if captured["kind"] == "task" and not captured.get("connection"):
                    captured.update(origin=origin, source=source, connection=connection,
                                    updated_at=timestamp())
                    if due and not captured["due"]:
                        captured["due"] = parse_date(due).isoformat()
                    self.save(db, captured)
                    self.event(db, captured["id"], "connected", {"source": source, "url": url})
                    return captured
        if parent:
            self.get(db, parent)
        if repeat and (not due or kind not in ("task", "habit")):
            raise TrackerError("Repeating tasks and habits need a due date.")
        at = timestamp()
        item_id = uuid.uuid4().hex[:12]
        item = {
            "id": item_id, "title": validate_text(title, "Title", 240), "kind": kind,
            "status": "inbox" if kind in ("task", "habit") else "todo", "source": source,
            "url": url, "origin": origin, "description": "", "next_action": "", "tags": [],
            "parent": parent, "due": None, "scheduled": None, "remind_at": None,
            "snoozed_until": None, "estimate_minutes": None, "repeat": None,
            "connection": connection, "created_at": at, "updated_at": at,
        }
        fields = {
            "description": description, "next_action": next_action, "due": due,
            "scheduled": scheduled, "remind": remind, "tags": list(tags), "estimate": estimate,
        }
        self.apply_fields(item, {key: value for key, value in fields.items() if value is not None})
        if repeat:
            item.update(repeat=recurrence(repeat), series=item_id, anchor=item["due"],
                        occurrence=item["due"])
        if watch:
            item["watch"] = watch
        self.save(db, item)
        self.event(db, item_id, "captured", {"source": source, "url": url})
        return item

    def add(self, title: str, **fields) -> dict:
        with self.db(write=True) as db:
            return self.insert(db, title, **fields)

    @staticmethod
    def apply_fields(item, fields):
        for key, value in fields.items():
            if key in ("title", "description", "next_action"):
                if key == "title" or value:
                    value = validate_text(value, key.replace("_", " ").capitalize(),
                                          {"title": 240, "description": 4000, "next_action": 500}[key])
                item[key] = value or ""
            elif key in ("due", "scheduled", "remind", "snooze"):
                target = {"remind": "remind_at", "snooze": "snoozed_until"}.get(key, key)
                item[target] = (parse_date(value).isoformat() if key == "due"
                                else timestamp(parse_time(value))) if value else None
            elif key == "tags":
                if not isinstance(value, list) or len(value) > 30:
                    raise TrackerError("Provide up to 30 tags.")
                item[key] = sorted({validate_text(tag, "Tag", 50).lower() for tag in value})
            elif key == "estimate":
                if value is not None and (type(value) is not int or value < 1):
                    raise TrackerError("Estimated minutes must be a positive integer.")
                item["estimate_minutes"] = value
            else:
                raise TrackerError(f"Unknown editable field {key}.")

    def edit(self, item_id: str, **fields) -> dict:
        with self.db(write=True) as db:
            item = self.get(db, item_id)
            previous = item.copy()
            self.apply_fields(item, fields)
            if item.get("repeat"):
                if not item["due"]:
                    raise TrackerError("Repeating items need a due date.")
                if item["due"] != previous["due"]:
                    item["occurrence"] = item["due"]
            changes = {key: value for key, value in item.items() if value != previous.get(key)}
            if changes:
                item["updated_at"] = timestamp()
                self.save(db, item)
                self.event(db, item_id, "edited", changes)
            return item

    def set_status(self, item_id: str, status: str) -> dict:
        if status not in STATES:
            raise TrackerError("Unknown status.")
        with self.db(write=True) as db:
            item = self.get(db, item_id)
            if item["status"] == status:
                return {"item": item, "next_occurrence": None}
            at = timestamp()
            item.update(status=status, updated_at=at, closed_at=at if status in CLOSED else None)
            self.save(db, item)
            self.event(db, item_id, "status", {"status": status}, at)
            if status in CLOSED:
                timer = db.execute(
                    "SELECT id,started FROM time_entries WHERE workspace=? AND item=? AND ended IS NULL",
                    (self.workspace, item_id),
                ).fetchone()
                if timer:
                    end = dt.datetime.fromisoformat(at)
                    minutes = max(1, round((end - dt.datetime.fromisoformat(timer["started"])).total_seconds() / 60))
                    self.end_timer(db, timer["id"], item_id, end, minutes)
            successor = self.repeat_item(db, item) if status in {"done", "skipped"} else None
            return {"item": item, "next_occurrence": successor}

    def repeat_item(self, db, item: dict) -> dict | None:
        if not item.get("repeat"):
            return None
        previous_day = parse_date(item["due"])
        day = next_occurrence(item["repeat"], previous_day, parse_date(item["anchor"]))
        existing = db.execute(
            "SELECT data FROM items WHERE workspace=? AND series=? AND occurrence=?",
            (self.workspace, item["series"], day.isoformat()),
        ).fetchone()
        if existing:
            return json.loads(existing["data"])
        at = timestamp()
        successor = {
            **item, "id": uuid.uuid4().hex[:12], "origin": None, "occurrence": day.isoformat(),
            "due": day.isoformat(), "status": "todo", "created_at": at, "updated_at": at,
            "closed_at": None, "snoozed_until": None,
        }
        if item["remind_at"]:
            original = dt.datetime.fromisoformat(item["remind_at"]).astimezone()
            shifted = dt.datetime.combine(original.date() + (day - previous_day), original.time())
            successor["remind_at"] = timestamp(shifted)
        if item["scheduled"]:
            original = dt.datetime.fromisoformat(item["scheduled"]).astimezone()
            shifted = dt.datetime.combine(original.date() + (day - previous_day), original.time())
            successor["scheduled"] = timestamp(shifted)
        self.save(db, successor)
        self.event(db, successor["id"], "recurring", {"previous": item["id"]})
        return successor

    def depend(self, item_id: str, target: str):
        with self.db(write=True) as db:
            self.get(db, item_id)
            self.get(db, target)
            cycle = db.execute(
                """WITH RECURSIVE deps(id) AS (
                       SELECT ? UNION SELECT target FROM relations JOIN deps ON item=deps.id
                       WHERE workspace=?
                   ) SELECT 1 FROM deps WHERE id=?""", (target, self.workspace, item_id),
            ).fetchone()
            if cycle:
                raise TrackerError("That dependency would create a cycle.")
            if db.execute(
                "INSERT OR IGNORE INTO relations VALUES (?,?,?)", (self.workspace, item_id, target)
            ).rowcount:
                self.event(db, item_id, "depends_on", {"id": target})

    def undepend(self, item_id: str, target: str):
        with self.db(write=True) as db:
            self.get(db, item_id)
            if db.execute(
                "DELETE FROM relations WHERE workspace=? AND item=? AND target=?",
                (self.workspace, item_id, target),
            ).rowcount:
                self.event(db, item_id, "dependency_removed", {"id": target})

    def attach(self, item_id: str, url: str):
        url = web_url(url)
        with self.db(write=True) as db:
            item = self.get(db, item_id)
            links = item.setdefault("links", [])
            if url != item.get("url") and url not in links:
                links.append(url)
                item["updated_at"] = timestamp()
                self.save(db, item)
                self.event(db, item_id, "linked", {"url": url})
            return item

    def history(self, item_id: str) -> dict:
        with self.db() as db:
            item = self.get(db, item_id)
            rows = db.execute(
                "SELECT at,kind,data FROM events WHERE workspace=? AND item=? ORDER BY sequence",
                (self.workspace, item_id),
            )
            return {"item": item, "history": [
                {"at": row["at"], "kind": row["kind"], **json.loads(row["data"])} for row in rows
            ]}

    def template(self, name: str, steps: list[str]):
        if not steps:
            raise TrackerError("A template needs at least one step.")
        with self.db(write=True) as db:
            self.set(db, "template", name, {"steps": [validate_text(s, "Step", 240) for s in steps]})

    def apply_template(self, name: str, title: str, parent: str | None = None,
                       due: str | None = None) -> dict:
        with self.db(write=True) as db:
            template = self.setting(db, "template", name)
            if template is None:
                raise TrackerError(f"Unknown template {name}.")
            checklist = self.insert(db, title, kind="checklist", parent=parent, due=due)
            children = [self.insert(db, step, parent=checklist["id"]) for step in template["steps"]]
            return {"checklist": checklist, "steps": children}

    def notify(self, db, item: str, identity: str, message: str, at: str) -> bool:
        return bool(db.execute(
            """INSERT OR IGNORE INTO notifications(workspace,item,identity,at,message)
               VALUES (?,?,?,?,?)""", (self.workspace, item, identity, at, message),
        ).rowcount)

    def notifications(self, *, seen: bool = False) -> list[dict]:
        with self.db() as db:
            rows = db.execute(
                """SELECT sequence,item,at,message,seen FROM notifications
                   WHERE workspace=? AND (seen=0 OR ?) ORDER BY sequence DESC""",
                (self.workspace, seen),
            )
            return [dict(row) for row in rows]

    def dismiss(self, notification: int | None = None):
        with self.db(write=True) as db:
            db.execute(
                "UPDATE notifications SET seen=1 WHERE workspace=? AND (? IS NULL OR sequence=?)",
                (self.workspace, notification, notification),
            )

    def start_timer(self, item_id: str):
        with self.db(write=True) as db:
            item = self.get(db, item_id)
            if item["status"] in CLOSED:
                raise TrackerError("Reopen this item before starting a timer.")
            try:
                db.execute("INSERT INTO time_entries(workspace,item,started) VALUES (?,?,?)",
                           (self.workspace, item_id, timestamp()))
            except sqlite3.IntegrityError as exc:
                raise TrackerError("Stop the active timer first.") from exc

    def stop_timer(self) -> dict:
        with self.db(write=True) as db:
            row = db.execute(
                "SELECT * FROM time_entries WHERE workspace=? AND ended IS NULL", (self.workspace,)
            ).fetchone()
            if not row:
                raise TrackerError("There is no active timer.")
            at = now()
            elapsed = max(1, round((at - dt.datetime.fromisoformat(row["started"])).total_seconds() / 60))
            self.end_timer(db, row["id"], row["item"], at, elapsed)
            return {"item": row["item"], "minutes": elapsed}

    def end_timer(self, db, entry: int, item: str, at: dt.datetime, minutes: int):
        end = timestamp(at)
        db.execute("UPDATE time_entries SET ended=?, minutes=? WHERE id=?", (end, minutes, entry))
        self.event(db, item, "time_logged", {"minutes": minutes}, end)

    def log_time(self, item_id: str, minutes: int, when: str | None = None):
        if type(minutes) is not int or minutes < 1:
            raise TrackerError("Logged minutes must be a positive integer.")
        with self.db(write=True) as db:
            self.get(db, item_id)
            at = parse_time(when) if when else now()
            start = timestamp(at - dt.timedelta(minutes=minutes))
            end = timestamp(at)
            db.execute(
                "INSERT INTO time_entries(workspace,item,started,ended,minutes) VALUES (?,?,?,?,?)",
                (self.workspace, item_id, start, end, minutes),
            )
            self.event(db, item_id, "time_logged", {"minutes": minutes}, end)
