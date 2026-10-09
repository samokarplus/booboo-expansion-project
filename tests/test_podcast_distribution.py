import json
import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

TEST_DIR = Path(tempfile.mkdtemp(prefix="pursuit_podcast_test_"))
os.environ.update(PURSUIT_STATE_DIR=str(TEST_DIR / "state"), PURSUIT_OUT=str(TEST_DIR / "out"),
                  PURSUIT_LOG=str(TEST_DIR / "log.txt"), PURSUIT_POSTFORME_KEY="test-key",
                  PURSUIT_PODCAST_MEDIA_BASE_URL="https://media.example/podcast/media",
                  PURSUIT_PODCAST_FEED_URL="https://media.example/podcast/feed.xml")

import autopilot as ap
import podcast_distribution as pd
import podcast_storage as ps

ORIGINAL_PATHS = {name: getattr(ap, name) for name in (
    "STATE_DIR", "OUT_DIR", "LOG_FILE", "STATUS_FILE", "STATE_FILE", "LEDGER_FILE",
    "CONFIG_FILE", "QUEUE_FILE", "LOCK_FILE", "PAUSE_FILE") if hasattr(ap, name)}


def configure_paths():
    ap.STATE_DIR = TEST_DIR / "state"
    ap.OUT_DIR = TEST_DIR / "out"
    ap.LOG_FILE = TEST_DIR / "log.txt"
    ap.STATUS_FILE = ap.OUT_DIR / "AUTOPILOT_STATUS.txt"
    for name, filename in (("STATE_FILE", "state.json"), ("LEDGER_FILE", "ledger.json"),
                           ("CONFIG_FILE", "config.json"), ("QUEUE_FILE", "queue.json"),
                           ("LOCK_FILE", "run.lock"), ("PAUSE_FILE", "PAUSED")):
        setattr(ap, name, ap.STATE_DIR / filename)


def ffmpeg():
    exe = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(exe).exists():
        raise unittest.SkipTest("ffmpeg is not installed")
    return exe


