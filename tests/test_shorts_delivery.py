"""Manual YouTube Shorts -> Google Drive delivery. Uses an in-memory fake Drive; never touches real accounts."""
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_BOOT = Path(tempfile.mkdtemp(prefix="pursuit_shorts_boot_"))
for k, v in {"PURSUIT_STATE_DIR": _BOOT / "state", "PURSUIT_OUT": _BOOT / "out", "PURSUIT_LOG": _BOOT / "log.txt",
             "PURSUIT_POSTFORME_KEY": "test-key"}.items():
    os.environ.setdefault(k, str(v))          # only if the autopilot tests haven't already set up a sandbox

import autopilot as ap  # noqa: E402
import pursuit_clips as pc  # noqa: E402
import shorts_delivery as sd  # noqa: E402

EP = "EPISODE0001"


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class FakeDrive:
    """Just enough of Drive v3 (files.get/list/create/update + resumable upload)."""

    def __init__(self):
        self.files, self.pending, self.calls, self.n = {}, {}, [], 0
        self.lose_response_after_upload = False

    def _new_id(self):
        self.n += 1
        return f"f{self.n}"

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url))
        params = params or {}
        if method == "GET" and url.startswith(sd.DRIVE + "/files/"):
            f = self.files.get(url.rsplit("/", 1)[1])
            return FakeResp(200, dict(f)) if f else FakeResp(404)
        if method == "GET" and url == sd.DRIVE + "/files":
            q = params["q"]
            key, value = re.search(r"key='(.*?)' and value='(.*?)'", q).groups()
            parent = re.search(r"'([^']+)' in parents", q)
            hits = [dict(f) for f in self.files.values() if (f.get("appProperties") or {}).get(key) == value
                    and not f.get("trashed") and (not parent or parent.group(1) in f.get("parents", []))]
            return FakeResp(200, {"files": hits})
        if method == "POST" and url == sd.DRIVE + "/files":
            fid = self._new_id()
            self.files[fid] = dict(json, id=fid, parents=json.get("parents", ["root"]), trashed=False,
                                   createdTime=ap.now().isoformat())
            return FakeResp(200, dict(self.files[fid]))
        if method == "POST" and url == sd.UPLOAD + "/files":
            loc = f"https://fake-upload/{len(self.pending)}"
            self.pending[loc] = json
            return FakeResp(200, {}, {"Location": loc})
        if method == "PUT" and url in self.pending:
            body = data.read()
            fid = self._new_id()
            self.files[fid] = dict(self.pending.pop(url), id=fid, size=str(len(body)), trashed=False,
                                   md5Checksum=hashlib.md5(body).hexdigest(), createdTime=ap.now().isoformat())
            if self.lose_response_after_upload:
                self.lose_response_after_upload = False
                raise ConnectionError("connection reset after the upload finished")
            return FakeResp(200, dict(self.files[fid]))
        if method == "PATCH" and url.startswith(sd.DRIVE + "/files/"):
            f = self.files.get(url.rsplit("/", 1)[1])
            if not f:
                return FakeResp(404)
            f.update(json)
            return FakeResp(200, dict(f))
        raise AssertionError(f"unexpected Drive call {method} {url}")

    def live(self, suffix=".mp4"):
        return [f for f in self.files.values() if not f.get("trashed") and f["name"].endswith(suffix)]


def no_postforme(*a, **k):
    raise AssertionError("Shorts delivery must never call Post for Me")


class ShortsDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ffmpeg, _ = pc.find_ffmpeg()
        cls.src_mp4 = Path(tempfile.mkdtemp(prefix="pursuit_shorts_media_")) / "clip.mp4"
        subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.src_mp4)],
                       check=True)

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="pursuit_shorts_"))
        self.root = root
        state_dir, out = root / "state", root / "out"
        state_dir.mkdir()
        self.patches = [
            patch.object(ap, "OUT_DIR", out), patch.object(ap, "STATE_DIR", state_dir),
            patch.object(ap, "STATE_FILE", state_dir / "state.json"), patch.object(ap, "QUEUE_FILE", state_dir / "queue.json"),
            patch.object(ap, "LEDGER_FILE", state_dir / "ledger.json"), patch.object(ap, "CONFIG_FILE", state_dir / "config.json"),
            patch.object(ap, "LOCK_FILE", state_dir / "run.lock"),
            patch.object(ap, "STATUS_FILE", out / "AUTOPILOT_STATUS.txt"), patch.object(ap, "PAUSE_FILE", state_dir / "PAUSED"),
            patch.object(sd, "DRIVE_DIR", state_dir / "drive"), patch.object(sd, "DRIVE_STATE", state_dir / "drive" / "drive_state.json"),
            patch.object(sd, "TOKEN_FILE", state_dir / "drive" / "google_token.json"),
            patch.object(sd, "CLIENT_FILE", state_dir / "drive" / "google_client.json"),
            patch.object(sd, "STAGING_DIR", state_dir / "drive" / "staging"),
            patch.object(ap, "notify", lambda *a, **k: None),
            patch.object(ap, "api", no_postforme), patch.object(ap, "upload_media", no_postforme),
            patch.object(ap, "latest_episodes", lambda limit=5: [
                {"id": EP, "title": "Give Me 10 Minutes", "url": f"https://www.youtube.com/watch?v={EP}", "duration": 600},
                {"id": "OLDEPISODE1", "title": "Old", "url": "https://www.youtube.com/watch?v=OLDEPISODE1", "duration": 900}]),
        ]
        for p in self.patches:
            p.start()
        self.drive = FakeDrive()
        self.make_production_episode()
        # TikTok side: a ledger, queue and config that must not change
        ap.save(ap.LEDGER_FILE, {"posts": [{"external_id": "pursuit-x", "status": "scheduled", "clip": "01_a",
                                            "scheduled_at": ap.now().isoformat()}]})
        ap.save(ap.CONFIG_FILE, {"accounts": {"tiktok": "spc_tt"}, "auto_posting": True})
        ap.save(ap.QUEUE_FILE, {"clips": [{"external_id": "pursuit-q", "status": "queued", "episode": EP,
                                           "ep_dir": str(self.ep_dir), "score": 85.0, "category": "",
                                           "episode_title": "Give Me 10 Minutes", "meta": {"id": EP},
                                           "clip": {"file": "01_a/01_a.mp4", "folder": "01_a"}}]})
        self.snapshot = self.tiktok_files()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- fixtures ---------------------------------------------------------------------------------
    def make_production_episode(self, passes=("01_a", "02_b", "04_d"), layouts=None):
        layouts = layouts or {"04_d": "audio"}
        self.ep_dir = ap.OUT_DIR / "2026-09-25 Give Me 10 Minutes"
        (self.ep_dir / ".work").mkdir(parents=True, exist_ok=True)
        ap.save(self.ep_dir / ".work" / "meta.json", {"id": EP, "title": "Give Me 10 Minutes", "upload_date": "20260925",
                                                      "url": f"https://www.youtube.com/watch?v={EP}"})
        rows, analysis = [], []
        for i, (folder, start, score) in enumerate([("01_a", 10, 85), ("02_b", 100, 78), ("03_c", 200, 90),
                                                    ("04_d", 300, 88)]):
            (self.ep_dir / folder).mkdir(exist_ok=True)
            shutil.copy(self.src_mp4, self.ep_dir / folder / f"{folder}.mp4")
            title = f"Clip {folder}"
            rows.append({"rank": i + 1, "folder": folder, "file": f"{folder}/{folder}.mp4", "score": score,
                         "start_sec": start, "end_sec": start + 30, "clip_title": title,
                         "layout": layouts.get(folder, "video")})
            analysis.append({"clip_title": title, "youtube_title": f"YT title {folder}", "overall": score,
                             "caption": f"A real insight from {folder}. Full episode of PURSUIT on YouTube.",
                             "hashtags": ["discipline", "#running"]})
        with open(self.ep_dir / "clips.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        ap.save(self.ep_dir / ".work" / "analysis.json", {"clips": analysis})
        report = [f"{'PASS' if r['folder'] in passes else 'SKIP'} {r['folder']} (score {r['score']}): ok" for r in rows]
        ap.save(ap.STATE_FILE, {"episodes": {EP: {"status": "queued", "report": report}},
                                "last_processed_video_id": EP})

    def tiktok_files(self):
        return {p: p.read_bytes() for p in (ap.LEDGER_FILE, ap.QUEUE_FILE, ap.CONFIG_FILE, ap.STATE_FILE)}

    def verified(self):
        sd.save_state(dict(sd.load_state(), test_verified=ap.now().isoformat()))

    def assert_tiktok_untouched(self):
        self.assertEqual(self.tiktok_files(), self.snapshot, "TikTok ledger/queue/config/state changed")

    # ---- selection & QC ---------------------------------------------------------------------------
    def test_latest_episode_is_the_newest_real_episode(self):
        self.assertEqual(sd._latest()["id"], EP)

    def test_only_qc_passed_video_clips_are_used_and_fewer_than_three_is_fine(self):
        meta, ep_dir, chosen, rendered = sd.gather_shorts(sd._latest(), 3)
        # 03_c scored highest but FAILED QC; 04_d passed but is an audio-only card -> neither is used
        self.assertEqual([c["folder"] for c in chosen], ["01_a", "02_b"])
        self.assertIsNone(rendered)                                    # reused production work, no re-render
        self.assert_tiktok_untouched()

    def test_best_first_and_never_overlapping(self):
        cands = [{"folder": "x", "score": 90, "start_sec": 0, "end_sec": 30},
                 {"folder": "y", "score": 80, "start_sec": 20, "end_sec": 50},    # overlaps x
                 {"folder": "z", "score": 70, "start_sec": 60, "end_sec": 90}]
        self.assertEqual([c["folder"] for c in sd.select_best(cands, 3)], ["x", "z"])

    def test_missing_mp4_rerenders_through_the_normal_pipeline_with_the_shared_lock(self):
        (self.ep_dir / "01_a" / "01_a.mp4").unlink()                  # cleaned up after TikTok posted it
        passed = [{"folder": "01_a", "file": "01_a/01_a.mp4", "score": 85, "start_sec": 10, "end_sec": 40,
                   "layout": "video", "clip_title": "Clip 01_a", "analysis": {"youtube_title": "t", "caption": "c"}}]
        summary = {"ep_dir": str(self.ep_dir), "meta": {"id": EP, "title": "Give Me 10 Minutes"}, "clips": passed}

        def rerender(ep, rec, use_claude=True):
            shutil.copy(self.src_mp4, self.ep_dir / "01_a" / "01_a.mp4")
            return summary, passed, ["PASS 01_a"]
        with patch.object(ap, "render_and_check", side_effect=rerender) as r:
            meta, ep_dir, chosen, rendered = sd.gather_shorts(sd._latest(), 3)
        self.assertTrue(r.called)
        self.assertEqual([c["folder"] for c in chosen], ["01_a"])
        self.assert_tiktok_untouched()

    def test_auto_rerender_does_not_try_to_reacquire_the_process_lock(self):
        (self.ep_dir / "01_a" / "01_a.mp4").unlink()
        self.verified()
        sd.save_state(dict(sd.load_state(), auto=True, auto_baseline="OLDEPISODE1"))
        passed = [{"folder": "01_a", "file": "01_a/01_a.mp4", "score": 85, "start_sec": 10,
                   "end_sec": 40, "layout": "video", "clip_title": "Clip 01_a",
                   "analysis": {"youtube_title": "t", "caption": "c"}}]
        summary = {"ep_dir": str(self.ep_dir), "meta": {"id": EP, "title": "Give Me 10 Minutes"},
                   "clips": passed}

        def rerender(ep, rec, use_claude=True):
            shutil.copy(self.src_mp4, self.ep_dir / "01_a" / "01_a.mp4")
            return summary, passed, ["PASS 01_a"]

        with patch.object(ap, "render_and_check", side_effect=rerender), \
                patch.object(sd, "_with_lock", side_effect=AssertionError("would self-deadlock")), \
                patch.object(sd, "session", lambda: self.drive):
            self.assertIn("Delivered 1", sd.auto_step())

    def test_episode_with_no_passing_clip_is_recorded_once_and_not_retried(self):
        ap.save(ap.STATE_FILE, {"episodes": {EP: {"status": "rejected", "report": ["SKIP 01_a (score 50): low"]}}})
        self.snapshot = self.tiktok_files()
        with patch.object(ap, "render_and_check", side_effect=ap.Stop("No clip passed the checks for 'x'.")) as r:
            msg, _ = sd.export(sd._latest(), 3, deliver_now=False)
            msg2, _ = sd.export(sd._latest(), 3, deliver_now=False)
        self.assertEqual(r.call_count, 1)
        self.assertIn("nothing to deliver", msg2)

    # ---- package ----------------------------------------------------------------------------------
    def test_package_has_valid_mp4s_and_phone_friendly_posting_info(self):
        msg, rec = sd.export(sd._latest(), 3, deliver_now=False)
        names = [it["name"] for it in rec["items"]]
        self.assertEqual(names, [f"2026-09-25 01_{pc.slugify('Clip 01_a', 50)}.mp4",
                                 f"2026-09-25 02_{pc.slugify('Clip 02_b', 50)}.mp4", "2026-09-25 POSTING_INFO.txt"])
        stage = sd.STAGING_DIR / EP
        for n in names[:2]:
            self.assertEqual(sd.verify_mp4(stage / n), [])
        info = (stage / names[2]).read_text()
        self.assertIn("TITLE (copy this):\nYT title 01_a", info)
        self.assertIn(f"https://www.youtube.com/watch?v={EP}", info)
        self.assertIn("https://www.youtube.com/@AnyaPostnikov", info)
        self.assertIn("PURSUIT", info)
        self.assertNotRegex(info, r"(?i)on youtube")            # the viewer is already on YouTube
        self.assertIn("0:10 – 0:40", info)
        self.assertEqual(self.drive.calls, [])                    # nothing uploaded without --deliver

    def test_corrupt_mp4_is_never_uploaded(self):
        (self.ep_dir / "01_a" / "01_a.mp4").write_bytes(b"not a video" * 1000)
        self.verified()
        with self.assertRaisesRegex(ap.Stop, "final check"):
            sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        self.assertEqual(self.drive.live(), [])

    # ---- upload -----------------------------------------------------------------------------------
    def test_deliver_requires_the_confirmed_test_first(self):
        with self.assertRaisesRegex(ap.Stop, "drive-test"):
            sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        self.assertEqual(self.drive.calls, [])

    def test_upload_tracks_file_ids_and_verifies_checksums(self):
        self.verified()
        msg, rec = sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        state = sd.load_state()
        files = state["deliveries"][EP]["files"]
        self.assertEqual(state["deliveries"][EP]["status"], "delivered")
        self.assertEqual(len(self.drive.live(".mp4")), 2)
        for item, f in files.items():
            remote = self.drive.files[f["file_id"]]
            self.assertTrue(f["verified"])
            self.assertEqual(remote["parents"], [state["folder_id"]])
            self.assertEqual(remote["appProperties"]["pursuit_tool"], sd.TOOL_TAG)
        self.assertFalse((sd.STAGING_DIR / EP).exists())            # local copies cleaned after verified upload
        self.assertTrue((self.ep_dir / "01_a" / "01_a.mp4").exists())  # production files untouched
        self.assert_tiktok_untouched()

    def test_only_google_drive_is_contacted_never_youtube_or_post_for_me(self):
        self.verified()
        sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        for method, url in self.drive.calls:
            self.assertTrue(url.startswith((sd.DRIVE, sd.UPLOAD, "https://fake-upload/")), url)
            self.assertNotIn("youtube", url)

    def test_lost_response_retry_adopts_the_upload_instead_of_duplicating(self):
        self.verified()
        self.drive.lose_response_after_upload = True
        with self.assertRaises(ap.Ambiguous):
            sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        msg, rec = sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))   # next run
        self.assertEqual(len(self.drive.live(".mp4")), 2)
        self.assertEqual(len(self.drive.live(".txt")), 1)
        self.assertEqual(sd.load_state()["deliveries"][EP]["status"], "delivered")

    def test_same_episode_is_never_delivered_twice(self):
        self.verified()
        sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        before = len(self.drive.files)
        msg, _ = sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        self.assertIn("Already delivered", msg)
        self.assertEqual(len(self.drive.files), before)

    def test_restart_resumes_the_exact_staged_package_without_reprocessing(self):
        sd.export(sd._latest(), 3, deliver_now=False)                   # packaged, then the Mac restarted
        self.verified()
        with patch.object(sd, "gather_shorts", side_effect=AssertionError("must not re-select or re-render")):
            msg, rec = sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        self.assertIn("Delivered 2", msg)

    def test_two_tagged_folders_or_a_missing_folder_fail_closed(self):
        d = sd.Drive(self.drive)
        for _ in range(2):
            self.drive.request("POST", sd.DRIVE + "/files", json={"name": sd.FOLDER_NAME,
                                                                  "appProperties": {"pursuit_tool": sd.TOOL_TAG + "-root"}})
        with self.assertRaisesRegex(ap.Stop, "not guessing"):
            sd.ensure_folder(d, sd.load_state())
        state = sd.load_state()
        state["folder_id"] = "f999"                                     # recorded folder no longer exists
        with self.assertRaisesRegex(ap.Stop, "missing"):
            sd.ensure_folder(d, state)

    # ---- retention --------------------------------------------------------------------------------
    def deliver_and_age(self, days):
        self.verified()
        sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        state = sd.load_state()
        state["deliveries"][EP]["delivered_at"] = (ap.now() - dt.timedelta(days=days)).isoformat()
        sd.save_state(state)
        return state

    def test_fourteen_day_retention(self):
        state = self.deliver_and_age(13)
        self.assertEqual(sd.cleanup(sd.Drive(self.drive), state), ([], []))
        self.assertEqual(len(self.drive.live(".mp4")), 2)
        state = self.deliver_and_age(15)
        removed, kept = sd.cleanup(sd.Drive(self.drive), state)
        self.assertEqual(len(removed), 3)                               # 2 Shorts + POSTING_INFO
        self.assertEqual(self.drive.live(".mp4") + self.drive.live(".txt"), [])
        self.assertEqual(sd.load_state()["deliveries"][EP]["status"], "expired")

    def test_fourteen_day_cleanup_reconciles_an_abandoned_partial_upload(self):
        self.verified()
        self.drive.lose_response_after_upload = True
        with self.assertRaises(ap.Ambiguous):
            sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        state = sd.load_state()
        rec = state["deliveries"][EP]
        rec["created"] = (ap.now() - dt.timedelta(days=15)).isoformat()
        sd.save_state(state)

        removed, kept = sd.cleanup(sd.Drive(self.drive), state)
        self.assertEqual(len(removed), 1)
        self.assertEqual(kept, [])
        self.assertEqual(self.drive.live(".mp4"), [])
        self.assertEqual(sd.load_state()["deliveries"][EP]["status"], "expired")

    def test_cleanup_never_touches_untracked_or_unprovable_files(self):
        state = self.deliver_and_age(15)
        folder = state["folder_id"]
        # a file in the same folder the tool didn't create, even with a look-alike name
        stranger = self.drive.request("POST", sd.DRIVE + "/files", json={"name": "2026-09-25 01_clip-01_a.mp4",
                                                                         "parents": [folder]}).json()
        # a tracked file that was moved out of the folder, and one whose tag no longer matches
        files = state["deliveries"][EP]["files"]
        self.drive.files[files["01"]["file_id"]]["parents"] = ["someone-elses-folder"]
        self.drive.files[files["02"]["file_id"]]["appProperties"] = {"pursuit_tool": "other"}
        removed, kept = sd.cleanup(sd.Drive(self.drive), state)
        self.assertEqual(sorted(kept), sorted([files["01"]["name"], files["02"]["name"]]))
        self.assertFalse(self.drive.files[stranger["id"]]["trashed"])
        self.assertFalse(self.drive.files[files["01"]["file_id"]]["trashed"])
        self.assertFalse(self.drive.files[files["02"]["file_id"]]["trashed"])
        st = sd.load_state()["deliveries"][EP]["files"]
        self.assertEqual(st["01"]["status"], "provenance_uncertain")
        self.assertIn("couldn't prove", "\n".join(sd.drive_status_lines()))

    def test_cleanup_requires_the_recorded_root_folder_to_still_be_ours(self):
        state = self.deliver_and_age(15)
        self.drive.files[state["folder_id"]]["appProperties"] = {"pursuit_tool": "other"}
        removed, kept = sd.cleanup(sd.Drive(self.drive), state)
        self.assertEqual(removed, [])
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(self.drive.live(".mp4")), 2)

    def test_cleanup_requires_drive_to_confirm_trash(self):
        state = self.deliver_and_age(15)
        real_request = self.drive.request

        def ignore_trash(method, url, **kw):
            if method == "PATCH":
                fid = url.rsplit("/", 1)[1]
                return FakeResp(200, dict(self.drive.files[fid], trashed=False))
            return real_request(method, url, **kw)

        self.drive.request = ignore_trash
        with self.assertRaisesRegex(ap.Stop, "did not confirm"):
            sd.cleanup(sd.Drive(self.drive), state)
        self.assertEqual(len(self.drive.live(".mp4")), 2)

    def test_local_cleanup_only_removes_old_staging_never_production(self):
        sd.export(sd._latest(), 3, deliver_now=False)
        stale = sd.STAGING_DIR / "SOMEOLDDELIVERY"
        stale.mkdir(parents=True)
        old = (ap.now() - dt.timedelta(days=20)).timestamp()
        os.utime(stale, (old, old))
        pruned = sd.prune_local_staging(sd.load_state())
        self.assertEqual(pruned, 1)
        self.assertTrue((sd.STAGING_DIR / EP).exists())                 # recent, undelivered: kept for retry
        self.assertTrue((self.ep_dir / "02_b" / "02_b.mp4").exists())

    # ---- state safety -----------------------------------------------------------------------------
    def test_corrupted_drive_state_stops_everything(self):
        sd.DRIVE_DIR.mkdir(parents=True, exist_ok=True)
        sd.DRIVE_STATE.write_text("{not json")
        with self.assertRaisesRegex(ap.Stop, "corrupted"):
            sd.export(sd._latest(), 3, deliver_now=True, drive=sd.Drive(self.drive))
        self.assertIn("STOPPED", sd.auto_step())
        self.assertEqual(self.drive.calls, [])

    def test_drive_state_is_private(self):
        sd.export(sd._latest(), 3, deliver_now=False)
        self.assertEqual(os.stat(sd.DRIVE_STATE).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(sd.DRIVE_DIR).st_mode & 0o777, 0o700)

    def test_secrets_are_written_private(self):
        sd._write_secret(sd.TOKEN_FILE, '{"refresh_token": "x"}')
        self.assertEqual(os.stat(sd.TOKEN_FILE).st_mode & 0o777, 0o600)

    # ---- controlled test & automation --------------------------------------------------------------
    def test_drive_test_needs_the_exact_phrase_then_verifies(self):
        self.assertFalse(sd.cmd_drive_test(None, drive=sd.Drive(self.drive), ask=lambda _: "yes"))
        self.assertEqual(self.drive.live(".mp4"), [])
        self.assertFalse(sd.load_state().get("test_verified"))
        self.assertTrue(sd.cmd_drive_test(None, drive=sd.Drive(self.drive), ask=lambda _: sd.TEST_PHRASE))
        self.assertEqual(len(self.drive.live(".mp4")), 1)
        self.assertTrue(self.drive.live(".mp4")[0]["name"].startswith("TEST "))
        self.assertTrue(sd.load_state()["test_verified"])
        self.assert_tiktok_untouched()

    def test_auto_delivery_is_off_until_enabled_after_a_verified_test(self):
        self.assertIsNone(sd.auto_step())
        with self.assertRaisesRegex(ap.Stop, "drive-test"):
            sd.cmd_drive_auto(type("A", (), {"state": "on"}))

    def test_auto_delivers_each_new_episode_once(self):
        self.verified()
        sd.save_state(dict(sd.load_state(), auto=True, auto_baseline="OLDEPISODE1"))
        with patch.object(sd, "session", lambda: self.drive):
            msg = sd.auto_step()
            self.assertIn("Delivered 2", msg)
            self.assertIsNone(sd.auto_step())                           # same episode: never a second batch
        self.assertEqual(len(self.drive.live(".mp4")), 2)
        self.assert_tiktok_untouched()

    def test_auto_skips_the_episode_that_was_current_when_enabled(self):
        self.verified()
        sd.save_state(dict(sd.load_state(), auto=True, auto_baseline=EP))
        with patch.object(sd, "session", lambda: self.drive):
            self.assertIsNone(sd.auto_step())
        self.assertEqual(self.drive.live(".mp4"), [])

    # ---- automatic delivery for every new episode ---------------------------------------------------
    EP2 = "EPISODE0002"

    def auto_env(self, episodes=None):
        self.verified()
        sd.save_state(dict(sd.load_state(), auto=True, auto_baseline="OLDEPISODE1"))
        eps = episodes or [self.EP2, EP, "OLDEPISODE1"]
        listing = [{"id": e, "title": f"Title {e}", "url": f"https://www.youtube.com/watch?v={e}", "duration": 900}
                   for e in eps]
        return [patch.object(ap, "latest_episodes", lambda limit=5: listing),
                patch.object(sd, "session", lambda: self.drive)]

    def add_second_episode(self, status="queued", processed=True):
        ep2 = ap.OUT_DIR / "2026-10-02 Second Episode"
        shutil.copytree(self.ep_dir, ep2)
        ap.save(ep2 / ".work" / "meta.json", {"id": self.EP2, "title": "Second Episode", "upload_date": "20261002",
                                              "url": f"https://www.youtube.com/watch?v={self.EP2}"})
        state = ap.load(ap.STATE_FILE, {})
        if processed:
            rec = dict(state["episodes"][EP], status=status)
            if status == "rejected":
                rec["report"] = ["SKIP 01_a (score 50): score 50 < 70"]
            state["episodes"][self.EP2] = rec
        ap.save(ap.STATE_FILE, state)
        self.snapshot = self.tiktok_files()

    def run_auto(self, patches):
        for p in patches:
            p.start()
        try:
            return sd.auto_step()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_auto_catches_up_on_several_new_episodes_oldest_first_once_each(self):
        self.add_second_episode()                          # two uploads while the Mac was asleep
        patches = self.auto_env()
        msg = self.run_auto(patches)
        st = sd.load_state()["deliveries"]
        self.assertEqual((st[EP]["status"], st[self.EP2]["status"]), ("delivered", "delivered"))
        self.assertLessEqual(st[EP]["delivered_at"], st[self.EP2]["delivered_at"])
        self.assertEqual(len(self.drive.live(".mp4")), 4)   # 2 Shorts per episode (QC let 2 through each)
        self.assertEqual(len(self.drive.live(".txt")), 2)
        self.assertIsNone(self.run_auto(self.auto_env()))  # next run: nothing new, nothing duplicated
        self.assertEqual(len(self.drive.live(".mp4")), 4)
        self.assert_tiktok_untouched()

    def test_auto_waits_for_the_autopilot_to_process_a_new_episode_instead_of_rendering(self):
        self.add_second_episode(processed=False)
        with patch.object(ap, "render_and_check", side_effect=AssertionError("Drive delivery must not render")):
            self.run_auto(self.auto_env())
        st = sd.load_state()["deliveries"]
        self.assertNotIn(self.EP2, st)                      # waiting, not failed or skipped forever
        state = ap.load(ap.STATE_FILE, {})                 # the autopilot finishes processing it
        state["episodes"][self.EP2] = dict(state["episodes"][EP])
        ap.save(ap.STATE_FILE, state)
        self.run_auto(self.auto_env())
        self.assertEqual(sd.load_state()["deliveries"][self.EP2]["status"], "delivered")

    def test_auto_records_no_shorts_for_a_rejected_episode_without_rendering(self):
        self.add_second_episode(status="rejected")
        with patch.object(ap, "render_and_check", side_effect=AssertionError("must not render")):
            self.run_auto(self.auto_env())
        self.assertEqual(sd.load_state()["deliveries"][self.EP2]["status"], "no_shorts")
        self.assertEqual(len(self.drive.live(".mp4")), 2)   # only EP's Shorts

    def test_auth_failure_keeps_the_packaged_shorts_and_delivers_them_later(self):
        patches = self.auto_env(episodes=[EP, "OLDEPISODE1"])
        patches[1] = patch.object(sd, "session", side_effect=sd.DriveError("sign-in expired"))
        with self.assertRaises(sd.DriveError):
            self.run_auto(patches)
        rec = sd.load_state()["deliveries"][EP]
        self.assertEqual(rec["status"], "packaged")         # secured locally before touching Drive
        self.assertEqual(self.drive.calls, [])
        for folder in ("01_a", "02_b"):                    # TikTok cleanup later deletes the production MP4s
            (self.ep_dir / folder / f"{folder}.mp4").unlink()
        with patch.object(sd, "gather_shorts", side_effect=AssertionError("must reuse the staged package")):
            msg = self.run_auto(self.auto_env(episodes=[EP, "OLDEPISODE1"]))
        self.assertIn("Delivered 2", msg)
        self.assertEqual(len(self.drive.live(".mp4")), 2)

    def test_test_delivery_files_do_not_count_as_an_episode_delivery(self):
        self.assertTrue(sd.cmd_drive_test(None, drive=sd.Drive(self.drive), ask=lambda _: sd.TEST_PHRASE))
        msg = self.run_auto(self.auto_env(episodes=[EP, "OLDEPISODE1"]))
        self.assertIn("Delivered 2", msg)
        names = sorted(f["name"] for f in self.drive.live(".mp4"))
        self.assertEqual(len(names), 3)                     # 1 TEST copy + the real batch of 2
        self.assertEqual(sum(n.startswith("TEST ") for n in names), 1)

    def test_there_is_no_youtube_publishing_code_or_permission(self):
        source = (Path(sd.__file__)).read_text()
        for forbidden in ("youtube/v3", "youtube.upload", "videos.insert", "googleapis.com/youtube"):
            self.assertNotIn(forbidden, source)
        self.assertEqual(sd.SCOPES, ["https://www.googleapis.com/auth/drive.file"])

    def test_scheduled_run_calls_delivery_last_and_isolated(self):
        with patch.object(ap, "shorts_auto_step", side_effect=ap.Stop("drive down")) as step:
            args = type("A", (), {"dry_run": False, "no_claude_qc": False})
            with patch.object(ap, "reconcile", lambda: ([], [])), patch.object(ap, "fill_schedule", lambda: 0), \
                    patch.object(ap, "check_new_episode", lambda a, s: None), \
                    patch.object(ap, "backlog_step", lambda *a, **k: ([], None)):
                ap.cmd_run(args)
        self.assertTrue(step.called)
        self.assertIn("shorts delivery: STOPPED", ap.STATUS_FILE.read_text())


if __name__ == "__main__":
    unittest.main()
