"""Tests for the pure schedule builders: validation, argv, cron lines, marker stripping."""
from __future__ import annotations

import unittest

from scanner import scheduler as s


class TestValidateTime(unittest.TestCase):
    def test_accepts_valid_times(self):
        for good in ("09:00", "00:00", "23:59"):
            self.assertTrue(s.validate_time(good), good)

    def test_rejects_bad_times(self):
        for bad in ("9:00", "24:00", "ab:cd", "12:60", "", "09:000", "09 00"):
            self.assertFalse(s.validate_time(bad), bad)

    def test_rejects_non_string(self):
        self.assertFalse(s.validate_time(None))
        self.assertFalse(s.validate_time(900))


class TestDayToCronDow(unittest.TestCase):
    def test_maps_all_seven_days(self):
        self.assertEqual(s.day_to_cron_dow("MON"), 1)
        self.assertEqual(s.day_to_cron_dow("TUE"), 2)
        self.assertEqual(s.day_to_cron_dow("WED"), 3)
        self.assertEqual(s.day_to_cron_dow("THU"), 4)
        self.assertEqual(s.day_to_cron_dow("FRI"), 5)
        self.assertEqual(s.day_to_cron_dow("SAT"), 6)
        self.assertEqual(s.day_to_cron_dow("SUN"), 0)

    def test_case_insensitive(self):
        self.assertEqual(s.day_to_cron_dow("mon"), 1)
        self.assertEqual(s.day_to_cron_dow("Sun"), 0)

    def test_invalid_day_raises(self):
        with self.assertRaises(ValueError):
            s.day_to_cron_dow("HOLIDAY")


class TestScanArgs(unittest.TestCase):
    def test_shared_by_both_backends(self):
        self.assertEqual(s.scan_args(), ["--scan", "--quiet", "--no-history"])
        tr = s.build_schtasks_argv("py", "main.py")[s.build_schtasks_argv("py", "main.py").index("/TR") + 1]
        self.assertIn(" ".join(s.scan_args()), tr)
        self.assertIn(" ".join(s.scan_args()), s.build_cron_line("py", "main.py"))


class TestBuildSchtasksArgv(unittest.TestCase):
    def test_exact_default_argv(self):
        self.assertEqual(
            s.build_schtasks_argv("py", "main.py"),
            ["schtasks", "/Create", "/F", "/TN", "SHDS weekly scan", "/SC", "WEEKLY",
             "/D", "MON", "/ST", "09:00", "/TR", '"py" "main.py" --scan --quiet --no-history'],
        )

    def test_embedded_quotes_survive_spaces_in_tr(self):
        argv = s.build_schtasks_argv(
            r"C:\Program Files\Python\python.exe", r"C:\Tools\shds run.py", "SAT", "07:15")
        tr = argv[argv.index("/TR") + 1]
        self.assertEqual(tr, r'"C:\Program Files\Python\python.exe" "C:\Tools\shds run.py"'
                             ' --scan --quiet --no-history')
        self.assertEqual(argv[argv.index("/D") + 1], "SAT")
        self.assertEqual(argv[argv.index("/ST") + 1], "07:15")
        self.assertEqual(argv[argv.index("/TN") + 1], "SHDS weekly scan")

    def test_day_normalized_to_uppercase(self):
        argv = s.build_schtasks_argv("py", "main.py", day="sat")
        self.assertEqual(argv[argv.index("/D") + 1], "SAT")

    def test_bare_executable_first_element(self):
        # plain executable name: the caller launches it with shell=False
        argv = s.build_schtasks_argv("py", "main.py")
        self.assertEqual(argv[0], "schtasks")
        self.assertNotIn("shell", argv)

    def test_bad_day_raises(self):
        for bad in ("HOLIDAY", "Monday", "", None):
            with self.assertRaises(ValueError):
                s.build_schtasks_argv("py", "main.py", day=bad)

    def test_bad_time_raises(self):
        for bad in ("9:00", "24:00", "12:60", "ab:cd"):
            with self.assertRaises(ValueError):
                s.build_schtasks_argv("py", "main.py", when=bad)


