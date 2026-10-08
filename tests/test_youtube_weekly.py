import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = Path(tempfile.mkdtemp(prefix="pursuit_weekly_test_"))
os.environ.update(PURSUIT_STATE_DIR=str(TEST_DIR / "state"), PURSUIT_OUT=str(TEST_DIR / "out"),
                  PURSUIT_LOG=str(TEST_DIR / "log.txt"), PURSUIT_POSTFORME_KEY="test-key")

import autopilot as ap
import youtube_weekly as yt


class WeeklyTests(unittest.TestCase):
    def test_slots_are_three_per_week_across_dst(self):
        current = dt.datetime(2026, 10, 30, 18, tzinfo=ap.TZ)
        result = yt.slots([], 6, current)
        self.assertEqual([x.weekday() for x in result], [0, 2, 4, 0, 2, 4])
        self.assertTrue(all(x.hour == 17 for x in result))
        self.assertTrue(all(x.utcoffset() == dt.timedelta(hours=-7) for x in result))

    def test_existing_schedule_and_half_hour_lead_time(self):
        current = dt.datetime(2026, 10, 7, 16, 45, tzinfo=ap.TZ)
        first = yt.slots([], 1, current)[0]
        self.assertEqual(first.date(), dt.date(2026, 10, 9))
        following = yt.slots([{"scheduled_at": first.isoformat()}], 1, current)[0]
        self.assertEqual(following.date(), dt.date(2026, 10, 12))

    def test_wrong_channel_is_rejected_even_with_same_name(self):
        with patch.object(ap, "connected_accounts", return_value=[
                {"id": "account", "platform": "youtube", "user_id": "wrong"}]):
            with self.assertRaises(ap.Stop):
                yt.verified_account({"account_id": "account", "channel_id": "expected"})

    def test_unconfirmed_submission_is_not_reposted(self):
        ledger = {"posts": [{"external_id": "clip-youtube", "status": "unknown",
                             "scheduled_at": "2026-10-09T17:00:00-06:00"}]}
        with patch.object(ap, "find_posts_by_external_id", return_value=[]), \
                patch.object(ap, "api") as api:
            with self.assertRaises(ap.Stop):
                yt.reconcile(ledger, "account")
            api.assert_not_called()
