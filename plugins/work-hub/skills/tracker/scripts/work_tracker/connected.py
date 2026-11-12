"""Promote connected inbox items to stable local records and verify updates."""

from __future__ import annotations

import concurrent.futures
import importlib
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

from .store import CLOSED, Store, TrackerError, timestamp, web_url


def workflow_module():
    scripts = Path(__file__).resolve().parents[3] / "personal-workflow/scripts"
    sys.path.insert(0, str(scripts))
    try:
        return importlib.import_module("personal_workflow")
    finally:
        sys.path.pop(0)


def track(store: Store, reference: str, brief_id: str | None = None) -> dict:
    workflow = workflow_module()
    try:
        snapshot = workflow.active_snapshot(brief_id)
    except workflow.IntakeError as exc:
        raise TrackerError(str(exc)) from exc
    matches = [item for item in snapshot["items"] if reference in (
        item.get("key"), item.get("id"), item.get("url"),
    )]
    if len(matches) != 1:
        raise TrackerError("Use the exact ID or URL of an item in the latest connected brief.")
    item = matches[0]
    source = item["source"]
    coverage = snapshot["source_status"].get(source, {})
    if not coverage.get("scope"):
        raise TrackerError("The source account was not verified in the brief.")
    if not item.get("url"):
        raise TrackerError("The source item did not provide a URL.")
    connection = {
        "scope": coverage["scope"], "kind": item.get("kind"), "id": item["id"],
        "key": item.get("key"), "source_status": item.get("status"),
    }
    return store.add(
        item["title"], source=source, url=item["url"],
        origin=f"{source}:{coverage['scope']}:{item['id']}", connection=connection,
        due=(item.get("due_date") or "")[:10] or None,
    )


def review_api_path(url: str, base: str, source: str) -> str | None:
    parsed = urlsplit(web_url(url))
    web = "https://github.com" if base == "https://api.github.com" else base.removesuffix(
        "/api/v3" if source == "github" else "/api/v4"
    )
    expected = urlsplit(web)
    prefix = expected.path.rstrip("/") + "/"
    if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc) or not parsed.path.startswith(prefix):
        return None
    path = parsed.path[len(prefix):]
    if source == "github":
        match = re.fullmatch(r"([\w.-]+/[\w.-]+)/(issues|pull)/([1-9]\d*)/?", path, re.ASCII)
    else:
        match = re.fullmatch(r"([\w./-]+)/-/(issues|merge_requests)/([1-9]\d*)/?", path, re.ASCII)
    if not match or any(part in ("", ".", "..") for part in match[1].split("/")):
        return None
    project, kind, number = match.groups()
    if source == "github":
        return f"/repos/{project}/{'pulls' if kind == 'pull' else kind}/{number}"
    return f"/projects/{quote(project, safe='')}/{kind}/{number}"


def source_state(workflow, config, coverage, item) -> str | None:
    source = item["source"]
    link = item["connection"]
    base = coverage["api_base"]
    if source == "jira":
        if not workflow.KEY.fullmatch(link.get("key") or ""):
            return None
        observed = workflow.jira_status(config, base, link["key"])
        if observed.get("completion") == "jira_done":
            return "done"
        return observed.get("jira_status")
    path = review_api_path(item["url"], base, source)
    if path is None:
        return None
    headers = (
        {"Authorization": "Bearer " + config["GITHUB_TOKEN"], "X-GitHub-Api-Version": "2022-11-28"}
        if source == "github" else {"PRIVATE-TOKEN": config["GITLAB_TOKEN"]}
    )
    try:
        response = workflow.request_json(base + path, headers)
    except workflow.IntakeError:
        return None
    return "merged" if response.get("merged") is True else response.get("state")


def sync(store: Store, workflow, config: dict, snapshot: dict):
    coverage = snapshot["source_status"]
    current = {item["id"]: item for item in snapshot.get("items", [])}
    with store.db() as db:
        connected = [item for item in store.all(db) if item.get("connection") and item["status"] not in CLOSED]

    def check(item):
        source = coverage.get(item["source"], {})
        link = item["connection"]
        if source.get("scope") != link["scope"]:
            return None
        present = current.get(link["id"])
        if present and present.get("status"):
            return present["status"]
        return source_state(workflow, config, source, item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(check, connected))
    at = snapshot["collected_at"]
    with store.db(write=True) as db:
        for item, state in zip(connected, states):
            latest = store.get(db, item["id"])
            link = latest["connection"]
            if (link.get("checked_at") or "") >= at or latest["status"] in CLOSED:
                continue
            previous = link.get("source_status")
            link.update(checked_at=at, verification="verified" if state else "unknown")
            if state:
                link["source_status"] = state
                if previous != state:
                    store.event(db, item["id"], "source_status", {"status": state, "url": item["url"]}, at)
                    if state.lower() in ("done", "closed", "merged"):
                        message = f"{item['title']}: source status is {state}"
                        store.notify(db, item["id"], f"source:{at}:{state}", message, at)
            store.save(db, latest)


def capture_many(store: Store, rows: list[dict]) -> list[dict]:
    """Import reviewed commitments from an external adapter in one transaction."""
    if not isinstance(rows, list) or len(rows) > 500:
        raise TrackerError("Provide a JSON array of up to 500 reviewed items.")
    allowed = {"title", "source", "url", "due", "description", "next_action", "tags", "parent", "reference"}
    with store.db(write=True) as db:
        results = []
        for row in rows:
            if not isinstance(row, dict) or "title" not in row or row.keys() - allowed:
                raise TrackerError("Each row needs a title and supported capture fields.")
            if row.get("source") not in ("email", "slack", "meeting", "markdown", "manual"):
                raise TrackerError("Use email, slack, meeting, markdown, or manual as the source.")
            if row["source"] != "manual" and not row.get("url"):
                raise TrackerError("Captured external commitments need a source permalink.")
            results.append(store.insert(db, **row))
        return results


def mirror_docs(store: Store, snapshot: dict, work: list[dict]):
    """Show selected documentation work beside standalone items in the dashboard."""
    coverage = snapshot["source_status"].get("jira", {})
    if not coverage.get("scope"):
        return
    scope = coverage["scope"]
    items = {item["id"]: item for item in snapshot["items"]}
    with store.db(write=True) as db:
        for row in work:
            if not row.get("selected_at") or not row.get("url"):
                continue
            source_id = "jira:" + row["key"]
            source_item = items.get(source_id, {})
            item = store.insert(
                db, row["title"], source="jira", url=row["url"],
                origin=f"jira:{scope}:{source_id}",
                due=source_item.get("due_date"),
                connection={
                    "scope": scope, "kind": "issue", "id": source_id,
                    "key": row["key"], "source_status": row.get("jira_status"),
                },
            )
            docs = {"stage": row.get("local_stage"), "next_action": row.get("next_action"),
                    "work_id": row["work_id"], "jira_status": row.get("jira_status"),
                    "outcomes": row.get("outcomes", {})}
            if item.get("documentation") == docs:
                continue
            item["documentation"] = docs
            store.save(db, item)
            store.event(db, item["id"], "documentation_progress", docs)
