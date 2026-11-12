"""Private, transactional history for Jira workstreams."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit


def scope_id(base: str, account: str | int | None) -> str | None:
    if account is None or account == "":
        return None
    return hashlib.sha256(json.dumps([base, str(account)]).encode()).hexdigest()


def work_id(scope: str, key: str) -> str:
    return f"{scope}/{key}"


@contextmanager
def database(path: Path, *, write: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS work (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS legacy_import (
                    url TEXT PRIMARY KEY
                )"""
            )
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
    finally:
        connection.close()


def read(connection, scope: str, key: str) -> dict | None:
    row = connection.execute(
        "SELECT data FROM work WHERE scope = ? AND key = ?", (scope, key)
    ).fetchone()
    return json.loads(row[0]) if row else None


def records(connection, scope: str) -> list[dict]:
    rows = connection.execute("SELECT data FROM work WHERE scope = ?", (scope,))
    return [json.loads(row[0]) for row in rows]


def save(connection, scope: str, record: dict) -> None:
    connection.execute(
        """INSERT INTO work (scope, key, data) VALUES (?, ?, ?)
           ON CONFLICT (scope, key) DO UPDATE SET data = excluded.data""",
        (scope, record["key"], json.dumps(record)),
    )


def legacy_imported(connection, url: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM legacy_import WHERE url = ?", (url,)
    ).fetchone() is not None


def mark_legacy_imported(connection, url: str) -> None:
    connection.execute("INSERT INTO legacy_import VALUES (?)", (url,))


def event(record: dict, at: str, kind: str, **details) -> None:
    record.setdefault("history", []).append({"at": at, "kind": kind, **details})


def new_record(issue: dict) -> dict:
    return {
        "key": issue["key"],
        "title": issue["title"],
        "url": issue["url"],
        "last_verified_status": issue.get("status"),
        "last_verified_category": issue.get("status_category"),
    }


def review_target(url: str, sources: dict) -> dict | None:
    """Build an API path on a configured provider from its normal review URL."""
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    for source, suffix in (("github", "/api/v3"), ("gitlab", "/api/v4")):
        status = sources.get(source, {})
        scope = status.get("scope")
        api = status.get("api_base", "")
        if not scope or not api:
            continue
        web = "https://github.com" if api == "https://api.github.com" else api.removesuffix(suffix)
        base = urlsplit(web)
        prefix = base.path.rstrip("/") + "/"
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
            or not parsed.path.startswith(prefix)
        ):
            continue
        path = parsed.path[len(prefix):]
        if source == "github":
            match = re.fullmatch(r"([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)", path, re.ASCII)
        else:
            match = re.fullmatch(r"([\w./-]+)/-/merge_requests/([1-9][0-9]*)", path, re.ASCII)
        if not match or any(part in {"", ".", ".."} for part in match[1].split("/")):
            continue
        project, number = match.groups()
        api_path = (
            f"/repos/{project}/pulls/{number}"
            if source == "github"
            else f"/projects/{quote(project, safe='')}/merge_requests/{number}"
        )
        return {"source": source, "scope": scope, "path": api_path, "url": url}
    return None
