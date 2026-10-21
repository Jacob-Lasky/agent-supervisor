#!/usr/bin/env python3
"""Tests for install.py. Run: python3 -I tests/install_test.py (from the repo root or anywhere)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import install  # noqa: E402


class Install(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def settings(self):
        with open(os.path.join(self.d, "settings.json")) as f:
            return json.load(f)

    def commands(self, event):
        return [h["command"] for e in self.settings()["hooks"].get(event, []) for h in e["hooks"]]

    def test_fresh_install_copies_everything_and_wires_four_events(self):
        install.install(self.d)
        for rel in ("skills/supervisor/SKILL.md", "skills/supervisor/scripts/fleet.py", "skills/supervisor/profiles/example.md",
                    "skills/hypervisor/SKILL.md", "skills/catchup/SKILL.md", "hooks/supervisor-event.sh",
                    "hooks/tests/supervisor_test.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.d, rel)), rel)
        self.assertTrue(os.access(os.path.join(self.d, "hooks", "guard-watch-untracked.sh"), os.X_OK))
        self.assertEqual(sorted(self.settings()["hooks"]), ["Notification", "PreToolUse", "Stop", "UserPromptSubmit"])
        self.assertEqual(self.commands("Stop"), ["$HOME/.claude/hooks/supervisor-event.sh"])

    def test_a_second_run_changes_nothing(self):
        install.install(self.d)
        self.assertEqual(install.install(self.d), [])
        self.assertEqual(len(self.commands("PreToolUse")), 1)

    def test_existing_settings_are_kept_and_backed_up(self):
        mine = {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-stop.sh"}]}]}}
        with open(os.path.join(self.d, "settings.json"), "w") as f:
            json.dump(mine, f)
        install.install(self.d, now=0)
        s = self.settings()
        self.assertEqual(s["model"], "x")
        self.assertEqual(self.commands("Stop"), ["my-stop.sh", "$HOME/.claude/hooks/supervisor-event.sh"])
        with open(os.path.join(self.d, "backups", "agent-supervisor-19700101T000000Z", "settings.json")) as f:
            self.assertEqual(json.load(f), mine)

    def test_a_changed_file_is_backed_up_before_it_is_replaced(self):
        install.install(self.d)
        skill = os.path.join(self.d, "skills", "catchup", "SKILL.md")
        with open(skill, "w") as f:
            f.write("local edit\n")
        changes = install.install(self.d, now=0)
        self.assertIn("update   skills/catchup/SKILL.md", changes)
        with open(os.path.join(self.d, "backups", "agent-supervisor-19700101T000000Z", "skills", "catchup", "SKILL.md")) as f:
            self.assertEqual(f.read(), "local edit\n")
        with open(skill) as f:
            self.assertNotEqual(f.read(), "local edit\n")

    def test_written_profiles_are_never_touched(self):
        install.install(self.d)
        mine = os.path.join(self.d, "skills", "supervisor", "profiles", "laptop.md")
        with open(mine, "w") as f:
            f.write("mine\n")
        install.install(self.d)
        with open(mine) as f:
            self.assertEqual(f.read(), "mine\n")

    def test_dry_run_writes_nothing(self):
        changes = install.install(self.d, dry_run=True)
        self.assertTrue(changes)
        self.assertEqual(os.listdir(self.d), [])

    def test_invalid_settings_json_stops_before_changing_it(self):
        path = os.path.join(self.d, "settings.json")
        with open(path, "w") as f:
            f.write("{not json")
        with self.assertRaises(SystemExit):
            install.install(self.d)
        with open(path) as f:
            self.assertEqual(f.read(), "{not json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
