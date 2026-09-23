"""Small Herdr 0.9+ Unix-socket client and persistent-server launcher.

The API returns Herdr's validated JSON records (including unknown future fields).
No Herdr session is stopped or deleted by this module.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping


class HerdrError(Exception):
    """Herdr API error, invalid response, or server/attach failure."""


class CreationOutcomeUnknownError(HerdrError):
    """The create request may have succeeded; inspect workspaces before retrying."""


class _APIUnavailableError(HerdrError):
    """Connection to the API failed (not a server-side error)."""

    def __init__(self, message: str, *, connected: bool = False) -> None:
        super().__init__(message)
        self.connected = connected


class _ServerError(HerdrError):
    """A well-formed Herdr error response."""


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HerdrError(f"{context} must be an object")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise HerdrError(f"{context} must be a nonempty string")
    return value


def _workspace(value: Any) -> dict[str, Any]:
    item = _object(value, "workspace")
    _string(item.get("workspace_id"), "workspace_id")
    _string(item.get("label"), "workspace label")
    _string(item.get("active_tab_id"), "active_tab_id")
    if type(item.get("focused")) is not bool:
        raise HerdrError("workspace focused must be a boolean")
    for field in ("number", "pane_count", "tab_count"):
        if type(item.get(field)) is not int or item[field] < (
            1 if field == "number" else 0
        ):
            raise HerdrError(f"workspace {field} must be a nonnegative integer")
    _string(item.get("agent_status"), "workspace agent_status")
    return item


def _records(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise HerdrError(f"{name} must be an array")
    return [_object(item, name + " entry") for item in value]


def _validate_snapshot(value: Any) -> None:
    snap = _object(value, "snapshot")
    _string(snap.get("version"), "snapshot version")
    if type(snap.get("protocol")) is not int or snap["protocol"] < 0:
        raise HerdrError("snapshot protocol must be a nonnegative integer")
    for workspace in _records(snap.get("workspaces"), "snapshot workspaces"):
        _workspace(workspace)
    for tab in _records(snap.get("tabs"), "snapshot tabs"):
        _string(tab.get("tab_id"), "snapshot tab_id")
        _string(tab.get("workspace_id"), "snapshot tab workspace_id")
        if type(tab.get("number")) is not int or tab["number"] < 1:
            raise HerdrError("snapshot tab number must be positive")
    for pane in _records(snap.get("panes"), "snapshot panes"):
        for field in ("pane_id", "workspace_id", "tab_id"):
            _string(pane.get(field), "snapshot pane " + field)
        if pane.get("cwd") is not None:
            _string(pane["cwd"], "snapshot pane cwd")
    _records(snap.get("layouts"), "snapshot layouts")
    _records(snap.get("agents"), "snapshot agents")


def _validate_result(result: Any, expected: str) -> dict[str, Any]:
    item = _object(result, "result")
    if item.get("type") != expected:
        raise HerdrError(f"expected result type {expected!r}, got {item.get('type')!r}")
    if expected == "workspace_list":
        for workspace in _records(item.get("workspaces"), "workspaces"):
            _workspace(workspace)
    elif expected in ("workspace_created", "workspace_info"):
        _workspace(item.get("workspace"))
        if expected == "workspace_created":
            tab = _object(item.get("tab"), "created tab")
            pane = _object(item.get("root_pane"), "created root pane")
            _string(tab.get("tab_id"), "created tab_id")
            _string(pane.get("pane_id"), "created pane_id")
    elif expected == "session_snapshot":
        _validate_snapshot(item.get("snapshot"))
    return item


def _parse_response(line: bytes, request_id: str, expected: str) -> dict[str, Any]:
    try:
        response = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HerdrError("invalid Herdr JSON response") from exc
    envelope = _object(response, "response")
    if envelope.get("id") != request_id:
        raise HerdrError("Herdr response id does not match request")
    if ("result" in envelope) == ("error" in envelope):
        raise HerdrError("Herdr response requires exactly one of result or error")
    if "error" in envelope:
        error = _object(envelope["error"], "error")
        code = _string(error.get("code"), "error code")
        message = _string(error.get("message"), "error message")
        raise _ServerError(f"Herdr {code}: {message}")
    return _validate_result(envelope["result"], expected)


@dataclass(frozen=True)
class Endpoint:
    """Resolved API address and environment shared by the server and TUI."""

    socket_path: Path
    key: str
    env: dict[str, str]


def resolve_endpoint(env: Mapping[str, str] | None = None) -> Endpoint:
    """Resolve HERDR_SOCKET_PATH, then HERDR_SESSION, then the default socket."""
    inherited = dict(os.environ if env is None else env)
    home = Path(inherited.get("HOME") or Path.home()).expanduser()
    root = (
        Path(inherited.get("XDG_CONFIG_HOME") or home / ".config").expanduser()
        / "herdr"
    )
    explicit = inherited.get("HERDR_SOCKET_PATH")
    session = inherited.get("HERDR_SESSION")
    if explicit:
        path = Path(explicit).expanduser().absolute()
    elif session:
        if (
            session in (".", "..")
            or "/" in session
            or "\\" in session
            or "\x00" in session
        ):
            raise HerdrError("invalid HERDR_SESSION name")
        path = root / "sessions" / session / "herdr.sock"
    else:
        path = root / "herdr.sock"
    path = path.absolute()
    client_override = inherited.get("HERDR_CLIENT_SOCKET_PATH")
    if client_override and Path(
        client_override
    ).expanduser().absolute() != path.with_name("herdr-client.sock"):
        raise HerdrError(
            "HERDR_CLIENT_SOCKET_PATH conflicts with the selected API socket"
        )
    inherited["HERDR_SOCKET_PATH"] = str(path)
    inherited.pop("HERDR_CLIENT_SOCKET_PATH", None)
    return Endpoint(path, str(path), inherited)


class HerdrClient:
    """One request per connection; API failures never imply safe create retries."""

    def __init__(
        self, endpoint: Endpoint | None = None, *, timeout: float = 2.0
    ) -> None:
        self.endpoint = endpoint if endpoint is not None else resolve_endpoint()
        self.timeout = timeout

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        expected: str,
        *,
        create: bool = False,
    ) -> dict[str, Any]:
        request_id = uuid4().hex
        payload = (
            json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
        ).encode()
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(str(self.endpoint.socket_path))
                sent = True  # sendall can fail after sending an arbitrary prefix
                conn.sendall(payload)
                with conn.makefile("rb") as stream:
                    line = stream.readline(4 * 1024 * 1024 + 1)
                if not line.endswith(b"\n") or len(line) > 4 * 1024 * 1024:
                    if create:
                        raise CreationOutcomeUnknownError(
                            "workspace creation outcome unknown: bad response length"
                        )
                    raise HerdrError("missing, truncated, or oversized Herdr response")
        except (OSError, TimeoutError) as exc:
            if create and sent:
                raise CreationOutcomeUnknownError(
                    "workspace creation outcome unknown after transport failure"
                ) from exc
            raise _APIUnavailableError(
                f"Herdr API unavailable at {self.endpoint.socket_path}: {exc}",
                connected=sent,
            ) from exc
        except KeyboardInterrupt as exc:
            if create and sent:
                raise CreationOutcomeUnknownError(
                    "workspace creation outcome unknown after interruption"
                ) from exc
            raise
        try:
            return _parse_response(line, request_id, expected)
        except _ServerError:
            raise
        except HerdrError as exc:
            # A server error is definitive; invalid create replies remain ambiguous.
            if create:
                raise CreationOutcomeUnknownError(
                    "workspace creation outcome unknown: " + str(exc)
                ) from exc
            raise

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self._request("workspace.list", {}, "workspace_list")["workspaces"]

    def create_workspace(self, cwd: str | Path) -> dict[str, Any]:
        try:
            path = Path(cwd).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HerdrError(f"invalid workspace cwd: {exc}") from exc
        if not path.is_dir():
            raise HerdrError(f"workspace cwd is not a directory: {path}")
        return self._request(
            "workspace.create",
            {"cwd": str(path), "label": str(path), "focus": False},
            "workspace_created",
            create=True,
        )["workspace"]

    def focus_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request(
            "workspace.focus",
            {"workspace_id": _string(workspace_id, "workspace_id")},
            "workspace_info",
        )["workspace"]

    def snapshot(self) -> dict[str, Any]:
        """Return snapshot data for tentative first-tab root-pane cwd matching."""
        return self._request("session.snapshot", {}, "session_snapshot")["snapshot"]

    def ensure_ready(self, *, timeout: float = 15.0) -> None:
        """Reuse a responsive server, or launch one detached and poll the API."""
        child = None
        try:
            self.list_workspaces()
            return
        except _APIUnavailableError as exc:
            if self.endpoint.env.get("HERDR_ENV") == "1":
                raise HerdrError("Herdr API unavailable inside Herdr") from exc
            if not exc.connected:
                try:
                    child = subprocess.Popen(
                        ["herdr", "server"],
                        env=self.endpoint.env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except OSError as start_error:
                    raise HerdrError(
                        f"cannot start Herdr server: {start_error}"
                    ) from start_error
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.list_workspaces()
                return
            except _APIUnavailableError as exc:
                if child is not None and child.poll() is not None:
                    # Another process may have won the startup race.
                    try:
                        self.list_workspaces()
                        return
                    except _APIUnavailableError:
                        raise HerdrError(
                            f"Herdr server exited ({child.returncode}): {exc}"
                        ) from exc
                if time.monotonic() >= deadline:
                    raise HerdrError(
                        f"Herdr API not ready at {self.endpoint.socket_path}"
                    ) from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def attach(self) -> int | None:
        """Attach the current terminal outside Herdr; return the TUI exit status."""
        if self.endpoint.env.get("HERDR_ENV") == "1":
            return None
        try:
            return subprocess.run(
                ["herdr"], env=self.endpoint.env, check=False
            ).returncode
        except OSError as exc:
            raise HerdrError(f"cannot attach Herdr TUI: {exc}") from exc
