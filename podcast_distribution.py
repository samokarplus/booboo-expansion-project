"""Podcast RSS / Spotify distribution workflow for PURSUIT.

Spotify discovers ordinary podcasts from their RSS feed; it does not provide a
general public API for uploading new episodes. This module keeps that workflow
separate from the Post for Me short-form automation.
"""

import argparse
import datetime as dt
import email.utils
import fcntl
import json
import mimetypes
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import autopilot as ap
import pursuit_clips as pc

STATE_NAME = "podcast-state.json"
LEDGER_NAME = "podcast-ledger.json"
PUBLIC_DIR_DEFAULT = Path.home() / "Desktop" / "PURSUIT_PODCAST_FEED"
SUPPORTED_SOURCE_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac")

DEFAULT_BITRATE = "160k"


class PodcastError(ap.Stop):
    """Fail-closed podcast workflow problem."""


class PodcastTransient(getattr(ap, "Transient", ap.Stop)):
    """Temporary podcast workflow problem."""


def state_file():
    return ap.STATE_DIR / STATE_NAME


def ledger_file():
    return ap.STATE_DIR / LEDGER_NAME


def load_state():
    state = ap.load(state_file(), {"episodes": {}})
    state.setdefault("episodes", {})
    return state


def load_ledger():
    ledger = ap.load(ledger_file(), {"episodes": []})
    ledger.setdefault("episodes", [])
    return ledger


def cfg():
    return (ap.load(ap.CONFIG_FILE, {}).get("podcast") or {})


def setting(name, default=None):
    env_name = "PURSUIT_PODCAST_" + name.upper()
    if env_name in os.environ:
        return os.environ[env_name]
    return cfg().get(name, default)


def truthy(value):
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def podcast_enabled():
    return truthy(setting("enabled", False))


def episode_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def clean_description(text):
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:3900] or "New episode of PURSUIT with Anya Postnikov."


def episode_record(ep, status, **extra):
    rec = {"id": ep["id"], "title": ep.get("title", ep["id"]), "status": status,
           "updated_at": ap.now().isoformat()}
    rec.update(extra)
    return rec


def pending_episodes(state, limit=20):
    eps = ap.latest_episodes(limit=limit)
    if not eps:
        raise PodcastTransient("Podcast: found no YouTube episodes to inspect.")
    if "baseline_video_id" not in state:
        state["baseline_video_id"] = eps[0]["id"]
        state["last_seen_video_id"] = eps[0]["id"]
        ap.save(state_file(), state)
        return [], f"Podcast: watching for episodes newer than {eps[0]['title']}"
    baseline = state["baseline_video_id"]
    newer = eps[:next((i for i, ep in enumerate(eps) if ep["id"] == baseline), len(eps))]
    todo = []
    for ep in reversed(newer):  # publish oldest first if several arrived while asleep
        rec = state["episodes"].get(ep["id"], {})
        if rec.get("status") in {"published", "uploaded", "rss_published", "skipped", "unknown"}:
            continue
        if rec.get("status") == "ready_to_upload" and setting("mode", "r2") == "spotify_manual":
            continue
        if rec.get("status") == "needs_source":
            continue
        todo.append(ep)
    return todo, None


def register_source(video_id, path):
    path = str(Path(path).expanduser().resolve())
    if not Path(path).exists():
        raise PodcastError(f"Source file not found: {path}")
    config = ap.load(ap.CONFIG_FILE, {})
    pod = config.setdefault("podcast", {})
    files = pod.setdefault("source_files", {})
    files[video_id] = path
    ap.save(ap.CONFIG_FILE, config)
    state = load_state()
    rec = state["episodes"].get(video_id, {})
    if rec.get("status") == "needs_source":
        rec["status"] = "source_ready"
        rec["updated_at"] = ap.now().isoformat()
        state["episodes"][video_id] = rec
        ap.save(state_file(), state)
    return path


def _configured_source(video_id):
    files = cfg().get("source_files") or {}
    path = files.get(video_id)
    if path and Path(path).expanduser().exists():
        return Path(path).expanduser()
    source_dir = setting("source_dir")
    if source_dir:
        root = Path(source_dir).expanduser()
        for ext in SUPPORTED_SOURCE_EXTS:
            for candidate in (root / f"{video_id}{ext}", root / f"*{video_id}*{ext}"):
                matches = sorted(root.glob(candidate.name))
                if matches:
                    return matches[0]
    return None


