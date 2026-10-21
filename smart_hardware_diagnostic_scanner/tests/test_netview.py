"""Tests for network view: address formatting, ordering, caps, degradation."""
from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scanner import netview as nv


def _conn(kind=socket.SOCK_STREAM, laddr=(), raddr=(), status="", pid=0):
    """Build a fake psutil network connection entry."""
    return SimpleNamespace(type=kind, laddr=laddr, raddr=raddr,
                           status=status, pid=pid)


def _sample_rows():
    """Six connection rows in deliberately unsorted order."""
    return [
        _conn(socket.SOCK_DGRAM, laddr=("0.0.0.0", 5353), raddr=(),
              status="NONE", pid=900),
        _conn(socket.SOCK_STREAM, laddr=("10.0.0.2", 52000),
              raddr=("93.184.216.34", 443), status="ESTABLISHED", pid=102),
        _conn(socket.SOCK_STREAM, laddr=("0.0.0.0", 445), raddr=(),
              status="LISTEN", pid=0),
        _conn(socket.SOCK_STREAM, laddr=("10.0.0.2", 51000),
              raddr=("93.184.216.34", 443), status="ESTABLISHED", pid=102),
        _conn(socket.SOCK_DGRAM, laddr=("127.0.0.1", 1900), raddr=(),
              status="NONE", pid=55),
        _conn(socket.SOCK_STREAM, laddr=("::1", 8080), raddr=("::1", 9090),
              status="ESTABLISHED", pid=300),
    ]


def _fake_psutil(rows, names_by_pid):
    """A psutil stub with net_connections/Process/AccessDenied, plus call log."""
    fake = MagicMock()
    fake.AccessDenied = type("AccessDenied", (Exception,), {})
    fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    looked_pids: list[int] = []

    def get_process(pid):
        looked_pids.append(pid)
        if pid not in names_by_pid:
            raise fake.NoSuchProcess(f"pid {pid} is gone")
        proc = MagicMock()
        proc.name.return_value = names_by_pid[pid]
        return proc

    fake.Process = MagicMock(side_effect=get_process)
    fake.net_connections = MagicMock(return_value=rows)
    return fake, looked_pids


