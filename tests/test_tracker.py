"""Work Hub tracker flows using a private temporary profile and simulated providers."""

import datetime as dt
import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[1] / "plugins/work-hub/skills/tracker/scripts"
)
sys.path.insert(0, str(SCRIPTS))
from work_tracker import agenda, connected, sources, views
from work_tracker.store import Store, TrackerError, local_today, now, timestamp
sys.path.pop(0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_WORKFLOW_HOME", str(tmp_path / "profile"))
    return Store()


def test_capture_search_saved_view_export_and_workspace(store):
    rows = [
        {"title": "Review launch copy", "source": "email", "url": "https://mail.example/thread/1",
         "due": local_today().isoformat(), "tags": ["Release"]},
        {"title": "Verify CLI flags", "source": "meeting", "url": "https://notes.example/meetings/2"},
        {"title": "Respond to feedback", "source": "slack", "url": "https://chat.example/message/3"},
    ]
    items = connected.capture_many(store, rows)
    assert [i["id"] for i in connected.capture_many(store, rows)] == [i["id"] for i in items]
    release = items[0]
    store.attach(release["id"], "https://git.example/release")
    views.save_view(store, "release-review", {"tag": "release", "layout": "board", "due": "today"})
    assert [i["id"] for i in views.query(store, "release-review")] == [release["id"]]
    assert len(views.query(store, "inbox")) == 3
    store.set_status(release["id"], "done")
    assert views.query(store, "release-review") == []
    assert views.query(store, q="launch")[0]["status"] == "done"
    assert "https://mail.example/thread/1" in views.export(views.query(store, "all"), "csv")
    assert "- [x] Review launch copy" in views.export(views.query(store, "all"), "markdown")
    assert len(json.loads(views.export(views.query(store, "all"), "json"))) == 3
    assert Store("personal").path == store.path
    assert views.query(Store("personal"), "all") == []
    assert [e["kind"] for e in store.history(release["id"])["history"]] == ["captured", "linked", "status"]
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_capture_rolls_back_on_invalid_batch(store):
    with pytest.raises(TrackerError, match="permalink"):
        connected.capture_many(store, [
            {"title": "Valid local task", "source": "manual"},
            {"title": "Missing source URL", "source": "slack"},
        ])
    assert not views.query(store, "all")


def test_projects_checklists_and_dependencies(store):
    project = store.add("Release 4.2", kind="project")
    milestone = store.add("Docs ready", kind="milestone", parent=project["id"])
    store.template("release", ["Verify flags", "Publish docs"])
    checklist = store.apply_template("release", "Launch checklist", parent=milestone["id"])
    verify, publish = checklist["steps"]
    store.depend(publish["id"], verify["id"])
    with pytest.raises(TrackerError, match="cycle"):
        store.depend(verify["id"], publish["id"])
    assert [i["id"] for i in views.query(store, "waiting")] == [publish["id"]]
    assert views.query(store, "projects")[0]["progress"] == {"done": 0, "total": 2, "waiting": 1}
    store.set_status(verify["id"], "done")
    assert not views.query(store, "waiting")
    assert views.query(store, "projects")[0]["progress"] == {"done": 1, "total": 2, "waiting": 0}
    store.undepend(publish["id"], verify["id"])
    assert store.history(publish["id"])["history"][-1]["kind"] == "dependency_removed"


def test_recurrence_snooze_reminders_and_time(store):
    today = local_today()
    habit = store.add("Study", kind="habit", due=today.isoformat(), repeat="daily",
                     remind=(now() - dt.timedelta(minutes=10)).isoformat(), estimate=25)
    store.start_timer(habit["id"])
    store.log_time(habit["id"], 15)
    with pytest.raises(TrackerError, match="active timer"):
        store.start_timer(habit["id"])
    store.edit(habit["id"], snooze=(now() + dt.timedelta(minutes=10)).isoformat())
    sources.poll(store)
    assert store.notifications() == []
    assert views.query(store, "today") == []
    sources.poll(store, clock=now() + dt.timedelta(minutes=11))
    sources.poll(store, clock=now() + dt.timedelta(minutes=12))
    assert len(store.notifications()) == 2
    store.dismiss(store.notifications()[0]["sequence"])
    assert len(store.notifications()) == 1
    assert len(store.notifications(seen=True)) == 2
    result = store.set_status(habit["id"], "done")
    tomorrow = today + dt.timedelta(days=1)
    assert result["next_occurrence"]["due"] == tomorrow.isoformat()
    assert store.set_status(habit["id"], "done")["next_occurrence"] is None
    skipped = store.set_status(result["next_occurrence"]["id"], "skipped")
    assert skipped["next_occurrence"]["due"] == (tomorrow + dt.timedelta(days=1)).isoformat()
    report = views.review(store)
    assert report["active_timer"] is None
    assert report["time_by_item"][0]["actual_minutes"] == 16
    assert report["time_by_item"][0]["planned_minutes"] == 25
    assert report["completed"][0]["id"] == habit["id"]
    assert report["habits"][0] == {"title": "Study", "done": 1, "due": 1}
    monthly = store.add("Renewal", due="2028-01-31", repeat="monthly")
    february = store.set_status(monthly["id"], "done")["next_occurrence"]
    assert february["due"] == "2028-02-29"
    assert store.set_status(february["id"], "done")["next_occurrence"]["due"] == "2028-03-31"


def test_calendar_recurring_events_and_task_export(store, tmp_path):
    calendar = tmp_path / "work.ics"
    calendar.write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:weekly\n"
        "DTSTART;VALUE=DATE:20261109\nDTEND;VALUE=DATE:20261110\n"
        "RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO,WE\nEXDATE;VALUE=DATE:20261111\n"
        "SUMMARY:Review\\, planning\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:weekly\nRECURRENCE-ID;VALUE=DATE:20261116\n"
        "DTSTART;VALUE=DATE:20261117\nSUMMARY:Moved review\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:timed\nDTSTART;TZID=Asia/Kolkata:20261112T090000\n"
        "DTEND;TZID=Asia/Kolkata:20261112T093000\nSUMMARY:Daily sync\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    agenda.add_calendar(store, "Work", str(calendar))
    agenda.add_calendar(store, "Missing", str(tmp_path / "missing.ics"))
    store.add("Finish plan", scheduled="2026-11-13T10:00:00Z", estimate=45)
    result = agenda.agenda(store, "2026-11-09", 12)
    assert [event["start"][:10] for event in result["calendar_events"] if event["all_day"]] == [
        "2026-11-09", "2026-11-17", "2026-11-18",
    ]
    assert any(event["title"] == "Daily sync" and not event["all_day"] for event in result["calendar_events"])
    assert result["items"][0]["title"] == "Finish plan"
    assert "Missing" in result["calendar_errors"]
    exported = agenda.calendar_export(store, views.query(store, "all"))
    assert "DTSTART:20261113T100000Z" in exported
    assert "DTEND:20261113T104500Z" in exported


def test_watch_all_types_and_failed_source_keeps_baseline(store, tmp_path, monkeypatch):
    radar = tmp_path / "signal-bank.jsonl"
    radar.write_text('{"theme_id":"first","date":"2026-11-11","status":"watching"}\n')
    page = "https://site.example/page"
    feed = "https://site.example/feed"
    release = "https://api.github.com/repos/acme/product/releases/latest"
    review = "https://api.github.com/repos/acme/product/pulls/14"
    data = {
        page: b"<p>Waiting</p>", feed: b"<rss><channel><item><guid>a</guid><title>A</title></item></channel></rss>",
        release: b'{"tag_name":"v1"}', review: b'{"merged":false}',
    }

    def fetch(url):
        value = data[url]
        if value is None:
            raise TrackerError("503")
        return value

    monkeypatch.setattr(sources, "fetch", fetch)
    watch = sources.add_watch(store, "Page", page, contains="available")
    sources.add_watch(store, "News", feed, kind="feed")
    sources.add_watch(store, "Product releases", "https://github.com/acme/product", kind="release")
    sources.add_watch(store, "Review", "https://github.com/acme/product/pull/14", kind="review")
    sources.add_watch(store, "Radar", str(radar), kind="radar")
    assert len(sources.poll(store, force=True)) == 5
    assert store.notifications() == []
    data[page] = None
    sources.poll(store, force=True)
    failed = store.history(watch["id"])["item"]["watch"]
    assert failed["error"] == "503"
    assert failed["last_success_at"] < failed["checked_at"]
    data.update({
        page: b"<p>Available now</p>",
        feed: b"<rss><channel><item><guid>b</guid><title>B</title></item></channel></rss>",
        release: b'{"tag_name":"v2"}', review: b'{"merged":true}',
    })
    radar.write_text('{"theme_id":"second","date":"2026-11-12","status":"surfaced"}\n')
    sources.poll(store, force=True)
    sources.poll(store, force=True)
    assert len(store.notifications()) == 5
    assert any("pull request merged" in item["message"] for item in store.notifications())
    store.set_status(watch["id"], "waiting")
    assert watch["id"] not in {row["id"] for row in sources.poll(store, force=True)}


def test_tracker_entrypoint_requires_no_env_and_persists_across_processes(store, tmp_path):
    def cli(*args):
        return subprocess.run([sys.executable, str(SCRIPTS / "tracker.py"), *args], cwd=tmp_path,
                              check=True, capture_output=True, text=True)

    item = json.loads(cli("add", "Call about renewal", "--due", local_today().isoformat()).stdout)
    assert json.loads(cli("list", "--view", "today").stdout)[0]["id"] == item["id"]
    assert json.loads(cli("history", item["id"]).stdout)["history"][0]["kind"] == "captured"
    assert not (tmp_path / ".env").exists()


def test_connected_item_keeps_local_stage_and_verifies_merge(store, monkeypatch):
    workflow = connected.workflow_module()
    source_url = "https://github.com/acme/product/pull/14"
    source_item = {"id": "github:" + source_url, "source": "github", "kind": "pull_request",
                   "title": "Implement change", "url": source_url, "status": "open"}
    snapshot = {"brief_id": "test", "items": [source_item], "source_status": {
        "github": {"scope": "account-one", "api_base": "https://api.github.com"},
    }, "collected_at": timestamp()}
    workflow.private_json(workflow.snapshot_path(), snapshot)
    captured = store.add("Implement change", url=source_url, next_action="Verify new flags")
    item = connected.track(store, source_url, "test")
    assert item["id"] == captured["id"]
    assert item["next_action"] == "Verify new flags"
    assert connected.track(store, source_url)["id"] == item["id"]
    store.set_status(item["id"], "waiting")

    def request(url, headers):
        assert url == "https://api.github.com/repos/acme/product/pulls/14"
        assert headers["Authorization"] == "Bearer fixture"
        return {"state": "closed", "merged": True}

    monkeypatch.setattr(workflow, "request_json", request)
    snapshot.update(items=[], collected_at=timestamp())
    connected.sync(store, workflow, {"GITHUB_TOKEN": "fixture"}, snapshot)
    tracked = store.history(item["id"])
    assert tracked["item"]["status"] == "waiting"
    assert tracked["item"]["connection"]["source_status"] == "merged"
    assert tracked["history"][-1]["kind"] == "source_status"
    snapshot["source_status"]["github"]["scope"] = "account-two"
    snapshot["collected_at"] = timestamp()
    connected.sync(store, workflow, {}, snapshot)
    assert store.history(item["id"])["item"]["connection"]["verification"] == "unknown"


def test_worker_delivers_due_reminder_without_claude(store, tmp_path):
    store.add("Call about renewal", remind=(now() - dt.timedelta(minutes=5)).isoformat())
    process = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "tracker.py"), "run", "--interval", "1"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "background tracker did not emit the due reminder"
        row = json.loads(process.stdout.readline())
        assert row["message"] == "Reminder: Call about renewal"
        assert len(store.notifications()) == 1
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_brief_without_env_shows_local_items_and_exact_setup_paths(store, tmp_path, monkeypatch):
    workflow = connected.workflow_module()
    fake_home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    for key in workflow.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    item = store.add("Check renewal date", due=local_today().isoformat())
    brief = workflow.make_brief(project, 25)
    assert [row["id"] for row in brief["tracker"]["today"]] == [item["id"]]
    assert {key: brief["source_status"][key]["state"] for key in ("jira", "github", "gitlab")} == {
        "jira": "missing", "github": "missing", "gitlab": "missing",
    }
    setup = brief["config"]["setup"]
    assert setup["recommended_file"] == str(fake_home / ".env")
    assert setup["alternative_file"] == str(project / ".env")
    assert setup["keys_by_source"]["jira"] == ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    assert not (fake_home / ".env").exists()
    assert not (project / ".env").exists()


