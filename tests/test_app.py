import sys
import unittest
from datetime import date
from unittest.mock import patch

from opencode_standup.app import Scope, ScopeKind, day_bounds_ms, fmt_cost, main


class MainTests(unittest.TestCase):
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
