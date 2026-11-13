"""Back up local Work Hub data and restore it into a fresh profile."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .store import TrackerError, profile_root, timestamp

DATABASES = {
    "cache/tracker.sqlite3": {"items", "events", "settings", "notifications", "relations", "time_entries"},
    "cache/work-history.sqlite3": {"work", "legacy_import"},
}
LEGACY = "cache/tracked-jira.json"
MANIFEST = "manifest.json"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        str(path) == name and not path.is_absolute() and ".." not in path.parts
        and "\\" not in name and not any(part.startswith(".") for part in path.parts)
        and (name in DATABASES or name == LEGACY or path.suffix == ".md")
    )


def copy_database(source: Path, target: Path):
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        result = sqlite3.connect(target)
        try:
            db.backup(result)
        finally:
            result.close()
    finally:
        db.close()


def create(destination: Path, root: Path | None = None) -> dict:
    root = (root or profile_root()).resolve()
    destination = destination.expanduser().resolve()
    if destination.is_relative_to(root):
        raise TrackerError("Save the backup outside the Work Hub profile.")
    if destination.exists():
        raise TrackerError("Choose a new backup filename.")
    entries = sorted(
        path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
        and allowed(path.relative_to(root).as_posix())
    )
    fd, temporary = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "w+b") as file, tempfile.TemporaryDirectory() as staging:
            with zipfile.ZipFile(file, "w", zipfile.ZIP_DEFLATED) as archive:
                for source in entries:
                    name = source.relative_to(root).as_posix()
                    if name in DATABASES:
                        copy = Path(staging) / source.name
                        copy_database(source, copy)
                        source = copy
                    archive.write(source, name)
                archive.writestr(MANIFEST, json.dumps({"format": 1, "created_at": timestamp()}))
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"backup": str(destination), "files": len(entries)}


def restore(source: Path, destination: Path) -> dict:
    destination = destination.expanduser().absolute()
    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(source.expanduser()) as archive:
            manifest = archive.getinfo(MANIFEST)
            if manifest.file_size > 4096:
                raise TrackerError("Invalid Work Hub backup manifest.")
            metadata = json.loads(archive.read(manifest))
            if not isinstance(metadata, dict) or metadata.get("format") != 1:
                raise TrackerError("Unsupported Work Hub backup format.")
            entries = [item for item in archive.infolist() if item.filename != MANIFEST]
            if (len({item.filename for item in entries}) != len(entries)
                    or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES
                    or any(not allowed(item.filename) for item in entries)):
                raise TrackerError("Invalid Work Hub backup contents.")
            for item in entries:
                target = destination / item.filename
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(item) as incoming, target.open("xb") as output:
                    target.chmod(0o600)
                    shutil.copyfileobj(incoming, output)
                if item.filename in DATABASES:
                    db = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
                    try:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise TrackerError("A database in the backup failed its integrity check.")
                        tables = {
                            row[0] for row in db.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if not DATABASES[item.filename] <= tables:
                            raise TrackerError("A database in the backup is missing Work Hub tables.")
                    finally:
                        db.close()
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {"profile": str(destination), "files": len(entries)}