def test_elapsed_habits_are_recorded_once_and_can_be_backfilled(store):
    today = local_today()
    start = today - dt.timedelta(days=4)
    item = store.add("Daily practice", kind="habit", due=start.isoformat(), repeat="daily", color="teal")
    current = views.query(store, "today", color="teal")
    assert [record["due"] for record in current] == [today.isoformat()]
    assert views.review(store)["habits"] == [{"title": "Daily practice", "due": 5, "done": 0}]
    assert views.review(store)["missed_habit_check_ins"] == 4
    assert len(views.query(store, "all")) == 5
    assert store.history(item["id"])["history"][-1]["kind"] == "missed"
    store.set_status(item["id"], "done")
    assert views.review(store)["habits"][0]["done"] == 1
    assert views.review(store)["missed_habit_check_ins"] == 3
    assert len(views.query(store, "all")) == 5


def test_reminder_and_missed_intervals_survive_worker_restart(store, monkeypatch):
    from work_tracker import service

    at = now()
    monkeypatch.setattr(service, "now", lambda: at)
    monkeypatch.setattr(sources, "now", lambda: at)
    store.add("Check launch", remind=(at - dt.timedelta(minutes=1)).isoformat())
    delivered = []
    monkeypatch.setattr(sources, "desktop_notification", lambda message: delivered.append(message) or True)
    initial = service.tick(store, interval=300, desktop=True, refresh=False)
    assert initial["last_success_at"] and initial["missed_total"] == 0
    assert delivered == ["Reminder: Check launch"]
    at += dt.timedelta(minutes=20)
    latest = service.tick(Store(path=store.path), interval=300, desktop=True, refresh=False)
    assert latest["missed_last_run"] == 3
    assert latest["missed_total"] == 3
    assert delivered == ["Reminder: Check launch"]
    assert service.manage(store, "status")["missed_total"] == 3
    with service.run_lock(store) as locked:
        assert locked
        assert service.tick(store, interval=300, desktop=True, refresh=False) == {"running": True}


