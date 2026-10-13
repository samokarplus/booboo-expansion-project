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
    def setUp(self):
        yt.LEDGER_FILE.unlink(missing_ok=True)
        patcher = patch.object(yt, "adopt_orphans")
        patcher.start()
        self.addCleanup(patcher.stop)

    def queue_item(self):
        ep_dir = TEST_DIR / "episode"
        clip_dir = ep_dir / "01_good"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "clip.mp4").write_bytes(b"mp4")
        return {"external_id": "pursuit-episode-01_good", "status": "queued", "score": 99,
                "episode": "episode", "ep_dir": str(ep_dir),
                "meta": {"id": "episode", "title": "Episode", "url": "https://youtu.be/episode01",
                         "upload_date": "20261001"},
                "analysis": {"youtube_title": "Title", "caption": "Caption"},
                "clip": {"folder": "01_good", "file": "01_good/clip.mp4",
                         "start_sec": 10, "end_sec": 50}}

    def scheduled_post(self, external_id="pursuit-episode-01_good-youtube-weekly", status="scheduled",
                       post_id="post_1", scheduled_at=None):
        scheduled_at = scheduled_at or dt.datetime(2026, 10, 12, 5, 30, tzinfo=ap.TZ).isoformat()
        return {"external_id": external_id, "status": status, "post_id": post_id,
                "scheduled_at": scheduled_at, "episode": "episode", "clip": "01_good",
                "start_sec": 10.0, "end_sec": 50.0}

    def test_slots_are_three_per_week_across_dst(self):
        current = dt.datetime(2026, 10, 30, 18, tzinfo=ap.TZ)
        result = yt.slots([], 6, current)
        self.assertEqual([x.weekday() for x in result], [0, 2, 4, 0, 2, 4])
        self.assertTrue(all((x.hour, x.minute) == (5, 0) for x in result))
        self.assertTrue(all(x.utcoffset() == dt.timedelta(hours=-7) for x in result))

    def test_existing_schedule_and_half_hour_lead_time(self):
        current = dt.datetime(2026, 10, 7, 16, 45, tzinfo=ap.TZ)
        first = yt.slots([], 1, current)[0]
        self.assertEqual(first.date(), dt.date(2026, 10, 9))
        following = yt.slots([{"status": "scheduled", "scheduled_at": first.isoformat()}], 1, current)[0]
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

    def test_quota_failure_becomes_retry_with_new_slot(self):
        item = self.queue_item()
        ledger = {"posts": [self.scheduled_post()]}
        ap.save(yt.LEDGER_FILE, ledger)
        ap.save(ap.QUEUE_FILE, {"clips": [item]})
        now = dt.datetime(2026, 10, 12, 6, 30, tzinfo=ap.TZ)
        created, deleted = [], []

        def api(method, path, body=None, query=None):
            if method == "GET" and path == "/v1/social-post-results":
                return {"data": [{"social_account_id": "account", "success": False,
                                  "error": {"code": 429, "message": "Quota exceeded for quota metric Video Uploads"}}]}
            if method == "DELETE":
                deleted.append(path)
                return {"success": True}
            if method == "POST" and path == "/v1/social-posts":
                created.append(body)
                return dict(body, id="post_2", status="scheduled")
            raise AssertionError((method, path))

        with patch.object(ap, "now", return_value=now), \
                patch.object(ap, "load", side_effect=lambda path, default=None:
                             {"youtube_weekly": {"enabled": True, "account_id": "account", "channel_id": "chan"}}
                             if path == ap.CONFIG_FILE else
                             {"clips": [item]} if path == ap.QUEUE_FILE else
                             ap.load.__wrapped__(path, default) if hasattr(ap.load, "__wrapped__") else
                             __import__("json").loads(path.read_text()) if path.exists() else default), \
                patch.object(ap, "connected_accounts", return_value=[
                    {"id": "account", "platform": "youtube", "user_id": "chan"}]), \
                patch.object(ap, "find_posts_by_external_id", return_value=[]), \
                patch.object(ap, "recent_visual_clip", return_value=True), \
                patch.object(ap, "upload_media", return_value="https://media.example/clip.mp4"), \
                patch.object(ap, "api", side_effect=api), \
                patch.object(ap, "notify") as notify:
            msg = yt.fill_schedule()

        saved = ap.load(yt.LEDGER_FILE, {"posts": []})["posts"]
        self.assertEqual(msg, "YouTube weekly: 1 Short(s) added; Monday/Wednesday/Friday at 05:00.")
        self.assertEqual(saved[0]["status"], "retry")
        self.assertEqual(saved[1]["external_id"], "pursuit-episode-01_good-youtube-weekly-r1")
        self.assertEqual(saved[1]["status"], "scheduled")
        self.assertEqual(dt.datetime.fromisoformat(saved[1]["scheduled_at"]).time(), yt.POST_TIME)
        self.assertEqual(dt.datetime.fromisoformat(saved[1]["scheduled_at"]).weekday(), 2)
        self.assertEqual(created[0]["external_id"], saved[1]["external_id"])
        self.assertEqual(deleted, ["/v1/social-posts/post_1"])
        notify.assert_not_called()

    def test_retry_cap_marks_failed_and_notifies(self):
        posts = [
            self.scheduled_post(status="retry", post_id="post_0"),
            self.scheduled_post("pursuit-episode-01_good-youtube-weekly-r1", "retry", "post_1"),
            self.scheduled_post("pursuit-episode-01_good-youtube-weekly-r2", "retry", "post_2"),
            self.scheduled_post("pursuit-episode-01_good-youtube-weekly-r3", "scheduled", "post_3"),
        ]
        ledger = {"posts": posts}

        def api(method, path, body=None, query=None):
            self.assertEqual((method, path), ("GET", "/v1/social-post-results"))
            return {"data": [{"social_account_id": "account", "success": False,
                              "error": "RESOURCE_EXHAUSTED: Quota exceeded"}]}

        with patch.object(ap, "now", return_value=dt.datetime(2026, 10, 12, 6, 30, tzinfo=ap.TZ)), \
                patch.object(ap, "api", side_effect=api), \
                patch.object(ap, "notify") as notify:
            yt.reconcile(ledger, "account")

        self.assertEqual(posts[-1]["status"], "failed")
        notify.assert_called_once()
        self.assertIn("retry limit", notify.call_args.args[1])

    def test_existing_retry_remote_post_is_adopted_not_duplicated(self):
        item = self.queue_item()
        prior = self.scheduled_post(status="retry")
        ledger = {"posts": [prior]}
        ap.save(yt.LEDGER_FILE, ledger)
        remote = {"id": "remote_r1", "status": "scheduled",
                  "external_id": "pursuit-episode-01_good-youtube-weekly-r1",
                  "scheduled_at": dt.datetime(2026, 10, 14, 5, 30, tzinfo=dt.timezone.utc).isoformat(),
                  "social_accounts": ["account"]}

        with patch.object(ap, "load", side_effect=lambda path, default=None:
                          {"youtube_weekly": {"enabled": True, "account_id": "account", "channel_id": "chan"}}
                          if path == ap.CONFIG_FILE else
                          {"clips": [item]} if path == ap.QUEUE_FILE else
                          __import__("json").loads(path.read_text()) if path.exists() else default), \
                patch.object(ap, "now", return_value=dt.datetime(2026, 10, 12, 6, 30, tzinfo=ap.TZ)), \
                patch.object(ap, "connected_accounts", return_value=[
                    {"id": "account", "platform": "youtube", "user_id": "chan"}]), \
                patch.object(ap, "find_posts_by_external_id", return_value=[remote]), \
                patch.object(ap, "recent_visual_clip", return_value=True), \
                patch.object(ap, "api") as api, \
                patch.object(ap, "upload_media") as upload:
            yt.fill_schedule()

        saved = ap.load(yt.LEDGER_FILE, {"posts": []})["posts"]
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1]["post_id"], "remote_r1")
        api.assert_not_called()
        upload.assert_not_called()

    def test_non_quota_failures_are_not_retried(self):
        ledger = {"posts": [self.scheduled_post()]}

        def api(method, path, body=None, query=None):
            self.assertEqual((method, path), ("GET", "/v1/social-post-results"))
            return {"data": [{"social_account_id": "account", "success": False,
                              "error": "invalid media format"}]}

        with patch.object(ap, "now", return_value=dt.datetime(2026, 10, 12, 6, 30, tzinfo=ap.TZ)), \
                patch.object(ap, "api", side_effect=api), \
                patch.object(ap, "notify") as notify:
            yt.reconcile(ledger, "account")

        self.assertEqual(ledger["posts"][0]["status"], "failed")
        notify.assert_called_once()