def source_media(ep, work_dir, allow_download=False):
    """Find authorized source media, or optionally fall back to the existing yt-dlp path.

    YouTube's Data API does not expose the original uploaded file. By default
    this workflow requires a local/exported source file registered with
    `./autopilot podcast-source VIDEO_ID /path/to/source.mp4`. Set
    PURSUIT_PODCAST_ALLOW_YTDLP=1 only when that access method is acceptable for
    the channel owner's workflow.
    """
    configured = _configured_source(ep["id"])
    if configured:
        return configured
    if allow_download or truthy(setting("allow_ytdlp", False)):
        ffmpeg, _ = pc.find_ffmpeg()
        return pc.download_video(ep["url"], work_dir, ffmpeg)
    raise PodcastError("source media unavailable; register an exported source with "
                       f"./autopilot podcast-source {ep['id']} /path/to/source.mp4")


def convert_mp3(source, out_path, bitrate=None):
    bitrate = bitrate or setting("bitrate", DEFAULT_BITRATE)
    ffmpeg, ffprobe = pc.find_ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.mp3")
    cmd = [ffmpeg, "-y", "-i", str(source), "-vn", "-map", "0:a:0", "-ac", "2", "-ar", "44100",
           "-codec:a", "libmp3lame", "-b:a", bitrate, "-id3v2_version", "3", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=2 * 3600)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
        tmp.unlink(missing_ok=True)
        raise PodcastError("FFmpeg could not create a valid MP3: " + (r.stderr[-500:] or "unknown error"))
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_name", "-of", "json", str(tmp)],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 0 or "mp3" not in probe.stdout:
        tmp.unlink(missing_ok=True)
        raise PodcastError("Converted file did not probe as MP3.")
    tmp.replace(out_path)
    return out_path


def probe_spotify_media(ffprobe, path):
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise PodcastError("Could not inspect Spotify media: " + result.stderr[-500:])
    return json.loads(result.stdout)


def convert_spotify_video(source, out_path):
    """Prepare full-length H.264/AAC MP4, without cropping or making a Short."""
    ffmpeg, ffprobe = pc.find_ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.mp4")
    try:
        info = probe_spotify_media(ffprobe, source)
        if not any(s.get("codec_type") == "audio" for s in info["streams"]):
            raise PodcastError("Spotify video needs a source with both video and audio.")
        # Preserve compatible source video; normalize other codecs for Spotify.
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        copy_video = video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
        encoding = ["-c:v", "copy"] if copy_video else [
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]
        command = [ffmpeg, "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
                   *encoding, "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
                   "-movflags", "+faststart", str(tmp)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=12 * 3600)
        if result.returncode or not tmp.exists() or tmp.stat().st_size < 1024:
            raise PodcastError("FFmpeg could not create Spotify video: " + result.stderr[-500:])
        prepared = probe_spotify_media(ffprobe, tmp)
        codecs = {s["codec_type"]: s.get("codec_name") for s in prepared["streams"]}
        duration = float(prepared["format"]["duration"])
        original_duration = float(info["format"]["duration"])
        if codecs.get("video") != "h264" or codecs.get("audio") != "aac":
            raise PodcastError("Spotify video must contain H.264 video and AAC audio.")
        if duration <= 0 or abs(duration - original_duration) > max(1, original_duration * 0.001):
            raise PodcastError("Spotify video duration differs from the full source episode.")
        if duration > 12 * 3600 or tmp.stat().st_size > 60_000_000_000:
            raise PodcastError("Spotify video exceeds the supported 12-hour or 60-GB limit.")
        tmp.replace(out_path)
        return out_path
    except (pc.Fail, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired) as exc:
        raise PodcastError(f"Could not validate full Spotify video: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def public_dir():
    return Path(setting("public_dir", PUBLIC_DIR_DEFAULT)).expanduser()


def feed_url():
    return setting("feed_url", "")


def media_base_url():
    base = setting("media_base_url", "")
    return base.rstrip("/")


def public_media_url(filename):
    base = media_base_url()
    if not base:
        raise PodcastError("Set PURSUIT_PODCAST_MEDIA_BASE_URL for self-hosted RSS enclosures.")
    return f"{base}/{urllib.parse.quote(filename)}"


def copy_public_media(mp3):
    target = public_dir() / "media" / Path(mp3).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.stat().st_size != Path(mp3).stat().st_size:
        shutil.copy2(mp3, target)
    return target


def rss_pub_date(value=None):
    if value:
        try:
            parsed = dt.datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=ap.TZ)
            return email.utils.format_datetime(parsed)
        except ValueError:
            pass
    return email.utils.format_datetime(ap.now())


