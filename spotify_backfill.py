"""Resumable local preparation queue. Spotify publication is verified separately."""

import argparse
import fcntl
import json
import shutil
import subprocess
from pathlib import Path

import podcast_distribution as pd

MANIFEST = pd.ap.STATE_DIR / "spotify-backfill.json"
EXCLUDED = {
    "-4UjxkcwUhE": "Marathon week and race-day vlog (confirmed by description/chapters)",
    "ARBrxiYQWTk": "Race/training vlog",
    "YBkG5dWMhDg": "Race-day vlog",
    "HE80OsnVLFo": "EMT exam video",
    "9QlJ3H6Kr-U": "Ultra race preparation/race-day vlog (confirmed by video inspection)",
}


def record_dashboard(video_id, spotify_episode_id, status, verification=""):
    """Record an observed dashboard result, never infer publication from a file."""
    if status not in {"uploading", "published"}:
        raise pd.PodcastError("Unknown Spotify dashboard status.")
    if status == "published" and not verification:
        raise pd.PodcastError("Publication needs a dashboard verification note.")
    path = pd.ap.STATE_DIR / "spotify-dashboard-ledger.json"
    details = pd.ap.load(Path.home() / "Desktop" / "PURSUIT_SPOTIFY_READY" / video_id / "episode.json", {})
    ledger = pd.ap.load(path, {"episodes": []})
    known = {e["youtube_video_id"]: e for e in ledger["episodes"]}
    previous = known.get(video_id, {})
    if previous.get("spotify_episode_id", spotify_episode_id) != spotify_episode_id:
        raise pd.PodcastError("This YouTube episode already has a different Spotify ID; reconcile before recording.")
    if previous.get("status") == "published" and status == "uploading":
        raise pd.PodcastError("Do not downgrade a confirmed publication to uploading.")
    known[video_id] = {**previous, "youtube_video_id": video_id, "spotify_episode_id": spotify_episode_id,
                       "spotify_show_id": "7KZqjxysKxNS4QntMCVG3B", "title": details.get("title", video_id),
                       "format": "video", "status": status, "method": "browser_assisted", "hosting": "spotify",
                       "spotify_url": f"https://open.spotify.com/episode/{spotify_episode_id}",
                       "updated_at": pd.ap.now().isoformat()}
    if status == "published":
        known[video_id].update(verified_at=pd.ap.now().isoformat(), verification=verification)
    ledger["episodes"] = list(known.values())
    pd.ap.save(path, ledger)


def inventory():
    result = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings", pd.ap.CHANNEL_URL],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise pd.PodcastError(result.stderr[-500:])
    entries = json.loads(result.stdout)["entries"]
    previous = {e["id"]: e for e in pd.ap.load(MANIFEST, {"episodes": []})["episodes"]}
    published = {e["youtube_video_id"]: e for e in pd.ap.load(
        pd.ap.STATE_DIR / "spotify-dashboard-ledger.json", {"episodes": []})["episodes"]}
    rows = []
    for e in reversed(entries):
        video_id = e["id"]
        if e.get("live_status") in {"is_live", "is_upcoming"} or not e.get("duration"):
            continue
        row = {"id": video_id, "title": e["title"], "duration": e["duration"],
               "url": pd.episode_url(video_id), "status": "pending", **previous.get(video_id, {})}
        known = published.get(video_id)
        if known:
            status = "uploading"
            if known.get("status") == "published":
                status = "published" if known.get("format") == "video" else "needs_video_replacement"
            row.update(status=status, spotify_episode_id=known["spotify_episode_id"])
        if video_id in EXCLUDED:
            row.update(status="excluded", reason=EXCLUDED[video_id])
        rows.append(row)
    pd.ap.save(MANIFEST, {"show_id": "7KZqjxysKxNS4QntMCVG3B", "episodes": rows})
    return rows


def prepare(rows, limit):
    count = 0
    for row in rows:
        if row["status"] not in {"pending", "failed"}:
            continue
        if count >= limit:
            break
        if shutil.disk_usage(pd.ap.STATE_DIR).free < 8 * 1024**3:
            raise pd.PodcastError("Backfill stopped: less than 8 GB disk space remains.")
        count += 1
        work = pd.ap.STATE_DIR / "podcast-work" / row["id"]
        work.mkdir(parents=True, exist_ok=True)
        print(f"Preparing {count}/{limit}: {row['title']}", flush=True)
        try:
            ep = {**row, **pd.pc.fetch_info(row["url"]), "url": row["url"]}
            ffmpeg, _ = pd.pc.find_ffmpeg()
            source = work / "backfill-source.mp4"
            if not source.exists():
                formats = ["bv[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<=1080][ext=mp4]/bv[height<=1080]+ba/b",
                           "bv*[height<=1080]+ba/b"]
                for selection in formats:
                    command = ["yt-dlp", "--force-ipv4", "--no-playlist", "--no-warnings", "-f", selection,
                               "--merge-output-format", "mp4", "--ffmpeg-location", str(Path(ffmpeg).parent),
                               "-o", str(source), row["url"]]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
                    if result.returncode == 0 or "403" not in result.stderr:
                        break
                if result.returncode or not source.exists():
                    raise pd.PodcastError("Source download failed: " + result.stderr[-500:])
            video = pd.convert_spotify_video(source, work / "spotify-episode.mp4")
            package = pd.prepare_spotify_package(ep, video)
            # Only remove our scratch files after the independent upload package exists.
            source.unlink(missing_ok=True)
            video.unlink(missing_ok=True)
            row.update(status="ready_to_upload", **package)
            row.pop("error", None)
            print(f"Ready (not published): {package['video_file']}", flush=True)
        except Exception as exc:
            row.update(status="failed", error=str(exc))
            raise
        finally:
            pd.ap.save(MANIFEST, {"show_id": "7KZqjxysKxNS4QntMCVG3B", "episodes": rows})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=int, default=0, metavar="COUNT",
                        help="prepare up to COUNT pending videos; does not upload to Spotify")
    args = parser.parse_args()
    if args.prepare < 0:
        parser.error("COUNT must be nonnegative")
    pd.ap.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(pd.ap.STATE_DIR / "spotify-backfill.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise pd.PodcastError("Another backfill preparation run is already active.")
        rows = inventory()
        print(f"Catalog: {len(rows)} videos; {sum(e['status'] == 'excluded' for e in rows)} excluded.", flush=True)
        if args.prepare:
            prepare(rows, args.prepare)
        statuses = {state: sum(e["status"] == state for e in rows) for state in {e["status"] for e in rows}}
        print(json.dumps(statuses, sort_keys=True))


if __name__ == "__main__":
    main()
