"""Tests for the largest-files finder (temp trees, read-only, headless)."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from scanner import filesutil as fu


def _mb(count: int) -> int:
    return count * 1024 * 1024


class TestLargestFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="shds_files_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _write(self, rel: str, size_bytes: int) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.seek(size_bytes - 1)  # sparse write: fast, exact logical size
            handle.write(b"\x00")
        return path

    def test_finds_largest_sorted_and_skips_symlinks(self):
        big = self._write("big.bin", _mb(5))
        self._write("a/b/c/d/e/deep.bin", _mb(2))
        small = self._write("small.txt", 10)
        link = self.root / "link.bin"
        try:
            link.symlink_to(big)
            linked = True
        except OSError:
            linked = False  # Windows may lack symlink privileges — stay defensive

        files, note = fu.largest_files(self.root, limit=10)
        self.assertEqual([f.size_bytes for f in files],
                         sorted([_mb(5), _mb(2), 10], reverse=True))
        self.assertEqual(files[0].size_bytes, _mb(5))
        self.assertEqual(Path(files[0].path).name, "big.bin")
        self.assertEqual(Path(files[1].path).name, "deep.bin")
        self.assertEqual(files[2].path, str(small))
        self.assertIn("file(s) seen", note)
        if linked:
            self.assertNotIn(str(link), [f.path for f in files])

    def test_depth_cap_hides_deeper_files(self):
        shallow = self._write("a/shallow.txt", 100)
        self._write("a/b/c/verydeep.txt", 100)
        files, _note = fu.largest_files(self.root, limit=10, max_depth=1)
        names = {Path(f.path).name for f in files}
        self.assertIn("shallow.txt", names)
        self.assertNotIn("verydeep.txt", names)
        self.assertEqual(Path(files[0].path), shallow)

    def test_missing_root(self):
        files, note = fu.largest_files(self.root / "does_not_exist")
        self.assertEqual(files, [])
        self.assertIn("does not exist", note)

    def test_single_file_as_root(self):
        only = self._write("only.bin", 2048)
        files, note = fu.largest_files(only)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, str(only))
        self.assertEqual(files[0].size_bytes, 2048)
        self.assertIn("single file", note)

    def test_limit_keeps_only_the_biggest(self):
        self._write("three.bin", _mb(3))
        self._write("one.bin", _mb(1))
        files, _note = fu.largest_files(self.root, limit=1)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].size_bytes, _mb(3))

    def test_unreadable_dir_skipped_without_raising(self):
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "inner.bin").write_bytes(b"x" * 64)
        visible = self._write("visible.bin", 256)
        access_denied = False
        try:
            try:
                os.chmod(locked, 0)
            except OSError:
                pass  # chmod may be a no-op on Windows; keep going defensively
            try:
                os.listdir(locked)
            except OSError:
                access_denied = True
            files, note = fu.largest_files(self.root, limit=10)
            paths = [f.path for f in files]
            self.assertIn(str(visible), paths)  # visible file always survives
            if access_denied:
                self.assertNotIn(str(locked / "inner.bin"), paths)
                self.assertIn("skipped", note)
            # If the platform still grants access, both files may appear — accept.
        finally:
            try:
                os.chmod(locked, stat.S_IRWXU)  # restore so cleanup can remove it
            except OSError:
                pass

    def test_zero_time_budget_reports_partial(self):
        self._write("a.bin", 1024)
        files, note = fu.largest_files(self.root, limit=10, time_budget_s=0.0)
        self.assertIn("partial scan", note)
        self.assertIn("time budget reached", note)
        self.assertIsInstance(files, list)  # may be empty — the budget is spent at once


if __name__ == "__main__":
    unittest.main()
