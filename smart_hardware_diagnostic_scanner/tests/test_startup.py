"""Tests for OS startup-entry enumeration (headless, read-only, temp dirs)."""
from __future__ import annotations

import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scanner import startup as st


class TestAutostartEntries(unittest.TestCase):
    """*.desktop parsing: valid, hidden, malformed, empty, missing dir."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shds_autostart_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, filename: str, content: str, encoding: str = "utf-8") -> Path:
        path = self.tmp / filename
        path.write_text(content, encoding=encoding)
        return path

    def test_valid_hidden_noshow_malformed_empty(self):
        # utf-8-sig writes a BOM — desktop parsing must handle it.
        self._write("app.desktop",
                    "[Desktop Entry]\nType=Application\nName=Test App\n"
                    "Exec=/usr/bin/test-app --flag\n", encoding="utf-8-sig")
        self._write("hidden.desktop",
                    "[Desktop Entry]\nType=Application\nName=Hidden App\n"
                    "Exec=/usr/bin/hidden\nHidden=true\n")
        self._write("noshow.desktop",
                    "[Desktop Entry]\nType=Application\nName=NoShow\n"
                    "Exec=/usr/bin/noshow\nNoDisplay=true\n")
        self._write("broken.desktop", "garbage with no section\nKEY=VALUE\n")
        self._write("empty.desktop", "")

        entries = st._autostart_entries([self.tmp])
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(set(by_name), {"Test App", "Hidden App", "NoShow"})

        app = by_name["Test App"]
        self.assertEqual(app.command, "/usr/bin/test-app --flag")
        self.assertEqual(app.kind, "autostart")
        self.assertEqual(app.source, str(self.tmp / "app.desktop"))
        self.assertEqual(app.note, "")
        self.assertEqual(by_name["Hidden App"].note, "hidden")
        self.assertEqual(by_name["NoShow"].note, "not shown")

    def test_name_falls_back_to_stem(self):
        self._write("fallback.desktop", "[Desktop Entry]\nType=Application\nExec=/bin/x\n")
        entries = st._autostart_entries([self.tmp])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "fallback")
        self.assertEqual(entries[0].command, "/bin/x")
        self.assertEqual(entries[0].note, "")

    def test_nonexistent_dir_returns_empty(self):
        self.assertEqual(st._autostart_entries([self.tmp / "missing"]), [])


class TestLaunchAgentEntries(unittest.TestCase):
    """*.plist parsing: valid, Program fallback, malformed, non-dict, missing dir."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shds_launchagents_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _dump(self, filename: str, data: object) -> Path:
        path = self.tmp / filename
        with open(path, "wb") as handle:
            plistlib.dump(data, handle)
        return path

    def test_valid_label_and_program_arguments(self):
        self._dump("com.example.agent.plist",
                   {"Label": "com.example.agent",
                    "ProgramArguments": ["/usr/bin/python3", "-c", "print(1)"]})
        entries = st._launchagent_entries([self.tmp])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, "com.example.agent")
        self.assertEqual(entry.command, "/usr/bin/python3 -c print(1)")
        self.assertEqual(entry.kind, "launchagent")
        self.assertEqual(entry.source, str(self.tmp / "com.example.agent.plist"))

    def test_program_fallback_and_stem_label(self):
        self._dump("bare.plist", {"Program": "/usr/sbin/helper"})
        entries = st._launchagent_entries([self.tmp])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "bare")
        self.assertEqual(entries[0].command, "/usr/sbin/helper")

    def test_malformed_and_non_dict_plists_skipped(self):
        (self.tmp / "broken.plist").write_bytes(b"<<< definitely not a plist >>>")
        self._dump("array.plist", [1, 2, 3])
        self._dump("good.plist", {"Label": "good.agent", "Program": "/bin/true"})
        entries = st._launchagent_entries([self.tmp])
        self.assertEqual([entry.name for entry in entries], ["good.agent"])

    def test_nonexistent_dir_returns_empty(self):
        self.assertEqual(st._launchagent_entries([self.tmp / "missing"]), [])


