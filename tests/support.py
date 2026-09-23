"""Process-boundary fakes for the public jumper command."""

import fcntl
import json
import os
import pty
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workspace(wid="w1", label="Existing", **changes):
    record = {
        "workspace_id": wid,
        "label": label,
        "active_tab_id": f"{wid}:t1",
        "focused": False,
        "number": 1,
        "pane_count": 1,
        "tab_count": 1,
        "agent_status": "idle",
    }
    record.update(changes)
    return record


def append_event(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(event) + "\n").encode())
    finally:
        os.close(fd)


FAKE_EXECUTABLE = r"""#!PYTHON
import json, os, signal, sys

def log(event):
    fd = os.open(
        os.environ['TEST_EVENTS'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    try:
        os.write(fd, (json.dumps(event) + '\n').encode())
    finally:
        os.close(fd)

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
event = {'exec': name, 'args': args}
if name == 'fzf':
    event['input'] = sys.stdin.read()
if name == 'herdr':
    event['socket'] = os.environ.get('HERDR_SOCKET_PATH')
    event['session'] = os.environ.get('HERDR_SESSION')
    event['client_socket'] = os.environ.get('HERDR_CLIENT_SOCKET_PATH')
log(event)
if name == 'herdr':
    if args == ['server']:
        open(os.environ['TEST_START'], 'w').close()
    else:
        if os.environ.get('TEST_ATTACH_INTERRUPT_PARENT'):
            os.kill(os.getppid(), signal.SIGINT)
        sys.exit(int(os.environ.get('TEST_ATTACH_EXIT', '0')))
elif name == 'fzf':
    code = int(os.environ.get('TEST_FZF_EXIT', '0'))
    if code:
        sys.exit(code)
    lines = event['input'].splitlines()
    index = int(os.environ.get('TEST_FZF_INDEX', '0'))
    sys.stdout.write(
        os.environ.get('TEST_FZF_OUTPUT', lines[index] + '\n' if lines else '')
    )
elif name in ('zoxide', 'fd'):
    sys.stdout.write(os.environ.get('TEST_' + name.upper() + '_OUTPUT', ''))
    sys.stderr.write(os.environ.get('TEST_' + name.upper() + '_ERROR', ''))
    sys.exit(int(os.environ.get('TEST_' + name.upper() + '_EXIT', '0')))
""".replace("#!PYTHON", "#!" + sys.executable)


