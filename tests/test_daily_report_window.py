"""Unit tests for reports.get_daily_report_window (pure, no network).

No Supabase, no Telegram, no live bot required. ``reports`` imports only the
``supabase`` class (no client construction), so it imports cleanly without env.
"""

import unittest
from datetime import datetime, timedelta, timezone

import pytz

from reports import get_daily_report_window

WAT = pytz.timezone("Africa/Lagos")
UTC = timezone.utc


def _wat(y, mo, d, h, mi=0):
    return WAT.localize(datetime(y, mo, d, h, mi))


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


class TestGetDailyReportWindow(unittest.TestCase):

    # --- NULL cursor (first run) -------------------------------------------

    def test_null_cursor_uses_start_of_today_wat(self):
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(None, now)
        self.assertEqual(start, _wat(2026, 10, 5, 0, 0))
        self.assertEqual(end, now)

    def test_null_cursor_first_run_is_sub_day_span_not_24h(self):
        # First run at 21:00 -> window is 00:00..21:00 (21h), not a full 24h.
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(None, now)
        self.assertEqual((end - start), timedelta(hours=21))

    def test_null_cursor_just_after_midnight_wat(self):
        # 00:30 WAT on Oct 6 -> start of today 00:00 Oct 6, end 00:30 Oct 6.
        now = _wat(2026, 10, 6, 0, 30)
        start, end = get_daily_report_window(None, now)
        self.assertEqual(start, _wat(2026, 10, 6, 0, 0))
        self.assertEqual(end, now)
        self.assertLess(start, end)

    def test_null_cursor_does_not_crash_on_naive_now(self):
        # A naive `now` must raise rather than silently produce a bad window.
        with self.assertRaises(ValueError):
            get_daily_report_window(None, datetime(2026, 10, 5, 21, 0))

    # --- Normal 24h window from cutoff -------------------------------------

    def test_normal_24h_window_from_cutoff(self):
        old = _wat(2026, 10, 4, 21, 0)
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(old, now)
        self.assertEqual(start, old)
        self.assertEqual(end, now)
        self.assertEqual((end - start), timedelta(hours=24))

    def test_subsequent_windows_are_24h(self):
        now1 = _wat(2026, 10, 5, 21, 0)
        _, end1 = get_daily_report_window(None, now1)
        now2 = _wat(2026, 10, 6, 21, 0)
        start2, end2 = get_daily_report_window(end1, now2)
        self.assertEqual((end2 - start2), timedelta(hours=24))

    # --- Cutoff / half-open boundary ---------------------------------------

    def test_order_at_cutoff_lands_in_exactly_one_report(self):
        # Day1 report (first run): [00:00, 21:00) — order at 21:00 excluded.
        # Day2 report: [21:00 day1, 21:00 day2) — order at 21:00 day1 included.
        now1 = _wat(2026, 10, 5, 21, 0)
        start1, end1 = get_daily_report_window(None, now1)
        self.assertLess(start1, _wat(2026, 10, 5, 21, 0))
        self.assertEqual(end1, _wat(2026, 10, 5, 21, 0))

        now2 = _wat(2026, 10, 6, 21, 0)
        start2, end2 = get_daily_report_window(end1, now2)
        self.assertEqual(start2, end1)        # 21:00 day1 is the start of window2
        self.assertEqual(end2, now2)
        # An order exactly at end1 (== start2) is excluded from window1 (half-open)
        # and included in window2 (>= start2). Exactly one report.

    def test_order_before_cutoff_lands_in_day1_only(self):
        # 20:59 day1 must be inside window1 and outside window2 -> no duplicate.
        now1 = _wat(2026, 10, 5, 21, 0)
        start1, end1 = get_daily_report_window(None, now1)
        order_time = _wat(2026, 10, 5, 20, 59)
        self.assertTrue(start1 <= order_time < end1)  # in window1

        now2 = _wat(2026, 10, 6, 21, 0)
        start2, end2 = get_daily_report_window(end1, now2)
        self.assertFalse(start2 <= order_time < end2)  # not in window2

    def test_order_after_cutoff_lands_in_day2_only(self):
        # 21:30 day1 must be outside window1 and inside window2.
        now1 = _wat(2026, 10, 5, 21, 0)
        start1, end1 = get_daily_report_window(None, now1)
        order_time = _wat(2026, 10, 5, 21, 30)
        self.assertFalse(start1 <= order_time < end1)  # not in window1

        now2 = _wat(2026, 10, 6, 21, 0)
        start2, end2 = get_daily_report_window(end1, now2)
        self.assertTrue(start2 <= order_time < end2)  # in window2

    # --- Long-gap clamp ----------------------------------------------------

    def test_long_gap_clamped_to_48h(self):
        old = _wat(2026, 10, 1, 21, 0)  # 4 days before now
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(old, now)
        self.assertEqual(start, now - timedelta(hours=48))
        self.assertEqual(end, now)
        self.assertLess(start, end)

    def test_gap_exactly_48h_is_not_clamped(self):
        # "more than 48h" -> exactly 48h is NOT clamped.
        old = _wat(2026, 10, 3, 21, 0)
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(old, now)
        self.assertEqual(start, old)
        self.assertEqual(end, now)

    def test_clamp_uses_lookback_from_now_not_from_old(self):
        # Verify the clamped start is now-48h regardless of how old the cursor is.
        old = _wat(2026, 9, 1, 21, 0)  # far in the past
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(old, now)
        self.assertEqual(start, now - timedelta(hours=48))

    # --- Empty window ------------------------------------------------------

    def test_empty_window_when_start_equals_end(self):
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(now, now)  # cursor already at now
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_empty_window_when_cutoff_in_future(self):
        now = _wat(2026, 10, 5, 21, 0)
        old = now + timedelta(hours=1)
        start, end = get_daily_report_window(old, now)
        self.assertIsNone(start)
        self.assertIsNone(end)

    # --- WAT-to-UTC boundary around midnight --------------------------------

    def test_wat_to_utc_boundary_around_midnight(self):
        # 00:30 WAT on Oct 6 == 23:30 UTC on Oct 5. Start of today WAT is
        # 00:00 Oct 6 WAT == 23:00 UTC Oct 5.
        now_utc = _utc(2026, 10, 5, 23, 30)
        start, end = get_daily_report_window(None, now_utc)
        self.assertEqual(start, _utc(2026, 10, 5, 23, 0))
        self.assertEqual(end, now_utc)
        self.assertLess(start, end)

    def test_window_datetimes_are_timezone_aware(self):
        now = _wat(2026, 10, 5, 21, 0)
        start, end = get_daily_report_window(None, now)
        self.assertIsNotNone(start.tzinfo)
        self.assertIsNotNone(end.tzinfo)

    def test_cutoff_in_utc_compared_against_now_in_wat(self):
        # Mixed timezones must still compare by instant.
        old_utc = _utc(2026, 10, 4, 20, 0)   # == 21:00 WAT Oct 4
        now_wat = _wat(2026, 10, 5, 21, 0)   # == 20:00 UTC Oct 5
        start, end = get_daily_report_window(old_utc, now_wat)
        self.assertEqual(start, old_utc)
        self.assertEqual(end, now_wat)
        self.assertEqual((end - start), timedelta(hours=24))


if __name__ == "__main__":
    unittest.main()
