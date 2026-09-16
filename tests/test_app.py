import sys
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from opencode_standup.app import (
    JiraIssue,
    Scope,
    ScopeKind,
    GitHubActivity,
    GitHubActivityKind,
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
    github_credentials_path,
    load_github_activity,
    parse_github_comment_search,
    save_github_credentials,
    validate_github_credentials,
    should_prompt_for_update,
    main,
    SessionRow,
)


class MainTests(unittest.TestCase):
    def test_github_search_is_org_scoped(self) -> None:
        from opencode_standup.app import _github_search_url

        self.assertIn("org%3AShamrockTrading", _github_search_url("org:ShamrockTrading is:pr", 25))
        self.assertIn("sort=updated", _github_search_url("org:ShamrockTrading is:pr", 25))

    @patch("opencode_standup.app._github_get_json")
    @patch("opencode_standup.app.github_credentials", return_value=("octocat", "secret-token"))
    def test_merged_pull_requests_use_supported_search_and_exact_merge_time(
        self, _credentials, get_json
    ) -> None:
        get_json.side_effect = [
            {"login": "octocat"},
            [{"full_name": "ShamrockTrading/app"}],
            {"items": []},
            {
                "items": [
                    {
                        "number": 8,
                        "title": "Merged PR",
                        "html_url": "https://github.com/ShamrockTrading/app/pull/8",
                        "repository": {"full_name": "ShamrockTrading/app"},
                        "pull_request": {"url": "https://api.github.com/repos/ShamrockTrading/app/pulls/8"},
                    }
                ]
            },
            {"merged_at": "2026-09-15T14:00:00Z"},
            {"items": []},
            {"items": []},
        ]

        result = load_github_activity(Scope(ScopeKind.CUSTOM, date(2026, 9, 15)), force_refresh=True)

        self.assertEqual([activity.kind for activity in result.activities], [GitHubActivityKind.MERGED])
        search_urls = [call.args[0] for call in get_json.call_args_list if "/search/issues?" in call.args[0]]
        self.assertTrue(any("is%3Amerged" in url for url in search_urls))
        self.assertFalse(any("merged%3A" in url for url in search_urls))

    @patch("opencode_standup.app.github_credentials", return_value=("octocat", "secret-token"))
    @patch("opencode_standup.app.urllib.request.urlopen")
    def test_github_422_shows_operation_and_api_message(self, urlopen, _credentials) -> None:
        error = HTTPError(
            "https://api.github.com/search/issues", 422, "Unprocessable Entity", {}, None
        )
        error.read = Mock(return_value=b'{"message":"Invalid search query"}')
        response = MagicMock()
        response.read.return_value = b'{"login":"octocat"}'
        urlopen.return_value.__enter__.return_value = response
        urlopen.side_effect = [urlopen.return_value, error]

        result = load_github_activity(Scope(ScopeKind.TODAY), force_refresh=True)

        self.assertFalse(result.available)
        self.assertIn("ShamrockTrading repository access", result.error or "")
        self.assertIn("Invalid search query", result.error or "")

    def test_github_activity_no_longer_uses_graphql(self) -> None:
        from opencode_standup import app

        self.assertFalse(hasattr(app, "GITHUB_GRAPHQL_URL"))
        self.assertFalse(hasattr(app, "GITHUB_ACTIVITY_QUERY"))

    def test_parses_pull_request_comments_from_rest_search(self) -> None:
        result = parse_github_comment_search(
            {
                "items": [
                    {
                        "number": 8,
                        "title": "Commented PR",
                        "html_url": "https://github.com/ShamrockTrading/app/pull/8",
                        "repository": {"full_name": "ShamrockTrading/app"},
                        "comments": [
                            {
                                "created_at": "2026-09-15T14:00:00Z",
                                "user": {"login": "octocat"},
                            }
                        ],
                    }
                ]
            },
            Scope(ScopeKind.CUSTOM, date(2026, 9, 15)),
            "octocat",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, GitHubActivityKind.COMMENTED)

    @patch("opencode_standup.app.load_jira_context", return_value=([], True, None))
    @patch("opencode_standup.app.load_session_excerpts", return_value=("work", set()))
    @patch("opencode_standup.app.load_github_activity")
    def test_summary_github_context_respects_character_budget(
        self, github_activity, _session_excerpts, _jira_context
    ) -> None:
        from opencode_standup.app import GITHUB_SUMMARY_MAX_CHARS, GitHubActivityResult, build_summary_context

        activity = GitHubActivity(
            GitHubActivityKind.OPENED,
            "ShamrockTrading/app",
            8,
            "x" * 500,
            "https://github.com/ShamrockTrading/app/pull/8",
            "2026-09-15T14:00:00Z",
        )
        github_activity.return_value = GitHubActivityResult(activities=(activity,) * 41)

        context = build_summary_context(
            "unused.db", [], {}, Scope(ScopeKind.CUSTOM, date(2026, 9, 15))
        )

        github_text = context.text.split("GitHub activity:\n", 1)[1]
        self.assertLessEqual(len(github_text), GITHUB_SUMMARY_MAX_CHARS)
        self.assertIn("additional GitHub activity omitted", github_text)

    @patch("opencode_standup.app.github_credentials", return_value=None)
    @patch("opencode_standup.app.urllib.request.urlopen")
    def test_github_activity_is_unavailable_without_credentials(self, urlopen, _credentials) -> None:
        result = load_github_activity(Scope(ScopeKind.TODAY))

        self.assertFalse(result.available)
        self.assertIn("credentials", result.error or "")
        urlopen.assert_not_called()

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
        response.read.return_value = b'{"tag_name":"v0.2.0","html_url":"https://github.com/RAGessler/opencode-standup/releases/tag/v0.2.0","assets":[{"name":"opencode_standup-0.2.0-py3-none-any.whl","url":"https://api.github.com/repos/RAGessler/opencode-standup/releases/assets/1"}]}'
        urlopen.return_value.__enter__.return_value = response

        self.assertEqual(
            check_for_update(force=True),
            UpdateInfo(
                "0.2.0",
                "https://github.com/RAGessler/opencode-standup/releases/tag/v0.2.0",
                "https://api.github.com/repos/RAGessler/opencode-standup/releases/assets/1",
            ),
        )

    @patch("opencode_standup.app._read_update_cache", return_value={"prompted_version": "0.2.0"})
    def test_does_not_prompt_for_same_release_twice(self, _cache) -> None:
        self.assertFalse(
            should_prompt_for_update(
                UpdateInfo("0.2.0", "https://example.test", "https://example.test/asset")
            )
        )

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

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/tmp/opencode-standup-test-config"}, clear=False)
    @patch("opencode_standup.app.github_credentials_path")
    @patch("opencode_standup.app.netrc.netrc")
    def test_saves_and_reads_github_credentials(self, netrc_mock, path_mock) -> None:
        netrc_mock.return_value.authenticators.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            path_mock.return_value = Path(directory) / "github.json"
            save_github_credentials("octocat", "secret-token")

            self.assertEqual(github_credentials(), ("octocat", "secret-token"))
            self.assertEqual(path_mock.return_value.stat().st_mode & 0o777, 0o600)

    @patch("opencode_standup.app._github_organization_access")
    @patch("opencode_standup.app.urllib.request.urlopen")
    def test_validates_github_credentials_from_user_response(self, urlopen, organization_access) -> None:
        response = Mock()
        response.read.return_value = b'{"login":"octocat"}'
        urlopen.return_value.__enter__.return_value = response

        valid, result = validate_github_credentials(("", "secret-token"))

        self.assertTrue(valid)
        self.assertEqual(result, "octocat")
        request = urlopen.call_args.args[0]
        self.assertNotIn("secret-token", request.full_url)
        self.assertIn("Bearer secret-token", request.get_header("Authorization"))
        organization_access.assert_called_once()

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
