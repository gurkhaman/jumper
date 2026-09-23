"""Project discovery and tentative workspace cwd matching."""

from __future__ import annotations

import subprocess
from pathlib import Path


class DiscoveryError(Exception):
    pass


def canonical(path: str) -> str | None:
    """Existing directories only; stale or inaccessible hints are not projects."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        return str(resolved) if resolved.is_dir() else None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, RuntimeError) as exc:
        raise DiscoveryError(f"cannot resolve directory {path}: {exc}") from exc


def run_discovery(args: list[str]) -> list[str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise DiscoveryError(f"cannot run {args[0]}: {exc}") from exc
    if result.returncode:
        raise DiscoveryError(
            f"{args[0]} failed: {result.stderr.strip() or result.returncode}"
        )
    return result.stdout.splitlines()


def discover(*, all_directories: bool = False, home: str | None = None) -> list[str]:
    paths = run_discovery(["zoxide", "query", "--list"])
    if all_directories:
        root = home or str(Path("~").expanduser())
        paths += run_discovery(
            [
                "fd",
                "--type",
                "d",
                "--hidden",
                "--absolute-path",
                "--exclude",
                ".git",
                "--exclude",
                "Library",
                "--exclude",
                "node_modules",
                "--exclude",
                ".Trash",
                "--exclude",
                ".cache",
                "--exclude",
                ".npm",
                ".",
                root,
            ]
        )
    return list(dict.fromkeys(found for path in paths if (found := canonical(path))))


def direct_target(value: str) -> str:
    """Treat existing paths as projects and reject path-looking misses."""
    path = Path(value).expanduser()
    if (
        path.exists()
        or path.is_symlink()
        or value.startswith((".", "~"))
        or "/" in value
        or "\\" in value
    ):
        result = canonical(value)
        if result is None:
            raise DiscoveryError(f"project directory does not exist: {value}")
        return result
    result = run_discovery(["zoxide", "query", value])
    if len(result) != 1 or not (project := canonical(result[0])):
        raise DiscoveryError(
            f"zoxide query did not resolve an existing directory: {value}"
        )
    return project


def matching_workspace_hints(
    project: str, workspaces: list[dict], snapshot: dict
) -> list[dict]:
    """First-tab root-pane cwd is a hint, never an association."""
    tabs = snapshot["tabs"]
    panes = snapshot["panes"]
    matched = []
    for workspace in workspaces:
        wid = workspace["workspace_id"]
        first_tabs = [tab for tab in tabs if tab["workspace_id"] == wid]
        if not first_tabs:
            continue
        tab = min(first_tabs, key=lambda item: item["number"])
        first_panes = [pane for pane in panes if pane["tab_id"] == tab["tab_id"]]
        if not first_panes:
            continue

        # Herdr allocates the root pane first in a tab. Pane IDs are wN:pN.
        def pane_number(pane: dict) -> int:
            try:
                return int(pane["pane_id"].rsplit(":p", 1)[1])
            except (IndexError, ValueError):
                return 2**63

        pane = min(first_panes, key=pane_number)
        if pane.get("cwd"):
            try:
                if canonical(pane["cwd"]) == project:
                    matched.append(workspace)
            except DiscoveryError:
                # An unavailable pane cwd cannot establish project identity.
                pass
    return matched
