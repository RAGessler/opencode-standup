import sys
import unittest
from unittest.mock import patch

from opencode_standup.app import ScopeKind, main


class MainTests(unittest.TestCase):
    @patch("opencode_standup.app.StandupApp.run")
    @patch("opencode_standup.app.Path.exists", return_value=True)
    @patch("opencode_standup.app.StandupApp")
    def test_defaults_to_last_workday(self, app, _exists, _run) -> None:
        with patch.object(sys, "argv", ["opencode-standup"]):
            main()

        self.assertEqual(app.call_args.kwargs["scope"].kind, ScopeKind.LAST_WORKDAY)


if __name__ == "__main__":
    unittest.main()