class TestBuildCronLine(unittest.TestCase):
    def test_monday_0930_exact_line(self):
        line = s.build_cron_line("python3", "main.py", "MON", "09:30")
        expected = "30 9 * * 1 python3 main.py --scan --quiet --no-history " + s.SCHEDULE_MARKER
        self.assertEqual(line, expected)

    def test_marker_is_last(self):
        self.assertTrue(s.build_cron_line("python3", "main.py").endswith(s.SCHEDULE_MARKER))

    def test_sunday_maps_to_zero_v5(self):
        self.assertTrue(
            s.build_cron_line("python3", "main.py", "SUN", "00:00").startswith("0 0 * * 0 "))

    def test_bad_day_raises(self):
        with self.assertRaises(ValueError):
            s.build_cron_line("python3", "main.py", day="FUNDAY")

    def test_bad_time_raises(self):
        with self.assertRaises(ValueError):
            s.build_cron_line("python3", "main.py", when="9:00")


class TestStripScheduledEntries(unittest.TestCase):
    def test_removes_marker_lines_keeps_others(self):
        text = ("SHELL=/bin/bash\n"
                "MAILTO=admin@example.com\n"
                "0 9 * * 1 python backup.py\n"
                "0 9 * * 1 python scan.py --scan --quiet --no-history " + s.SCHEDULE_MARKER + "\n"
                + s.SCHEDULE_MARKER + "\n"
                "0 12 * * * python report.py\n")
        new_text, count = s.strip_scheduled_entries(text)
        self.assertEqual(count, 2)
        self.assertEqual(new_text.splitlines(), [
            "SHELL=/bin/bash",
            "MAILTO=admin@example.com",
            "0 9 * * 1 python backup.py",
            "0 12 * * * python report.py",
        ])

    def test_empty_string_is_none_safe(self):
        self.assertEqual(s.strip_scheduled_entries(""), ("", 0))

    def test_no_marker_leaves_text_unchanged(self):
        text = "SHELL=/bin/bash\n0 9 * * 1 python backup.py\n"
        new_text, count = s.strip_scheduled_entries(text)
        self.assertEqual(count, 0)
        self.assertEqual(new_text, text)

    def test_trailing_blank_lines_are_collapsed(self):
        text = "0 9 * * 1 python backup.py\n" + s.SCHEDULE_MARKER + "\n\n\n"
        new_text, count = s.strip_scheduled_entries(text)
        self.assertEqual(count, 1)
        self.assertEqual(new_text, "0 9 * * 1 python backup.py")

    def test_non_string_input_is_none_safe(self):
        self.assertEqual(s.strip_scheduled_entries(None), ("", 0))


class TestMergeCrontab(unittest.TestCase):
    def test_none_or_empty_means_no_crontab(self):
        self.assertEqual(s.merge_crontab(None), "")
        self.assertEqual(s.merge_crontab(""), "")
        self.assertEqual(s.merge_crontab("   \n  "), "")

    def test_keeps_other_jobs_drops_old_marker_lines(self):
        existing = ("MAILTO=admin@example.com\n"
                    "0 9 * * 1 python scan.py --scan --quiet --no-history " + s.SCHEDULE_MARKER + "\n"
                    "30 7 * * 5 python db_backup.py\n"
                    + s.SCHEDULE_MARKER + "\n")
        merged = s.merge_crontab(existing)
        self.assertNotIn(s.SCHEDULE_MARKER, merged)
        self.assertEqual(merged.splitlines(), [
            "MAILTO=admin@example.com",
            "30 7 * * 5 python db_backup.py",
        ])

    def test_plain_crontab_is_returned_unchanged(self):
        existing = "0 5 * * * python backup.py\n"
        self.assertEqual(s.merge_crontab(existing), existing)


if __name__ == "__main__":
    unittest.main()