def test_login_agent_has_isolated_profile_and_correct_command(store, tmp_path, monkeypatch):
    import plistlib
    from work_tracker import service

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(service.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(service, "launchctl", lambda *args, **kw: calls.append(args) or True)
    assert service.manage(store, "enable", interval=300, desktop=True, refresh=True)["installed"]
    path = service.agent_path(store)
    definition = plistlib.loads(path.read_bytes())
    assert definition["RunAtLoad"] and definition["StartInterval"] == 300
    assert definition["ProgramArguments"][-4:] == ["--interval", "300", "--desktop", "--connected"]
    assert definition["EnvironmentVariables"]["PERSONAL_WORKFLOW_HOME"] == str(store.path.parent.parent.resolve())
    assert path.stat().st_mode & 0o777 == 0o600
    assert service.manage(store, "status")["loaded"]
    service.manage(store, "disable")
    assert not path.exists()
    assert [call[0] for call in calls] == ["bootstrap", "print", "bootout"]


def test_backup_restore_preserves_both_databases_and_omits_secrets(store, tmp_path):
    from work_tracker import backup
    import zipfile

    workflow = connected.workflow_module()
    root = store.path.parent.parent
    item = store.add("Fix docs", color="purple")
    store.log_time(item["id"], 30)
    views.save_view(store, "purple tasks", {"color": "purple"})
    with workflow.memory.database(workflow.tracked_path(), write=True) as db:
        workflow.memory.save(db, "example-account", {"key": "DOCS-12", "next_action": "Check examples"})
    (root / ".env").write_text("GITHUB_TOKEN=fixture\n")
    (root / "persona.md").write_text("Use short headings.")
    workflow.snapshot_path().write_text('{"private":"transient intake"}')
    archive = tmp_path / "backup.zip"
    assert backup.create(archive)["files"] == 3
    assert archive.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(archive) as file:
        assert set(file.namelist()) == {
            "manifest.json", "persona.md", "cache/tracker.sqlite3", "cache/work-history.sqlite3",
        }
    target = tmp_path / "restored"
    backup.restore(archive, target)
    restored = Store(path=target / "cache/tracker.sqlite3")
    assert views.query(restored, "purple tasks")[0]["id"] == item["id"]
    assert views.review(restored)["actual_minutes"] == 30
    with workflow.memory.database(target / "cache/work-history.sqlite3") as db:
        assert workflow.memory.read(db, "example-account", "DOCS-12")["next_action"] == "Check examples"
    assert (target / "persona.md").read_text() == "Use short headings."
    assert not (target / ".env").exists()
    with pytest.raises(FileExistsError):
        backup.restore(archive, target)


def test_restore_rejects_corruption_and_path_traversal(tmp_path):
    import zipfile
    from work_tracker import backup

    for name, contents in (("../unsafe.md", "unexpected"), ("cache/tracker.sqlite3", "corrupt")):
        archive = tmp_path / "invalid.zip"
        with zipfile.ZipFile(archive, "w") as file:
            file.writestr("manifest.json", '{"format":1}')
            file.writestr(name, contents)
        target = tmp_path / "restored"
        with pytest.raises((TrackerError, backup.sqlite3.DatabaseError)):
            backup.restore(archive, target)
        assert not target.exists()
    assert not (tmp_path / "unsafe.md").exists()