class TestUserStartupDirs(unittest.TestCase):
    """user_startup_dirs: platform dispatch, pure (never creates anything)."""

    def test_linux_paths_returned_without_creating_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            with patch.object(os.path, "expanduser", return_value=str(fake_home)), \
                 patch.object(sys, "platform", "linux"):
                dirs = st.user_startup_dirs()
            self.assertEqual(dirs, [fake_home / ".config" / "autostart",
                                    Path("/etc/xdg/autostart")])
            for directory in dirs:
                self.assertFalse(directory.exists())

    def test_macos_paths_returned_without_creating_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            with patch.object(os.path, "expanduser", return_value=str(fake_home)), \
                 patch.object(sys, "platform", "darwin"):
                dirs = st.user_startup_dirs()
            self.assertEqual(dirs, [fake_home / "Library" / "LaunchAgents",
                                    Path("/Library/LaunchAgents")])
            for directory in dirs:
                self.assertFalse(directory.exists())

    def test_windows_has_no_autostart_dirs(self):
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(st.user_startup_dirs(), [])


class TestCollectStartupEntries(unittest.TestCase):
    """collect_startup_entries: never raises; note always non-empty."""

    def test_returns_tuple_with_note_and_typed_entries(self):
        entries, note = st.collect_startup_entries()
        self.assertIsInstance(entries, list)
        self.assertIsInstance(note, str)
        self.assertTrue(note.strip())
        allowed_kinds = {"registry", "startup-folder", "autostart", "launchagent"}
        for entry in entries:
            self.assertIsInstance(entry, st.StartupEntry)
            self.assertIn(entry.kind, allowed_kinds)
            self.assertTrue(entry.source)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only sources")
    def test_note_merges_counts_and_source_errors(self):
        fake = st.StartupEntry(name="App One", command="app1.exe",
                               source=r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                               kind="registry")
        with patch.object(st, "_win_registry_entries", return_value=([fake, fake], "")), \
             patch.object(st, "_win_startup_folder_entries",
                          return_value=([], "APPDATA not set — folder skipped")):
            entries, note = st.collect_startup_entries()
        self.assertEqual(len(entries), 2)
        self.assertIn("2 startup entries found (2 registry)", note)
        self.assertIn("source errors", note)
        self.assertIn("APPDATA not set", note)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only sources")
    def test_registry_source_is_read_only_and_typed(self):
        entries, note = st._win_registry_entries()
        self.assertIsInstance(entries, list)
        self.assertIsInstance(note, str)
        for entry in entries:
            self.assertEqual(entry.kind, "registry")
            self.assertTrue(entry.source.startswith(("HKCU\\", "HKLM\\")))

    @unittest.skipUnless(sys.platform == "win32", "Windows-only sources")
    def test_startup_folder_missing_appdata_degrades(self):
        env = dict(os.environ)
        env.pop("APPDATA", None)
        with patch.dict(os.environ, env, clear=True):
            entries, note = st._win_startup_folder_entries()
        self.assertEqual(entries, [])
        self.assertIn("APPDATA", note)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only sources")
    def test_startup_folder_entries_from_fake_appdata(self):
        base = Path(tempfile.mkdtemp(prefix="shds_appdata_"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        startup_dir = (base / "Microsoft" / "Windows" / "Start Menu"
                       / "Programs" / "Startup")
        startup_dir.mkdir(parents=True)
        (startup_dir / "App Shortcuts.lnk").write_bytes(b"\x00\x01")
        (startup_dir / "notes.txt").write_bytes(b"hello")
        env = dict(os.environ)
        env["APPDATA"] = str(base)
        with patch.dict(os.environ, env, clear=True):
            entries, note = st._win_startup_folder_entries()
        self.assertEqual(len(entries), 2)
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(set(by_name), {"App Shortcuts", "notes"})
        self.assertEqual(by_name["App Shortcuts"].command, "App Shortcuts.lnk")
        self.assertEqual(by_name["notes"].command, "notes.txt")
        self.assertEqual(by_name["App Shortcuts"].kind, "startup-folder")
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
