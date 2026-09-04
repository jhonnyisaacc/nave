import json

from typer.testing import CliRunner

from cli.main import app
from research.core.contracts import ResearchResult


def test_research_help_is_registered():
    result = CliRunner().invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "report" in result.stdout


def test_research_report_renders_json_and_markdown(tmp_path):
    fixture = {
        "schema_version": 1,
        "workflow": "fixture.scan",
        "status": "NO_SETUP",
        "metadata": {
            "strategy_name": "fixture",
            "strategy_version": "1",
            "run_id": "run-1",
            "decision_time": "2026-09-04T12:00:00+00:00",
            "started_at": "2026-09-04T11:59:00+00:00",
            "completed_at": "2026-09-04T12:00:00+00:00",
            "input_available_at": "2026-09-04T11:58:00+00:00",
        },
        "generated_at": "2026-09-04T12:00:00+00:00",
        "payload": {"scanned": 1},
        "evidence": [],
        "warnings": [],
        "safety_boundary": "READ_ONLY_RESEARCH_ONLY_HUMAN_GATED",
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    json_result = CliRunner().invoke(app, ["research", "report", "--json-file", str(path)])
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["status"] == "NO_SETUP"
    markdown_result = CliRunner().invoke(
        app, ["research", "report", "--json-file", str(path), "--markdown"]
    )
    assert markdown_result.exit_code == 0
    assert "# fixture.scan" in markdown_result.stdout
