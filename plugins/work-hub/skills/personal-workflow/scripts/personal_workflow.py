#!/usr/bin/env python3
"""Read-only cross-system work intake for the work-hub plugin.

All commands emit JSON. Credentials are read as data from the active repository's
.env and ~/.env; process variables win, matching docs-tools. A legacy Work Hub
.env is a fallback for users of version 0.1.0. No command
prints credential values or writes to Jira, GitLab, or GitHub.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from types import SimpleNamespace
from pathlib import Path

import work_memory as memory


SKILL_ROOT = Path(__file__).resolve().parents[1]
TRACKER_SCRIPTS = SKILL_ROOT.parent / "tracker/scripts"
sys.path.insert(0, str(TRACKER_SCRIPTS))
try:
    from work_tracker.tls import certificate_failure, https_context
finally:
    sys.path.pop(0)

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


def config_paths(cwd: Path) -> tuple[tuple[str, Path], ...]:
    return (
        ("legacy_work_hub", profile_root() / ".env"),
        ("global", Path.home() / ".env"),
        ("active_directory", cwd / ".env"),
    )


def resolved_config(cwd: Path) -> tuple[dict[str, str], dict[str, dict], list[str]]:
    """Resolve values and safe provenance without exposing credentials."""
    values: dict[str, str] = {}
    sources: dict[str, dict] = {}
    warnings: list[str] = []
    for scope, path in config_paths(cwd):
        for key, value in parse_env_file(path).items():
            values[key] = value
            sources[key] = {"scope": scope, "path": str(path)}
    for key in sorted(ENV_KEYS):
        value = os.environ.get(key)
        if not value:
            continue
        file_source = sources.get(key)
        matches_file = key in values and values[key] == value
        if key in values and values[key] != value:
            warnings.append(
                f"{key} from the inherited process environment overrides "
                f"{sources[key]['path']}; restart the terminal and Claude Code "
                "after updating that .env file"
            )
        values[key] = value
        sources[key] = {"scope": "process_environment"}
        if matches_file and file_source and file_source.get("path"):
            sources[key]["matches_file"] = file_source["path"]
    if not values.get("GITHUB_API_URL"):
        values["GITHUB_API_URL"] = "https://api.github.com"
        sources["GITHUB_API_URL"] = {"scope": "default"}
    return values, sources, warnings


def load_config(cwd: Path) -> dict[str, str]:
    return resolved_config(cwd)[0]


def config_diagnostics(cwd: Path) -> dict:
    files = []
    _, sources, warnings = resolved_config(cwd)
    for label, path in config_paths(cwd):
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
    global_file = Path.home() / ".env"
    project_file = cwd / ".env"
    setup = None
    if not any(f["scope"] in ("global", "active_directory") for f in files):
        setup = {
            "message": "No docs-tools-compatible .env found. For connected sources, create the global file for use across projects, or the active project's file for project-only use.",
            "recommended_file": str(global_file),
            "alternative_file": str(project_file),
            "format": "KEY=value",
            "keys_by_source": {
                "jira": ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
                "gitlab": ["GITLAB_URL", "GITLAB_TOKEN"],
                "github": ["GITHUB_TOKEN"],
            },
        }
    return {
        "files_read": files,
        "key_sources": {key: sources[key] for key in sorted(sources)},
        "warnings": warnings,
        "setup": setup,
    }


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
        with urllib.request.urlopen(request, timeout=25, context=https_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        path = urllib.parse.urlsplit(url).path
        raise IntakeError(f"HTTP {exc.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        path = urllib.parse.urlsplit(url).path
        reason = "TLS certificate verification failed" if certificate_failure(exc) else type(exc).__name__
        raise IntakeError(f"Request failed for {path}: {reason}") from None


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
        "status_category": (
            (fields.get("status") or {}).get("statusCategory") or {}
        ).get("key"),
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
        account = request_json(f"{base}/rest/api/3/myself", jira_headers(config))
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
    identity = account.get("accountId") or account.get("key") or account.get("name")
    return list(items.values()), {
        "state": state,
        "scope": memory.scope_id(base, identity),
        "api_base": base,
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
        account = request_json(api + "/user", headers)
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
        "scope": memory.scope_id(api, account.get("id")),
        "api_base": api,
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
        account = request_json(base + "/user", headers)
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
        "scope": memory.scope_id(base, account.get("id")),
        "api_base": base,
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
        "collected_at": now_utc(),
        "brief_id": uuid.uuid4().hex,
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
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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


def tracked_path() -> Path:
    return profile_root() / "cache/work-history.sqlite3"


def read_private_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, dict) else default


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def item_summary(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "url": item.get("url"),
        "status": item.get("status"),
    }


def snapshot_changes(previous: dict, current: dict) -> dict:
    """Compare only sources whose two runs had complete coverage."""
    display_limit = 50
    result = {
        "previous_snapshot_at": previous.get("collected_at"),
        "display_limit": display_limit,
        "new_items_count": 0,
        "new_items": [],
        "no_longer_visible_count": 0,
        "no_longer_visible": [],
        "status_changes_count": 0,
        "status_changes": [],
        "incomparable_sources": [],
    }
    if not previous.get("collected_at"):
        result["note"] = "First snapshot; this run establishes the comparison baseline."
        return result
    old_status = previous.get("source_status") or {}
    new_status = current.get("source_status") or {}
    comparable = set()
    for source in ("jira", "gitlab", "github"):
        old_source = old_status.get(source, {})
        new_source = new_status.get(source, {})
        if (
            old_source.get("state") == new_source.get("state") == "ok"
            and old_source.get("scope")
            and old_source["scope"] == new_source.get("scope")
        ):
            comparable.add(source)
        else:
            result["incomparable_sources"].append(source)
    old = {
        item["id"]: item
        for item in previous.get("items", [])
        if item.get("source") in comparable and item.get("id")
    }
    new = {
        item["id"]: item
        for item in current.get("items", [])
        if item.get("source") in comparable and item.get("id")
    }
    new_ids = sorted(new.keys() - old.keys())
    missing_ids = sorted(old.keys() - new.keys())
    result["new_items_count"] = len(new_ids)
    result["no_longer_visible_count"] = len(missing_ids)
    result["new_items"] = [item_summary(new[key]) for key in new_ids[:display_limit]]
    result["no_longer_visible"] = [
        item_summary(old[key]) for key in missing_ids[:display_limit]
    ]
    for key in sorted(old.keys() & new.keys()):
        if old[key].get("status") != new[key].get("status"):
            result["status_changes_count"] += 1
            if len(result["status_changes"]) < display_limit:
                result["status_changes"].append(
                    {
                        **item_summary(new[key]),
                        "previous_status": old[key].get("status"),
                    }
                )
    return result


def jira_status(config: dict, base: str, key: str) -> dict:
    try:
        issue = request_json(
            f"{base}/rest/api/3/issue/{key}?fields=summary,status,updated",
            jira_headers(config),
        )
        fields = issue.get("fields") or {}
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key")
        completion = "jira_done" if category == "done" else "jira_not_done"
        return {
            "jira_verification": "verified",
            "jira_status": status.get("name"),
            "jira_status_category": category,
            "jira_updated": fields.get("updated"),
            "title": fields.get("summary"),
            "completion": completion if category else "unknown",
        }
    except IntakeError as exc:
        return {"jira_verification": "unavailable", "reason": str(exc)}


def review_status(config: dict, sources: dict, target: dict) -> str:
    source = target["source"]
    if source == "github":
        headers = {
            "Authorization": "Bearer " + config["GITHUB_TOKEN"],
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        headers = {"PRIVATE-TOKEN": config["GITLAB_TOKEN"]}
    try:
        review = request_json(sources[source]["api_base"] + target["path"], headers)
    except IntakeError:
        return "unknown"
    state = review.get("state")
    if state == "merged" or review.get("merged") is True:
        return "merged"
    return {"open": "open", "opened": "open", "closed": "closed"}.get(state, "unknown")


def refresh_work(cwd_config: dict, snapshot: dict) -> tuple[list[dict], dict, dict]:
    sources = snapshot["source_status"]
    jira = sources.get("jira", {})
    scope = jira.get("scope")
    if not scope:
        return [], {}, {}
    base = jira["api_base"]
    at = snapshot["collected_at"]
    path = tracked_path()
    legacy_path = path.with_name("tracked-jira.json")
    legacy = read_private_json(legacy_path, {})
    with memory.database(path) as db:
        existing = memory.records(db, scope)
        known = {record["key"] for record in existing}
        pending_import = [
            {
                **saved,
                "key": key,
                "url": f"{base}/browse/{key}",
            }
            for key, saved in legacy.items()
            if KEY.fullmatch(key)
            and isinstance(saved, dict)
            and saved.get("url") == f"{base}/browse/{key}"
            and key not in known
            and not memory.legacy_imported(db, saved["url"])
        ]

    @functools.lru_cache(maxsize=None)
    def fetch_jira(key):
        return jira_status(cwd_config, base, key)

    @functools.lru_cache(maxsize=None)
    def fetch_review(url):
        target = memory.review_target(url, sources)
        return review_status(cwd_config, sources, target) if target else "unknown"

    selected = [r for r in existing if r.get("selected_at") or r.get("waiting")]
    selected.extend(pending_import)
    selected.sort(
        key=lambda r: (
            bool(r.get("waiting")),
            r.get("last_worked_at") or r.get("selected_at") or "",
        ),
        reverse=True,
    )
    observations = []
    for record in selected[:25]:
        status = fetch_jira(record["key"])
        reviews = {
            url: fetch_review(url)
            for url, saved in record.get("reviews", {}).items()
            if saved["scope"] == sources.get(saved["source"], {}).get("scope")
        }
        waiting = record.get("waiting") or {}
        dependency = waiting.get("dependency")
        resolved = None
        if dependency and dependency["kind"] == "jira":
            resolved = {
                "jira_done": "ready", "jira_not_done": "waiting"
            }.get(fetch_jira(dependency["key"]).get("completion"), "unknown")
        elif dependency:
            resolved = {"merged": "ready", "open": "waiting", "closed": "closed"}.get(
                reviews.get(dependency["url"]), "unknown"
            )
        observations.append((record, status, reviews, resolved))

    rows = []
    with memory.database(path, write=True) as db:
        for original, status, reviews, resolved in observations:
            key = original["key"]
            record = memory.read(db, scope, key)
            if key in known:
                # A newer brief or a local edit won the race. Recheck on the next run.
                if record != original or (record.get("observed_at") or "") > at:
                    continue
            else:
                if record or memory.legacy_imported(db, original["url"]):
                    continue
                if status["jira_verification"] != "verified":
                    continue
                record = original.copy()
                memory.event(record, at, "imported", stage=record.get("local_stage"))
                memory.mark_legacy_imported(db, record["url"])

            row = dict(status)
            record["observed_at"] = at
            if status["jira_verification"] == "verified":
                previous = record.get("last_verified_category")
                category = status["jira_status_category"]
                row["newly_completed"] = category == "done" and previous not in (None, "done")
                if (record.get("last_verified_status"), previous) != (status["jira_status"], category):
                    memory.event(
                        record, at, "jira_status", status=status["jira_status"],
                        category=category, url=record["url"],
                    )
                record["last_verified_category"] = category
                record["last_verified_status"] = status["jira_status"]
                record["verified_brief_id"] = snapshot["brief_id"]
                if status.get("title"):
                    record["title"] = status["title"]

            for url, state in reviews.items():
                review = record["reviews"][url]
                if state != "unknown" and review.get("last_verified_state") != state:
                    review["last_verified_state"] = state
                    memory.event(record, at, "review_status", url=url, status=state)
            row.update(work_summary(record, scope))
            row["review_status"] = {
                url: reviews.get(url, "unknown") for url in record.get("reviews", {})
            }
            if record.get("waiting"):
                waiting = record["waiting"]
                if resolved not in (None, "unknown") and waiting.get("last_verified") != resolved:
                    waiting["last_verified"] = resolved
                    memory.event(record, at, "dependency_status", status=resolved,
                                 dependency=waiting["dependency"])
                row["waiting"] = {
                    **waiting,
                    "dependency_status": resolved,
                    "check_due": bool(
                        waiting.get("check_on") and waiting["check_on"] <= dt.date.today().isoformat()
                    ),
                }
            memory.save(db, scope, record)
            rows.append(row)
        counts = {}
        decisions = {}
        for record in memory.records(db, scope):
            if record.get("recommendation"):
                status = recommendation_status(record)
                counts[status] = counts.get(status, 0) + 1
            if record.get("deferred"):
                decisions[record["key"]] = record["deferred"]
        save_snapshot(snapshot)
    return rows, counts, decisions


def recommendation_status(record: dict) -> str:
    events = [e for e in record["history"] if e["at"] >= record["recommendation"]["at"]]
    kinds = {e["kind"] for e in events}
    for outcome in ("published", "merged"):
        if outcome in kinds:
            return outcome
    if record.get("deferred") and "deferred" in kinds:
        return "deferred"
    if any(e.get("stage") in ("draft_result", "reviewed") for e in events):
        return "drafted"
    return "selected" if "selected" in kinds else "pending"


def work_summary(record: dict, scope: str) -> dict:
    return {
        "work_id": memory.work_id(scope, record["key"]),
        "key": record["key"],
        "title": record["title"],
        "url": record["url"],
        "selected_at": record.get("selected_at"),
        "last_worked_at": record.get("last_worked_at"),
        "local_stage": record.get("local_stage"),
        "next_action": record.get("next_action"),
        "deferred": record.get("deferred"),
        "recommendation": record.get("recommendation"),
        "outcomes": record.get("outcomes", {}),
        "recent_history": record.get("history", [])[-8:],
    }


def save_snapshot(snapshot: dict) -> None:
    """Replace the latest snapshot while holding the work database write lock."""
    path = snapshot_path()
    latest = read_private_json(path, {})
    if (latest.get("collected_at") or "") <= snapshot["collected_at"]:
        private_json(path, snapshot)


def tracker_context(config, snapshot, tracked, workspace=None) -> dict:
    from work_tracker import agenda, connected, sources, views
    from work_tracker.store import Store, TrackerError

    try:
        store = Store(workspace or os.environ.get("WORK_HUB_WORKSPACE", "work"))
        connected.mirror_docs(store, snapshot, tracked)
        provider = SimpleNamespace(
            KEY=KEY, jira_status=jira_status, request_json=request_json, IntakeError=IntakeError,
        )
        connected.sync(store, provider, config, snapshot)
        sources.poll(store, reminders_only=True)
        schedule = agenda.agenda(store, days=1)
        return {
            "workspace": store.workspace,
            "today": views.query(store, "today")[:20],
            "inbox": views.query(store, "inbox")[:20],
            "waiting": views.query(store, "waiting")[:20],
            "agenda": schedule["calendar_events"][:20],
            "calendar_errors": schedule["calendar_errors"],
            "notifications": store.notifications()[:20],
        }
    except (TrackerError, OSError, sqlite3.Error, ValueError) as exc:
        return {"error": str(exc)}


def make_brief(cwd: Path, cap: int, *, tracker_workspace: str | None = None) -> dict:
    config = load_config(cwd)
    path = snapshot_path()
    previous = read_private_json(path, {})
    snapshot = collect(config, cap)
    diagnostics = config_diagnostics(cwd)
    changes = snapshot_changes(previous, snapshot)
    tracked_status, recommendations, deferred = refresh_work(config, snapshot)
    if not snapshot["source_status"].get("jira", {}).get("scope"):
        # Non-Jira briefs still work, and use the same lock when replacing the snapshot.
        with memory.database(tracked_path(), write=True):
            save_snapshot(snapshot)
    by_id = {item["id"]: item for item in snapshot["items"]}
    candidates = []
    for hint in snapshot["triage_hints"][:60]:
        item = by_id[hint["id"]]
        candidate = {**item, "triage_hint": hint}
        if item["source"] == "jira" and item.get("key") in deferred:
            candidate["deferred"] = deferred[item["key"]]
        candidates.append(candidate)
    return {
        "brief_id": snapshot["brief_id"],
        "snapshot_path": str(path),
        "collected_at": snapshot["collected_at"],
        "total_items": len(snapshot["items"]),
        "source_status": snapshot["source_status"],
        "changes_since_previous": changes,
        "tracked_jira_work": tracked_status,
        "recommendation_outcomes": recommendations,
        "tracker": tracker_context(config, snapshot, tracked_status, tracker_workspace),
        "config": diagnostics,
        "candidate_items": candidates,
        "candidate_limit": 60,
    }


def active_snapshot(brief_id: str | None = None) -> dict:
    snapshot = read_private_json(snapshot_path(), {})
    if not snapshot.get("brief_id"):
        raise IntakeError("Run personal-workflow to refresh the work brief")
    if brief_id and brief_id != snapshot["brief_id"]:
        raise IntakeError("The brief was replaced. Refresh it before continuing")
    return snapshot


def jira_scope(snapshot: dict) -> str:
    scope = snapshot["source_status"].get("jira", {}).get("scope")
    if not scope:
        raise IntakeError("The brief has no verified Jira account")
    return scope


def snapshot_issue(snapshot: dict, key: str) -> dict | None:
    return next(
        (i for i in snapshot["items"] if i.get("source") == "jira" and i.get("key") == key),
        None,
    )


def select_ticket(key: str, brief_id: str | None = None) -> dict:
    check_key(key)
    with memory.database(tracked_path(), write=True) as db:
        snapshot = active_snapshot(brief_id)
        scope = jira_scope(snapshot)
        issue = snapshot_issue(snapshot, key)
        at = now_utc()
        record = memory.read(db, scope, key)
        if issue is None:
            if not record or record.get("verified_brief_id") != snapshot["brief_id"]:
                raise IntakeError("The selected Jira issue was not verified in the current brief")
            issue = {name: record[name] for name in ("key", "title", "url")}
        record = record or memory.new_record(issue)
        if record.get("selected_brief_id") != snapshot["brief_id"] or record.get("deferred"):
            memory.event(record, at, "selected")
            record["selected_brief_id"] = snapshot["brief_id"]
        record.setdefault("selected_at", at)
        record.setdefault("local_stage", "selected")
        record["last_worked_at"] = at
        record.pop("deferred", None)
        memory.save(db, scope, record)
    # Ticket-key matches are candidates. Only confirmed links go into the draft arguments.
    candidates = sorted({
        item["url"]
        for item in snapshot["items"]
        if key in (item.get("issue_keys") or [])
        and item.get("kind") in ("merge_request", "pull_request")
    })
    links = sorted(
        url for url, review in record.get("reviews", {}).items()
        if review["scope"] == snapshot["source_status"].get(review["source"], {}).get("scope")
    )
    return {
        **work_summary(record, scope),
        "ticket": key,
        "issue_url": issue["url"],
        "issue": issue,
        "linked_reviews": links,
        "review_candidates": candidates,
        "skill": "docs-tools:docs-orchestrator",
        "args": [key, *(arg for url in links for arg in ("--pr", url)), "--draft"],
        "jira_graph_skill": "docs-tools:jira-reader",
        "jira_graph_args": ["--graph", key],
        "mode": "draft",
        "tracked_at": str(tracked_path()),
    }


def check_key(key: str) -> None:
    if not KEY.fullmatch(key):
        raise IntakeError("Use a Jira issue key such as PROJ-123")


def record_scope(key: str, work_id: str | None) -> str:
    check_key(key)
    if not work_id:
        return jira_scope(active_snapshot())
    scope, _, saved_key = work_id.partition("/")
    if not re.fullmatch(r"[a-f0-9]{64}", scope) or saved_key != key:
        raise IntakeError("Use the work ID returned for this Jira issue")
    return scope


def note(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 500 or len(value.splitlines()) != 1:
        raise IntakeError("Use a single line of 1–500 characters")
    return value


def record_recommendation(
    key: str, reason: str, brief_id: str | None, *, deferred: bool = False
) -> dict:
    check_key(key)
    reason = note(reason)
    with memory.database(tracked_path(), write=True) as db:
        snapshot = active_snapshot(brief_id)
        scope = jira_scope(snapshot)
        issue = snapshot_issue(snapshot, key)
        at = now_utc()
        record = memory.read(db, scope, key)
        if not issue and (
            not record or record.get("verified_brief_id") != snapshot["brief_id"]
        ):
            raise IntakeError("Use a Jira issue verified in the current brief")
        record = record or memory.new_record(issue)
        if deferred:
            if (record.get("deferred") or {}).get("reason") != reason:
                record["deferred"] = {"at": at, "reason": reason}
                memory.event(record, at, "deferred", reason=reason)
        else:
            recommendation = record.setdefault("recommendation", {"at": at})
            if recommendation.get("reason") != reason:
                recommendation.update(brief_id=snapshot["brief_id"], reason=reason)
                memory.event(record, at, "recommended", reason=reason)
        memory.save(db, scope, record)
    return work_summary(record, scope)


def update_selected(key: str, command: str, args: argparse.Namespace) -> dict:
    scope = record_scope(key, args.work_id)
    at = now_utc()
    with memory.database(tracked_path(), write=True) as db:
        record = memory.read(db, scope, key)
        if not record or not record.get("selected_at"):
            raise IntakeError("That Jira issue has not been selected in Work Hub")
        if command == "progress":
            next_action = record.get("next_action")
            if args.clear_next_action:
                next_action = None
            elif args.next_action is not None:
                next_action = note(args.next_action)
            if (args.stage, next_action) != (record.get("local_stage"), record.get("next_action")):
                record["local_stage"] = args.stage
                record["next_action"] = next_action
                memory.event(
                    record, at, "progress", stage=args.stage,
                    next_action=record.get("next_action"),
                )
        elif command == "link":
            snapshot = active_snapshot(args.brief_id)
            if jira_scope(snapshot) != scope:
                raise IntakeError("The review must be linked from the same Jira account's brief")
            target = memory.review_target(args.url, snapshot["source_status"])
            if not target:
                raise IntakeError("Use a PR or MR URL on a GitHub or GitLab source verified in the brief")
            reviews = record.setdefault("reviews", {})
            if args.url not in reviews:
                if len(reviews) >= 5:
                    raise IntakeError("Work Hub tracks up to five linked reviews per Jira issue")
                memory.event(record, at, "review_linked", url=args.url)
            if (reviews.get(args.url) or {}).get("scope") != target["scope"]:
                reviews[args.url] = target
        elif command == "wait":
            reason = note(args.reason)
            check_on = dt.date.fromisoformat(args.check_on).isoformat() if args.check_on else None
            dependency = None
            if args.on and KEY.fullmatch(args.on):
                if args.on == key:
                    raise IntakeError("Choose a Jira dependency other than the selected issue")
                dependency = {"kind": "jira", "key": args.on}
            elif args.on in record.get("reviews", {}):
                dependency = {"kind": "review", "url": args.on}
            elif args.on:
                raise IntakeError("Use a Jira issue key or link the PR or MR before waiting on it")
            if not check_on and not dependency:
                raise IntakeError("Provide a check date or a dependency")
            waiting = {"reason": reason, "check_on": check_on, "dependency": dependency}
            previous = record.get("waiting") or {}
            if any(previous.get(key) != value for key, value in waiting.items()):
                record["waiting"] = waiting
                memory.event(record, at, "waiting", **waiting)
        elif command == "clear-wait":
            if record.pop("waiting", None):
                memory.event(record, at, "wait_cleared")
        elif command == "outcome":
            evidence = urllib.parse.urlsplit(args.url)
            if (
                evidence.scheme not in ("http", "https") or not evidence.hostname
                or evidence.username or evidence.password
            ):
                raise IntakeError("Provide a source URL for the outcome")
            outcomes = record.setdefault("outcomes", {})
            if (outcomes.get(args.stage) or {}).get("url") != args.url:
                outcomes[args.stage] = {"url": args.url, "at": at}
                memory.event(record, at, args.stage, url=args.url)
        record["last_worked_at"] = at
        memory.save(db, scope, record)
    return {
        **work_summary(record, scope),
        "waiting": record.get("waiting"),
        "tracked_at": str(tracked_path()),
    }


def record_progress(
    key: str, stage: str, next_action: str | None = None, work_id: str | None = None,
    *, clear_next_action: bool = False,
) -> dict:
    return update_selected(
        key, "progress",
        argparse.Namespace(
            stage=stage, next_action=next_action, work_id=work_id, clear_next_action=clear_next_action
        ),
    )


def work_history(key: str, work_id: str | None) -> dict:
    scope = record_scope(key, work_id)
    with memory.database(tracked_path()) as db:
        record = memory.read(db, scope, key)
    if not record:
        raise IntakeError("There is no saved history for that Jira issue")
    return {
        **work_summary(record, scope),
        "history": record.get("history", []),
        "waiting": record.get("waiting"),
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
    subparsers.add_parser("config")
    brief = subparsers.add_parser("brief")
    brief.add_argument("--cap", type=int, default=500)
    select = subparsers.add_parser("select")
    select.add_argument("key")
    select.add_argument("--brief-id")
    for command in ("recommend", "defer"):
        decision = subparsers.add_parser(command)
        decision.add_argument("key")
        decision.add_argument("--reason", required=True)
        decision.add_argument("--brief-id")
    progress = subparsers.add_parser("progress")
    progress.add_argument("key")
    progress.add_argument(
        "stage",
        choices=["researching", "draft_started", "draft_result", "reviewed", "blocked"],
    )
    next_action = progress.add_mutually_exclusive_group()
    next_action.add_argument("--next-action")
    next_action.add_argument("--clear-next-action", action="store_true")
    link = subparsers.add_parser("link")
    link.add_argument("key")
    link.add_argument("url")
    link.add_argument("--brief-id")
    wait = subparsers.add_parser("wait")
    wait.add_argument("--reason", required=True)
    wait.add_argument("--check-on", metavar="YYYY-MM-DD")
    wait.add_argument("--on", metavar="JIRA-KEY-OR-REVIEW-URL")
    outcome = subparsers.add_parser("outcome")
    outcome.add_argument("key")
    outcome.add_argument("stage", choices=["merged", "published"])
    outcome.add_argument("url")
    clear = subparsers.add_parser("clear-wait")
    history = subparsers.add_parser("history")
    for action in (wait, clear, history):
        action.add_argument("key")
    for action in (progress, link, wait, outcome, clear, history):
        action.add_argument("--work-id")
    target = subparsers.add_parser("target")
    target.add_argument(
        "kind", choices=["preference", "project", "checklist", "lesson", "candidate"]
    )
    target.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize()
        elif args.command == "config":
            result = config_diagnostics(Path.cwd())
        elif args.command == "brief":
            if args.cap < 1:
                raise IntakeError("--cap must be positive")
            result = make_brief(Path.cwd(), args.cap)
        elif args.command == "select":
            result = select_ticket(args.key, args.brief_id)
        elif args.command in ("recommend", "defer"):
            result = record_recommendation(
                args.key, args.reason, args.brief_id, deferred=args.command == "defer"
            )
        elif args.command == "history":
            result = work_history(args.key, args.work_id)
        elif args.command == "progress":
            result = record_progress(
                args.key, args.stage, args.next_action, args.work_id,
                clear_next_action=args.clear_next_action,
            )
        elif args.command in ("link", "wait", "clear-wait", "outcome"):
            result = update_selected(args.key, args.command, args)
        else:
            result = memory_target(args.kind, args.name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (IntakeError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
