# opencode-standup

`opencode-standup` is a Textual TUI command center for OpenCode sessions across your repositories.

It provides:

- A standup view grouping sessions by project for today, the last workday, this week, or all time
- A current sprint view for September 14 through September 25, 2026
- An on-demand AI summary of the previous workday using OpenCode chat and Jira context
- Cost and token rollups, including subagent spend
- A todo view of outstanding todo items across sessions
- Session rename, archive, unarchive, and resume actions

## Requirements

- Python 3.10 or newer
- OpenCode installed and usable from the terminal
- An OpenCode session database at `~/.local/share/opencode/opencode.db`

The app reads OpenCode's SQLite database directly. It also starts `opencode serve` on demand when you rename or archive a session, and runs `opencode <directory> --session <id>` when you open a session. The OpenCode database schema and REST API are therefore part of the compatibility surface.

## First-Time Setup

### 1. Install from PyPI

For normal use, install the published package with `pipx`. This keeps the tool isolated and lets it update itself without requiring a repository checkout or branch selection.

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install opencode-standup
```

If `pipx` is not available, install it into a dedicated virtual environment instead:

```bash
python3 -m venv ~/.local/share/opencode-standup/venv
~/.local/share/opencode-standup/venv/bin/python -m pip install opencode-standup
mkdir -p ~/.local/bin
ln -sf ~/.local/share/opencode-standup/venv/bin/opencode-standup ~/.local/bin/opencode-standup
```

### 2. Check prerequisites

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

### 3. Launch it

```bash
opencode-standup
```

The default view shows sessions from the previous workday. Use the keybindings below to change the time range, switch to todos, refresh, or open a session.

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
- `[` and `]`: switch tabs
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

The context and process limits can be adjusted with environment variables:

```bash
OPENCODE_STANDUP_SUMMARY_MAX_CHARS=12000
OPENCODE_STANDUP_SUMMARY_SESSION_MAX_CHARS=2600
OPENCODE_STANDUP_SUMMARY_TIMEOUT=120
OPENCODE_STANDUP_JIRA_TIMEOUT=20
```

## Updating

The TUI checks PyPI for a newer release once per day in the background. It does not block startup if the network is unavailable. When a release is available, the TUI shows the current and available versions and asks for confirmation before upgrading.

You can also update manually:

```bash
pipx upgrade opencode-standup
```

Press `U` in the TUI to check immediately for updates. The update check only requests public package metadata from PyPI; it does not upload OpenCode sessions, transcripts, Jira data, or credentials.

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

Releases are created by pushing a version tag such as `v0.2.0`. GitHub Actions runs tests, audits the artifacts, and publishes them to PyPI using trusted publishing. Configure the PyPI project’s trusted publisher for this repository and the `pypi` GitHub environment before pushing the first release tag.

The application requires a real OpenCode database to launch the interactive TUI. Use `--db` to point it at a test database when validating locally.
