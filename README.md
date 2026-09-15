# opencode-standup

`opencode-standup` is a Textual TUI command center for OpenCode sessions across your repositories.

It provides:

- A standup view grouping sessions by project for today, the last workday, this week, or all time
- Cost and token rollups, including subagent spend
- A todo view of outstanding todo items across sessions
- Session rename, archive, unarchive, and resume actions

## Requirements

- Python 3.10 or newer
- OpenCode installed and usable from the terminal
- An OpenCode session database at `~/.local/share/opencode/opencode.db`

The app reads OpenCode's SQLite database directly. It also starts `opencode serve` on demand when you rename or archive a session, and runs `opencode <directory> --session <id>` when you open a session. The OpenCode database schema and REST API are therefore part of the compatibility surface.

## First-Time Setup

### 1. Clone the repository

This repository is private, so the user needs GitHub access and an authenticated GitHub CLI or Git credential first.

```bash
gh auth login
gh repo clone RAGessler/opencode-standup
cd opencode-standup
```

If the repository has already been cloned, update it before installing:

```bash
cd /path/to/opencode-standup
git pull --ff-only
```

### 2. Check prerequisites

```bash
python3 --version
opencode --version
test -f "$HOME/.local/share/opencode/opencode.db" && echo "OpenCode database found"
```

Python must be 3.10 or newer. If the database check fails, launch OpenCode and create or use at least one session first, then check the path again.

### 3. Install the command

Run this from the repository checkout:

```bash
./scripts/install.sh
```

The installer creates or reuses `~/.local/share/opencode-standup/venv`, installs this checkout in editable mode, and installs the `opencode-standup` launcher at `~/.local/bin/opencode-standup`.

If the command is not found afterward, add the launcher directory to the current shell's `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add the same line to `~/.zshrc` or `~/.bashrc` to make it permanent, then open a new terminal or reload the file.

To install the virtual environment somewhere else:

```bash
OPENCODE_STANDUP_INSTALL_ROOT=/path/to/install ./scripts/install.sh
```

The launcher is always written to `~/.local/bin`.

### 4. Launch it

```bash
opencode-standup
```

The default view shows sessions from the previous workday. Use the keybindings below to change the time range, switch to todos, refresh, or open a session.

## Usage

```bash
opencode-standup
opencode-standup --scope week
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

- `1` through `4`: change the standup time scope
- `[` and `]`: switch tabs
- `e`: rename the selected session
- `x`: archive the selected session
- `A`: show or hide archived sessions
- `u`: unarchive the selected archived session
- `o`: open the selected session in OpenCode
- `r`: refresh
- `q`: quit

Select a session row with the arrow keys before using an action. Rename and archive start a local `opencode serve` process only when needed. Unarchive writes directly to the OpenCode database because the current OpenCode API does not expose an unarchive operation.

## Updating

The install is editable, so update the checkout and rerun the installer:

```bash
cd /path/to/opencode-standup
git pull --ff-only
./scripts/install.sh
```

## Uninstalling

Remove the launcher and the dedicated virtual environment:

```bash
rm "$HOME/.local/bin/opencode-standup"
rm -rf "$HOME/.local/share/opencode-standup"
```

This does not modify OpenCode's database or sessions.

## Troubleshooting

### `opencode-standup: command not found`

The launcher directory is not on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### `python3: command not found` or Python is too old

Install Python 3.10 or newer, then rerun `./scripts/install.sh`.

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

The application requires a real OpenCode database to launch the interactive TUI. Use `--db` to point it at a test database when validating locally.
