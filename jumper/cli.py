"""The public jumper command."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .herdr import (
    CreationOutcomeUnknownError,
    HerdrClient,
    HerdrError,
    resolve_endpoint,
)
from .projects import DiscoveryError, candidates, direct_target, discover
from .store import Associations, StoreDurabilityError, StoreError


class JumperError(Exception):
    pass


def choose(
    options: list[tuple[str, str]], *, prompt: str, required: bool = False
) -> str | None:
    if required:
        try:
            with Path("/dev/tty").open():
                pass
        except OSError as exc:
            raise JumperError("confirmation requires an interactive terminal") from exc
    if not options:
        return None
    display = [
        "".join(char if char.isprintable() else " " for char in label)
        for _, label in options
    ]
    try:
        result = subprocess.run(
            ["fzf", "--prompt", prompt, "--delimiter", "\t", "--with-nth", "2"],
            input="".join(f"{index}\t{label}\n" for index, label in enumerate(display)),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise JumperError(f"cannot open fzf picker: {exc}") from exc
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise JumperError(f"fzf failed: {result.stderr.strip() or result.returncode}")
    try:
        index = int(result.stdout.split("\t", 1)[0])
        selected = options[index]
    except (ValueError, IndexError) as exc:
        raise JumperError("invalid fzf picker selection") from exc
    if result.stdout != f"{index}\t{display[index]}\n":
        raise JumperError("invalid fzf picker selection")
    return selected[0]


def open_project(
    project: str, client: HerdrClient, store: Associations, workspaces: list[dict]
) -> str | None:
    endpoint = client.endpoint.key
    saved = store.get(endpoint, project)
    present = {workspace["workspace_id"] for workspace in workspaces}
    if saved and saved in present:
        return saved
    if not saved:
        hints = candidates(project, workspaces, client.snapshot()) if workspaces else []
        if hints:
            options = [
                (item["workspace_id"], f"Adopt {item['workspace_id']}  {item['label']}")
                for item in hints
            ]
            if len(hints) == 1:
                options.append(("new", "Create new instead"))
            else:
                options.append(("new", "Create new workspace"))
            selected = choose(
                options, prompt="Confirm project association > ", required=True
            )
            if selected is None:
                return None
            if selected != "new":
                store.save(endpoint, project, selected)
                return selected
    try:
        created = client.create_workspace(project)
    except CreationOutcomeUnknownError as exc:
        try:
            available = client.list_workspaces()
            details = ", ".join(item["workspace_id"] for item in available) or "none"
        except HerdrError as diagnostic:
            details = f"list unavailable ({diagnostic})"
        raise JumperError(
            "creation outcome unknown; no association saved. "
            f"Available workspace IDs: {details}. "
            "Use jumper --workspaces to select manually"
        ) from exc
    wid = created["workspace_id"]
    try:
        store.save(endpoint, project, wid)
    except StoreDurabilityError as exc:
        raise JumperError(f"workspace {wid} created; {exc}") from exc
    except StoreError as exc:
        raise JumperError(
            f"workspace {wid} created, but association not saved: {exc}"
        ) from exc
    return wid


def launch(args: argparse.Namespace) -> int:
    endpoint = resolve_endpoint()
    client = HerdrClient(endpoint)
    store = Associations(endpoint.env)
    client.ensure_ready()
    workspaces = client.list_workspaces()
    if args.target is not None:
        # Validate before taking any workspace-creation decision.
        project = direct_target(args.target)
        wid = open_project(project, client, store, workspaces)
    else:
        options = [
            (
                f"w:{workspace['workspace_id']}",
                f"workspace  {workspace['label']}  [{workspace['workspace_id']}]",
            )
            for workspace in workspaces
        ]
        if not args.workspaces:
            options += [
                (f"p:{path}", f"project  {path}")
                for path in discover(
                    all_directories=args.all, home=endpoint.env.get("HOME")
                )
            ]
        selected = choose(options, prompt="Jumper > ")
        if selected is None:
            return 0
        if selected.startswith("w:"):
            wid = selected[2:]
        else:
            wid = open_project(selected[2:], client, store, workspaces)
    if wid is None:
        return 0
    try:
        focused = client.focus_workspace(wid)
        if focused["workspace_id"] != wid:
            raise HerdrError(
                f"Herdr focused {focused['workspace_id']} instead of {wid}"
            )
        status = client.attach()
    except KeyboardInterrupt:
        print(
            f"jumper: interrupted; workspace {wid} and any saved association remain",
            file=sys.stderr,
        )
        return 130
    if status not in (None, 0):
        raise JumperError(
            f"Herdr TUI attach failed (exit {status}); "
            f"workspace {wid} and any saved association remain"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jumper")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--all",
        action="store_true",
        help="also discover directories under home using fd",
    )
    group.add_argument(
        "--workspaces", action="store_true", help="pick from Herdr workspaces only"
    )
    parser.add_argument("target", nargs="?", metavar="PATH/QUERY")
    args = parser.parse_args(argv)
    if args.target is not None and (args.all or args.workspaces):
        parser.error("PATH/QUERY cannot be combined with picker options")
    try:
        return launch(args)
    except (JumperError, HerdrError, DiscoveryError, StoreError) as exc:
        print(f"jumper: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "jumper: interrupted; any completed server/workspace changes are retained",
            file=sys.stderr,
        )
        return 130