class TestFormatAddr(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(nv._format_addr(("1.2.3.4", 80)), "1.2.3.4:80")

    def test_ipv6_is_bracketed(self):
        self.assertEqual(nv._format_addr(("::1", 8080)), "[::1]:8080")
        self.assertEqual(nv._format_addr(("2001:db8::1", 443)), "[2001:db8::1]:443")

    def test_dual_stack_tuple_uses_first_two(self):
        self.assertEqual(nv._format_addr(("fe80::1", 546, 0, 17)),
                         "[fe80::1]:546")

    def test_unknown_addresses_become_dash(self):
        self.assertEqual(nv._format_addr(()), "-")
        self.assertEqual(nv._format_addr(None), "-")
        self.assertEqual(nv._format_addr(("only-ip",)), "-")
        self.assertEqual(nv._format_addr(("", 0)), "-")

    def test_zero_port_is_kept(self):
        self.assertEqual(nv._format_addr(("0.0.0.0", 0)), "0.0.0.0:0")

    def test_unusable_port_becomes_dash(self):
        self.assertEqual(nv._format_addr(("1.2.3.4", "junk")), "-")


class TestCollectConnections(unittest.TestCase):
    def test_missing_psutil_degrades(self):
        with patch.object(nv, "HAS_PSUTIL", False), patch.object(nv, "psutil", None):
            rows, note = nv.collect_connections()
        self.assertEqual(rows, [])
        self.assertEqual(note, "psutil not installed")

    def test_access_denied_degrades(self):
        fake = MagicMock()
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        fake.net_connections = MagicMock(
            side_effect=fake.AccessDenied("cannot see"))
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            rows, note = nv.collect_connections()
        self.assertEqual(rows, [])
        self.assertIn("privileges", note)

    def test_psutil_failure_degrades(self):
        fake = MagicMock()
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        fake.net_connections = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            rows, note = nv.collect_connections()
        self.assertEqual(rows, [])
        self.assertIn("boom", note)

    def test_empty_table_note(self):
        fake, looked = _fake_psutil([], {})
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            rows, note = nv.collect_connections()
        self.assertEqual(rows, [])
        self.assertEqual(note, "no connections reported.")
        self.assertEqual(looked, [])

    def test_sorting_tcp_first_then_other_tcp_then_udp(self):
        fake, looked = _fake_psutil(_sample_rows(), {55: "bonjour.exe",
                                                     102: "chrome.exe",
                                                     300: "app.exe"})
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            rows, note = nv.collect_connections()
        fake.net_connections.assert_called_once_with(kind="inet")
        # Read-only: nothing that could signal a process is ever touched.
        fake.kill.assert_not_called()
        fake.terminate.assert_not_called()
        fake.send_signal.assert_not_called()
        # Sorted: TCP ESTABLISHED (by pid, then local), other TCP, UDP.
        self.assertEqual([r.kind for r in rows], ["TCP"] * 4 + ["UDP"] * 2)
        self.assertEqual([r.status for r in rows[:3]],
                         ["ESTABLISHED"] * 3)
        self.assertEqual([r.local for r in rows],
                         ["10.0.0.2:51000", "10.0.0.2:52000", "[::1]:8080",
                          "0.0.0.0:445", "127.0.0.1:1900", "0.0.0.0:5353"])
        self.assertEqual([r.pid for r in rows], [102, 102, 300, 0, 55, 900])
        self.assertEqual(rows[0].remote, "93.184.216.34:443")
        self.assertEqual(rows[3].remote, "-")   # listener has no peer
        # Names: cached, one lookup per pid > 0 only; dead pid -> "".
        self.assertEqual([r.process for r in rows],
                         ["chrome.exe", "chrome.exe", "app.exe", "",
                          "bonjour.exe", ""])
        self.assertEqual(looked, [55, 102, 300, 900])
        self.assertEqual(note, "6 connection(s) listed.")

    def test_limit_caps_rows_and_note_reports_it(self):
        fake, _looked = _fake_psutil(_sample_rows(), {55: "bonjour.exe",
                                                      102: "chrome.exe",
                                                      300: "app.exe"})
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            rows, note = nv.collect_connections(limit=4)
        fake.net_connections.assert_called_once_with(kind="inet")
        self.assertEqual(len(rows), 4)
        # The first rows are the sorted head, not the raw psutil order.
        self.assertEqual([r.kind for r in rows], ["TCP"] * 4)
        self.assertEqual([r.status for r in rows[:3]], ["ESTABLISHED"] * 3)
        self.assertEqual(note, "showing first 4 of 6 connections")


class TestProcessNameLookups(unittest.TestCase):
    def test_lookup_only_for_pid_over_zero(self):
        rows = [_conn(status="ESTABLISHED", pid=42,
                      laddr=("10.0.0.2", 1000), raddr=("1.1.1.1", 443)),
                _conn(status="LISTEN", pid=0, laddr=("0.0.0.0", 12345)),
                _conn(socket.SOCK_DGRAM, pid=999, laddr=("127.0.0.1", 5301))]
        fake, looked = _fake_psutil(rows, {42: "named.exe"})
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            got, _note = nv.collect_connections()
        self.assertEqual(looked, [42, 999])  # pid 0 was never looked up
        self.assertEqual(got[0].process, "named.exe")
        self.assertEqual(got[1].process, "")
        self.assertEqual(got[2].process, "")  # dead/failed lookup degrades to ""

    def test_process_lookup_failure_is_swallowed(self):
        fake = MagicMock()
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        fake.Process = MagicMock(side_effect=PermissionError("no"))
        fake.net_connections = MagicMock(
            return_value=[_conn(status="ESTABLISHED", pid=5,
                                laddr=("1.1.1.1", 1), raddr=("2.2.2.2", 2))])
        with patch.object(nv, "HAS_PSUTIL", True), patch.object(nv, "psutil", fake):
            got, _note = nv.collect_connections()
        self.assertEqual(got[0].pid, 5)
        self.assertEqual(got[0].process, "")


if __name__ == "__main__":
    unittest.main()
