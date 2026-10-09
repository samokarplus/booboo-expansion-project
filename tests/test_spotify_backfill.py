import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import spotify_backfill as backfill


class BackfillInventoryTests(unittest.TestCase):
    def snapshot(self, entries, previous=None, ledger=None):
        def load(path, default):
            return {"episodes": ledger or []} if path.name == "spotify-dashboard-ledger.json" else {
                "episodes": previous or []}
        with patch.object(backfill.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps({"entries": entries}))), \
                patch.object(backfill.pd.ap, "load", side_effect=load), \
                patch.object(backfill.pd.ap, "save"):
            return backfill.inventory()

    def test_short_talk_is_not_lost_to_social_pipeline_length_filter(self):
        rows = self.snapshot([{"id": "new", "title": "New", "duration": 600},
                              {"id": "w3Tkyfy-G7U", "title": "6 lessons in 6 months", "duration": 349}])
        self.assertEqual([e["id"] for e in rows], ["w3Tkyfy-G7U", "new"])
        self.assertTrue(all(e["status"] == "pending" for e in rows))

    def test_explicit_exclusions(self):
        rows = self.snapshot([{"id": key, "title": key, "duration": 600}
                              for key in backfill.EXCLUDED])
        self.assertEqual(sum(e["status"] == "excluded" for e in rows), 5)
        self.assertEqual(rows[0]["status"], "excluded")

    def test_preparation_and_publication_survive_inventory_refresh(self):
        entries = [{"id": key, "title": key, "duration": 600} for key in ["ready", "audio", "video"]]
        rows = self.snapshot(entries, previous=[{"id": "ready", "status": "ready_to_upload"}], ledger=[
            {"youtube_video_id": "audio", "status": "published", "spotify_episode_id": "a"},
            {"youtube_video_id": "video", "status": "published", "format": "video", "spotify_episode_id": "v"}])
        self.assertEqual({e["id"]: e["status"] for e in rows}, {
            "ready": "ready_to_upload", "audio": "needs_video_replacement", "video": "published"})

    def test_live_and_upcoming_videos_are_not_queued(self):
        rows = self.snapshot([{"id": "live", "title": "Live", "duration": 600, "live_status": "is_live"},
                              {"id": "soon", "title": "Soon", "duration": 600, "live_status": "is_upcoming"}])
        self.assertEqual(rows, [])

    def test_inventory_failure_does_not_replace_saved_queue(self):
        with patch.object(backfill.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="offline")), \
                patch.object(backfill.pd.ap, "save") as save:
            with self.assertRaises(backfill.pd.PodcastError):
                backfill.inventory()
        save.assert_not_called()

    def test_publication_requires_verified_dashboard_result(self):
        with self.assertRaises(backfill.pd.PodcastError):
            backfill.record_dashboard("abc", "spotify", "published")

    def test_existing_spotify_identity_cannot_be_silently_replaced(self):
        def load(path, default):
            if path.name == "spotify-dashboard-ledger.json":
                return {"episodes": [{"youtube_video_id": "abc", "spotify_episode_id": "first"}]}
            return {"title": "Episode"}
        with patch.object(backfill.pd.ap, "load", side_effect=load), patch.object(backfill.pd.ap, "save") as save:
            with self.assertRaises(backfill.pd.PodcastError):
                backfill.record_dashboard("abc", "different", "uploading")
        save.assert_not_called()

    def test_inventory_keeps_existing_draft_identity(self):
        rows = self.snapshot([{"id": "abc", "title": "Episode", "duration": 600}], ledger=[
            {"youtube_video_id": "abc", "status": "uploading", "spotify_episode_id": "draft"}])
        self.assertEqual(rows[0]["status"], "uploading")
        self.assertEqual(rows[0]["spotify_episode_id"], "draft")

    def test_release_dates_are_cached_and_saved_in_ready_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "ready" / "abc" / "episode.json"
            package.parent.mkdir(parents=True)
            package.write_text(json.dumps({"title": "Episode"}))
            rows = [{"id": "abc", "url": "https://youtu.be/abc", "status": "ready_to_upload"},
                    {"id": "excluded", "status": "excluded"}]
            with patch.object(backfill.pd.ap, "STATE_DIR", root), \
                    patch.object(backfill.pd, "setting", return_value=root / "ready"), \
                    patch.object(backfill.pd.pc, "fetch_info", return_value={"upload_date": "20250930"}) as fetch:
                dates = backfill.cache_release_dates(rows)
                backfill.cache_release_dates(rows)
            fetch.assert_called_once()
            self.assertEqual(dates["abc"]["date"], "2025-09-30")
            self.assertEqual(json.loads(package.read_text())["source_upload_date"], "20250930")


if __name__ == "__main__":
    unittest.main()
