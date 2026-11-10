"""Offline tests for the personal-workflow skill script."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "plugins/work-hub/skills/personal-workflow/scripts/personal_workflow.py"
)
SPEC = importlib.util.spec_from_file_location("personal_workflow", SCRIPT)
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


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
            assert (home / ".env").stat().st_mode & 0o777 == 0o600
            assert "JIRA_URL=" in (home / ".env").read_text()
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


def test_brief_saves_private_snapshot_and_selects_live_ticket():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snapshot = {
            "collected_at": "2026-11-10T12:00:00+00:00",
            "source_status": {"jira": {"state": "ok", "count": 1}},
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
        assert "https://gitlab.example.org/mr/4" in plan["args"]


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
