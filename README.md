# opencode-standup

`opencode-standup` is a Textual TUI command center for OpenCode sessions across your repositories.

It provides:

- A standup view grouping sessions by project for today, the last workday, this week, or all time
- A current sprint view for September 14 through September 25, 2026
- An on-demand AI summary of the previous workday using OpenCode chat and Jira context
- Cost and token rollups, including subagent spend
- A GitHub activity view for pull requests you opened, merged, reviewed, or commented on
- Session rename, archive, unarchive, and resume actions

## Requirements

- Python 3.10 or newer
- OpenCode installed and usable from the terminal
- An OpenCode session database at `~/.local/share/opencode/opencode.db`

The app reads OpenCode's SQLite database directly. It also starts `opencode serve` on demand when you rename or archive a session, and runs `opencode <directory> --session <id>` when you open a session. The OpenCode database schema and REST API are therefore part of the compatibility surface.

## First-Time Setup

### 1. Configure GitHub access, if needed

The tool is distributed as a wheel attached to each GitHub Release. If the repository is public, no GitHub credentials are needed. For GitHub activity, launch the app, switch to the GitHub tab, and press `c` to start the guided setup. It opens GitHub's token settings, validates the token and `ShamrockTrading` organization visibility, and stores it locally with owner-only permissions.

You can also configure a GitHub personal access token (classic) with repository read access using environment variables:

```bash
export OPENCODE_STANDUP_GITHUB_USERNAME=YOUR_GITHUB_USERNAME
export OPENCODE_STANDUP_GITHUB_TOKEN=YOUR_GITHUB_TOKEN
```

The token is used only for GitHub API and release-asset access.

### 2. Install from a GitHub Release

For normal use, download the wheel from the repository's Releases page and install it with `pipx`. This keeps the tool isolated and lets it update itself without requiring a repository checkout or branch selection.

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install /path/to/opencode_standup-0.1.0-py3-none-any.whl
```

The wheel installs its dependencies from public PyPI. Put the GitHub variables in your shell profile if desired; the token is never included in the update URL or displayed in the TUI.

### 3. Check prerequisites

```bash
python3 --version
opencode --version
test -f "$HOME/.local/share/opencode/opencode.db" && echo "OpenCode database found"
```

Python must be 3.10 or newer. If the database check fails, launch OpenCode and create or use at least one session first, then check the path again.

If the command is not found afterward, add the launcher directory to the current shell's `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add the same line to `~/.zshrc` or `~/.bashrc` to make it permanent, then open a new terminal or reload the file.

### 4. Launch it

```bash
opencode-standup
```

The default view shows sessions from the previous workday. Use the keybindings below to change the time range, refresh, or open a session.

## Usage

```bash
opencode-standup
opencode-standup --scope week
opencode-standup --scope sprint
opencode-standup --date 2026-09-15
opencode-standup --days-ago 1
opencode-standup --db /path/to/opencode.db
```

The database path can also be configured with `OPENCODE_STANDUP_DB`:

```bash
OPENCODE_STANDUP_DB=/path/to/opencode.db opencode-standup
```

The command exits with an error if the configured database does not exist.

In the TUI, press `?` for the full keybinding list. The primary bindings are:

- `1` through `5`: change the standup time scope, including the current sprint
- `s`: generate an AI summary when the selected scope is Last workday
- `[` and `]`: switch between the Standup and GitHub tabs
- `g`: refresh GitHub activity
- `e`: rename the selected session
- `x`: archive the selected session
- `A`: show or hide archived sessions
- `u`: unarchive the selected archived session
- `o`: open the selected session in OpenCode
- `r`: refresh
- `U`: check for updates
- `q`: quit

Select a session row with the arrow keys before using an action. Rename and archive start a local `opencode serve` process only when needed. Unarchive writes directly to the OpenCode database because the current OpenCode API does not expose an unarchive operation.

### AI Standup Summary

When the selected scope is **Last workday**, press `s` to generate a summary above the normal overview. It contains a short narrative followed by bullets for progress, Jira work, blockers or risks, and next steps. The summary is generated on demand with the provider and model already configured for `opencode`.

The app sends bounded excerpts from relevant OpenCode session prompts and assistant responses, along with session metadata and Jira ticket metadata, to OpenCode. Tool output and reasoning are excluded. Jira enrichment uses the authenticated `twg` command for your Jira activity during the previous workday and is optional; if it is unavailable, the app generates a clearly labeled chat-only summary.

### GitHub Activity