def add_text(parent, name, text, attrs=None):
    child = ET.SubElement(parent, name, attrs or {})
    child.text = str(text or "")
    return child


def render_self_hosted_rss(episodes):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    rss = ET.Element("rss", {"version": "2.0",
                             "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
                             "xmlns:content": "http://purl.org/rss/1.0/modules/content/"})
    channel = ET.SubElement(rss, "channel")
    title = setting("title", "PURSUIT with Anya Postnikov")
    add_text(channel, "title", title)
    add_text(channel, "link", setting("website_url", ap.CHANNEL_URL.removesuffix("/videos")))
    add_text(channel, "description", setting("description", "PURSUIT with Anya Postnikov."))
    add_text(channel, "language", setting("language", "en-us"))
    add_text(channel, "itunes:author", setting("author", "Anya Postnikov"))
    add_text(channel, "itunes:explicit", setting("explicit", "false"))
    add_text(channel, "itunes:category", "", {"text": setting("category", "Society & Culture")})
    if setting("owner_email"):
        owner = ET.SubElement(channel, "itunes:owner")
        add_text(owner, "itunes:name", setting("owner_name", setting("author", "Anya Postnikov")))
        add_text(owner, "itunes:email", setting("owner_email"))
    if setting("artwork_url"):
        ET.SubElement(channel, "itunes:image", {"href": setting("artwork_url")})
        add_text(channel, "image", "")
        image = channel[-1]
        add_text(image, "url", setting("artwork_url"))
        add_text(image, "title", title)
        add_text(image, "link", setting("website_url", ap.CHANNEL_URL.removesuffix("/videos")))
    for ep in sorted(episodes, key=lambda e: e.get("published_at", ""), reverse=True):
        item = ET.SubElement(channel, "item")
        add_text(item, "title", ep["title"])
        add_text(item, "guid", ep["guid"], {"isPermaLink": "false"})
        add_text(item, "pubDate", ep["pubDate"])
        add_text(item, "description", ep.get("description", ""))
        add_text(item, "content:encoded", ep.get("description", ""))
        add_text(item, "itunes:explicit", ep.get("explicit", setting("explicit", "false")))
        ET.SubElement(item, "enclosure", {"url": ep["enclosure_url"], "length": str(ep["length"]),
                                          "type": "audio/mpeg"})
    public_dir().mkdir(parents=True, exist_ok=True)
    feed_path = public_dir() / "feed.xml"
    ET.ElementTree(rss).write(feed_path, encoding="utf-8", xml_declaration=True)
    return feed_path


def publish_self_hosted(ep, mp3):
    public_mp3 = copy_public_media(mp3)
    ledger = load_ledger()
    existing = [x for x in ledger["episodes"] if x["video_id"] != ep["id"]]
    desc = clean_description(ep.get("description")) + f"\n\nYouTube version: {ep['url']}"
    row = {"video_id": ep["id"], "title": ep.get("title") or ep["id"], "guid": f"pursuit:{ep['id']}",
           "pubDate": rss_pub_date(ep.get("upload_date")), "published_at": ap.now().isoformat(),
           "description": desc, "enclosure_url": public_media_url(public_mp3.name),
           "length": public_mp3.stat().st_size, "explicit": setting("explicit", "false")}
    ledger["episodes"] = existing + [row]
    ap.save(ledger_file(), ledger)
    feed_path = render_self_hosted_rss(ledger["episodes"])
    return {"provider": "self_hosted", "feed_path": str(feed_path), "feed_url": feed_url()}


