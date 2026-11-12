"""Offline tests for the personal-workflow skill script."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "plugins/work-hub/skills/personal-workflow/scripts/personal_workflow.py"
)
SPEC = importlib.util.spec_from_file_location("personal_workflow", SCRIPT)
workflow = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(workflow)
finally:
    sys.path.pop(0)


JIRA = "https://jira.example.org"
JIRA_SCOPE = workflow.memory.scope_id(JIRA, "writer")
JIRA_SOURCE = {"state": "ok", "api_base": JIRA, "scope": JIRA_SCOPE}


def test_main_init_smoke_and_no_overwrite():
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory) / "profile"
        with patch.dict(os.environ, {"PERSONAL_WORKFLOW_HOME": str(home)}):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                assert workflow.main(["init"]) == 0
            result = json.loads(output.getvalue())
            assert Path(result["profile_root"]).is_dir()
            assert (home / "persona.md").is_file()
            assert "@work-hub/CLAUDE.md" in (home.parent / "CLAUDE.md").read_text()
            assert not (home / ".env").exists()
            (home / "persona.md").write_text("# My changed preferences\n")
            workflow.initialize()
            assert (home / "persona.md").read_text() == "# My changed preferences\n"


def test_jira_auth_failure_is_error_not_empty_inbox():
    config = {
        "JIRA_URL": "https://jira.example.org",
        "JIRA_EMAIL": "writer@example.org",
        "JIRA_API_TOKEN": "stale",
    }
    with patch.object(
        workflow,
        "request_json",
        side_effect=workflow.IntakeError("HTTP 401 for /rest/api/3/myself"),
    ):
        rows, status = workflow.collect_jira(config, 10)
    assert rows == []
    assert status["state"] == "error"
    assert "authentication" in status["errors"][0]


def test_jira_search_pages_without_project_filter():
    pages = [
        {"issues": [{"key": "A-1"}], "nextPageToken": "next"},
        {"issues": [{"key": "B-2"}]},
    ]
    with patch.object(workflow, "request_json", side_effect=pages) as request:
        rows, capped = workflow.jira_search(
            {
                "JIRA_URL": "https://jira.example.org",
                "JIRA_EMAIL": "a",
                "JIRA_API_TOKEN": "t",
            },
            workflow.JIRA_QUERIES["assigned"],
            50,
        )
    assert [row["key"] for row in rows] == ["A-1", "B-2"]
    assert not capped
    assert "project =" not in str(request.call_args_list).lower()


def test_env_local_override_and_no_value_echo():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / ".env").write_text(
            "JIRA_EMAIL=writer@example.org\nJIRA_API_TOKEN=local-secret\n"
        )
        with (
            patch.object(workflow.Path, "home", return_value=root / "other"),
            patch.dict(os.environ, {}, clear=True),
        ):
            (root / "other").mkdir()
            (root / "other/.env").write_text("JIRA_API_TOKEN=global-secret\n")
            config = workflow.load_config(root)
        assert config["JIRA_API_TOKEN"] == "local-secret"
        assert "secret" not in json.dumps(
            {"configured_keys": sorted(k for k, v in config.items() if v)}
        )


def test_existing_env_beats_legacy_work_hub_env():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        profile = root / "profile"
        profile.mkdir()
        (profile / ".env").write_text("JIRA_API_TOKEN=legacy-secret\n")
        (root / ".env").write_text("JIRA_API_TOKEN=project-secret\n")
        with (
            patch.dict(
                os.environ, {"PERSONAL_WORKFLOW_HOME": str(profile)}, clear=True
            ),
            patch.object(workflow.Path, "home", return_value=root / "other"),
        ):
            (root / "other").mkdir()
            (root / "other/.env").write_text("JIRA_API_TOKEN=global-secret\n")
            assert workflow.load_config(root)["JIRA_API_TOKEN"] == "project-secret"


def test_legacy_colon_env_reports_syntax_without_revealing_values():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / ".env").write_text(
            "email: writer@example.org\n"
            "jira_token: jira-secret\n"
            "gitlab_token: gitlab-secret\n"
        )
        with (
            patch.object(workflow.Path, "home", return_value=root / "other"),
            patch.dict(os.environ, {}, clear=True),
        ):
            config = workflow.load_config(root)
            diagnostics = workflow.config_diagnostics(root)
        assert config["JIRA_EMAIL"] == "writer@example.org"
        assert config["JIRA_API_TOKEN"] == "jira-secret"
        assert config["GITLAB_TOKEN"] == "gitlab-secret"
        assert diagnostics["files_read"][-1]["scope"] == "active_directory"
        assert len(diagnostics["warnings"]) == 3
        assert "secret" not in json.dumps(diagnostics)


def test_config_reports_stale_process_override_without_value():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (home / ".env").write_text("JIRA_API_TOKEN=new-secret\n")
        with (
            patch.object(workflow.Path, "home", return_value=home),
            patch.dict(
                os.environ,
                {
                    "JIRA_API_TOKEN": "old-secret",
                    "PERSONAL_WORKFLOW_HOME": str(root / "profile"),
                },
                clear=True,
            ),
        ):
            config = workflow.load_config(root)
            diagnostics = workflow.config_diagnostics(root)
        assert config["JIRA_API_TOKEN"] == "old-secret"
        assert (
            diagnostics["key_sources"]["JIRA_API_TOKEN"]["scope"]
            == "process_environment"
        )
        assert any(
            "JIRA_API_TOKEN" in warning and "restart" in warning
            for warning in diagnostics["warnings"]
        )
        assert "secret" not in json.dumps(diagnostics)


def test_config_reports_exact_setup_location_when_no_env_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        with (
            patch.object(workflow.Path, "home", return_value=home),
            patch.dict(
                os.environ,
                {"PERSONAL_WORKFLOW_HOME": str(root / "profile")},
                clear=True,
            ),
        ):
            diagnostics = workflow.config_diagnostics(root)
        assert diagnostics["files_read"] == []
        assert diagnostics["setup"]["recommended_file"] == str(home / ".env")
        assert diagnostics["setup"]["alternative_file"] == str(root / ".env")
        assert diagnostics["setup"]["keys_by_source"]["jira"] == [
            "JIRA_URL",
            "JIRA_EMAIL",
            "JIRA_API_TOKEN",
        ]


def test_config_cli_emits_safe_diagnostics():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with (
            patch.object(workflow.Path, "home", return_value=root),
            patch.object(workflow.Path, "cwd", return_value=root),
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            assert workflow.main(["config"]) == 0
        result = json.loads(output.getvalue())
        assert result["setup"]["recommended_file"] == str(root / ".env")


def test_brief_saves_private_snapshot_and_selects_live_ticket():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snapshot = {
            "collected_at": "2026-11-10T12:00:00+00:00",
            "brief_id": "initial",
            "source_status": {"jira": JIRA_SOURCE},
            "items": [
                {
                    "id": "jira:DOCS-12",
                    "source": "jira",
                    "key": "DOCS-12",
                    "title": "Update docs",
                    "url": "https://jira.example.org/browse/DOCS-12",
                    "signals": ["assigned"],
                },
                {
                    "id": "gitlab:mr",
                    "source": "gitlab",
                    "kind": "merge_request",
                    "title": "DOCS-12 implementation",
                    "url": "https://gitlab.example.org/mr/4",
                    "issue_keys": ["DOCS-12"],
                    "signals": ["mr_review_requested"],
                },
            ],
        }
        snapshot["triage_hints"] = workflow.triage_hints(snapshot)
        with (
            patch.dict(os.environ, {"PERSONAL_WORKFLOW_HOME": str(root / "profile")}),
            patch.object(workflow, "load_config", return_value={}),
            patch.object(workflow, "collect", return_value=snapshot),
        ):
            result = workflow.make_brief(root, 10)
            assert result["total_items"] == 2
            saved = Path(result["snapshot_path"])
            assert saved.stat().st_mode & 0o777 == 0o600
            plan = workflow.select_ticket("DOCS-12")
        assert plan["skill"] == "docs-tools:docs-orchestrator"
        assert plan["args"][-1] == "--draft"
        assert plan["review_candidates"] == ["https://gitlab.example.org/mr/4"]
    assert plan["args"] == ["DOCS-12", "--draft"]


def test_next_brief_remembers_selected_issue_and_verifies_jira_completion():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        issue = {
            "id": "jira:DOCS-12",
            "source": "jira",
            "key": "DOCS-12",
            "title": "Update docs",
            "url": "https://jira.example.org/browse/DOCS-12",
            "status": "In Progress",
            "status_category": "indeterminate",
            "signals": ["assigned"],
        }
        first = {
            "collected_at": "2026-11-10T12:00:00+00:00",
            "brief_id": "first",
            "source_status": {"jira": JIRA_SOURCE},
            "items": [issue],
        }
        second = {
            "collected_at": "2026-11-11T12:00:00+00:00",
            "brief_id": "second",
            "source_status": {"jira": JIRA_SOURCE},
            "items": [],
        }
        for snapshot in (first, second):
            snapshot["triage_hints"] = workflow.triage_hints(snapshot)
        with (
            patch.dict(os.environ, {"PERSONAL_WORKFLOW_HOME": str(root / "profile")}),
            patch.object(
                workflow,
                "load_config",
                return_value={
                    "JIRA_URL": "https://jira.example.org",
                    "JIRA_EMAIL": "writer@example.org",
                    "JIRA_API_TOKEN": "test-token",
                },
            ),
            patch.object(workflow, "config_diagnostics", return_value={}),
            patch.object(workflow, "collect", side_effect=[first, second]),
            patch.object(
                workflow,
                "request_json",
                return_value={
                    "fields": {
                        "status": {"name": "Done", "statusCategory": {"key": "done"}},
                        "updated": "2026-11-11T08:00:00+00:00",
                    }
                },
            ) as request,
        ):
            initial = workflow.make_brief(root, 10)
            assert initial["changes_since_previous"]["previous_snapshot_at"] is None
            workflow.select_ticket("DOCS-12")
            workflow.record_progress("DOCS-12", "draft_result")
            next_day = workflow.make_brief(root, 10)
            tracked_file = workflow.tracked_path()
            assert tracked_file.stat().st_mode & 0o777 == 0o600
        assert (
            next_day["changes_since_previous"]["no_longer_visible"][0]["id"]
            == "jira:DOCS-12"
        )
        tracked = next_day["tracked_jira_work"][0]
        assert tracked["local_stage"] == "draft_result"
        assert tracked["completion"] == "jira_done"
        assert tracked["newly_completed"] is True
        assert request.call_args.args[0].endswith(
            "/rest/api/3/issue/DOCS-12?fields=summary,status,updated"
        )


def test_disappearance_or_failed_coverage_does_not_prove_completion():
    previous = {
        "collected_at": "2026-11-10T12:00:00+00:00",
        "source_status": {"jira": {"state": "ok"}},
        "items": [{"id": "jira:DOCS-12", "source": "jira", "title": "Update docs"}],
    }
    current = {
        "collected_at": "2026-11-11T12:00:00+00:00",
        "source_status": {"jira": {"state": "partial"}},
        "items": [],
    }
    changes = workflow.snapshot_changes(previous, current)
    assert changes["no_longer_visible"] == []
    assert "jira" in changes["incomparable_sources"]
    with patch.object(
        workflow,
        "request_json",
        side_effect=workflow.IntakeError("HTTP 403 for /rest/api/3/issue/DOCS-12"),
    ):
        status = workflow.jira_status(
            {
                "JIRA_URL": "https://jira.example.org",
                "JIRA_EMAIL": "writer@example.org",
                "JIRA_API_TOKEN": "test-token",
            },
            JIRA,
            "DOCS-12",
        )
    assert status["jira_verification"] == "unavailable"
    assert "completion" not in status


def test_main_brief_smoke_with_mocked_collection():
    with (
        patch.object(
            workflow,
            "make_brief",
            return_value={"source_status": {}, "candidate_items": []},
        ),
        contextlib.redirect_stdout(io.StringIO()) as output,
    ):
        assert workflow.main(["brief", "--cap", "1"]) == 0
    assert json.loads(output.getvalue())["candidate_items"] == []


def test_memory_target_scopes_names_and_rejects_empty():
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(os.environ, {"PERSONAL_WORKFLOW_HOME": directory}),
    ):
        project = workflow.memory_target("project", "Dev Spaces")
        assert project["path"].endswith("/projects/dev-spaces.md")
        assert project["index"].endswith("/projects/index.md")
        assert workflow.memory_target("preference")["path"].endswith("/persona.md")
        try:
            workflow.memory_target("project", "../")
        except workflow.IntakeError:
            pass
        else:
            raise AssertionError("path traversal was accepted")


def test_session_hook_scaffolds_and_nudges_without_storing_prompt():
    hook = SCRIPT.with_name("personal_hook.py")
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory) / "profile"
        env = {**os.environ, "PERSONAL_WORKFLOW_HOME": str(home)}

        def run(mode, event):
            return subprocess.run(
                ["python3", str(hook), mode],
                input=json.dumps(event),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )

        start = json.loads(run("start", {"session_id": "one"}).stdout)
        assert start["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert (home / "persona.md").exists()
        (home / "persona.md").write_text("# Personal edit\n")
        run("start", {"session_id": "one"})
        assert (home / "persona.md").read_text() == "# Personal edit\n"
        assert not run(
            "prompt", {"session_id": "one", "prompt": "Actually, use DITA"}
        ).stdout
        feedback = json.loads(run("stop", {"session_id": "one"}).stdout)
        assert (
            "personalize-workflow"
            in feedback["hookSpecificOutput"]["additionalContext"]
        )
        assert "Actually, use DITA" not in json.dumps(feedback)
        assert not run("stop", {"session_id": "one"}).stdout


def test_work_history_cli_across_briefs_and_provider_failures(tmp_path, monkeypatch, capsys):
    """Run the CLI through an implementation merge and a distinct docs outcome."""
    monkeypatch.setenv("PERSONAL_WORKFLOW_HOME", str(tmp_path / "profile"))
    config = {
        "JIRA_URL": JIRA,
        "JIRA_EMAIL": "writer@example.org",
        "JIRA_API_TOKEN": "test-jira-token",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_TOKEN": "test-github-token",
        "GITLAB_URL": "https://gitlab.example.org",
        "GITLAB_TOKEN": "test-gitlab-token",
    }
    monkeypatch.setattr(workflow, "load_config", lambda cwd: config)
    monkeypatch.setattr(workflow, "config_diagnostics", lambda cwd: {})
    account = "writer"
    phase = "open"
    github_review = "https://github.com/acme/product/pull/17"
    gitlab_review = "https://gitlab.example.org/acme/team/product/-/merge_requests/31"
    requested = []

    def request(url, headers, *, data=None):
        requested.append(url)
        if url == JIRA + "/rest/api/3/myself":
            return {"accountId": account}
        if url.startswith(JIRA + "/rest/api/3/search/jql"):
            if phase in {"open", "alternate"}:
                return {
                    "issues": [
                        {
                            "key": key,
                            "fields": {
                                "summary": title,
                                "status": {
                                    "name": "In Progress",
                                    "statusCategory": {"key": "indeterminate"},
                                },
                            },
                        }
                        for key, title in (
                            ("DOCS-12", "Update upgrade docs"),
                            ("DOCS-13", "Draft release notes"),
                        )
                    ]
                }
            return {"issues": []}
        if url.startswith(JIRA + "/rest/api/3/issue/DOCS-12?"):
            return {"fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}}
        if url.startswith(JIRA + "/rest/api/3/issue/ENG-31?"):
            category = "done" if phase == "merged" else "indeterminate"
            return {"fields": {"status": {"statusCategory": {"key": category}}}}
        if url == "https://api.github.com/user":
            return {"id": 11}
        if url.startswith("https://api.github.com/issues?"):
            return []
        if url.startswith("https://api.github.com/search/issues?"):
            return {"items": [], "total_count": 0}
        if url == "https://api.github.com/repos/acme/product/pulls/17":
            if phase == "unavailable":
                raise workflow.IntakeError("HTTP 503 for review")
            return {"state": "closed", "merged": phase == "merged"}
        if url == "https://gitlab.example.org/api/v4/user":
            return {"id": 12}
        if url == "https://gitlab.example.org/api/v4/projects/acme%2Fteam%2Fproduct/merge_requests/31":
            return {"state": "merged" if phase == "merged" else "opened"}
        if url.startswith("https://gitlab.example.org/api/v4/"):
            return []
        raise AssertionError(url)

    monkeypatch.setattr(workflow, "request_json", request)

    def cli(*args, expected=0):
        assert workflow.main(list(args)) == expected
        return json.loads(capsys.readouterr().out)

    first = cli("brief")
    brief = ("--brief-id", first["brief_id"])
    jira = "DOCS-12"
    cli("recommend", jira, "--reason", "Release is approaching", *brief)
    cli("recommend", "DOCS-13", "--reason", "Prepare the release notes", *brief)
    cli("defer", "DOCS-13", "--reason", "Still waiting for engineering", *brief)
    chosen = cli("select", jira, *brief)
    work = ("--work-id", chosen["work_id"])
    cli("progress", jira, "draft_result", "--next-action", "Verify flag names", *work)
    cli("link", jira, github_review, *work, *brief)
    cli("link", jira, gitlab_review, *work, *brief)
    cli("link", jira, "https://other.example.org/acme/product/pull/17", *work, *brief, expected=1)
    cli("wait", jira, "--reason", "Waiting for implementation", "--on", github_review,
        "--check-on", "2000-01-01", *work)

    phase = "closed"
    closed = cli("brief")
    assert closed["tracked_jira_work"][0]["waiting"]["dependency_status"] == "closed"
    assert closed["tracked_jira_work"][0]["waiting"]["check_due"] is True
    phase = "unavailable"
    failed = cli("brief")
    assert failed["tracked_jira_work"][0]["waiting"]["dependency_status"] == "unknown"
    phase = "merged"
    merged = cli("brief")
    row = merged["tracked_jira_work"][0]
    assert row["waiting"]["dependency_status"] == "ready"
    assert row["review_status"] == {github_review: "merged", gitlab_review: "merged"}
    assert row["completion"] == "jira_done"
    assert row["local_stage"] == "draft_result"
    assert row["next_action"] == "Verify flag names"
    assert merged["recommendation_outcomes"] == {"drafted": 1, "deferred": 1}
    assert cli("select", jira, *brief, expected=1)["error"].startswith("The brief was replaced")

    resumed = cli("select", jira, "--brief-id", merged["brief_id"])
    assert resumed["local_stage"] == "draft_result"
    assert resumed["next_action"] == "Verify flag names"
    assert set(resumed["linked_reviews"]) == {github_review, gitlab_review}
    cli("wait", jira, "--on", "ENG-31", "--reason", "Check engineering ticket", *work)
    assert cli("brief")["tracked_jira_work"][0]["waiting"]["dependency_status"] == "ready"
    cli("clear-wait", jira, *work)
    cli("outcome", jira, "published", "https://docs.example.org/upgrade", *work)
    assert cli("brief")["recommendation_outcomes"] == {"published": 1, "deferred": 1}
    history = cli("history", jira, *work)
    assert history["waiting"] is None
    assert sum(e["kind"] == "review_status" and e.get("status") == "merged"
               for e in history["history"]) == 2
    assert history["outcomes"]["published"]["url"] == "https://docs.example.org/upgrade"

    phase = "alternate"
    account = "different-writer"
    alternate = cli("brief")
    assert alternate["tracked_jira_work"] == []
    assert alternate["recommendation_outcomes"] == {}
    assert alternate["changes_since_previous"]["new_items"] == []
    assert "jira" in alternate["changes_since_previous"]["incomparable_sources"]
    other = cli("select", jira, "--brief-id", alternate["brief_id"])
    assert other["work_id"] != chosen["work_id"]
    assert other["next_action"] is None
    assert any("/pulls/17" in url for url in requested)
    assert workflow.tracked_path().stat().st_mode & 0o777 == 0o600


def test_legacy_stage_imports_once_after_direct_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_WORKFLOW_HOME", str(tmp_path / "profile"))
    scope = JIRA_SCOPE
    key = "DOCS-12"
    url = f"{JIRA}/browse/{key}"
    workflow.private_json(workflow.tracked_path().with_name("tracked-jira.json"), {
        key: {
            "title": "Legacy ticket",
            "url": url,
            "selected_at": "2026-11-10T08:00:00+00:00",
            "local_stage": "reviewed",
        },
        "DOCS-90": {"title": "Other instance", "url": "https://else.example.org/browse/DOCS-90"},
    })
    snapshot = {
        "brief_id": "first",
        "collected_at": "2026-11-12T08:00:00+00:00",
        "source_status": {"jira": JIRA_SOURCE},
    }
    with patch.object(workflow, "jira_status", return_value={"jira_verification": "unavailable"}):
        assert workflow.refresh_work({}, snapshot)[0] == []
    verified = {
        "jira_verification": "verified",
        "jira_status": "In Progress",
        "jira_status_category": "indeterminate",
    }
    with patch.object(workflow, "jira_status", return_value=verified) as check:
        rows, _, _ = workflow.refresh_work({}, snapshot)
        assert len(rows) == 1
        assert rows[0]["local_stage"] == "reviewed"
        assert rows[0]["work_id"] == workflow.memory.work_id(scope, key)
        assert rows[0]["recent_history"][0]["kind"] == "imported"
        check.assert_called_once_with({}, JIRA, key)
        snapshot["source_status"]["jira"] = {**JIRA_SOURCE, "scope": "other-account"}
        assert workflow.refresh_work({}, snapshot)[0] == []
        assert check.call_count == 1