The GitHub tab loads automatically in the background for the selected time scope. It shows pull requests you opened, pull requests you authored that were merged, pull requests you reviewed, and pull requests where you added a conversation comment in the `ShamrockTrading` organization. Inline code-review comments are represented by review activity rather than duplicated as comments. `o` opens the selected pull request in your browser and `g` refreshes the activity manually. GitHub searches return up to 100 recently updated matching pull requests per category; the All time view is also bounded to the most recent 366 days.

If GitHub credentials are not configured, switch to the GitHub tab and press `c`. The guided setup opens GitHub's token settings, asks for the token, validates it against `/user`, and stores it in `$XDG_CONFIG_HOME/opencode-standup/github.json` (or `~/.config/opencode-standup/github.json`) with owner-only permissions. Environment variables and `.netrc` remain supported and take precedence over the guided setup credentials.

GitHub activity uses the authenticated account returned by GitHub, not the configured username. It uses authenticated REST API searches with `OPENCODE_STANDUP_GITHUB_TOKEN` and `OPENCODE_STANDUP_GITHUB_USERNAME`, the `github.com` entry in `.netrc`, or credentials saved by the guided setup. The token must be able to read the `ShamrockTrading` repositories whose activity you want to see, including private repositories. A classic token needs the `repo` scope. A fine-grained token must be granted read access to the relevant organization repositories and pull requests. If ShamrockTrading uses SAML SSO, authorize the token for the organization in GitHub after creating it. Missing credentials, organization access, authentication errors, rate limits, and GitHub outages are shown as unavailable activity and do not block the standup view.

When generating the Last workday AI summary, the app adds up to 40 GitHub activity records and 4,000 characters of GitHub metadata such as repository, pull request number, title, activity type, and URL. It never sends the GitHub token to OpenCode and does not send GitHub comment bodies.

The context and process limits can be adjusted with environment variables:

```bash
OPENCODE_STANDUP_SUMMARY_MAX_CHARS=12000
OPENCODE_STANDUP_SUMMARY_SESSION_MAX_CHARS=2600
OPENCODE_STANDUP_SUMMARY_TIMEOUT=120
OPENCODE_STANDUP_JIRA_TIMEOUT=20
```

## Updating

The TUI checks the latest GitHub Release once per day in the background. It does not block startup if GitHub is unavailable or credentials are missing. When a release is available, the TUI shows the current and available versions and asks for confirmation before upgrading.

You can also update manually by downloading the newer wheel from the GitHub Release and running:

```bash
pipx install --force /path/to/opencode_standup-0.2.0-py3-none-any.whl
```

Press `U` in the TUI to check immediately for updates. The update check requests release metadata from GitHub and the upgrade downloads the wheel attached to that GitHub Release. It does not upload OpenCode sessions, transcripts, or Jira data.

The legacy `scripts/install.sh` remains available for development from a source checkout. It installs that checkout in editable mode and is not the normal end-user installation path.

## Uninstalling

Remove the launcher and the dedicated virtual environment:

```bash
pipx uninstall opencode-standup
```

This does not modify OpenCode's database or sessions.

## Troubleshooting

### `opencode-standup: command not found`

The launcher directory is not on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### `python3: command not found` or Python is too old

Install Python 3.10 or newer, then rerun the `pipx` installation command.

### `opencode database not found`

Check the default location:

```bash
ls -l "$HOME/.local/share/opencode/opencode.db"
```

If OpenCode stores its database elsewhere, pass it explicitly with `--db` or `OPENCODE_STANDUP_DB`.

### Rename or archive fails

Confirm that OpenCode is available on `PATH` and can start its server:

```bash
command -v opencode
opencode --version
opencode serve --help
```

### Open/resume fails

The selected session's repository directory must still exist, and the `opencode` executable must be available on `PATH`.

## Development

The repository is private, so contributors need GitHub access and an authenticated GitHub CLI or Git credential:

```bash
gh auth login
gh repo clone RAGessler/opencode-standup
cd opencode-standup
```

Create a development environment and install the project in editable mode:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --editable .
.venv/bin/opencode-standup --help
```

Basic validation:

```bash
.venv/bin/python -m compileall src
```

Build and audit release artifacts locally:

```bash
.venv/bin/python -m pip install build twine
rm -rf dist build
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python scripts/audit_package.py dist/*
```

Releases are created by pushing a version tag such as `v0.2.0`. GitHub Actions runs tests, audits the artifacts, attaches the wheel and source distribution to a GitHub Release, and publishes the release. Consumers download the wheel from that release; no Python package registry is required.

The application requires a real OpenCode database to launch the interactive TUI. Use `--db` to point it at a test database when validating locally.
