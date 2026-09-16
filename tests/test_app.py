import sys
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from opencode_standup.app import (
    JiraIssue,
    Scope,
    ScopeKind,
    SummaryGenerationError,
    day_bounds_ms,
    extract_jira_keys,
    fmt_cost,
    parse_jira_output,
    parse_opencode_output,
    load_session_excerpts,
    build_report,
    is_summary_session,
    UpdateInfo,
    check_for_update,
    github_credentials,
    should_prompt_for_update,
    main,
    SessionRow,
)


class MainTests(unittest.TestCase):
    def test_current_version_is_reported(self) -> None:
        with patch.object(sys, "argv", ["opencode-standup", "--version"]):
            with self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(raised.exception.code, 0)

    @patch("opencode_standup.app.mark_update_checked")
    @patch("opencode_standup.app.app_version", return_value="0.1.0")
    @patch("opencode_standup.app.urllib.request.urlopen")
    @patch(
        "opencode_standup.app.github_credentials",
        return_value=("octocat", "secret-token"),
    )
    def test_detects_newer_github_release(self, _credentials, urlopen, _version, _mark_checked) -> None:
        response = Mock()
        response.read.return_value = b'{"tag_name":"v0.2.0","html_url":"https://github.com/RAGessler/opencode-standup/releases/tag/v0.2.0"}'
        urlopen.return_value.__enter__.return_value = response

        self.assertEqual(
            check_for_update(force=True),
            UpdateInfo(
                "0.2.0",
                "https://github.com/RAGessler/opencode-standup/releases/tag/v0.2.0",
            ),
        )

    @patch("opencode_standup.app._read_update_cache", return_value={"prompted_version": "0.2.0"})
    def test_does_not_prompt_for_same_release_twice(self, _cache) -> None:
        self.assertFalse(should_prompt_for_update(UpdateInfo("0.2.0", "https://example.test")))

    @patch.dict(
        "os.environ",
        {
            "OPENCODE_STANDUP_GITHUB_USERNAME": "octocat",
            "OPENCODE_STANDUP_GITHUB_TOKEN": "secret-token",
        },
        clear=False,
    )
    def test_reads_github_credentials_from_environment(self) -> None:
        self.assertEqual(github_credentials(), ("octocat", "secret-token"))

    def test_summary_sessions_are_excluded_from_reports(self) -> None:
        normal = SessionRow(
            id="normal",
            parent_id=None,
            project_id="project",
            directory="/repo",
            title="Normal work",
            agent=None,
            time_created=day_bounds_ms(date(2026, 9, 15))[0],
            time_archived=None,
            cost=1.0,
            tokens_input=0,
            tokens_output=0,
            tokens_reasoning=0,
            tokens_cache_read=0,
            tokens_cache_write=0,
            additions=0,
            deletions=0,
            files=0,
        )
        generated = SessionRow(**{**normal.__dict__, "id": "generated", "title": "opencode-standup AI summary"})
        legacy = SessionRow(**{**normal.__dict__, "id": "legacy", "title": "Create a factual standup summary for 2026-09-15."})

        report = build_report(
            [normal, generated, legacy],
            {"project": "Example"},
            Scope(ScopeKind.CUSTOM, date(2026, 9, 15)),
        )

        self.assertTrue(is_summary_session(generated))
        self.assertTrue(is_summary_session(legacy))
        self.assertEqual([task.root.id for task in report[0].tasks], ["normal"])

    def test_extracts_jira_keys_case_insensitively(self) -> None:
        self.assertEqual(extract_jira_keys("Fix BSS-123 and bss-123, not abc-4"), {"BSS-123", "ABC-4"})

    def test_parses_jira_issue_context(self) -> None:
        raw = '{"data":{"items":{"sections":{"issues":[{"key":"BSS-123","summary":"Preview","status":"Open","webUrl":"https://jira/BSS-123"}]}}}}'

        self.assertEqual(
            parse_jira_output(raw),
            [JiraIssue("BSS-123", "Preview", "Open", "https://jira/BSS-123")],
        )

    def test_invalid_jira_json_is_explicit(self) -> None:
        with self.assertRaises(SummaryGenerationError):
            parse_jira_output("not json")

    def test_jira_error_payload_is_unavailable(self) -> None:
        with self.assertRaises(SummaryGenerationError):
            parse_jira_output('{"data":{"error":"unauthorized"}}')

    def test_parses_text_from_opencode_json_events(self) -> None:
        raw = '{"part":{"type":"text","text":"Summary"}}\n{"part":{"type":"tool","state":{}}}'

        self.assertEqual(parse_opencode_output(raw), "Summary")

    def test_rejects_opencode_output_without_text(self) -> None:
        with self.assertRaises(SummaryGenerationError):
            parse_opencode_output('{"part":{"type":"tool"}}')

    def test_session_excerpts_include_descendants_but_exclude_noise(self) -> None:
        root = SessionRow(
            id="root",
            parent_id=None,
            project_id="project",
            directory="/repo",
            title="BSS-123 implementation",
            agent=None,
            time_created=day_bounds_ms(date(2026, 9, 15))[0],
            time_archived=None,
            cost=0.5,
            tokens_input=0,
            tokens_output=0,
            tokens_reasoning=0,
            tokens_cache_read=0,
            tokens_cache_write=0,
            additions=2,
            deletions=1,
            files=1,
        )
        child = SessionRow(
            **{**root.__dict__, "id": "child", "parent_id": "root", "title": "child analysis"}
        )
        outside = SessionRow(
            **{**root.__dict__, "id": "outside", "time_created": day_bounds_ms(date(2026, 9, 16))[0]}
        )

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "test.db")
            day_start = day_bounds_ms(date(2026, 9, 15))[0]
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE session_input (session_id TEXT, prompt TEXT, time_created INTEGER);
                CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
                CREATE TABLE part (message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                """
            )
            conn.executemany(
                "INSERT INTO session_input VALUES (?, ?, ?)",
                [("root", "Implement BSS-123", day_start), ("outside", "Do not include", 2)],
            )
            conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?)",
                [
                    ("m1", "root", json.dumps({"role": "assistant"})),
                    ("m2", "child", json.dumps({"role": "assistant"})),
                    ("m3", "child", json.dumps({"role": "assistant"})),
                    ("m4", "child", json.dumps({"role": "assistant"})),
                ],
            )
            conn.executemany(
                "INSERT INTO part VALUES (?, ?, ?, ?)",
                [
                    ("m1", "root", json.dumps({"type": "text", "text": "Implemented it"}), day_start),
                    ("m2", "child", json.dumps({"type": "text", "text": "Analyzed details"}), day_start + 1),
                    ("m3", "child", json.dumps({"type": "tool", "text": "secret tool output"}), day_start + 2),
                    ("m4", "child", json.dumps({"type": "reasoning", "text": "private reasoning"}), day_start + 3),
                ],
            )
            conn.commit()
            conn.close()

            text, keys = load_session_excerpts(
                db_path,
                [root, child, outside],
                {"project": "Example"},
                Scope(ScopeKind.CUSTOM, date(2026, 9, 15)),
            )

        self.assertIn("Implemented it", text)
        self.assertIn("Analyzed details", text)
        self.assertNotIn("secret tool output", text)
        self.assertNotIn("private reasoning", text)
        self.assertNotIn("Do not include", text)
        self.assertEqual(keys, {"BSS-123"})

    def test_costs_are_displayed_to_two_decimal_places(self) -> None:
        self.assertEqual(fmt_cost(0.1234), "$0.12")
        self.assertEqual(fmt_cost(1234.5678), "$1,234.57")

    @patch("opencode_standup.app.StandupApp.run")
    @patch("opencode_standup.app.Path.exists", return_value=True)
    @patch("opencode_standup.app.StandupApp")
    def test_defaults_to_last_workday(self, app, _exists, _run) -> None:
        with patch.object(sys, "argv", ["opencode-standup"]):
            main()

        self.assertEqual(app.call_args.kwargs["scope"].kind, ScopeKind.LAST_WORKDAY)

    def test_sprint_scope_includes_the_full_final_day(self) -> None:
        scope = Scope(ScopeKind.SPRINT)

        self.assertEqual(
            scope.bounds_ms(),
            (day_bounds_ms(date(2026, 9, 14))[0], day_bounds_ms(date(2026, 9, 25))[1]),
        )

    @patch("opencode_standup.app.StandupApp.run")
    @patch("opencode_standup.app.Path.exists", return_value=True)
    @patch("opencode_standup.app.StandupApp")
    def test_sprint_scope_can_be_selected_from_the_command_line(self, app, _exists, _run) -> None:
        with patch.object(sys, "argv", ["opencode-standup", "--scope", "sprint"]):
            main()

        self.assertEqual(app.call_args.kwargs["scope"].kind, ScopeKind.SPRINT)


if __name__ == "__main__":
    unittest.main()