class FakeHerdr:
    def __init__(self, path, events, start_marker, *, cold=False):
        self.path = path
        self.events = events
        self.start_marker = start_marker
        self.cold = cold
        self.items = []
        self.tabs = []
        self.panes = []
        self.errors = {}
        self.modes = {}
        self.create_entered = threading.Event()
        self.create_release = None
        self.create_count = 0
        self.block_state_on_create = None
        self.focused = None
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.handlers = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        if not cold and not self.ready.wait(2):
            raise RuntimeError("fake Herdr did not start")

    def serve(self):
        if self.cold:
            while not self.stop.is_set() and not self.start_marker.exists():
                time.sleep(0.005)
        if self.stop.is_set():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.path))
            server.listen()
            server.settimeout(0.05)
            self.ready.set()
            while not self.stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                handler = threading.Thread(
                    target=self.handle, args=(conn,), daemon=True
                )
                self.handlers.append(handler)
                handler.start()

    def handle(self, conn):
        with conn:
            conn.settimeout(2)
            with conn.makefile("rb") as stream:
                line = stream.readline()
            if not line:
                return
            request = json.loads(line)
            method = request["method"]
            append_event(self.events, {"method": method, "params": request["params"]})
            if self.modes.get(method) == "drop":
                return
            if self.modes.get(method) == "drop_after_create":
                self.result(method, request["params"])
                return
            if self.modes.get(method) == "malformed":
                conn.sendall(b"{bad json}\n")
                return
            if method in self.errors:
                response = {"error": {"code": "denied", "message": self.errors[method]}}
            else:
                response = {"result": self.result(method, request["params"])}
            with suppress(BrokenPipeError):
                conn.sendall(
                    (json.dumps({"id": request["id"], **response}) + "\n").encode()
                )

    def result(self, method, params):
        if method == "workspace.list":
            return {
                "type": "workspace_list",
                "workspaces": [
                    {**item, "focused": item["workspace_id"] == self.focused}
                    if self.focused is not None
                    else item
                    for item in self.items
                ],
            }
        if method == "session.snapshot":
            return {
                "type": "session_snapshot",
                "snapshot": {
                    "version": "0.9",
                    "protocol": 1,
                    "workspaces": self.items,
                    "tabs": self.tabs,
                    "panes": self.panes,
                    "layouts": [],
                    "agents": [],
                },
            }
        if method == "workspace.create":
            self.create_count += 1
            wid = (
                "w-created"
                if self.create_count == 1
                else f"w-created-{self.create_count}"
            )
            item = workspace(wid, params["label"])
            self.items.append(item)
            if self.block_state_on_create is not None:
                self.block_state_on_create.parent.mkdir(parents=True, exist_ok=True)
                self.block_state_on_create.write_text("not a directory")
            self.create_entered.set()
            if self.create_release is not None:
                self.create_release.wait(timeout=5)
            return {
                "type": "workspace_created",
                "workspace": item,
                "tab": {"tab_id": item["active_tab_id"]},
                "root_pane": {"pane_id": f"{wid}:p1"},
            }
        if method == "workspace.focus":
            wid = self.modes.get(method) or params["workspace_id"]
            self.focused = wid
            return {"type": "workspace_info", "workspace": workspace(wid, focused=True)}
        raise AssertionError(method)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("fake server did not stop")
        for handler in self.handlers:
            handler.join(timeout=3)
            if handler.is_alive():
                raise RuntimeError("fake Herdr request did not finish")


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("herdr", "fzf", "zoxide", "fd"):
            path = self.bin / name
            path.write_text(FAKE_EXECUTABLE)
            path.chmod(0o755)
        self.events_path = self.root / "events.jsonl"
        self.marker = self.root / "start"
        self.socket_path = self.root / "herdr.sock"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("HERDR_", "XDG_", "TEST_"))
        }
        self.env.update(
            HOME=str(self.home),
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            PYTHONPATH=str(ROOT),
            TEST_EVENTS=str(self.events_path),
            TEST_START=str(self.marker),
            HERDR_SOCKET_PATH=str(self.socket_path),
        )
        self.server = None

    def start(self, *, cold=False):
        self.server = FakeHerdr(
            self.socket_path, self.events_path, self.marker, cold=cold
        )
        self.addCleanup(self.server.close)
        return self.server

    def run_jumper(self, *args, tty=False, **env):
        run_env = {**self.env, **{k: str(v) for k, v in env.items()}}
        if tty:
            master, slave = pty.openpty()

            def controlling_tty():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            try:
                with subprocess.Popen(
                    [sys.executable, "-m", "jumper", *args],
                    cwd=ROOT,
                    env=run_env,
                    stdin=slave,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=controlling_tty,
                ) as child:
                    stdout, stderr = child.communicate(timeout=8)
                    return subprocess.CompletedProcess(
                        child.args, child.returncode, stdout.decode(), stderr.decode()
                    )
            finally:
                os.close(master)
                os.close(slave)
        return subprocess.run(
            [sys.executable, "-m", "jumper", *args],
            cwd=ROOT,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )

    def events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def actions(self):
        return [
            e.get("method", e.get("exec"))
            + (" server" if e.get("args") == ["server"] else "")
            for e in self.events()
        ]

    def association(self):
        path = self.home / ".local/state/jumper/associations.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def project(self, name="project"):
        path = self.home / name
        path.mkdir(exist_ok=True)
        return str(path)
