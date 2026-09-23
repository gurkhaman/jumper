# Jumper agent notes

- Entry point: `jumper.cli:main` coordinates discovery (`jumper/projects.py`), the Herdr Unix-socket client/server (`jumper/herdr.py`), and endpoint-scoped associations (`jumper/store.py`).
- Runtime tools: Herdr 0.9+, zoxide, and fzf; `--all` additionally needs fd. Tests fake these executables and the Herdr socket in `tests/support.py`, so the suite needs no running Herdr server.
- Verify with `python -m unittest discover -s tests`, `uv run --group dev ruff check .`, and `uv run --group dev ruff format --check .`. For one test: `python -m unittest tests.test_command.CommandTests.test_present_saved_id_wins_even_after_id_reuse_and_competing_cwd_hint` (substitute the desired module/class/method).
- Local CLI installs are separate from the checkout: after changing code, refresh with `uv tool install --force .` before testing the installed `jumper` command.
- A workspace's root-pane cwd is only an adoption hint; `jumper/cli.py` requires interactive confirmation before saving that association. A create request whose response is lost has an unknown outcome: inspect Herdr workspaces rather than automatically retrying creation.
- Domain terms: read root `CONTEXT.md` before changing domain concepts; see `docs/agents/domain.md` for ADR guidance.
- Linear issues belong to the Gurkhaman team's Jumper project: `docs/agents/issue-tracker.md`. For triage use the canonical labels in `docs/agents/triage-labels.md`.
