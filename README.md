# Jumper

A Herdr-native project picker and workspace launcher.

Requires Python 3.10+, Herdr 0.9.0+, [zoxide](https://github.com/ajeetdsouza/zoxide),
and [fzf](https://github.com/junegunn/fzf). `fd` is needed for `--all` only.

## Installation

Install directly from GitHub with [uv](https://docs.astral.sh/uv/guides/tools/):

```sh
uv tool install git+https://github.com/gurkhaman/jumper.git
```

Then run `jumper` from any directory. If your shell cannot find the command,
run `uv tool update-shell` and restart the shell. To update a GitHub installation
later, run `uv tool upgrade jumper`.

From a local clone, install with `uv tool install .` and reinstall changed local
code with `uv tool install --force .`.

Initialize zoxide in your shell so it can learn the directories you visit; see
the [zoxide setup instructions](https://github.com/ajeetdsouza/zoxide#step-2-add-zoxide-to-your-shell).

## Usage

```text
jumper                 Pick from zoxide projects and Herdr workspaces
jumper --workspaces    Pick only existing Herdr workspaces
jumper --all           Also search directories under home (requires fd)
jumper ~/path/to/repo  Open a directory directly
jumper repo            Resolve and open a zoxide query
```

Outside Herdr, Jumper starts or reuses the persistent server, focuses the
workspace, and attaches the current terminal. Inside Herdr, it switches
workspaces without nesting another client. If a directory appears to match an
unassociated workspace, Jumper asks before adopting it. Press Esc to leave the
picker without opening anything.

Project associations are saved in `$XDG_STATE_HOME/jumper/associations.json`
(default `~/.local/state/jumper/`).

## Development

Run the tests with `python -m unittest discover -s tests`.
Lint and check formatting with `uv run --group dev ruff check .` and
`uv run --group dev ruff format --check .`.
