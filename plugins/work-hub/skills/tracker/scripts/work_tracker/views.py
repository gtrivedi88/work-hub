"""Saved queries, exports and weekly reviews over the tracker."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json

from .store import CLOSED, KINDS, SOURCES, STATES, Store, TrackerError, local_today, now, parse_date

BUILTIN = {
    "inbox": {"status": "inbox"},
    "today": {"due": "today"},
    "waiting": {"waiting": True},
    "upcoming": {"due": "upcoming"},
    "projects": {"kind": "project"},
    "all": {"status": "all", "snoozed": True},
}


def validate_filters(filters: dict) -> dict:
    choices = {"status": (*STATES, "all"), "kind": KINDS, "source": SOURCES,
               "due": ("today", "overdue", "upcoming"), "layout": ("list", "board")}
    allowed = set(choices) | {"q", "tag", "parent", "waiting", "snoozed"}
    for key, value in filters.items():
        if key not in allowed or (key in choices and value not in choices[key]):
            raise TrackerError(f"Unknown filter or value for {key}.")
        if key in ("q", "tag", "parent") and not isinstance(value, str):
            raise TrackerError(f"{key} must be text.")
        if key in ("waiting", "snoozed") and type(value) is not bool:
            raise TrackerError(f"{key} must be true or false.")
    return filters


def save_view(store: Store, name: str, filters: dict):
    if name.strip() in BUILTIN:
        raise TrackerError("Use a different name for a custom view.")
    with store.db(write=True) as db:
        store.set(db, "view", name, validate_filters(filters))


def query(store: Store, view: str | None = None, **overrides) -> list[dict]:
    with store.db() as db:
        filters = {}
        if view:
            filters = BUILTIN.get(view) or store.setting(db, "view", view)
            if filters is None:
                raise TrackerError(f"Unknown view {view}.")
        return query_db(store, db, {**filters, **validate_filters(overrides)})


def query_db(store: Store, db, filters: dict) -> list[dict]:
    rows = store.all(db)
    by_id = {item["id"]: item for item in rows}
    dependencies = {}
    for row in db.execute("SELECT item,target FROM relations WHERE workspace=?", (store.workspace,)):
        dependencies.setdefault(row["item"], []).append(row["target"])

    children = {}
    for item in rows:
        children.setdefault(item["parent"], []).append(item)

    def descendants(item_id):
        for child in children.get(item_id, []):
            yield child
            yield from descendants(child["id"])

    today = local_today().isoformat()
    cutoff = (local_today() + dt.timedelta(days=30)).isoformat()
    current = now().isoformat()
    result = []
    for item in rows:
        status = item["status"]
        if filters.get("status") not in (None, "all", status):
            continue
        if not filters.get("status") and not filters.get("q") and status in CLOSED:
            continue
        if not filters.get("snoozed") and not filters.get("q") and (item["snoozed_until"] or "") > current:
            continue
        if any(item[key] != filters[key] for key in ("kind", "source", "parent") if key in filters):
            continue
        if filters.get("tag") and filters["tag"].lower() not in item["tags"]:
            continue
        blocked_by = [key for key in dependencies.get(item["id"], []) if by_id[key]["status"] != "done"]
        if filters.get("waiting") and status != "waiting" and not blocked_by:
            continue
        due = item.get("due")
        scheduled = item.get("scheduled")
        planned = dt.datetime.fromisoformat(scheduled).astimezone().date().isoformat() if scheduled else None
        if filters.get("due") == "today" and not (due and due <= today or planned == today):
            continue
        if filters.get("due") == "overdue" and not (due and due < today):
            continue
        if filters.get("due") == "upcoming" and not any(
            date and today < date <= cutoff for date in (due, planned)
        ):
            continue
        text = " ".join([
            item["id"], item["title"], item["description"], item["next_action"],
            item["url"] or "", *item["tags"], *item.get("links", []),
            (item.get("documentation") or {}).get("next_action") or "",
        ]).casefold()
        if any(word not in text for word in filters.get("q", "").casefold().split()):
            continue
        result.append({**item, "blocked_by": blocked_by})
        if item["kind"] in ("project", "milestone", "checklist"):
            steps = [child for child in descendants(item["id"]) if child["kind"] in ("task", "habit")]
            result[-1]["progress"] = {
                "done": sum(child["status"] == "done" for child in steps),
                "total": sum(child["status"] != "canceled" for child in steps),
                "waiting": sum(
                    child["status"] not in CLOSED and (
                        child["status"] == "waiting" or any(
                            by_id[key]["status"] != "done"
                            for key in dependencies.get(child["id"], [])
                        )
                    )
                    for child in steps
                ),
            }
    return sorted(result, key=lambda item: (item["due"] or "9999", item["title"].casefold(), item["id"]))


def export(items: list[dict], kind: str) -> str:
    if kind == "json":
        return json.dumps(items, indent=2, ensure_ascii=False) + "\n"
    if kind == "csv":
        output = io.StringIO()
        fields = ("id", "title", "kind", "status", "source", "due", "scheduled", "next_action", "url")
        writer = csv.DictWriter(output, fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            # Keep untrusted titles and notes inert when opened in a spreadsheet.
            writer.writerow({
                key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value)
                for key in fields for value in [item.get(key)]
            })
        return output.getvalue()
    if kind == "markdown":
        return "".join(
            f"- [{ 'x' if item['status'] == 'done' else ' ' }] "
            f"{item['title'].replace(chr(10), ' ')} ({item['id']})"
            f"{' · due ' + item['due'] if item['due'] else ''}\n" for item in items
        )
    raise TrackerError("Use json, csv, or markdown for this export.")


def review(store: Store, since: str | None = None) -> dict:
    start = parse_date(since) if since else local_today() - dt.timedelta(days=6)
    if start > local_today():
        raise TrackerError("Review start must be today or earlier.")
    cutoff = dt.datetime.combine(start, dt.time.min).astimezone(dt.timezone.utc).isoformat()
    with store.db() as db:
        items = query_db(store, db, BUILTIN["all"])
        rows = db.execute(
            """SELECT item, SUM(minutes) AS minutes FROM time_entries
               WHERE workspace=? AND ended>=? GROUP BY item""", (store.workspace, cutoff),
        ).fetchall()
        spent = {row["item"]: row["minutes"] for row in rows}
        active = db.execute(
            "SELECT item,started FROM time_entries WHERE workspace=? AND ended IS NULL",
            (store.workspace,),
        ).fetchone()
        completions = {
            row["item"]: row["at"] for row in db.execute(
                """SELECT item,at,data FROM events WHERE workspace=? AND kind='status'
                   AND at>=? ORDER BY sequence""", (store.workspace, cutoff),
            ) if json.loads(row["data"]).get("status") == "done"
        }
    work = [
        {"id": item["id"], "title": item["title"], "actual_minutes": spent[item["id"]],
         "planned_minutes": item["estimate_minutes"]}
        for item in items if item["id"] in spent
    ]
    completed = [
        {**item, "completed_in_review_at": completions[item["id"]]}
        for item in items if item["id"] in completions
    ]
    open_items = [item for item in items if item["status"] not in CLOSED]
    habits = {}
    for item in items:
        if item["kind"] == "habit" and item["due"] and start <= parse_date(item["due"]) <= local_today():
            series = item.get("series", item["id"])
            totals = habits.setdefault(series, {"title": item["title"], "due": 0, "done": 0})
            totals["due"] += 1
            totals["done"] += item["status"] == "done"
    return {
        "since": start.isoformat(), "through": local_today().isoformat(),
        "completed": completed, "time_by_item": work, "active_timer": dict(active) if active else None,
        "actual_minutes": sum(spent.values()),
        "waiting": [item for item in open_items if item["status"] == "waiting" or item["blocked_by"]],
        "carried_over": [
            item for item in open_items
            if item["created_at"] < cutoff and item["kind"] in ("task", "habit", "milestone")
        ],
        "habits": list(habits.values()),
    }