class NewBehaviorTests(unittest.TestCase):
    def test_retry_slot_ignores_failed_and_retry_entries(self):
        now = dt.datetime(2026, 10, 13, 12, 0, tzinfo=ap.TZ)
        posts = [{"status": "failed", "scheduled_at": "2026-11-11T05:00:00-07:00"},
                 {"status": "retry", "scheduled_at": "2026-11-11T05:00:00-07:00"}]
        first = yt.slots(posts, 1, now)[0]
        self.assertEqual((first.month, first.day, first.hour), (10, 14, 5))

    def test_delete_rejection_does_not_abort(self):
        def api(*a, **k):
            raise ap.Stop("Post for Me refused DELETE (400): processed posts cannot be deleted")
        with patch.object(ap, "api", api), patch.object(ap, "log"):
            yt.retire_remote_post({"post_id": "sp_x"})

    def test_orphan_remote_posts_are_adopted_once(self):
        remote = {"data": [{"id": "sp_o", "external_id": "pursuit-e-01_a-youtube-weekly", "status": "scheduled",
                            "scheduled_at": "2026-10-21T11:30:00+00:00", "social_accounts": [{"id": "acct"}]},
                           {"id": "sp_w", "external_id": "pursuit-e-03_c-youtube-weekly", "status": "scheduled",
                            "scheduled_at": "2026-10-21T11:30:00+00:00", "social_accounts": [{"id": "other"}]},
                           {"id": "sp_t", "external_id": "pursuit-e-02_b", "status": "scheduled",
                            "scheduled_at": "2026-10-21T11:30:00+00:00"}], "meta": {"next": None}}
        ledger = {"posts": []}
        with patch.object(ap, "api", return_value=remote):
            yt.adopt_orphans(ledger, "acct", {"clips": []})
            yt.adopt_orphans(ledger, "acct", {"clips": []})
        self.assertEqual([p["post_id"] for p in ledger["posts"]], ["sp_o"])

    def test_newest_episode_gets_priority(self):
        today = ap.now().strftime("%Y%m%d")
        fresh = {"score": 70, "meta": {"upload_date": today}}
        old = {"score": 75, "meta": {"upload_date": "20200101"}}
        self.assertGreater(fresh["score"] + yt.freshness(fresh), old["score"] + yt.freshness(old))

    def test_slots_fill_gaps_before_later_posts(self):
        now = dt.datetime(2026, 10, 13, 12, 0, tzinfo=ap.TZ)
        later = [{"status": "scheduled", "scheduled_at": "2026-10-21T05:30:00-06:00"}]
        days = [x.day for x in yt.slots(later, 3, now)]
        self.assertEqual(days, [14, 16, 19])

    def test_tiktok_cleanup_keeps_clip_until_youtube_uses_it(self):
        f = TEST_DIR / "episode" / "09_keep" / "clip.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"mp4")
        post = {"status": "posted", "file": str(f)}
        cfg = {"youtube_weekly": {"enabled": True}}
        with patch.object(ap, "load", side_effect=lambda path, default=None:
                          cfg if path == ap.CONFIG_FILE else {"posts": []}):
            ap.cleanup_posted_media(post)
        self.assertTrue(f.exists())
        used = {"posts": [{"clip": "09_keep", "status": "scheduled"}]}
        with patch.object(ap, "load", side_effect=lambda path, default=None:
                          cfg if path == ap.CONFIG_FILE else used):
            ap.cleanup_posted_media(post)
        self.assertFalse(f.exists())