def already_published(video_id):
    ledger = load_ledger()
    return any(e.get("video_id") == video_id for e in ledger["episodes"])


def remember_published(ep, result, mp3):
    ledger = load_ledger()
    ledger["episodes"] = [e for e in ledger["episodes"] if e.get("video_id") != ep["id"]]
    ledger["episodes"].append({"video_id": ep["id"], "title": ep.get("title") or ep["id"],
                               "published_at": ap.now().isoformat(), "mp3": str(mp3), **result})
    ap.save(ledger_file(), ledger)


def prepare_spotify_package(ep, media):
    folder = Path(setting("ready_dir", Path.home() / "Desktop" / "PURSUIT_SPOTIFY_READY")).expanduser() / ep["id"]
    folder.mkdir(parents=True, exist_ok=True)
    is_video = Path(media).suffix.lower() == ".mp4"
    target = folder / ("episode.mp4" if is_video else "episode.mp3")
    shutil.copy2(media, target)
    description = clean_description(ep.get("description")) + f"\n\nYouTube version: {ep['url']}"
    details = {"video_id": ep["id"], "title": ep["title"], "description": description,
               "explicit": truthy(setting("explicit", False)), "source_url": ep["url"],
               "source_upload_date": ep.get("upload_date"),
               "status": "ready_to_upload", "media_type": "video" if is_video else "audio",
               "media_file": str(target), "video_file" if is_video else "audio_file": str(target)}
    ap.save(folder / "episode.json", details)
    (folder / "POSTING_INFO.txt").write_text(
        f"TITLE\n{ep['title']}\n\nDESCRIPTION\n{description}\n\n"
        f"EXPLICIT\n{'Yes' if details['explicit'] else 'No'}\n\n"
        "NEXT STEP\nOpen https://creators.spotify.com/ and choose your PURSUIT show.\n"
        f"Upload {target.name}, copy the title and description. If this episode already\n"
        "exists, update that episode instead of creating a duplicate.\n"
        f"Use the original YouTube release date ({details['source_upload_date'] or 'verify on YouTube'})\n"
        "in Spotify's Schedule controls so the archive remains chronological.\n"
        "review the details, then publish. This package has not been uploaded or published.\n",
        encoding="utf-8")
    return {"provider": "spotify_manual", "package_dir": str(folder),
            "media_type": details["media_type"], "media_file": str(target),
            "video_file" if is_video else "audio_file": str(target)}


def process_episode(ep, state, dry_run=False, allow_download=False, prepare_only=False, media_format=None):
    if not prepare_only and already_published(ep["id"]):
        state["episodes"][ep["id"]] = episode_record(ep, "published", note="already in podcast ledger")
        ap.save(state_file(), state)
        return f"Podcast: already published {ep['title']}"
    meta = pc.fetch_info(ep["url"])
    ep = {**ep, **meta, "url": ep["url"]}
    work = ap.STATE_DIR / "podcast-work" / ep["id"]
    work.mkdir(parents=True, exist_ok=True)
    try:
        source = source_media(ep, work, allow_download=allow_download)
        state["episodes"][ep["id"]] = episode_record(ep, "source_ready", source=str(source))
        ap.save(state_file(), state)
        mode = "spotify_manual" if prepare_only else setting("mode", "r2")
        selected_format = media_format or setting("spotify_format", "video")
        if mode == "spotify_manual" and selected_format == "video":
            video = convert_spotify_video(source, work / "spotify-episode.mp4")
            if dry_run:
                return f"Spotify dry-run: prepared video {ep['title']} -> {video}"
            result = prepare_spotify_package(ep, video)
            state["episodes"][ep["id"]] = episode_record(ep, "ready_to_upload", **result)
            ap.save(state_file(), state)
            return f"Spotify: video ready to upload (not published): {result['package_dir']}"
        if mode == "spotify_manual" and selected_format != "audio":
            raise PodcastError("Spotify format must be video or audio.")
        mp3 = work / (pc.slugify(ep["title"], 80) + ".mp3")
        if not mp3.exists():
            convert_mp3(source, mp3)
        state["episodes"][ep["id"]] = episode_record(ep, "audio_converted", source=str(source), mp3=str(mp3))
        ap.save(state_file(), state)
        if dry_run:
            return f"Podcast dry-run: converted {ep['title']} -> {mp3}"
        if mode == "r2":
            import podcast_storage
            result = podcast_storage.publish(ep, mp3)
        elif mode == "spotify_manual":
            result = prepare_spotify_package(ep, mp3)
            state["episodes"][ep["id"]] = episode_record(ep, "ready_to_upload", **result)
            ap.save(state_file(), state)
            return f"Spotify: ready to upload (not published): {result['package_dir']}"
        elif mode == "self_hosted":
            result = publish_self_hosted(ep, mp3)
        else:
            raise PodcastError(f"Unknown podcast mode {mode!r}; expected r2, spotify_manual or self_hosted.")
        state["episodes"][ep["id"]] = episode_record(ep, "published", source=str(source), mp3=str(mp3), **result)
        ap.save(state_file(), state)
        return f"Podcast: published {ep['title']} via {result['provider']}"
    except PodcastError as e:
        state["episodes"][ep["id"]] = episode_record(ep, "needs_source" if "source media unavailable" in str(e) else "failed",
                                                      error=str(e))
        ap.save(state_file(), state)
        raise


