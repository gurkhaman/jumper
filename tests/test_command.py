"""Exercise the command through real subprocess, socket, and executable boundaries."""

import os
import signal
import subprocess
import sys
import threading

from tests.support import ROOT, CommandCase, workspace


class CommandTests(CommandCase):
    def test_present_saved_id_wins_even_after_id_reuse_and_competing_cwd_hint(self):
        from jumper.store import Associations

        project = self.project()
        server = self.start()
        server.items.extend(
            [
                workspace("reused", "Someone else's label"),
                workspace("candidate", project),
            ]
        )
        server.tabs.append(
            {"workspace_id": "candidate", "tab_id": "candidate:t1", "number": 1}
        )
        server.panes.append(
            {
                "workspace_id": "candidate",
                "tab_id": "candidate:t1",
                "pane_id": "candidate:p1",
                "cwd": project,
            }
        )
        Associations(self.env).save(str(self.socket_path), project, "reused")
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            ["workspace.list", "workspace.list", "workspace.focus", "herdr"],
        )
        self.assertEqual(server.focused, "reused")
        self.assertEqual(self.association()[str(self.socket_path)][project], "reused")

    def test_moved_project_gets_new_association_but_old_workspace_remains_selectable(
        self,
    ):
        project = self.project()
        server = self.start()
        first = self.run_jumper(project)
        self.assertEqual(first.returncode, 0, first.stderr)
        moved = str(self.home / "moved")
        os.rename(project, moved)
        self.events_path.unlink()
        missing = self.run_jumper(project)
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn("workspace.create", self.actions())
        self.events_path.unlink()
        second = self.run_jumper(moved)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )
        self.assertEqual(
            self.association()[str(self.socket_path)][moved], "w-created-2"
        )
        self.events_path.unlink()
        selected = self.run_jumper("--workspaces", TEST_FZF_INDEX=0)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(server.focused, "w-created")
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )

    def test_interrupt_after_focus_preserves_confirmed_create_and_mapping(self):
        project = self.project()
        server = self.start()
        result = self.run_jumper(project, TEST_ATTACH_INTERRUPT_PARENT=1)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertIn(
            "workspace w-created and any saved association remain", result.stderr
        )
        self.assertEqual([item["workspace_id"] for item in server.items], ["w-created"])
        self.assertEqual(server.focused, "w-created")
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )

    def test_interrupt_during_create_does_not_guess_or_retry_and_next_run_is_fresh(
        self,
    ):
        project = self.project()
        server = self.start()
        server.create_release = threading.Event()
        with subprocess.Popen(
            [sys.executable, "-m", "jumper", project],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child:
            try:
                self.assertTrue(server.create_entered.wait(5))
                os.kill(child.pid, signal.SIGINT)
                _, stderr = child.communicate(timeout=8)
            finally:
                server.create_release.set()
            self.assertEqual(child.returncode, 1, stderr.decode())
            self.assertIn("creation outcome unknown", stderr.decode())
        self.assertEqual(self.actions().count("workspace.create"), 1)
        self.assertNotIn("workspace.focus", self.actions())
        self.assertEqual(self.association(), {})
        self.events_path.unlink()
        # A later command is an independent invocation; it must request adoption.
        server.tabs.append(
            {"workspace_id": "w-created", "tab_id": "w-created:t1", "number": 1}
        )
        server.panes.append(
            {
                "workspace_id": "w-created",
                "tab_id": "w-created:t1",
                "pane_id": "w-created:p1",
                "cwd": project,
            }
        )
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("interactive terminal", result.stderr)
        self.assertNotIn("workspace.create", self.actions())
        self.assertEqual(self.association(), {})

    def test_plain_picker_cancel_starts_server_before_fzf_without_attaching(self):
        project = self.project()
        self.start(cold=True)
        result = self.run_jumper(TEST_FZF_EXIT=130, TEST_ZOXIDE_OUTPUT=project + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            ["herdr server", "workspace.list", "workspace.list", "zoxide", "fzf"],
        )
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.association(), {})

    def test_default_endpoint_and_two_sockets_in_one_named_session_are_isolated(self):
        project = self.project()
        self.env.pop("HERDR_SOCKET_PATH")
        self.socket_path = self.home / ".config/herdr/herdr.sock"
        default = self.start()
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )
        default.close()
        self.socket_path = self.root / "other.sock"
        self.start()
        result = self.run_jumper(
            project, HERDR_SOCKET_PATH=self.socket_path, HERDR_SESSION="shared"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )

    def test_fd_exclusions_and_hidden_search_arguments(self):
        self.start()
        result = self.run_jumper("--all", TEST_FZF_EXIT=130)
        self.assertEqual(result.returncode, 0, result.stderr)
        fd = next(event for event in self.events() if event.get("exec") == "fd")
        self.assertEqual(fd["args"].count("--exclude"), 6)
        for name in (".git", "Library", "node_modules", ".Trash", ".cache", ".npm"):
            self.assertIn(name, fd["args"])
        self.assertIn("--hidden", fd["args"])

    def test_symlink_alias_and_deleted_workspace_direct_selection(self):
        project = self.project()
        alias = self.home / "alias"
        alias.symlink_to(project, target_is_directory=True)
        server = self.start()
        server.items.append(workspace("w-old", "Old missing directory"))
        result = self.run_jumper("--workspaces", TEST_FZF_INDEX=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.association(), {})
        self.events_path.unlink()
        result = self.run_jumper(
            TEST_ZOXIDE_OUTPUT=f"{alias}\n{project}\n", TEST_FZF_INDEX=1
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[3]["input"].count(project), 1)
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )

    def test_focus_server_error_retains_saved_id_and_does_not_attach(self):
        project = self.project()
        server = self.start()
        server.errors["workspace.focus"] = "denied focus"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("denied focus", result.stderr)
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )
        self.assertNotIn("herdr", self.actions())

    def test_cold_start_launches_server_then_creates_focuses_and_attaches(self):
        project = self.project()
        self.start(cold=True)
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            [
                "herdr server",
                "workspace.list",
                "workspace.list",
                "workspace.create",
                "workspace.focus",
                "herdr",
            ],
        )
        created = next(
            e for e in self.events() if e.get("method") == "workspace.create"
        )
        self.assertEqual(
            created["params"], {"cwd": project, "label": project, "focus": False}
        )
        self.assertEqual(
            self.association(), {str(self.socket_path): {project: "w-created"}}
        )
        self.assertEqual(
            next(e for e in self.events() if e.get("method") == "workspace.focus")[
                "params"
            ],
            {"workspace_id": "w-created"},
        )
        herdr = [e for e in self.events() if e.get("exec") == "herdr"]
        self.assertTrue(
            all(
                e["socket"] == str(self.socket_path) and e["client_socket"] is None
                for e in herdr
            )
        )

    def test_warm_server_reuses_association_without_snapshot_or_create(self):
        project = self.project()
        server = self.start()
        server.items.append(workspace("w-saved"))
        from jumper.store import Associations

        Associations(self.env).save(str(self.socket_path), project, "w-saved")
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            ["workspace.list", "workspace.list", "workspace.focus", "herdr"],
        )
        self.assertEqual(self.events()[-2]["params"], {"workspace_id": "w-saved"})

    def test_session_endpoint_and_explicit_socket_precedence(self):
        project = self.project()
        session_socket = self.home / ".config/herdr/sessions/other/herdr.sock"
        self.socket_path = session_socket
        self.env.pop("HERDR_SOCKET_PATH")
        self.start()
        result = self.run_jumper(project, HERDR_SESSION="other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["socket"], str(session_socket))
        self.assertEqual(
            self.association(), {str(session_socket): {project: "w-created"}}
        )

    def test_explicit_socket_wins_over_session_and_xdg_config(self):
        project = self.project()
        self.start()
        result = self.run_jumper(
            project, HERDR_SESSION="other", XDG_CONFIG_HOME=self.root / "config"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["socket"], str(self.socket_path))

    def test_conflicting_client_socket_and_invalid_session_fail_before_api(self):
        for overrides in (
            {"HERDR_CLIENT_SOCKET_PATH": str(self.root / "wrong.sock")},
            {"HERDR_SOCKET_PATH": "", "HERDR_SESSION": "../unsafe"},
        ):
            with self.subTest(overrides=overrides):
                result = self.run_jumper(self.project(), **overrides)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.events(), [])

    def test_confirm_cwd_hint_then_remember_association(self):
        project = self.project()
        server = self.start()
        server.items.append(workspace("w1"))
        server.tabs.append({"tab_id": "w1:t1", "workspace_id": "w1", "number": 1})
        server.panes.append(
            {
                "pane_id": "w1:p1",
                "tab_id": "w1:t1",
                "workspace_id": "w1",
                "cwd": project,
            }
        )
        result = self.run_jumper(project, tty=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            [
                "workspace.list",
                "workspace.list",
                "session.snapshot",
                "fzf",
                "workspace.focus",
                "herdr",
            ],
        )
        self.assertIn("Adopt w1", self.events()[3]["input"])
        self.assertIn("Create new instead", self.events()[3]["input"])
        self.assertEqual(
            self.events()[3]["args"],
            [
                "--prompt",
                "Confirm project association > ",
                "--delimiter",
                "\t",
                "--with-nth",
                "2",
            ],
        )
        self.assertEqual(self.association()[str(self.socket_path)][project], "w1")
        self.events_path.unlink()
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fzf", self.actions())
        self.assertNotIn("session.snapshot", self.actions())

    def test_hint_can_be_rejected_for_creation_and_cancel_does_nothing(self):
        project = self.project()
        server = self.start()
        server.items.append(workspace())
        server.tabs.append({"tab_id": "w1:t1", "workspace_id": "w1", "number": 1})
        server.panes.append(
            {
                "pane_id": "w1:p1",
                "tab_id": "w1:t1",
                "workspace_id": "w1",
                "cwd": project,
            }
        )
        cancelled = self.run_jumper(project, tty=True, TEST_FZF_EXIT=130)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertNotIn("workspace.create", self.actions())
        self.assertEqual(self.association(), {})
        self.events_path.unlink()
        created = self.run_jumper(project, tty=True, TEST_FZF_INDEX=1)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )
        self.assertLess(
            self.actions().index("workspace.create"),
            self.actions().index("workspace.focus"),
        )

    def test_multiple_hints_require_explicit_choice(self):
        project = self.project()
        server = self.start()
        server.items.extend([workspace("w1"), workspace("w2", "Second")])
        for wid in ("w1", "w2"):
            server.tabs.append(
                {"tab_id": f"{wid}:t1", "workspace_id": wid, "number": 1}
            )
            server.panes.append(
                {
                    "pane_id": f"{wid}:p1",
                    "tab_id": f"{wid}:t1",
                    "workspace_id": wid,
                    "cwd": project,
                }
            )
        result = self.run_jumper(project, tty=True, TEST_FZF_INDEX=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Adopt w1", self.events()[3]["input"])
        self.assertIn("Adopt w2", self.events()[3]["input"])
        self.assertIn("Create new workspace", self.events()[3]["input"])
        self.assertNotIn("workspace.create", self.actions())
        self.assertEqual(self.association()[str(self.socket_path)][project], "w2")

    def test_noninteractive_hint_requires_confirmation(self):
        project = self.project()
        server = self.start()
        server.items.append(workspace())
        server.tabs.append({"tab_id": "w1:t1", "workspace_id": "w1", "number": 1})
        server.panes.append(
            {
                "pane_id": "w1:p1",
                "tab_id": "w1:t1",
                "workspace_id": "w1",
                "cwd": project,
            }
        )
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("interactive terminal", result.stderr)
        self.assertEqual(
            self.actions(), ["workspace.list", "workspace.list", "session.snapshot"]
        )

    def test_stale_association_creates_without_adopting_hint(self):
        project = self.project()
        server = self.start()
        server.items.append(workspace())
        from jumper.store import Associations

        Associations(self.env).save(str(self.socket_path), project, "gone")
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("session.snapshot", self.actions())
        self.assertNotIn("fzf", self.actions())
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )

    def test_unknown_creation_outcome_diagnoses_once_without_retry_or_focus(self):
        project = self.project()
        server = self.start()
        server.modes["workspace.create"] = "drop_after_create"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("creation outcome unknown", result.stderr)
        self.assertIn("Available workspace IDs: w-created", result.stderr)
        self.assertIn("Use jumper --workspaces", result.stderr)
        self.assertEqual(
            self.actions(),
            ["workspace.list", "workspace.list", "workspace.create", "workspace.list"],
        )
        self.assertEqual([item["workspace_id"] for item in server.items], ["w-created"])
        self.assertEqual(self.association(), {})

    def test_invalid_create_response_is_ambiguous_but_server_error_is_definitive(self):
        project = self.project()
        server = self.start()
        server.modes["workspace.create"] = "malformed"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("creation outcome unknown", result.stderr)
        self.assertEqual(self.actions().count("workspace.create"), 1)
        self.events_path.unlink()
        server.modes.clear()
        server.errors["workspace.create"] = "no capacity"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no capacity", result.stderr)
        self.assertNotIn("outcome unknown", result.stderr)
        self.assertEqual(
            self.actions(), ["workspace.list", "workspace.list", "workspace.create"]
        )
        self.assertEqual(self.association(), {})

    def test_ambiguous_creation_with_failed_diagnostic_list_reports_both_failures(self):
        project = self.project()
        server = self.start()
        server.modes["workspace.create"] = "drop"
        original = server.result
        lists = 0

        def result(method, params):
            nonlocal lists
            if method == "workspace.list":
                lists += 1
                if lists == 3:
                    return {"type": "workspace_list", "workspaces": "invalid"}
            return original(method, params)

        server.result = result
        response = self.run_jumper(project)
        self.assertEqual(response.returncode, 1)
        self.assertIn("creation outcome unknown", response.stderr)
        self.assertIn("list unavailable", response.stderr)
        self.assertEqual(self.actions().count("workspace.create"), 1)
        self.assertEqual(self.association(), {})

    def test_create_persists_before_focus_and_reports_association_save_failure(self):
        project = self.project()
        server = self.start()
        server.block_state_on_create = self.home / ".local/state/jumper"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "workspace w-created created, but association not saved", result.stderr
        )
        self.assertNotIn("workspace.focus", self.actions())
        self.assertNotIn("herdr", self.actions())

    def test_api_error_does_not_start_another_server(self):
        project = self.project()
        server = self.start()
        server.errors["workspace.list"] = "busy"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("busy", result.stderr)
        self.assertEqual(self.actions(), ["workspace.list"])

    def test_focus_mismatch_and_attach_failure_never_retry_creation(self):
        project = self.project()
        server = self.start()
        server.modes["workspace.focus"] = "w-other"
        result = self.run_jumper(project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("instead of w-created", result.stderr)
        self.assertNotIn("herdr", self.actions())
        self.assertEqual(
            self.association()[str(self.socket_path)][project], "w-created"
        )
        server.modes.clear()
        self.events_path.unlink()
        result = self.run_jumper(project, TEST_ATTACH_EXIT=7)
        self.assertEqual(result.returncode, 1)
        self.assertIn("exit 7", result.stderr)
        self.assertIn(
            "workspace w-created and any saved association remain", result.stderr
        )
        self.assertEqual(
            self.actions(),
            ["workspace.list", "workspace.list", "workspace.focus", "herdr"],
        )

    def test_inside_herdr_never_starts_server_or_attaches(self):
        project = self.project()
        missing = self.run_jumper(project, HERDR_ENV=1)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("unavailable inside Herdr", missing.stderr)
        self.assertEqual(self.events(), [])
        self.start()
        result = self.run_jumper(project, HERDR_ENV=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("herdr", self.actions())

    def test_picker_workspaces_only_skips_discovery_and_focuses_selected_id(self):
        server = self.start()
        server.items.extend([workspace("w1"), workspace("w2", "Second")])
        result = self.run_jumper("--workspaces", TEST_FZF_INDEX=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            ["workspace.list", "workspace.list", "fzf", "workspace.focus", "herdr"],
        )
        self.assertIn("w2", self.events()[2]["input"])
        self.assertEqual(self.events()[2]["args"][0:2], ["--prompt", "Jumper > "])
        self.assertEqual(self.events()[3]["params"], {"workspace_id": "w2"})
        self.assertEqual(self.association(), {})

    def test_workspace_with_control_characters_in_label_is_selectable(self):
        server = self.start()
        server.items.append(workspace("odd", "One\nTwo\tThree\rFour"))
        result = self.run_jumper("--workspaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        picker = next(event for event in self.events() if event.get("exec") == "fzf")
        self.assertEqual(picker["input"], "0\tworkspace  One Two Three Four  [odd]\n")
        self.assertEqual(server.focused, "odd")

    def test_picker_cancel_and_invalid_selection(self):
        server = self.start()
        server.items.append(workspace())
        cancelled = self.run_jumper("--workspaces", TEST_FZF_EXIT=1)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertNotIn("workspace.focus", self.actions())
        self.events_path.unlink()
        invalid = self.run_jumper("--workspaces", TEST_FZF_OUTPUT="999\tbogus\n")
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("invalid fzf picker selection", invalid.stderr)
        self.assertNotIn("workspace.focus", self.actions())

    def test_picker_program_failure_does_not_focus(self):
        server = self.start()
        server.items.append(workspace())
        result = self.run_jumper("--workspaces", TEST_FZF_EXIT=2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("fzf failed", result.stderr)
        self.assertNotIn("workspace.focus", self.actions())

    def test_default_picker_discovers_zoxide_and_all_adds_fd(self):
        project = self.project()
        other = self.project("other")
        self.start()
        result = self.run_jumper(TEST_ZOXIDE_OUTPUT=f"{project}\n{project}\n/missing\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions(),
            [
                "workspace.list",
                "workspace.list",
                "zoxide",
                "fzf",
                "workspace.create",
                "workspace.focus",
                "herdr",
            ],
        )
        self.assertEqual(self.events()[2]["args"], ["query", "--list"])
        self.assertEqual(self.events()[3]["input"].count(project), 1)
        self.events_path.unlink()
        result = self.run_jumper(
            "--all",
            TEST_ZOXIDE_OUTPUT=project + "\n",
            TEST_FD_OUTPUT=other + "\n",
            TEST_FZF_INDEX=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions()[2:5], ["zoxide", "fd", "fzf"])
        fd_args = self.events()[3]["args"]
        self.assertEqual(fd_args[-2:], [".", str(self.home)])
        self.assertIn("--hidden", fd_args)
        self.assertEqual(
            self.association()[str(self.socket_path)][other], "w-created-2"
        )

    def test_direct_query_resolution_and_invalid_path_never_creates(self):
        project = self.project()
        self.start()
        result = self.run_jumper("shortname", TEST_ZOXIDE_OUTPUT=project + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[2]["args"], ["query", "shortname"])
        self.events_path.unlink()
        missing = self.run_jumper("./missing")
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn("zoxide", self.actions())
        self.assertNotIn("workspace.create", self.actions())

    def test_discovery_failure_and_mutually_exclusive_flags(self):
        self.start()
        result = self.run_jumper(
            "--all", TEST_FD_EXIT=4, TEST_FD_ERROR="fd unavailable"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("fd unavailable", result.stderr)
        self.assertNotIn("fzf", self.actions())
        self.events_path.unlink()
        result = self.run_jumper("--workspaces", self.project())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.events(), [])
