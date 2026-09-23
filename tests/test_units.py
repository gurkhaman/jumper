"""Functional checks for protocol validation, associations, and project hints."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jumper.herdr import (
    HerdrClient,
    HerdrError,
    _APIUnavailableError,
    _parse_response,
    resolve_endpoint,
)
from jumper.projects import (
    DiscoveryError,
    canonical,
    direct_target,
    matching_workspace_hints,
)
from jumper.store import Associations, StoreDurabilityError, StoreError
from tests.support import workspace


class ProtocolTests(unittest.TestCase):
    def test_connected_but_unusable_api_times_out_without_spawning_or_stopping_server(
        self,
    ):
        with tempfile.TemporaryDirectory() as home:
            client = HerdrClient(resolve_endpoint({"HOME": home}))
            self.assert_unusable_api_does_not_spawn(client)

    def assert_unusable_api_does_not_spawn(self, client):
        with (
            patch.object(
                client,
                "list_workspaces",
                side_effect=_APIUnavailableError("timed out", connected=True),
            ),
            patch("jumper.herdr.subprocess.Popen") as spawn,
        ):
            with self.assertRaisesRegex(HerdrError, "not ready"):
                client.ensure_ready(timeout=0)
            spawn.assert_not_called()

    def parse(self, response, expected="workspace_list"):
        return _parse_response(
            (json.dumps(response) + "\n").encode(), "request-1", expected
        )

    def test_valid_response_preserves_unknown_fields(self):
        item = workspace(extra="future field")
        result = self.parse(
            {
                "id": "request-1",
                "result": {"type": "workspace_list", "workspaces": [item], "extra": 42},
            }
        )
        self.assertEqual(result["workspaces"][0]["extra"], "future field")
        self.assertEqual(result["extra"], 42)

    def test_bad_envelopes_and_records_are_rejected(self):
        cases = [
            (
                {"id": "wrong", "result": {"type": "workspace_list", "workspaces": []}},
                "id",
            ),
            ({"id": "request-1"}, "exactly one"),
            ({"id": "request-1", "result": {}, "error": {}}, "exactly one"),
            ({"id": "request-1", "result": {"type": "workspace_info"}}, "result type"),
            (
                {
                    "id": "request-1",
                    "result": {"type": "workspace_list", "workspaces": {}},
                },
                "array",
            ),
            (
                {
                    "id": "request-1",
                    "result": {
                        "type": "workspace_list",
                        "workspaces": [workspace(focused=1)],
                    },
                },
                "boolean",
            ),
            (
                {
                    "id": "request-1",
                    "result": {
                        "type": "workspace_list",
                        "workspaces": [workspace(number=True)],
                    },
                },
                "number",
            ),
        ]
        for response, message in cases:
            with (
                self.subTest(response=response),
                self.assertRaisesRegex(HerdrError, message),
            ):
                self.parse(response)
        with self.assertRaisesRegex(HerdrError, "JSON"):
            _parse_response(b"{oops}\n", "request-1", "workspace_list")

    def test_create_and_snapshot_require_structural_fields(self):
        created = {
            "id": "request-1",
            "result": {
                "type": "workspace_created",
                "workspace": workspace(),
                "tab": {"tab_id": "w1:t1"},
                "root_pane": {},
            },
        }
        with self.assertRaisesRegex(HerdrError, "pane_id"):
            self.parse(created, "workspace_created")
        snap = {
            "id": "request-1",
            "result": {
                "type": "session_snapshot",
                "snapshot": {
                    "version": "0.9",
                    "protocol": True,
                    "workspaces": [],
                    "tabs": [],
                    "panes": [],
                    "layouts": [],
                    "agents": [],
                },
            },
        }
        with self.assertRaisesRegex(HerdrError, "protocol"):
            self.parse(snap, "session_snapshot")

    def test_endpoint_default_session_override_and_client_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {"HOME": tmp}
            self.assertEqual(
                resolve_endpoint(env).socket_path, home / ".config/herdr/herdr.sock"
            )
            env["HERDR_SESSION"] = "alpha"
            self.assertEqual(
                resolve_endpoint(env).socket_path,
                home / ".config/herdr/sessions/alpha/herdr.sock",
            )
            env["HERDR_SOCKET_PATH"] = str(home / "custom.sock")
            self.assertEqual(resolve_endpoint(env).socket_path, home / "custom.sock")
            env["HERDR_CLIENT_SOCKET_PATH"] = str(home / "herdr-client.sock")
            self.assertNotIn("HERDR_CLIENT_SOCKET_PATH", resolve_endpoint(env).env)
            env["HERDR_CLIENT_SOCKET_PATH"] = str(home / "bad.sock")
            with self.assertRaisesRegex(HerdrError, "conflicts"):
                resolve_endpoint(env)
            for invalid in ("..", "nested/name", "nested\\name"):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaisesRegex(HerdrError, "invalid"),
                ):
                    resolve_endpoint({"HOME": tmp, "HERDR_SESSION": invalid})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Associations({"HOME": str(self.home)})

    def test_endpoint_scoping_persistence_and_permissions(self):
        self.assertIsNone(self.store.get("socket-a", "/project"))
        self.store.save("socket-a", "/project", "w1")
        self.store.save("socket-b", "/project", "w2")
        self.store.save("socket-a", "/another", "w3")
        other = Associations({"HOME": str(self.home)})
        self.assertEqual(other.get("socket-a", "/project"), "w1")
        self.assertEqual(other.get("socket-b", "/project"), "w2")
        self.assertEqual(other.get("socket-a", "/another"), "w3")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.store.path.parent.glob(".associations-*")), [])

    def test_directory_sync_failure_reports_visible_but_unconfirmed_association(self):
        from os import fsync

        calls = 0

        def fail_directory_sync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory sync failed")
            return fsync(fd)

        with (
            patch("jumper.store.os.fsync", side_effect=fail_directory_sync),
            self.assertRaisesRegex(StoreDurabilityError, "durability not confirmed"),
        ):
            self.store.save("socket", "/project", "w1")
        self.assertEqual(self.store.get("socket", "/project"), "w1")

    def test_xdg_state_location_and_corrupt_or_invalid_contents(self):
        state = self.home / "state"
        store = Associations({"HOME": str(self.home), "XDG_STATE_HOME": str(state)})
        store.save("endpoint", "project", "wid")
        self.assertEqual(store.path, state / "jumper/associations.json")
        for contents in ("{", "[]", '{"endpoint": {"project": 123}}'):
            with self.subTest(contents=contents):
                store.path.write_text(contents)
                with self.assertRaises(StoreError):
                    store.get("endpoint", "project")
                with self.assertRaises(StoreError):
                    store.save("endpoint", "project", "wid")


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def test_canonical_directory_symlink_and_path_looking_miss(self):
        alias = self.root / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(canonical(str(alias)), str(self.project))
        self.assertEqual(direct_target(str(alias)), str(self.project))
        self.assertIsNone(canonical(str(self.root / "missing")))
        file = self.root / "file"
        file.write_text("text")
        self.assertIsNone(canonical(str(file)))
        with self.assertRaisesRegex(DiscoveryError, "does not exist"):
            direct_target(str(file))

    def test_only_first_tab_root_pane_cwd_is_a_hint(self):
        wid = workspace("w1")
        tabs = [
            {"workspace_id": "w1", "tab_id": "w1:t2", "number": 2},
            {"workspace_id": "w1", "tab_id": "w1:t1", "number": 1},
        ]
        panes = [
            {
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "pane_id": "w1:p9",
                "cwd": str(self.project),
            },
            {
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "pane_id": "w1:p1",
                "cwd": str(self.root),
            },
            {
                "workspace_id": "w1",
                "tab_id": "w1:t2",
                "pane_id": "w1:p2",
                "cwd": str(self.project),
            },
        ]
        snapshot = {"tabs": tabs, "panes": panes}
        self.assertEqual(
            matching_workspace_hints(str(self.project), [wid], snapshot), []
        )
        panes[1]["cwd"] = str(self.project)
        self.assertEqual(
            matching_workspace_hints(str(self.project), [wid], snapshot), [wid]
        )
        panes[1]["cwd"] = str(self.root / "stale")
        self.assertEqual(
            matching_workspace_hints(str(self.project), [wid], snapshot), []
        )