def run(args):
    ap.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if not podcast_enabled() and not args.once:
        return "Podcast: OFF (run ./autopilot podcast-auto on after setup)."
    eps, msg = pending_episodes(state)
    if msg:
        return msg
    if not eps:
        return "Podcast: no new full-length episode."
    return process_episode(eps[0], state, dry_run=args.dry_run)


def write_status(lines):
    state = load_state()
    ledger = load_ledger()
    lines += ["", "Podcast feed / Spotify dashboard:"]
    lines.append(f"  Feed/preparation automation: {'ON' if podcast_enabled() else 'OFF'}")
    lines.append(f"  Mode: {setting('mode', 'r2')}")
    if setting("mode", "r2") == "r2":
        lines.append(f"  RSS feed: {setting('r2_public_url', '(not configured)').rstrip('/')}/feed.xml")
        lines.append("  Spotify imports this feed after one-time registration; listing is not confirmed by this tool")
    elif setting("mode", "r2") == "spotify_manual":
        lines.append("  Hosting: free Spotify for Creators; upload and publish manually")
        lines.append(f"  Ready folder: {setting('ready_dir', Path.home() / 'Desktop' / 'PURSUIT_SPOTIFY_READY')}")
    elif setting("mode", "r2") == "self_hosted":
        lines.append(f"  RSS feed: {feed_url() or '(set PURSUIT_PODCAST_FEED_URL after hosting feed.xml)'}")
        lines.append(f"  Local feed: {public_dir() / 'feed.xml'}")
    else:
        lines.append("  Run podcast-setup to configure automatic publication.")
    for rec in list(state.get("episodes", {}).values())[-8:]:
        msg = f"  [{rec.get('status')}] {rec.get('title', rec.get('id'))}"
        if rec.get("error"):
            msg += f" -- {rec['error'][:160]}"
        if rec.get("package_dir"):
            msg += f" -- {rec['package_dir']}"
        lines.append(msg)
    if ledger["episodes"]:
        lines.append(f"  RSS/feed publications recorded: {len(ledger['episodes'])}")
    dashboard = ap.load(ap.STATE_DIR / "spotify-dashboard-ledger.json", {"episodes": []})["episodes"]
    videos = sum(e.get("status") == "published" and e.get("format") == "video" for e in dashboard)
    uploading = sum(e.get("status") == "uploading" for e in dashboard)
    lines.append(f"  Spotify dashboard videos verified: {videos}; tracked uploads: {uploading}")
    lines.append("  Spotify dashboard uploads are assisted; no recurring dashboard uploader is installed.")


def cmd_source(args):
    path = register_source(args.video_id, args.path)
    print(f"Podcast source registered for {args.video_id}: {path}")


