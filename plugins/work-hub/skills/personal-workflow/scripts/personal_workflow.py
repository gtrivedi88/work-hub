#!/usr/bin/env python3
"""Read-only cross-system work intake for the work-hub plugin.

All commands emit JSON. Credentials are read as data from the active repository's
.env, then ~/.env; process variables win. A legacy Work Hub .env is a fallback
for users of version 0.1.0. No command
prints credential values or writes to Jira, GitLab, or GitHub.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = SKILL_ROOT / "defaults"
KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
ENV_KEYS = {
    "JIRA_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_AUTH_TOKEN",
    "GITLAB_URL",
    "GITLAB_TOKEN",
    "GITHUB_API_URL",
    "GITHUB_TOKEN",
}
LEGACY_KEYS = {
    "email": "JIRA_EMAIL",
    "jira_token": "JIRA_API_TOKEN",
    "gitlab_token": "GITLAB_TOKEN",
}
JIRA_FIELDS = [
    "summary",
    "status",
    "priority",
    "updated",
    "duedate",
    "fixVersions",
    "issuelinks",
    "parent",
    "subtasks",
    "project",
    "labels",
]
JIRA_QUERIES = {
    "assigned": "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
    "watching": "watcher = currentUser() AND statusCategory != Done ORDER BY updated DESC",
    "reported": "reporter = currentUser() AND statusCategory != Done ORDER BY updated DESC",
}


class IntakeError(Exception):
    """A safe-to-display intake error with no credential values."""


def profile_root() -> Path:
    return Path(
        os.environ.get("PERSONAL_WORKFLOW_HOME", Path.home() / ".claude/work-hub")
    )


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            legacy = re.fullmatch(r"(email|jira_token|gitlab_token)\s*:\s*(.+)", line)
            if legacy:
                parsed.setdefault(LEGACY_KEYS[legacy.group(1)], legacy.group(2).strip())
                continue
            if re.match(r"(?:export\s+)?(?:JIRA_|GITLAB_|GITHUB_)", line):
                raise IntakeError(f"Invalid .env syntax in {path} at line {number}")
            continue
        name, value = match.groups()
        if name not in ENV_KEYS:
            continue
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise IntakeError(
                    f"Invalid quoted .env value in {path} at line {number}"
                ) from exc
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if value:
            parsed[name] = value
    return parsed


def load_config(cwd: Path) -> dict[str, str]:
    values = parse_env_file(profile_root() / ".env")
    values.update(parse_env_file(Path.home() / ".env"))
    values.update(parse_env_file(cwd / ".env"))
    values.update(
        {key: value for key, value in os.environ.items() if key in ENV_KEYS and value}
    )
    values.setdefault("GITHUB_API_URL", "https://api.github.com")
    return values


def config_diagnostics(cwd: Path) -> dict:
    files = []
    warnings = []
    for label, path in (
        ("legacy_work_hub", profile_root() / ".env"),
        ("global", Path.home() / ".env"),
        ("active_directory", cwd / ".env"),
    ):
        if not path.is_file():
            continue
        files.append({"scope": label, "path": str(path)})
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.fullmatch(r"\s*(email|jira_token|gitlab_token)\s*:\s*(.+)", raw)
            if match:
                warnings.append(
                    f"{path}:{number}: {match.group(1)}: is legacy syntax; "
                    f"use {LEGACY_KEYS[match.group(1)]}= in this same file "
                    "for docs-tools compatibility"
                )
    return {"files_read": files, "warnings": warnings}


def request_json(url: str, headers: dict[str, str], *, data: dict | None = None):
    payload = None if data is None else json.dumps(data).encode("utf-8")
    method = "POST" if data is not None else "GET"
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        path = urllib.parse.urlsplit(url).path
        raise IntakeError(f"HTTP {exc.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        path = urllib.parse.urlsplit(url).path
        raise IntakeError(f"Request failed for {path}: {type(exc).__name__}") from None


def jira_headers(config: dict[str, str]) -> dict[str, str]:
    token = config.get("JIRA_API_TOKEN") or config.get("JIRA_AUTH_TOKEN") or ""
    email = config.get("JIRA_EMAIL") or ""
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}


def jira_search(config: dict[str, str], jql: str, cap: int):
    results: list[dict] = []
    token = None
    base = config["JIRA_URL"].rstrip("/")
    while len(results) < cap:
        body = {
            "jql": jql,
            "fields": JIRA_FIELDS,
            "maxResults": min(100, cap - len(results)),
        }
        if token:
            body["nextPageToken"] = token
        page = request_json(
            f"{base}/rest/api/3/search/jql", jira_headers(config), data=body
        )
        rows = page.get("issues", [])
        results.extend(rows)
        next_token = page.get("nextPageToken")
        if not rows or not next_token or next_token == token:
            break
        token = next_token
    return results[:cap], len(results) >= cap


def normalize_jira(row: dict, base: str) -> dict:
    fields = row.get("fields") or {}
    key = row.get("key", "")
    relations = []
    parent = fields.get("parent") or {}
    if parent.get("key"):
        relations.append({"key": parent["key"], "type": "parent"})
    for child in fields.get("subtasks") or []:
        if child.get("key"):
            relations.append({"key": child["key"], "type": "subtask"})
    for link in fields.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        if other.get("key"):
            relations.append(
                {
                    "key": other["key"],
                    "type": (link.get("type") or {}).get("name") or "linked",
                }
            )
    return {
        "id": f"jira:{key}",
        "source": "jira",
        "kind": "issue",
        "key": key,
        "title": fields.get("summary") or "(no title)",
        "url": f"{base}/browse/{key}",
        "status": (fields.get("status") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "updated": fields.get("updated"),
        "due_date": fields.get("duedate"),
        "versions": [
            {"name": version.get("name"), "release_date": version.get("releaseDate")}
            for version in fields.get("fixVersions") or []
        ],
        "project": (fields.get("project") or {}).get("key"),
        "relations": relations,
        "signals": [],
    }


def collect_jira(config: dict[str, str], cap: int):
    if not (
        config.get("JIRA_URL")
        and config.get("JIRA_EMAIL")
        and (config.get("JIRA_API_TOKEN") or config.get("JIRA_AUTH_TOKEN"))
    ):
        return [], {
            "state": "missing",
            "missing": [
                key
                for key in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
                if not config.get(key)
            ],
            "count": 0,
        }
    base = config["JIRA_URL"].rstrip("/")
    try:
        request_json(f"{base}/rest/api/3/myself", jira_headers(config))
    except IntakeError as exc:
        return [], {
            "state": "error",
            "errors": [f"Jira authentication: {exc}"],
            "count": 0,
        }
    items: dict[str, dict] = {}
    errors: list[str] = []
    truncated: list[str] = []
    for signal, jql in JIRA_QUERIES.items():
        try:
            rows, capped = jira_search(config, jql, cap)
            if capped:
                truncated.append(signal)
            for row in rows:
                key = row.get("key")
                if key:
                    item = items.setdefault(key, normalize_jira(row, base))
                    if signal not in item["signals"]:
                        item["signals"].append(signal)
        except IntakeError as exc:
            errors.append(f"Jira {signal}: {exc}")
    state = "partial" if errors or truncated else "ok"
    return list(items.values()), {
        "state": state,
        "errors": errors,
        "truncated": truncated,
        "count": len(items),
    }


def paged(url: str, headers: dict[str, str], cap: int):
    rows: list[dict] = []
    page = 1
    while len(rows) < cap:
        sep = "&" if "?" in url else "?"
        batch = request_json(f"{url}{sep}per_page=100&page={page}", headers)
        if not isinstance(batch, list):
            raise IntakeError("Expected a list from paged endpoint")
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return rows[:cap], len(rows) >= cap


def collect_gitlab(config: dict[str, str], cap: int):
    if not (config.get("GITLAB_URL") and config.get("GITLAB_TOKEN")):
        return [], {
            "state": "missing",
            "missing": [
                key for key in ("GITLAB_URL", "GITLAB_TOKEN") if not config.get(key)
            ],
            "count": 0,
        }
    base = config["GITLAB_URL"].rstrip("/")
    api = base if base.endswith("/api/v4") else base + "/api/v4"
    headers = {"PRIVATE-TOKEN": config["GITLAB_TOKEN"]}
    try:
        request_json(api + "/user", headers)
    except IntakeError as exc:
        return [], {
            "state": "error",
            "errors": [f"GitLab authentication: {exc}"],
            "count": 0,
        }
    items: dict[str, dict] = {}
    errors: list[str] = []
    truncated: list[str] = []

    def add(row: dict, signal: str, kind: str):
        target = row.get("target") or {}
        url = row.get("web_url") or row.get("target_url") or target.get("web_url")
        if not url:
            return
        title = (
            row.get("title") or target.get("title") or row.get("body") or "GitLab to-do"
        )
        item = items.setdefault(
            url,
            {
                "id": "gitlab:" + url,
                "source": "gitlab",
                "kind": kind,
                "title": title,
                "url": url,
                "updated": row.get("updated_at") or row.get("created_at"),
                "status": row.get("state"),
                "due_date": row.get("due_date"),
                "draft": row.get("draft"),
                "issue_keys": sorted(set(KEY.findall(title))),
                "signals": [],
            },
        )
        if signal not in item["signals"]:
            item["signals"].append(signal)

    requests = [
        ("mr_review_requested", "merge_requests", "reviews_for_me", "merge_request"),
        ("mr_assigned", "merge_requests", "assigned_to_me", "merge_request"),
        ("mr_authored", "merge_requests", "created_by_me", "merge_request"),
        ("issue_assigned", "issues", "assigned_to_me", "issue"),
        ("issue_authored", "issues", "created_by_me", "issue"),
    ]
    for signal, endpoint, scope, kind in requests:
        query = urllib.parse.urlencode(
            {
                "scope": scope,
                "state": "opened",
                "order_by": "updated_at",
                "sort": "desc",
            }
        )
        try:
            rows, capped = paged(f"{api}/{endpoint}?{query}", headers, cap)
            if capped:
                truncated.append(signal)
            for row in rows:
                add(row, signal, kind)
        except IntakeError as exc:
            errors.append(f"GitLab {signal}: {exc}")
    try:
        rows, capped = paged(f"{api}/todos?state=pending", headers, cap)
        if capped:
            truncated.append("todos")
        for row in rows:
            add(
                row,
                "todo:"
                + str(row.get("action_name") or row.get("action") or "attention"),
                "todo_target",
            )
    except IntakeError as exc:
        errors.append(f"GitLab todos: {exc}")
    state = "partial" if errors or truncated else "ok"
    return list(items.values()), {
        "state": state,
        "errors": errors,
        "truncated": truncated,
        "count": len(items),
    }


def collect_github(config: dict[str, str], cap: int):
    if not config.get("GITHUB_TOKEN"):
        return [], {"state": "missing", "missing": ["GITHUB_TOKEN"], "count": 0}
    base = config["GITHUB_API_URL"].rstrip("/")
    headers = {
        "Authorization": "Bearer " + config["GITHUB_TOKEN"],
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        request_json(base + "/user", headers)
    except IntakeError as exc:
        return [], {
            "state": "error",
            "errors": [f"GitHub authentication: {exc}"],
            "count": 0,
        }
    items: dict[str, dict] = {}
    errors: list[str] = []
    truncated: list[str] = []

    def add(row: dict, signal: str):
        url = row.get("html_url")
        if not url:
            return
        title = row.get("title") or "(no title)"
        item = items.setdefault(
            url,
            {
                "id": "github:" + url,
                "source": "github",
                "kind": "pull_request" if row.get("pull_request") else "issue",
                "title": title,
                "url": url,
                "updated": row.get("updated_at"),
                "status": row.get("state"),
                "draft": row.get("draft"),
                "issue_keys": sorted(set(KEY.findall(title))),
                "signals": [],
            },
        )
        if signal not in item["signals"]:
            item["signals"].append(signal)

    for signal in ("assigned", "mentioned", "created"):
        query = urllib.parse.urlencode(
            {"filter": signal, "state": "open", "sort": "updated", "direction": "desc"}
        )
        try:
            rows, capped = paged(f"{base}/issues?{query}", headers, cap)
            if capped:
                truncated.append(signal)
            for row in rows:
                add(row, signal)
        except IntakeError as exc:
            errors.append(f"GitHub {signal}: {exc}")
    query = urllib.parse.urlencode(
        {"q": "state:open is:pr user-review-requested:@me", "per_page": min(cap, 100)}
    )
    try:
        page = request_json(f"{base}/search/issues?{query}", headers)
        rows = page.get("items", [])
        if page.get("total_count", len(rows)) > len(rows):
            truncated.append("review_requested")
        for row in rows:
            add(row, "review_requested")
    except IntakeError as exc:
        errors.append(f"GitHub review_requested: {exc}")
    state = "partial" if errors or truncated else "ok"
    return list(items.values()), {
        "state": state,
        "errors": errors,
        "truncated": truncated,
        "count": len(items),
    }


def triage_hints(snapshot: dict) -> list[dict]:
    today = dt.datetime.fromisoformat(
        snapshot["collected_at"].replace("Z", "+00:00")
    ).date()
    hints = []
    for item in snapshot.get("items", []):
        score = 0
        reasons: list[str] = []
        signals = item.get("signals") or []
        if "assigned" in signals or any(s.endswith("_assigned") for s in signals):
            score += 1
            reasons.append("assigned")
        if "review_requested" in signals or "mr_review_requested" in signals:
            score += 2
            reasons.append("review requested")
        if any(s.startswith("todo:") for s in signals):
            score += 1
            reasons.append("pending to-do")
        priority = (item.get("priority") or "").lower()
        if priority in ("highest", "blocker", "critical"):
            score += 2
            reasons.append(priority + " priority")
        elif priority == "high":
            score += 1
            reasons.append("high priority")
        dates = [("due", item.get("due_date"))]
        dates += [
            ("release", version.get("release_date"))
            for version in item.get("versions") or []
        ]
        for kind, raw in dates:
            if not raw:
                continue
            try:
                days = (dt.date.fromisoformat(raw[:10]) - today).days
            except ValueError:
                continue
            if days < 0:
                score += 4
                reasons.append(f"{kind} date passed")
            elif days <= 2:
                score += 3
                reasons.append(f"{kind} in {days} days")
            elif days <= 7:
                score += 1
                reasons.append(f"{kind} in {days} days")
        if any(
            "block" in (relation.get("type") or "").lower()
            for relation in item.get("relations") or []
        ):
            ease = "small next action: resolve dependency; completion effort unknown"
        elif (
            "review_requested" in signals or "mr_review_requested" in signals
        ) and not item.get("draft"):
            ease = "ready to inspect; review effort unknown"
        else:
            ease = "needs scoping; effort unknown"
        hints.append(
            {
                "id": item.get("id"),
                "url": item.get("url"),
                "signal_score": score,
                "score_reasons": reasons,
                "start_ease": ease,
            }
        )
    return sorted(hints, key=lambda hint: (-hint["signal_score"], hint["id"] or ""))


def collect(config: dict[str, str], cap: int = 500) -> dict:
    snapshot = {
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "items": [],
        "source_status": {},
        "limits": {"per_query_cap": cap},
    }
    for name, collector in (
        ("jira", collect_jira),
        ("gitlab", collect_gitlab),
        ("github", collect_github),
    ):
        rows, status = collector(config, cap)
        snapshot["items"].extend(rows)
        snapshot["source_status"][name] = status
    snapshot["source_status"]["slack"] = {"state": "inspect_in_claude_mcp"}
    snapshot["source_status"]["aem"] = {"state": "inspect_in_claude_mcp"}
    snapshot["triage_hints"] = triage_hints(snapshot)
    return snapshot


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2, ensure_ascii=False)
        output.write("\n")


def initialize() -> dict:
    root = profile_root()
    created = []
    for template in DEFAULTS.rglob("*.md"):
        target = root / template.relative_to(DEFAULTS)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(target, 0o600)
            created.append(str(target))
    global_contract = root.parent / "CLAUDE.md"
    import_line = "@work-hub/CLAUDE.md"
    global_contract.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if global_contract.exists():
        text = global_contract.read_text(encoding="utf-8")
        if import_line not in text:
            global_contract.write_text(
                text.rstrip() + "\n\n" + import_line + "\n", encoding="utf-8"
            )
            created.append(str(global_contract) + " (import appended)")
    else:
        global_contract.write_text(
            "# Personal Claude instructions\n\n" + import_line + "\n", encoding="utf-8"
        )
        created.append(str(global_contract))
    return {
        "profile_root": str(root),
        "global_contract": str(global_contract),
        "created": created,
    }


def snapshot_path() -> Path:
    return profile_root() / "cache/latest-snapshot.json"


def make_brief(cwd: Path, cap: int) -> dict:
    config = load_config(cwd)
    snapshot = collect(config, cap)
    diagnostics = config_diagnostics(cwd)
    path = snapshot_path()
    private_json(path, snapshot)
    by_id = {item["id"]: item for item in snapshot["items"]}
    candidates = []
    for hint in snapshot["triage_hints"][:60]:
        item = by_id[hint["id"]]
        candidates.append({**item, "triage_hint": hint})
    return {
        "snapshot_path": str(path),
        "collected_at": snapshot["collected_at"],
        "total_items": len(snapshot["items"]),
        "source_status": snapshot["source_status"],
        "config": diagnostics,
        "candidate_items": candidates,
        "candidate_limit": 60,
    }


def select_ticket(key: str) -> dict:
    if not KEY.fullmatch(key):
        raise IntakeError("Select a Jira issue key such as PROJ-123")
    path = snapshot_path()
    if not path.is_file():
        raise IntakeError("No live snapshot; run personal-workflow first")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    issue = next(
        (
            item
            for item in snapshot["items"]
            if item.get("source") == "jira" and item.get("key") == key
        ),
        None,
    )
    if issue is None:
        raise IntakeError("The selected Jira issue is not in the current work brief")
    args = [key]
    links = []
    for item in snapshot["items"]:
        if key in (item.get("issue_keys") or []) and item.get("kind") in (
            "merge_request",
            "pull_request",
        ):
            links.append(item["url"])
            args.extend(["--pr", item["url"]])
    args.append("--draft")
    return {
        "ticket": key,
        "issue_url": issue["url"],
        "issue": issue,
        "linked_reviews": sorted(set(links)),
        "skill": "docs-tools:docs-orchestrator",
        "args": args,
        "jira_graph_skill": "docs-tools:jira-reader",
        "jira_graph_args": ["--graph", key],
        "mode": "draft",
    }


def memory_target(kind: str, name: str | None = None) -> dict:
    fixed = {
        "preference": "persona.md",
        "lesson": "memory/lessons.md",
        "candidate": "memory/candidates.md",
    }
    if kind in fixed:
        relative = fixed[kind]
    elif kind in ("project", "checklist"):
        slug = re.sub(r"[^a-z0-9-]", "-", (name or "").strip().lower()).strip("-")
        if not slug or slug in ("index", "template"):
            raise IntakeError(f"A specific {kind} name is required")
        relative = f"{'projects' if kind == 'project' else 'checklists'}/{slug}.md"
    else:
        raise IntakeError("Unknown memory kind")
    path = profile_root() / relative
    return {
        "kind": kind,
        "path": str(path),
        "exists": path.is_file(),
        "index": str(
            profile_root()
            / ("projects/index.md" if kind == "project" else "checklists/index.md")
        )
        if kind in ("project", "checklist")
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    brief = subparsers.add_parser("brief")
    brief.add_argument("--cap", type=int, default=500)
    select = subparsers.add_parser("select")
    select.add_argument("key")
    target = subparsers.add_parser("target")
    target.add_argument(
        "kind", choices=["preference", "project", "checklist", "lesson", "candidate"]
    )
    target.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize()
        elif args.command == "brief":
            if args.cap < 1:
                raise IntakeError("--cap must be positive")
            result = make_brief(Path.cwd(), args.cap)
        elif args.command == "select":
            result = select_ticket(args.key)
        else:
            result = memory_target(args.kind, args.name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (IntakeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
