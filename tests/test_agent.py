"""
tests/test_agent.py - Tests for agent orchestration and report building.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import build_report, _dispatch_tool, _summarise_result, _fmt_args


class TestBuildReport:
    """Tests for the report assembly and validation logic."""

    def _make_violation(self, **overrides):
        base = {
            "rule_id": "SEC-001",
            "category": "security",
            "severity": "critical",
            "file": "config.py",
            "line": 17,
            "description": "Hardcoded API credential",
            "evidence": "Credential assigned directly in source code",
            "recommendation": "Use a secrets manager or environment variable",
        }
        base.update(overrides)
        return base

    def test_valid_violation_passes_through(self):
        v = self._make_violation()
        report = build_report("https://github.com/x/y", [v])
        assert len(report["violations"]) == 1
        assert report["violations"][0]["rule_id"] == "SEC-001"

    def test_missing_field_violation_dropped(self):
        v = {"rule_id": "SEC-001"}  # missing required fields
        report = build_report("https://github.com/x/y", [v])
        assert len(report["violations"]) == 0

    def test_invalid_severity_normalised(self):
        v = self._make_violation(severity="bad_value")
        report = build_report("https://github.com/x/y", [v])
        assert report["violations"][0]["severity"] == "medium"

    def test_empty_string_file_becomes_null(self):
        v = self._make_violation(file="")
        report = build_report("https://github.com/x/y", [v])
        assert report["violations"][0]["file"] is None

    def test_zero_line_becomes_null(self):
        v = self._make_violation(line=0)
        report = build_report("https://github.com/x/y", [v])
        assert report["violations"][0]["line"] is None

    def test_report_structure(self):
        report = build_report("https://github.com/test/repo", [])
        assert "repository" in report
        assert "violations" in report
        assert report["violations"] == []

    def test_multiple_valid_violations(self):
        violations = [
            self._make_violation(rule_id="SEC-001"),
            self._make_violation(rule_id="HYG-001", category="repository_hygiene",
                                 severity="medium", file=None, line=None),
        ]
        report = build_report("https://github.com/x/y", violations)
        assert len(report["violations"]) == 2

    def test_non_dict_violation_dropped(self):
        report = build_report("https://github.com/x/y", ["not a dict", 42, None])
        assert len(report["violations"]) == 0


class TestHelpers:
    """Tests for internal helper functions."""

    def test_fmt_args_short(self):
        result = _fmt_args({"repo_path": "/tmp/x", "pattern": "test"})
        assert "repo_path" in result

    def test_fmt_args_truncates_long_values(self):
        result = _fmt_args({"key": "x" * 200})
        assert "..." in result

    def test_summarise_result_truncates(self):
        big_data = {"data": "x" * 10_000}
        result = _summarise_result("some_tool", big_data, max_chars=1000)
        assert len(result) <= 1100  # slight buffer for truncation message
        assert "truncated" in result

    def test_summarise_result_short_data(self):
        data = {"key": "value"}
        result = _summarise_result("tool", data, max_chars=1000)
        assert "key" in result
        assert "truncated" not in result


class TestDispatchTool:
    """Tests for tool dispatcher with mock repo."""

    def test_dispatch_unknown_tool(self, tmp_path):
        result = _dispatch_tool("totally_unknown", {}, str(tmp_path))
        assert "error" in result

    def test_dispatch_list_files_empty_repo(self, tmp_path):
        result = _dispatch_tool("list_files", {}, str(tmp_path))
        assert "tree" in result

    def test_dispatch_read_file_missing(self, tmp_path):
        result = _dispatch_tool("read_file", {"relative_path": "missing.txt"}, str(tmp_path))
        assert result["error"] is not None

    def test_dispatch_injects_repo_path(self, tmp_path):
        # repo_path should be auto-injected
        result = _dispatch_tool("list_files", {}, str(tmp_path))
        assert "error" in result or "tree" in result  # either outcome is valid


class TestReportJsonCompliance:
    """Ensure report output is valid JSON and complies with the schema."""

    def test_json_serialisable(self):
        report = build_report("https://github.com/x/y", [])
        json_text = json.dumps(report)
        parsed = json.loads(json_text)
        assert parsed["repository"] == "https://github.com/x/y"
        assert parsed["violations"] == []

    def test_violation_fields_present(self):
        from agent import build_report
        v = {
            "rule_id": "CI-001",
            "category": "ci_cd",
            "severity": "high",
            "file": ".github/workflows/ci.yml",
            "line": 5,
            "description": "Workflow has no test gate",
            "evidence": "No pytest step found in workflow",
            "recommendation": "Add a pytest step to the CI workflow",
        }
        report = build_report("https://github.com/x/y", [v])
        assert len(report["violations"]) == 1
        required = {"rule_id", "category", "severity", "file", "line",
                    "description", "evidence", "recommendation"}
        assert required.issubset(set(report["violations"][0].keys()))