def cmd_auto(args):
    if args.state == "on" and setting("mode", "r2") == "r2":
        import podcast_storage
        podcast_storage.validate_settings()
        if not any(ep.get("enclosure_url", "").startswith(podcast_storage.public_base() + "/")
                   for ep in load_ledger()["episodes"]):
            raise PodcastError("Publish and verify the first feed episode with podcast-publish latest before enabling automation.")
    config = ap.load(ap.CONFIG_FILE, {})
    pod = config.setdefault("podcast", {})
    pod["enabled"] = args.state == "on"
    ap.save(ap.CONFIG_FILE, config)
    print(f"Podcast automation {'ON' if pod['enabled'] else 'OFF'}.")


def cmd_status(args):
    lines = []
    write_status(lines)
    print("\n".join(lines).strip())


def cmd_rss(args):
    ledger = load_ledger()
    path = render_self_hosted_rss(ledger["episodes"])
    print(f"RSS written: {path}")


def add_cli(sub):
    setup = sub.add_parser("podcast-setup", help="configure automatic RSS publication through Cloudflare R2")
    setup.add_argument("--account-id", required=True)
    setup.add_argument("--bucket", required=True)
    setup.add_argument("--public-url", required=True)
    setup.add_argument("--owner-email", required=True)
    setup.add_argument("--artwork-url", required=True)
    publish = sub.add_parser("podcast-publish", help="publish the latest or selected full episode to the configured public feed")
    publish.add_argument("episode", nargs="?", default="latest")
    publish.add_argument("--source", help="optional local full-episode export")
    publish.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("podcast-prepare", help="prepare a full episode for manual upload to free Spotify hosting")
    p.add_argument("episode", nargs="?", default="latest", help="latest or a YouTube episode URL")
    p.add_argument("--source", help="local full-episode audio/video export")
    p.add_argument("--download", action="store_true", help="retrieve your episode using yt-dlp")
    p.add_argument("--format", choices=["video", "audio"], default="video",
                   help="Spotify dashboard package format (default: full video MP4)")
    r = sub.add_parser("podcast-run", help="prepare one new full episode; publish only in an explicitly configured host mode")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--once", action="store_true", help="run even if podcast automation is off")
    s = sub.add_parser("podcast-source", help="register an authorized source media file for a YouTube episode")
    s.add_argument("video_id")
    s.add_argument("path")
    a = sub.add_parser("podcast-auto", help="turn podcast distribution on/off")
    a.add_argument("state", choices=["on", "off"])
    sub.add_parser("podcast-status", help="show podcast distribution state")
    sub.add_parser("podcast-rss", help="rewrite the self-hosted RSS feed from the podcast ledger")


def dispatch(args):
    if args.cmd != "podcast-status":
        ap.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(ap.LOCK_FILE, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PodcastError("Autopilot is busy; retry the podcast command after it finishes.")
            return dispatch_locked(args)
    return dispatch_locked(args)


def dispatch_locked(args):
    if args.cmd == "podcast-setup":
        import podcast_storage
        podcast_storage.setup(args)
    elif args.cmd in {"podcast-prepare", "podcast-publish"}:
        if args.cmd == "podcast-publish" and setting("mode", "r2") != "r2":
            raise PodcastError("Run ./autopilot podcast-setup before publishing to the automatic R2 feed.")
        if args.cmd == "podcast-publish":
            import podcast_storage
            podcast_storage.validate_settings()
        if args.episode == "latest":
            episodes = ap.latest_episodes(limit=1)
            if not episodes:
                raise PodcastError("No full-length episode found.")
            ep = episodes[0]
        else:
            parsed = urllib.parse.urlparse(args.episode)
            if parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}:
                raise PodcastError("Use latest or an HTTPS YouTube episode URL.")
            ep = pc.fetch_info(args.episode)
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", ep.get("id", "")):
            raise PodcastError("Invalid YouTube video ID.")
        if args.source:
            register_source(ep["id"], args.source)
        print(process_episode(ep, load_state(), dry_run=getattr(args, "dry_run", False),
                              allow_download=getattr(args, "download", False),
                              prepare_only=args.cmd == "podcast-prepare",
                              media_format=getattr(args, "format", None)))
    elif args.cmd == "podcast-run":
        print(run(args))
    elif args.cmd == "podcast-source":
        cmd_source(args)
    elif args.cmd == "podcast-auto":
        cmd_auto(args)
    elif args.cmd == "podcast-status":
        cmd_status(args)
    elif args.cmd == "podcast-rss":
        cmd_rss(args)
