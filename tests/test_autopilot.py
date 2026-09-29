"""Autopilot tests against a local fake Post for Me server. Nothing here touches a real account."""
import datetime as dt
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

TMP = Path(tempfile.mkdtemp(prefix="pursuit_autopilot_test_"))
os.environ.update(PURSUIT_STATE_DIR=str(TMP / "state"), PURSUIT_OUT=str(TMP / "out"),
                  PURSUIT_LOG=str(TMP / "log.txt"), PURSUIT_POSTFORME_KEY="test-key")

import autopilot as ap  # noqa: E402
import pursuit_clips as pc  # noqa: E402

ACCOUNTS = {"youtube": "spc_yt", "instagram": "spc_ig", "tiktok": "spc_tt"}
EXPECTED = {p: "pursuitthepod" for p in ACCOUNTS}   # tests simulate all three verified
PINS = {"tiktok": "open_tiktok"}


class FakePostForMe(BaseHTTPRequestHandler):
    posts, uploads, results = {}, {}, {}
    slow_create = False      # simulate: server creates the post but the reply never arrives in time
    disconnected = set()
    usernames = {}
    user_ids = {}
    tiktok = {"handle": "pursuitthepod", "open_id": "open_tiktok", "scope_ok": True}
    fail_platform = None

    def log_message(self, *a):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_PUT(self):
        FakePostForMe.uploads[self.path] = len(self._body())
        self._json(200, {})

    def do_DELETE(self):
        FakePostForMe.posts.pop(self.path.rsplit("/", 1)[-1], None)
        self._json(200, {"success": True})

    def do_GET(self):
        if self.path.startswith("/v2/user/info/"):   # plays TikTok's own user-info endpoint
            if self.headers.get("Authorization") != "Bearer tok_tiktok":
                return self._json(401, {"error": {"code": "access_token_invalid"}})
            t = FakePostForMe.tiktok
            if not t["scope_ok"] and "username" in self.path:
                return self._json(401, {"data": {}, "error": {"code": "scope_not_authorized"}})
            return self._json(200, {"data": {"user": {"open_id": t["open_id"], "username": t["handle"],
                                                      "display_name": "Anya YT"}}, "error": {"code": "ok"}})
        if self.headers.get("Authorization") != "Bearer test-key":
            return self._json(401, {"error": "bad key"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/v1/social-accounts":
            data = [{"id": i, "platform": p, "username": self.usernames.get(p, "pursuitthepod"), "status": "connected",
                     "user_id": self.user_ids.get(p, f"open_{p}"), "access_token": f"tok_{p}",
                     "external_id": f"pursuit-{p}"}
                    for p, i in ACCOUNTS.items() if i not in self.disconnected]
            return self._json(200, {"data": data, "meta": {}})
        if u.path == "/v1/social-posts":
            ext = q.get("external_id", [])
            return self._json(200, {"data": [p for p in self.posts.values() if p["external_id"] in ext], "meta": {}})
        if u.path == "/v1/social-post-results":
            pid = q["post_id"][0]
            return self._json(200, {"data": self.results.get(pid, []), "meta": {}})
        self._json(404, {})

    def do_POST(self):
        u = urlparse(self.path)
        body = json.loads(self._body() or b"{}")
        port = self.server.server_address[1]
        if u.path == "/v1/media/create-upload-url":
            n = len(self.uploads) + len(self.posts) + int(time.time() * 1000) % 100000
            return self._json(200, {"upload_url": f"http://127.0.0.1:{port}/upload/{n}",
                                    "media_url": f"https://media.example/{n}.mp4"})
        if u.path == "/v1/social-posts":
            pid = f"sp_{len(self.posts) + 1}"
            status = "draft" if body.get("isDraft") else "scheduled"
            created = dict(body, id=pid, status=status)
            FakePostForMe.posts[pid] = created
            if FakePostForMe.slow_create:
                time.sleep(1.5)   # longer than the client's timeout
            return self._json(200, created)
        self._json(404, {})


class AutopilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePostForMe)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        ap.API = f"http://127.0.0.1:{cls.server.server_address[1]}"
        ap.TIKTOK_API = ap.API
        ap.API_TIMEOUT = 1
        ap.notify = lambda title, msg: ap.log(f"NOTIFY {title}: {msg}")
        ap.youtube_is_public = lambda url: True

    def setUp(self):
        for f in (ap.LEDGER_FILE, ap.STATE_FILE):
            f.unlink(missing_ok=True)
        FakePostForMe.posts.clear()
        FakePostForMe.results.clear()
        FakePostForMe.slow_create = False
        FakePostForMe.disconnected = set()
        FakePostForMe.usernames = {}
        FakePostForMe.user_ids = {}
        FakePostForMe.tiktok = {"handle": "pursuitthepod", "open_id": "open_tiktok", "scope_ok": True}
        ap.QUEUE_FILE.unlink(missing_ok=True)
        ap.save(ap.CONFIG_FILE, {"accounts": ACCOUNTS, "expected_usernames": EXPECTED, "verified_ids": PINS})
        self.ep_dir = TMP / "out" / "ep"
        self.clips = []
        for i in range(3):
            folder = f"0{i + 1}_clip-{i}"
            (self.ep_dir / folder).mkdir(parents=True, exist_ok=True)
            (self.ep_dir / folder / f"{folder}.mp4").write_bytes(b"x" * 1000)
            self.clips.append({"folder": folder, "file": f"{folder}/{folder}.mp4", "score": 90 - i, "clip_title": f"Clip {i}",
                               "analysis": {"youtube_title": f"Title {i}", "caption": f"Caption {i}.",
                                            "hashtags": ["running", "#ultra"], "overall": 90 - i}})
        self.summary = {"ep_dir": str(self.ep_dir), "meta": {"id": "VIDEOID1234", "title": "Episode",
                                                             "url": "https://www.youtube.com/watch?v=VIDEOID1234"}}

    def test_schedules_one_per_day_on_all_platforms_without_duplicates(self):
        done = ap.schedule_clips(self.clips, self.summary, dry_run=False)
        self.assertEqual(len(done), 3)
        posts = sorted(FakePostForMe.posts.values(), key=lambda p: p["scheduled_at"])
        self.assertEqual(len(posts), 3)
        times = [dt.datetime.fromisoformat(p["scheduled_at"].replace("Z", "+00:00")).astimezone(ap.TZ) for p in posts]
        self.assertTrue(all(t.hour == ap.POST_HOUR for t in times))
        self.assertEqual([(b - a).days for a, b in zip(times, times[1:])], [1, 1])
        self.assertGreater(times[0], ap.now())
        p = posts[0]
        self.assertEqual(sorted(p["social_accounts"]), sorted(ACCOUNTS.values()))
        self.assertEqual(p["platform_configurations"]["instagram"]["placement"], "reels")
        self.assertEqual(p["platform_configurations"]["youtube"]["title"], "Title 0")
        self.assertIn("watch?v=VIDEOID1234", p["platform_configurations"]["youtube"]["description"])
        self.assertIn("#running #ultra", p["caption"])
        # second run: everything is in the ledger, nothing new gets created
        self.assertEqual(ap.schedule_clips(self.clips, self.summary, dry_run=False), [])
        self.assertEqual(len(FakePostForMe.posts), 3)

    def test_new_episode_queues_after_existing_posts(self):
        ap.schedule_clips(self.clips[:2], self.summary, dry_run=False)
        last = max(p["scheduled_at"] for p in ap.load(ap.LEDGER_FILE, {})["posts"])
        other = dict(self.summary, meta=dict(self.summary["meta"], id="OTHERVIDEO1"))
        ap.schedule_clips(self.clips[2:], other, dry_run=False)
        newest = ap.load(ap.LEDGER_FILE, {})["posts"][-1]["scheduled_at"]
        self.assertGreater(dt.datetime.fromisoformat(newest), dt.datetime.fromisoformat(last))

    def test_ambiguous_create_is_never_reposted(self):
        FakePostForMe.slow_create = True
        with self.assertRaises(ap.Stop):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        FakePostForMe.slow_create = False
        ledger = ap.load(ap.LEDGER_FILE, {})
        self.assertEqual(ledger["posts"][0]["status"], "unknown")
        ap.reconcile()   # looks it up by external id and adopts it
        self.assertEqual(ap.load(ap.LEDGER_FILE, {})["posts"][0]["status"], "scheduled")
        ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(len(FakePostForMe.posts), 1)

    def test_remote_post_without_local_ledger_is_adopted(self):
        eid = ap.ext_id(self.summary["meta"], self.clips[0])
        when = ap.next_slots({"posts": []}, 1)[0]
        body = ap.build_post(self.clips[0], self.summary["meta"], ACCOUNTS, "https://media/1", when, eid)
        FakePostForMe.posts["sp_1"] = dict(body, id="sp_1", status="scheduled")
        done = ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(done, [eid])
        self.assertEqual(len(FakePostForMe.posts), 1)
        self.assertEqual(ap.load(ap.LEDGER_FILE, {})["posts"][0]["post_id"], "sp_1")

    def test_disconnected_account_stops_before_upload(self):
        FakePostForMe.disconnected = {"spc_tt"}
        with self.assertRaisesRegex(ap.Stop, "tiktok"):
            ap.schedule_clips(self.clips, self.summary, dry_run=False)
        self.assertEqual(FakePostForMe.posts, {})

    def test_wrong_platform_account_id_stops_before_upload(self):
        ap.save(ap.CONFIG_FILE, {"accounts": dict(ACCOUNTS, youtube="spc_ig"), "expected_usernames": EXPECTED, "verified_ids": PINS})
        with self.assertRaisesRegex(ap.Stop, "actually instagram"):
            ap.schedule_clips(self.clips, self.summary, dry_run=False)
        self.assertEqual(FakePostForMe.posts, {})

    def test_multiple_remote_posts_with_same_external_id_fail_closed(self):
        eid = ap.ext_id(self.summary["meta"], self.clips[0])
        when = ap.next_slots({"posts": []}, 1)[0]
        body = ap.build_post(self.clips[0], self.summary["meta"], ACCOUNTS, "https://media/1", when, eid)
        FakePostForMe.posts["sp_1"] = dict(body, id="sp_1", status="scheduled")
        FakePostForMe.posts["sp_2"] = dict(body, id="sp_2", status="scheduled")
        with self.assertRaisesRegex(ap.Stop, "Multiple"):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(len(FakePostForMe.posts), 2)

    # ---- only verified accounts ------------------------------------------------------------
    def tiktok_only(self):
        ap.save(ap.CONFIG_FILE, {"accounts": {"tiktok": "spc_tt"}, "verified_ids": PINS})

    def test_tiktok_only_config_posts_only_to_tiktok(self):
        self.tiktok_only()
        ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        post = list(FakePostForMe.posts.values())[0]
        self.assertEqual(post["social_accounts"], ["spc_tt"])
        self.assertEqual(list(post["platform_configurations"]), ["tiktok"])
        self.assertEqual(ap.load(ap.LEDGER_FILE, {})["posts"][0]["platforms"], ["tiktok"])

    def test_different_tiktok_account_stops_before_upload(self):
        self.tiktok_only()
        FakePostForMe.user_ids = {"tiktok": "open_someone_else"}   # same display name, different TikTok account
        with self.assertRaisesRegex(ap.Stop, "not the one verified as @pursuitthepod"):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(FakePostForMe.posts, {})

    def test_platform_without_expected_handle_is_never_used(self):
        ap.save(ap.CONFIG_FILE, {"accounts": {"tiktok": "spc_tt", "youtube": "spc_yt"}, "verified_ids": PINS})
        with self.assertRaisesRegex(ap.Stop, "not guessing"):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(FakePostForMe.posts, {})

    def test_setup_map_enables_only_verified_handles(self):
        FakePostForMe.usernames = {"youtube": "random_channel"}
        ap.CONFIG_FILE.unlink()
        with patch("autopilot.subprocess.run") as run, patch("builtins.print"):
            run.return_value.returncode = 0   # keychain says the key exists
            ap.cmd_setup(type("A", (), {"new_key": False, "reconnect": False, "connect": None}))
        cfg = ap.load(ap.CONFIG_FILE, {})
        self.assertEqual(cfg["accounts"], {"tiktok": "spc_tt"})

    def test_reconcile_checks_only_the_platforms_a_post_used(self):
        self.tiktok_only()
        ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        ledger = ap.load(ap.LEDGER_FILE, {})
        ledger["posts"][0]["scheduled_at"] = (ap.now() - dt.timedelta(hours=1)).isoformat()
        ap.save(ap.LEDGER_FILE, ledger)
        FakePostForMe.results[ledger["posts"][0]["post_id"]] = [
            {"social_account_id": "spc_tt", "success": True, "platform_data": {"url": "https://tiktok.com/@pursuitthepod/video/1"}}]
        published, problems = ap.reconcile()
        self.assertEqual(problems, [])
        self.assertEqual(ap.load(ap.LEDGER_FILE, {})["posts"][0]["status"], "posted")

    # ---- approved queue --------------------------------------------------------------------
    def queue_two_episodes(self):
        passed = [dict(c, analysis=dict(c["analysis"], category="running")) for c in self.clips]
        ap.add_to_queue(passed, self.summary, fresh=False)
        other = dict(self.summary, meta=dict(self.summary["meta"], id="OTHERVIDEO1", title="Other"))
        ap.add_to_queue([dict(self.clips[2], score=70, analysis=dict(self.clips[2]["analysis"], category="love"))],
                        other, fresh=False)

    def test_queue_never_holds_duplicates(self):
        self.queue_two_episodes()
        self.queue_two_episodes()
        ids = [q["external_id"] for q in ap.load(ap.QUEUE_FILE, {})["clips"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 4)

    def test_queue_is_not_posted_until_auto_posting_is_enabled(self):
        self.tiktok_only()
        self.queue_two_episodes()
        self.assertEqual(ap.fill_schedule(), 0)
        self.assertEqual(FakePostForMe.posts, {})

    def test_auto_post_on_requires_a_confirmed_live_post(self):
        self.tiktok_only()
        with self.assertRaisesRegex(ap.Stop, "live test"):
            ap.cmd_auto_post(type("A", (), {"state": "on"}))
        self.assertFalse(ap.load(ap.CONFIG_FILE, {}).get("auto_posting"))

    def test_fill_schedule_keeps_a_few_ahead_varied_and_no_duplicates(self):
        self.tiktok_only()
        cfg = ap.load(ap.CONFIG_FILE, {})
        ap.save(ap.CONFIG_FILE, dict(cfg, auto_posting=True))
        self.queue_two_episodes()
        self.assertEqual(ap.fill_schedule(), ap.SCHEDULE_AHEAD)
        self.assertEqual(ap.fill_schedule(), 0)   # already enough ahead: nothing more, nothing twice
        posts = sorted(ap.load(ap.LEDGER_FILE, {})["posts"], key=lambda p: p["scheduled_at"])
        self.assertEqual(len({p["external_id"] for p in posts}), len(posts))
        # the other episode's clip is mixed in instead of three from the same episode in a row
        self.assertIn("OTHERVIDEO1", [p["episode"] for p in posts[:2]])
        times = [dt.datetime.fromisoformat(p["scheduled_at"]) for p in posts]
        self.assertTrue(all(t.hour in ap.POST_HOURS for t in times))
        self.assertTrue(all((b - a) >= dt.timedelta(hours=2) for a, b in zip(times, times[1:])))

    def test_deleting_a_clip_file_vetoes_it(self):
        self.tiktok_only()
        self.queue_two_episodes()
        item = ap.load(ap.QUEUE_FILE, {})["clips"][0]
        (Path(item["ep_dir"]) / item["clip"]["file"]).unlink()
        self.assertIsNone(ap.schedule_from_queue(item, ap.now() + dt.timedelta(hours=1)))
        self.assertEqual(ap.load(ap.QUEUE_FILE, {})["clips"][0]["status"], "removed")
        self.assertEqual(FakePostForMe.posts, {})

    def run_setup(self, typed=None, keep_config=False):
        """typed=None: no Terminal (launchd). typed="...": interactive, and that's what you type."""
        if not keep_config:
            ap.CONFIG_FILE.unlink(missing_ok=True)
        with patch("autopilot.subprocess.run") as run, patch("builtins.print") as out, \
                patch("autopilot.sys.stdin.isatty", return_value=typed is not None), \
                patch("builtins.input", return_value=typed or "") as asked:
            run.return_value.returncode = 0   # keychain says the key exists
            ap.cmd_setup(type("A", (), {"new_key": False, "reconnect": False, "connect": None}))
        self.prompted = asked.called
        return ap.load(ap.CONFIG_FILE, {}), " ".join(str(c) for c in out.call_args_list)

    def test_tiktok_display_name_differs_from_handle_is_verified_via_tiktok(self):
        # the real case: Post for Me "username" is the display name, external_id is a free-text label
        FakePostForMe.usernames = {"tiktok": "Anya YT"}
        cfg, printed = self.run_setup()
        self.assertEqual(cfg["accounts"], {"tiktok": "spc_tt"})
        self.assertEqual(cfg["verified_ids"], {"tiktok": "open_tiktok"})
        self.assertEqual(cfg["usernames"]["tiktok"], "pursuitthepod")
        ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)   # and posting accepts it
        self.assertEqual(list(FakePostForMe.posts.values())[0]["social_accounts"], ["spc_tt"])

    def test_tiktok_display_name_alone_never_verifies(self):
        # display name looks right, but TikTok says the handle is someone else's
        FakePostForMe.usernames = {"tiktok": "pursuitthepod"}
        FakePostForMe.tiktok = {"handle": "not_pursuit", "open_id": "open_tiktok", "scope_ok": True}
        cfg, printed = self.run_setup()
        self.assertEqual(cfg["accounts"], {})
        self.assertIn("@not_pursuit", printed)

    # ---- the real situation: Post for Me's TikTok app can't read the handle ----------------------
    def no_handle_scope(self):
        FakePostForMe.usernames = {"tiktok": "Anya YT"}
        FakePostForMe.tiktok = {"handle": "pursuitthepod", "open_id": "open_tiktok", "scope_ok": False}

    def test_without_handle_scope_unattended_setup_never_enables(self):
        self.no_handle_scope()
        cfg, printed = self.run_setup(typed=None)
        self.assertEqual(cfg["accounts"], {})
        self.assertIn("interactive Terminal", printed)

    def test_without_handle_scope_typed_confirmation_pins_both_ids(self):
        self.no_handle_scope()
        cfg, printed = self.run_setup(typed="@PursuitThePod")
        self.assertEqual(cfg["accounts"], {"tiktok": "spc_tt"})
        self.assertEqual(cfg["verified_ids"], {"tiktok": "open_tiktok"})
        self.assertEqual(cfg["verified_by"], {"tiktok": "you"})
        self.assertIn("Anya YT", printed)            # you were shown what you're confirming
        ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(list(FakePostForMe.posts.values())[0]["social_accounts"], ["spc_tt"])

    def test_display_name_or_blank_is_not_a_confirmation(self):
        for typed in ("Anya YT", "", "pursuit", "www.tiktok.com/@pursuitthepod2"):
            self.no_handle_scope()
            cfg, _ = self.run_setup(typed=typed)
            self.assertEqual(cfg["accounts"], {}, typed)

    def test_rerunning_setup_keeps_the_confirmed_pin_without_asking_again(self):
        self.no_handle_scope()
        self.run_setup(typed="pursuitthepod")
        cfg, _ = self.run_setup(typed=None, keep_config=True)
        self.assertFalse(self.prompted)
        self.assertEqual(cfg["verified_ids"], {"tiktok": "open_tiktok"})

    def test_a_different_tiktok_account_is_never_switched_to_automatically(self):
        self.no_handle_scope()
        self.run_setup(typed="pursuitthepod")
        # the connection now belongs to another TikTok user (same display name)
        FakePostForMe.user_ids = {"tiktok": "open_intruder"}
        FakePostForMe.tiktok = dict(FakePostForMe.tiktok, open_id="open_intruder")
        with self.assertRaisesRegex(ap.Stop, "not the one verified"):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        cfg, _ = self.run_setup(typed=None, keep_config=True)   # unattended re-setup doesn't adopt it
        self.assertEqual(cfg["accounts"], {})
        self.assertEqual(FakePostForMe.posts, {})

    def test_pinned_account_disappearing_fails_closed(self):
        self.no_handle_scope()
        self.run_setup(typed="pursuitthepod")
        FakePostForMe.disconnected = {"spc_tt"}
        with self.assertRaisesRegex(ap.Stop, "disconnected"):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(FakePostForMe.posts, {})

    def test_tiktok_open_id_mismatch_is_not_enabled(self):
        FakePostForMe.tiktok = {"handle": "pursuitthepod", "open_id": "open_other", "scope_ok": True}
        cfg, printed = self.run_setup()
        self.assertEqual(cfg["accounts"], {})
        self.assertIn("disagree", printed)

    def test_unpinned_tiktok_config_never_posts(self):
        ap.save(ap.CONFIG_FILE, {"accounts": {"tiktok": "spc_tt"}})   # e.g. an old config without verification
        with self.assertRaisesRegex(ap.Stop, "hasn't been verified and pinned"):
            ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        self.assertEqual(FakePostForMe.posts, {})

    def test_bad_key_stops(self):
        os.environ["PURSUIT_POSTFORME_KEY"] = "wrong"
        try:
            with self.assertRaisesRegex(ap.Stop, "API key"):
                ap.connected_accounts()
        finally:
            os.environ["PURSUIT_POSTFORME_KEY"] = "test-key"

    def test_setup_refuses_ambiguous_accounts(self):
        duplicate = {"id": "spc_yt_2", "platform": "youtube", "username": "other", "status": "connected",
                     "external_id": None}
        original = ap.connected_accounts
        try:
            ap.connected_accounts = lambda: [dict(a, external_id=None) if a["platform"] == "youtube" else a
                                               for a in original()] + [duplicate]
            with self.assertRaisesRegex(ap.Stop, "Multiple connected youtube"):
                ap.setup_account_map()
        finally:
            ap.connected_accounts = original

    def test_test_post_creates_and_deletes_only_a_system_draft(self):
        ap.cmd_test_post(type("A", (), {})())
        self.assertEqual(FakePostForMe.posts, {})

    def test_live_test_requires_interactive_terminal(self):
        with patch("autopilot.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ap.Stop, "interactive"):
                ap.cmd_live_test(type("A", (), {"url": "https://youtu.be/VIDEOID1234"})())

    def test_reconcile_records_links_and_reports_failures(self):
        ap.schedule_clips(self.clips[:1], self.summary, dry_run=False)
        ledger = ap.load(ap.LEDGER_FILE, {})
        ledger["posts"][0]["scheduled_at"] = (ap.now() - dt.timedelta(hours=1)).isoformat()
        ap.save(ap.LEDGER_FILE, ledger)
        pid = ledger["posts"][0]["post_id"]
        FakePostForMe.results[pid] = [
            {"social_account_id": "spc_yt", "success": True, "platform_data": {"url": "https://youtube.com/shorts/abc"}},
            {"social_account_id": "spc_ig", "success": True, "platform_data": {"url": "https://instagram.com/reel/x"}},
            {"social_account_id": "spc_tt", "success": False, "error": {"message": "spam risk"}, "platform_data": {}},
        ]
        published, problems = ap.reconcile()
        entry = ap.load(ap.LEDGER_FILE, {})["posts"][0]
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(entry["results"]["youtube"]["url"], "https://youtube.com/shorts/abc")
        self.assertTrue(any("tiktok" in p for p in problems))

    def test_first_run_sets_baseline_then_processes_only_new(self):
        ap.save(ap.CONFIG_FILE, {"accounts": ACCOUNTS, "expected_usernames": EXPECTED, "verified_ids": PINS, "backlog": False})
        calls = []
        args = type("A", (), {"dry_run": False, "no_claude_qc": False})
        old = [{"id": "OLDEPISODE1", "title": "Old", "url": "u", "duration": 999}]
        new = [{"id": "NEWEPISODE1", "title": "New", "url": "u", "duration": 999}]
        with patch("autopilot.latest_episodes", return_value=old), patch(
                "autopilot.process_episode", side_effect=lambda ep, state, dry_run, use_claude=True, **kw: calls.append(ep["id"]) or "ok"):
            ap.cmd_run(args)
            self.assertEqual(calls, [])
            self.assertEqual(ap.load(ap.STATE_FILE, {})["last_processed_video_id"], "OLDEPISODE1")
            ap.cmd_run(args)
            self.assertEqual(calls, [])
        with patch("autopilot.latest_episodes", return_value=new), patch(
                "autopilot.process_episode", side_effect=lambda ep, state, dry_run, use_claude=True, **kw: calls.append(ep["id"]) or "ok"):
            ap.cmd_run(args)
            self.assertEqual(calls, ["NEWEPISODE1"])

    def test_first_dry_run_does_not_create_baseline_state(self):
        args = type("A", (), {"dry_run": True, "no_claude_qc": False})
        episodes = [{"id": "OLDEPISODE1", "title": "Old", "url": "u", "duration": 999}]
        with patch("autopilot.latest_episodes", return_value=episodes):
            ap.cmd_run(args)
        self.assertFalse(ap.STATE_FILE.exists())

    def test_schedule_boundaries_and_dst_are_local_and_future(self):
        original_now = ap.now
        try:
            ap.now = lambda: dt.datetime(2026, 10, 31, 15, 0, tzinfo=ap.TZ)
            slots = ap.next_slots({"posts": []}, 3)
            self.assertEqual([x.hour for x in slots], [17, 17, 17])
            self.assertEqual(slots[0].date(), dt.date(2026, 10, 31))
            self.assertNotEqual(slots[0].utcoffset(), slots[1].utcoffset())  # DST ends Nov 1
            ap.now = lambda: dt.datetime(2026, 10, 31, 17, 1, tzinfo=ap.TZ)
            self.assertEqual(ap.next_slots({"posts": []}, 1)[0].date(), dt.date(2026, 11, 1))
        finally:
            ap.now = original_now

    def test_post_confirmation_must_match_accounts_and_time(self):
        when = ap.now() + dt.timedelta(days=1)
        post = {"id": "sp_1", "status": "scheduled", "external_id": "eid",
                "scheduled_at": when.astimezone(dt.timezone.utc).isoformat(),
                "social_accounts": list(ACCOUNTS.values())}
        ap.validate_post(post, "eid", list(ACCOUNTS.values()), when, newly_created=True)
        with self.assertRaisesRegex(ap.Stop, "destination accounts"):
            ap.validate_post(dict(post, social_accounts=["spc_yt"]), "eid", list(ACCOUNTS.values()), when, True)
        with self.assertRaisesRegex(ap.Stop, "different scheduled time"):
            ap.validate_post(dict(post, scheduled_at=(when + dt.timedelta(hours=1)).isoformat()),
                             "eid", list(ACCOUNTS.values()), when, True)

    def test_episode_filter_skips_short_live_and_missing_duration(self):
        payload = {"entries": [
            {"id": "SHORTVIDEO1", "title": "short", "duration": 359},
            {"id": "PREMIERE001", "title": "premiere", "duration": 900, "live_status": "is_upcoming"},
            {"id": "NODURATION1", "title": "unknown", "duration": None},
            {"id": "EPISODE0001", "title": "episode", "duration": 360, "live_status": "not_live"},
        ]}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch("autopilot.subprocess.run", return_value=completed):
            self.assertEqual([x["id"] for x in ap.latest_episodes()], ["EPISODE0001"])

    def test_pause_blocks_runs(self):
        ap.PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ap.PAUSE_FILE.touch()
        try:
            with patch("autopilot.latest_episodes", side_effect=AssertionError("should not be called")):
                ap.cmd_run(type("A", (), {"dry_run": False, "no_claude_qc": False}))
        finally:
            ap.PAUSE_FILE.unlink()

    def test_state_save_is_private_and_valid_json(self):
        ap.save(ap.STATE_FILE, {"unicode": "Anya’s", "value": 1})
        self.assertEqual(ap.load(ap.STATE_FILE, {}), {"unicode": "Anya’s", "value": 1})
        self.assertEqual(ap.STATE_FILE.stat().st_mode & 0o777, 0o600)


class TechnicalCheckTests(unittest.TestCase):
    """Runs the real ffprobe/ffmpeg checks on a synthetic 1080x1920 clip."""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = pc.find_ffmpeg()
        cls.dir = TMP / "tech" / "01_test"
        cls.dir.mkdir(parents=True, exist_ok=True)
        cls.mp4 = cls.dir / "01_test.mp4"
        subprocess.run([cls.ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=15",
                        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=15",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.mp4)], check=True)
        (cls.dir / "copy.txt").write_text("x")
        cls.words = [{"w": f"w{i}", "s": 10 + i * 0.4, "e": 10 + i * 0.4 + 0.3} for i in range(40)]

    def clip(self, start=9.9, end=24.75):
        return {"file": "01_test/01_test.mp4", "folder": "01_test", "duration_sec": 15.0,
                "start_sec": start, "end_sec": end}

    def test_good_clip_passes(self):
        self.assertEqual(ap.technical_checks(self.ffmpeg, self.ffprobe, self.clip(), TMP / "tech", self.words, True), [])

    def test_mid_word_cut_is_caught(self):
        probs = ap.technical_checks(self.ffmpeg, self.ffprobe, self.clip(start=10.15), TMP / "tech", self.words, True)
        self.assertTrue(any("mid-word" in p for p in probs), probs)

    def test_corrupt_file_is_caught(self):
        bad_dir = TMP / "tech" / "02_bad"
        bad_dir.mkdir(exist_ok=True)
        data = self.mp4.read_bytes()
        (bad_dir / "02_bad.mp4").write_bytes(data[: len(data) // 2])
        clip = dict(self.clip(), file="02_bad/02_bad.mp4", folder="02_bad")
        self.assertTrue(ap.technical_checks(self.ffmpeg, self.ffprobe, clip, TMP / "tech", self.words, True))

    def test_whisper_loop_is_caught_but_normal_repetition_is_not(self):
        loop = "and so that is what i said".split() * 5 + ["w"] * 5
        words = [{"w": t, "s": 10 + i * 0.4, "e": 10 + i * 0.4 + 0.3} for i, t in enumerate(loop)]
        probs = ap.technical_checks(self.ffmpeg, self.ffprobe, self.clip(), TMP / "tech", words, True)
        self.assertTrue(any("loop" in p for p in probs), probs)
        rhetorical = ("constantly constantly constantly going and going and going every single day every single day "
                      "you are able to do this because you care").split()
        words = [{"w": t, "s": 10 + i * 0.4, "e": 10 + i * 0.4 + 0.3} for i, t in enumerate(rhetorical)]
        probs = ap.technical_checks(self.ffmpeg, self.ffprobe, dict(self.clip(), end_sec=21.95), TMP / "tech", words, True)
        self.assertFalse(any("loop" in p for p in probs), probs)

    def test_low_score_is_held_back(self):
        probs = ap.score_checks({}, {"overall": 65, "scores": {"standalone": 9}, "youtube_title": "t", "caption": "c"})
        self.assertTrue(any("score" in p for p in probs))


if __name__ == "__main__":
    unittest.main()
