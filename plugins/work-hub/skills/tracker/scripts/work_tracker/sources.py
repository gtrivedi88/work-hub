"""Read-only watches and calendar sources."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .tls import certificate_failure, https_context
from .store import CLOSED, Store, TrackerError, now, timestamp, validate_text, web_url

WATCH_TYPES = ("page", "feed", "release", "review", "radar")
MAX_SOURCE_BYTES = 4 * 1024 * 1024


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urlsplit(req.full_url)
        new = urlsplit(web_url(newurl))
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise TrackerError("The source redirected to a different origin. Use its final URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> bytes:
    url = web_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Work-Hub/0.3"})
    try:
        opener = urllib.request.build_opener(
            SameOriginRedirect(), urllib.request.HTTPSHandler(context=https_context()),
        )
        with opener.open(request, timeout=10) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise TrackerError(f"Source returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        if certificate_failure(exc):
            raise TrackerError("TLS certificate verification failed. Check Python's trusted CA bundle.") from exc
        raise TrackerError("Could not reach the source.") from exc
    if len(data) > MAX_SOURCE_BYTES:
        raise TrackerError("Source exceeds the 4 MiB limit.")
    return data


def local_data(path: str) -> bytes:
    with Path(path).expanduser().open("rb") as file:
        data = file.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise TrackerError("Source exceeds the 4 MiB limit.")
    return data


def github_endpoint(url: str, kind: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise TrackerError("Use a public https://github.com URL for this watch.")
    suffix = r"(?:/releases(?:/latest)?)?" if kind == "release" else r"/pull/([1-9]\d*)"
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)" + suffix + "/?", parsed.path)
    if not match or any(part in (".", "..") for part in match[1].split("/")):
        raise TrackerError("Use a GitHub repository URL for a release or a PR URL for a review.")
    route = "releases/latest" if kind == "release" else f"pulls/{match[2]}"
    return f"https://api.github.com/repos/{match[1]}/{route}"


def add_watch(store: Store, title: str, target: str, *, kind="page", contains=None,
              every=60, parent=None) -> dict:
    if kind not in WATCH_TYPES or type(every) is not int or every < 1:
        raise TrackerError("Use a valid watch type and a positive interval in minutes.")
    if contains:
        contains = validate_text(contains, "Watch text", 200)
    if kind == "review" and contains:
        raise TrackerError("Review watches alert when a PR merges.")
    if kind == "radar":
        target = str(Path(target).expanduser().resolve())
        if Path(target).is_dir():
            target = str(Path(target) / "signal-bank.jsonl")
        endpoint = target
    else:
        target = web_url(target)
        endpoint = github_endpoint(target, kind) if kind in ("release", "review") else target
    origin = f"watch:{kind}:{target}:{contains or ''}"
    return store.add(
        title, kind="watch", source="radar" if kind == "radar" else "web",
        url=target if kind != "radar" else None, origin=origin, parent=parent,
        watch={"type": kind, "target": endpoint, "contains": contains, "interval_minutes": every},
    )


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def feed_entries(data: bytes) -> dict:
    root = ET.fromstring(data)
    entries = {}
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        fields = {child.tag.split("}")[-1]: "".join(child.itertext()).strip() for child in node}
        title = fields.get("title", "")
        identity = fields.get("guid") or fields.get("id") or fields.get("link") or title
        if identity:
            entries[identity] = title or identity
    return entries


def observe(watch: dict) -> tuple[object, str | None]:
    kind = watch["type"]
    target = watch["target"]
    contains = (watch.get("contains") or "").casefold()
    data = local_data(target) if kind == "radar" else fetch(target)
    if kind == "page":
        page = PageText()
        page.feed(data.decode("utf-8", errors="replace"))
        visible = " ".join(" ".join(page.parts).split())
        if contains:
            found = contains in visible.casefold()
            return found, "matching text appeared" if found else None
        return hashlib.sha256(visible.encode()).hexdigest(), "page text changed"
    if kind == "release":
        release = json.loads(data)
        tag = release["tag_name"]
        found = not contains or contains in f"{tag} {release.get('name') or ''}".casefold()
        return tag, f"release {tag}" if found else None
    if kind == "review":
        review = json.loads(data)
        merged = review.get("merged") is True
        return merged, "pull request merged" if merged else None
    if kind == "radar":
        entries = {}
        for line in data.decode().splitlines():
            if not line.strip():
                continue
            signal = json.loads(line)
            name = signal.get("theme") or signal.get("theme_id") or ""
            date = signal.get("date", "")
            if name and date and signal.get("status") in ("surfaced", "watching"):
                identity = f"{signal.get('theme_id') or name}/{date}"
                entries[identity] = f"{name} ({date})"
    else:
        entries = feed_entries(data)
    entries = {key: title for key, title in entries.items() if contains in title.casefold()}
    return entries, None


def poll(store: Store, *, force: bool = False, clock: dt.datetime | None = None,
         reminders_only: bool = False) -> list[dict]:
    at = clock or now()
    stamp = timestamp(at)
    with store.db(write=True) as db:
        items = store.all(db)
        for item in items:
            if item["status"] in CLOSED:
                continue
            snooze = item["snoozed_until"]
            if snooze and snooze > stamp:
                continue
            if snooze:
                store.notify(db, item["id"], f"snooze:{snooze}", f"Snoozed item is back: {item['title']}", stamp)
            reminder = item["remind_at"]
            if reminder and reminder <= stamp:
                store.notify(db, item["id"], f"reminder:{reminder}", f"Reminder: {item['title']}", stamp)
    checked = []
    if reminders_only:
        return checked
    for item in items:
        watch = item.get("watch")
        if not watch or item["status"] in CLOSED | {"waiting"} or (item["snoozed_until"] or "") > stamp:
            continue
        last = watch.get("checked_at")
        if last and not force and at < dt.datetime.fromisoformat(last) + dt.timedelta(minutes=watch["interval_minutes"]):
            continue
        try:
            value, message = observe(watch)
            error = None
        except (TrackerError, OSError, ValueError, ET.ParseError, KeyError, TypeError) as exc:
            value, message, error = None, None, str(exc)[:250]
        with store.db(write=True) as db:
            current = store.get(db, item["id"])
            if current["watch"] != watch or current["status"] in CLOSED | {"waiting"}:
                continue
            updated = {**watch, "checked_at": stamp, "error": error}
            if error is None:
                updated["last_success_at"] = stamp
                previous = watch.get("value")
                if isinstance(value, dict) and isinstance(previous, dict):
                    added = sorted(set(value) - set(previous))
                    if added:
                        message = "new signals: " + ", ".join(value[key] for key in added[:5])
                    # Retain recent observed IDs to avoid re-alerting when a feed drops entries.
                    updated["value"] = dict(list({**previous, **value}.items())[-1000:])
                else:
                    updated["value"] = value
                if previous is not None and value != previous and message:
                    message = f"{item['title']}: {message}"[:500]
                    store.notify(db, item["id"], f"watch:{stamp}", message, stamp)
                    store.event(db, item["id"], "watch_changed", {"message": message}, stamp)
            current["watch"] = updated
            store.save(db, current)
            checked.append({"id": item["id"], "checked_at": stamp, "error": error})
    return checked


def desktop_notification(message: str) -> bool:
    if shutil.which("osascript"):
        command = [
            "osascript", "-e",
            'on run argv\n display notification (item 1 of argv) with title "Work Hub"\nend run',
            message,
        ]
    elif shutil.which("notify-send"):
        command = ["notify-send", "Work Hub", message]
    else:
        return False
    try:
        return subprocess.run(command, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