def make_wav(path):
    subprocess.run([ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ac", "1", str(path)], check=True, capture_output=True)


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail_key = None

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise MissingObject()
        return {"Body": io.BytesIO(self.objects[Key])}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.uploads.append(key)
        if key == self.fail_key:
            raise RuntimeError("upload failed")
        self.objects[key] = Path(filename).read_bytes()


class PodcastDistributionTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name, value in ORIGINAL_PATHS.items():
            setattr(ap, name, value)

    def setUp(self):
        artwork = patch.object(ps, "check_artwork")
        artwork.start()
        self.addCleanup(artwork.stop)
        configure_paths()
        shutil.rmtree(TEST_DIR / "state", ignore_errors=True)
        shutil.rmtree(TEST_DIR / "public", ignore_errors=True)
        ap.STATE_DIR.mkdir(parents=True, exist_ok=True)
        ap.save(ap.CONFIG_FILE, {"podcast": {"mode": "self_hosted", "enabled": True,
                                             "public_dir": str(TEST_DIR / "public"),
                                             "media_base_url": "https://media.example/podcast/media",
                                             "feed_url": "https://media.example/podcast/feed.xml",
                                             "owner_email": "owner@example.com",
                                             "artwork_url": "https://media.example/artwork.jpg"}})

    def test_audio_conversion_outputs_mp3(self):
        src = TEST_DIR / "tone.wav"
        dst = TEST_DIR / "tone.mp3"
        make_wav(src)
        pd.convert_mp3(src, dst, bitrate="128k")
        self.assertGreater(dst.stat().st_size, 1024)
        probe = subprocess.run([shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe", "-v", "error",
                                "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                                "-of", "default=nw=1:nk=1", str(dst)], capture_output=True, text=True, check=True)
        self.assertIn("mp3", probe.stdout)

    def test_self_hosted_rss_has_enclosure_and_owner_email(self):
        mp3 = TEST_DIR / "episode.mp3"
        make_wav(TEST_DIR / "episode.wav")
        pd.convert_mp3(TEST_DIR / "episode.wav", mp3)
        ep = {"id": "abc123", "title": "A & B", "url": "https://www.youtube.com/watch?v=abc123",
              "description": "Episode description", "upload_date": "20261008"}
        result = pd.publish_self_hosted(ep, mp3)
        self.assertEqual(result["provider"], "self_hosted")
        tree = ET.parse(TEST_DIR / "public" / "feed.xml")
        root = tree.getroot()
        self.assertEqual(root.tag, "rss")
        enclosure = root.find("./channel/item/enclosure")
        self.assertEqual(enclosure.attrib["type"], "audio/mpeg")
        self.assertIn("episode.mp3", enclosure.attrib["url"])
        self.assertIn("owner@example.com", (TEST_DIR / "public" / "feed.xml").read_text())

    def test_duplicate_prevention_uses_podcast_ledger(self):
        ap.save(pd.ledger_file(), {"episodes": [{"video_id": "abc123", "title": "Episode"}]})
        state = pd.load_state()
        msg = pd.process_episode({"id": "abc123", "title": "Episode", "url": "https://www.youtube.com/watch?v=abc123"}, state)
        self.assertIn("already published", msg)

    def test_status_distinguishes_feed_records_from_verified_spotify_videos(self):
        ap.save(pd.ledger_file(), {"episodes": [{"video_id": "feed-only"}]})
        ap.save(ap.STATE_DIR / "spotify-dashboard-ledger.json", {"episodes": [
            {"status": "published", "format": "video"}, {"status": "published", "format": "audio"},
            {"status": "uploading", "format": "video"}, {"status": "ready_to_upload", "format": "video"}]})
        lines = []
        pd.write_status(lines)
        self.assertIn("  RSS/feed publications recorded: 1", lines)
        self.assertIn("  Spotify dashboard videos verified: 1; tracked uploads: 1", lines)

    def test_free_default_prepares_package_without_host_publication(self):
        source = TEST_DIR / "free-source.wav"
        make_wav(source)
        ready = TEST_DIR / "ready"
        ap.save(ap.CONFIG_FILE, {"podcast": {"mode": "spotify_manual", "ready_dir": str(ready),
                                             "spotify_format": "audio",
                                             "source_files": {"abc123": str(source)}}})
        ep = {"id": "abc123", "title": "Full episode", "description": "A conversation",
              "url": "https://www.youtube.com/watch?v=abc123", "upload_date": "20251007"}
        with patch.object(pd.pc, "fetch_info", return_value=ep), \
                patch.object(ps, "publish", side_effect=AssertionError("No upload during preparation")):
            message = pd.process_episode(ep, pd.load_state())
        self.assertIn("not published", message)
        self.assertGreater((ready / "abc123" / "episode.mp3").stat().st_size, 1024)
        details = json.loads((ready / "abc123" / "episode.json").read_text())
        self.assertEqual(details["title"], "Full episode")
        self.assertEqual(details["source_upload_date"], "20251007")
        self.assertIn(ep["url"], details["description"])
        self.assertEqual(pd.load_state()["episodes"]["abc123"]["status"], "ready_to_upload")
        self.assertFalse(pd.already_published("abc123"))

    def test_video_package_bypasses_external_audio_ledger_without_publishing(self):
        source = TEST_DIR / "full-video.mkv"
        subprocess.run([ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1",
                        "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "mpeg4", "-c:a", "aac",
                        str(source)], check=True, capture_output=True)
        ready = TEST_DIR / "video-ready"
        ap.save(ap.CONFIG_FILE, {"podcast": {"mode": "r2", "ready_dir": str(ready),
                                             "source_files": {"EPISODE0001": str(source)}}})
        ap.save(pd.ledger_file(), {"episodes": [{"video_id": "EPISODE0001"}]})
        ep = {"id": "EPISODE0001", "title": "Full video", "url": "https://youtu.be/EPISODE0001"}
        with patch.object(pd.pc, "fetch_info", return_value=ep), \
                patch.object(ps, "publish", side_effect=AssertionError("Must not publish RSS")):
            message = pd.process_episode(ep, pd.load_state(), prepare_only=True)
        details = json.loads((ready / ep["id"] / "episode.json").read_text())
        self.assertIn("not published", message)
        self.assertEqual(details["media_type"], "video")
        self.assertNotIn("audio_file", details)
        info = pd.probe_spotify_media(shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe",
                                      Path(details["video_file"]))
        self.assertEqual({s["codec_name"] for s in info["streams"]}, {"h264", "aac"})
        self.assertAlmostEqual(float(info["format"]["duration"]), 1, delta=0.1)
        self.assertEqual(pd.load_state()["episodes"][ep["id"]]["status"], "ready_to_upload")

    def test_video_rejects_audio_only_source(self):
        source = TEST_DIR / "audio-only.wav"
        make_wav(source)
        output = TEST_DIR / "should-not-exist.mp4"
        with self.assertRaises(pd.PodcastError):
            pd.convert_spotify_video(source, output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".tmp.mp4").exists())

    def test_video_preserves_compatible_source_track(self):
        source = TEST_DIR / "compatible.mp4"
        subprocess.run([ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:duration=1",
                        "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac",
                        str(source)], check=True, capture_output=True)
        output = TEST_DIR / "remuxed.mp4"
        with patch.object(pd.subprocess, "run", wraps=subprocess.run) as run:
            pd.convert_spotify_video(source, output)
        encode = next(c.args[0] for c in run.call_args_list if "-movflags" in c.args[0])
        self.assertEqual(encode[encode.index("-c:v") + 1], "copy")

    def test_ready_package_does_not_block_later_pending_episode(self):
        config = ap.load(ap.CONFIG_FILE, {})
        config["podcast"]["mode"] = "spotify_manual"
        ap.save(ap.CONFIG_FILE, config)
        state = {"baseline_video_id": "old", "episodes": {"ready": {"status": "ready_to_upload"}}}
        with patch.object(ap, "latest_episodes", return_value=[{"id": "new"}, {"id": "ready"}, {"id": "old"}]):
            episodes, _ = pd.pending_episodes(state)
        self.assertEqual([ep["id"] for ep in episodes], ["new"])

    def r2_fixture(self):
        config = ap.load(ap.CONFIG_FILE, {})
        config["podcast"].update(mode="r2", r2_bucket="pursuit", r2_account_id="a" * 32,
                                  r2_public_url="https://podcast.example")
        ap.save(ap.CONFIG_FILE, config)
        audio = TEST_DIR / "r2.mp3"
        audio.write_bytes(b"audio" * 1024)
        ep = {"id": "EPISODE0001", "title": "Full conversation", "description": "Description",
              "url": "https://www.youtube.com/watch?v=EPISODE0001", "upload_date": "20261008"}
        return FakeStorage(), ep, audio

    def test_r2_uploads_audio_before_feed_and_recovers_without_local_ledger(self):
        storage, ep, audio = self.r2_fixture()
        def public_feed(*args, **kwargs):
            return io.BytesIO(storage.objects["feed.xml"])
        with patch.object(ps, "client", return_value=storage), patch.object(ps, "check_audio"), \
                patch.object(ps.urllib.request, "urlopen", side_effect=public_feed):
            result = ps.publish(ep, audio)
            self.assertEqual(storage.uploads, ["media/EPISODE0001.mp3", "feed.xml"])
            self.assertEqual(result["feed_url"], "https://podcast.example/feed.xml")
            pd.ledger_file().unlink()
            ps.publish(ep, audio)
            self.assertEqual(len(storage.uploads), 2)
            self.assertTrue(pd.already_published(ep["id"]))

    def test_r2_preserves_remote_episodes_when_local_state_is_missing(self):
        storage, ep, audio = self.r2_fixture()
        with patch.object(ps, "client", return_value=storage), patch.object(ps, "check_audio"), \
                patch.object(ps.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(storage.objects["feed.xml"])):
            ps.publish(ep, audio)
            pd.ledger_file().unlink()
            ps.publish({**ep, "id": "EPISODE0002", "title": "Next conversation"}, audio)
        root = ET.fromstring(storage.objects["feed.xml"])
        self.assertEqual({i.findtext("guid") for i in root.findall("./channel/item")},
                         {"pursuit:EPISODE0001", "pursuit:EPISODE0002"})

    def test_r2_audio_failure_does_not_publish_feed_or_record_success(self):
        storage, ep, audio = self.r2_fixture()
        storage.fail_key = "media/EPISODE0001.mp3"
        with patch.object(ps, "client", return_value=storage), self.assertRaises(pd.PodcastTransient):
            ps.publish(ep, audio)
        self.assertNotIn("feed.xml", storage.objects)
        self.assertFalse(pd.already_published(ep["id"]))

    def test_r2_public_audio_failure_leaves_feed_unchanged(self):
        storage, ep, audio = self.r2_fixture()
        with patch.object(ps, "client", return_value=storage), \
                patch.object(ps, "check_audio", side_effect=pd.PodcastError("not public")), \
                self.assertRaises(pd.PodcastError):
            ps.publish(ep, audio)
        self.assertNotIn("feed.xml", storage.objects)
        self.assertFalse(pd.already_published(ep["id"]))

    def test_r2_feed_failure_can_retry_without_duplicate_guid(self):
        storage, ep, audio = self.r2_fixture()
        storage.fail_key = "feed.xml"
        with patch.object(ps, "client", return_value=storage), patch.object(ps, "check_audio"), \
                patch.object(ps.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(storage.objects["feed.xml"])):
            with self.assertRaises(pd.PodcastTransient):
                ps.publish(ep, audio)
            self.assertFalse(pd.already_published(ep["id"]))
            storage.fail_key = None
            ps.publish(ep, audio)
        self.assertEqual(len(ET.fromstring(storage.objects["feed.xml"]).findall("./channel/item")), 1)

    def test_r2_refuses_to_overwrite_foreign_feed(self):
        storage, ep, audio = self.r2_fixture()
        storage.objects["feed.xml"] = b'<rss><channel><item><guid>someone-elses-show</guid></item></channel></rss>'
        with patch.object(ps, "client", return_value=storage), self.assertRaises(pd.PodcastError):
            ps.publish(ep, audio)
        self.assertEqual(storage.uploads, [])

    def test_r2_setup_keeps_credentials_private_and_automation_off(self):
        args = type("Args", (), {"account_id": "a" * 32, "bucket": "pursuit",
                                 "public_url": "https://podcast.example", "owner_email": "anya@example.com",
                                 "artwork_url": "https://podcast.example/cover.jpg"})()
        with patch.object(ps.getpass, "getpass", side_effect=["test-id", "test-secret"]):
            ps.setup(args)
        self.assertEqual(ps.credentials_file().stat().st_mode & 0o777, 0o600)
        config = ap.load(ap.CONFIG_FILE, {})["podcast"]
        self.assertEqual(config["mode"], "r2")
        self.assertTrue(config["allow_ytdlp"])
        self.assertFalse(config["enabled"])
        self.assertNotIn("test-secret", ap.CONFIG_FILE.read_text())

    def test_r2_automation_requires_first_verified_publication(self):
        self.r2_fixture()
        args = type("Args", (), {"state": "on"})()
        with self.assertRaises(pd.PodcastError):
            pd.cmd_auto(args)

    def test_manual_prepared_episode_can_be_published_after_switch_to_r2(self):
        self.r2_fixture()
        state = {"baseline_video_id": "old", "episodes": {"ready": {"status": "ready_to_upload"}}}
        with patch.object(ap, "latest_episodes", return_value=[{"id": "ready"}, {"id": "old"}]):
            episodes, _ = pd.pending_episodes(state)
        self.assertEqual([ep["id"] for ep in episodes], ["ready"])



if __name__ == "__main__":
    unittest.main()
