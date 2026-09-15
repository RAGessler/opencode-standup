# opencode-standup

`opencode-standup` is a Textual TUI command center for OpenCode sessions across your repositories.

It provides:

- A standup view grouping sessions by project for today, the last workday, this week, or all time
- Cost and token rollups, including subagent spend
- A todo view of outstanding todo items across sessions
- Session rename, archive, unarchive, and resume actions

## Requirements

- Python 3.10 or newer
- The `opencode` CLI installed and available on `PATH` for rename, archive, and resume actions
- An OpenCode installation with its SQLite database at `~/.local/share/opencode/opencode.db`

The app reads OpenCode's SQLite database directly and uses `opencode serve` for session mutations. The database schema and REST endpoints are therefore part of the compatibility surface.

## Install

From a checkout of this repository:

```bash
./scripts/install.sh
```

The installer creates or reuses `~/.local/share/opencode-standup/venv`, installs the checkout in editable mode, and installs the `opencode-standup` launcher at `~/.local/bin/opencode-standup`.

To use a different install location:

```bash
OPENCODE_STANDUP_INSTALL_ROOT=/path/to/install ./scripts/install.sh
```

Ensure `~/.local/bin` is on your `PATH`.

## Usage

```bash
opencode-standup
opencode-standup --scope week
opencode-standup --date 2026-09-15
opencode-standup --days-ago 1
opencode-standup --db /path/to/opencode.db
```

The database path can also be configured with `OPENCODE_STANDUP_DB`.

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
