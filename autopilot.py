#!/usr/bin/env python3
"""PURSUIT autopilot: new episode on YouTube -> clips -> quality checks -> scheduled posts.

Runs from launchd a few times a day. Each run:
  1. reconciles already-scheduled posts (did they actually go out?)
  2. looks for a new full episode on the channel
  3. if there is one: renders clips with pursuit_clips.py, checks every clip, and
     schedules the good ones on Post for Me (YouTube Shorts + Instagram Reels + TikTok),
     one clip per day.

Fail closed: anything unexpected means nothing gets posted and you get a notification.

    ./autopilot setup            one-time: API key + connect accounts
    ./autopilot run              what launchd runs (safe to run by hand)
    ./autopilot run --dry-run    everything except creating posts
    ./autopilot status           what's scheduled / posted / failed
    ./autopilot pause | resume
    ./autopilot process URL      push one specific episode through (e.g. the current one)
    ./autopilot test-post        verifies API + accounts with a draft that is never published
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pursuit_clips as pc  # noqa: E402  (reuse probe/ffmpeg helpers and the Claude CLI wrapper)

CHANNEL_URL = "https://www.youtube.com/@PursuitThePod/videos"   # /videos = long-form only, no Shorts
OUT_DIR = Path(os.environ.get("PURSUIT_OUT", pc.DEFAULT_OUT))    # env overrides are for testing
STATE_DIR = Path(os.environ.get("PURSUIT_STATE_DIR", Path.home() / "Library" / "Application Support" / "PURSUIT_AUTOPILOT"))
STATE_FILE = STATE_DIR / "state.json"      # episodes seen / processed
LEDGER_FILE = STATE_DIR / "ledger.json"    # every post we ever created
CONFIG_FILE = STATE_DIR / "config.json"    # connected account ids
LOG_FILE = Path(os.environ.get("PURSUIT_LOG", Path.home() / "Library" / "Logs" / "pursuit-autopilot.log"))
STATUS_FILE = OUT_DIR / "AUTOPILOT_STATUS.txt"
PAUSE_FILE = STATE_DIR / "PAUSED"
LOCK_FILE = STATE_DIR / "run.lock"

API = os.environ.get("PURSUIT_POSTFORME_API", "https://api.postforme.dev")
KEYCHAIN_SERVICE = "pursuit-postforme-api-key"
PLATFORMS = ["youtube", "instagram", "tiktok"]   # supported; we only post to the ones configured AND verified
# A platform is only ever posted to if its connected account has exactly this handle. Never guess.
# (config.json "expected_usernames" can add/override, e.g. once YouTube/Instagram are connected.)
EXPECTED_USERNAMES = {"tiktok": "pursuitthepod"}
# Post for Me's "username" for TikTok is the *display name* (e.g. "Anya YT") and its "external_id" is a free-text
# label chosen by whoever connected the account. Neither proves identity, and Post for Me's TikTok app can't request
# user.info.profile (the scope that exposes the handle). So identity is set once, explicitly, in ./autopilot setup:
# TikTok confirms which account the token belongs to (user.info.basic), you confirm it's @pursuitthepod by typing
# the handle, and both Post for Me's account id and TikTok's open_id are pinned. Posts require both, exactly.
PINNED_PLATFORMS = {"tiktok"}
TIKTOK_API = os.environ.get("PURSUIT_TIKTOK_API", "https://open.tiktokapis.com")
QUEUE_FILE = STATE_DIR / "queue.json"      # approved clips waiting to be posted
POST_HOURS = [10, 14, 19]      # queue posting slots (local time) once auto-posting is enabled
SCHEDULE_AHEAD = 3             # keep this many queued clips scheduled in advance (covers the Mac sleeping)
BACKLOG_PER_RUN = 1            # old episodes processed per scheduled run
BACKLOG_TARGET = 21            # stop working on the back catalog while a week of approved clips is waiting
RETRY_AFTER_DAYS = 7           # episodes that failed MAX_ATTEMPTS times get another chance after this

# --- policy ---------------------------------------------------------------------------
TZ = ZoneInfo(os.environ.get("PURSUIT_TZ", "America/Denver"))
POST_HOUR = 17                 # one clip per day at 5pm local time
MAX_CLIPS_RENDER = 8
MAX_CLIPS_POST = 7             # a week's worth
MIN_POST_SCORE = 70            # Claude's overall score needed to publish
MIN_STANDALONE = 7             # clip must make sense without context
MIN_EPISODE_MINUTES = 6        # shorter uploads are vlogs/trailers, not episodes
MIN_CLIP_SEC, MAX_CLIP_SEC = 12, 90
MAX_FAIL_FRACTION = 0.5        # if more than half the clips fail checks, trust nothing from this episode
MAX_ATTEMPTS = 3               # retries per episode before giving up (and telling you)


# ------------------------------------------------------------------------------ plumbing

def now():
    return dt.datetime.now(TZ)


def log(msg):
    line = f"[{now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, msg):
    """macOS notification + log line. Never raises."""
    log(f"NOTIFY {title}: {msg}")
    safe = lambda s: s.replace("\\", "").replace('"', "'")[:230]
    subprocess.run(["osascript", "-e", f'display notification "{safe(msg)}" with title "{safe(title)}" sound name "Glass"'],
                   capture_output=True)


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    # Make the rename durable across a sudden shutdown, not just process crashes.
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Stop(Exception):
    """Fail-closed stop with a human message."""


class Transient(Stop):
    """Temporarily unavailable (e.g. Claude or the internet). Nothing was attempted; try again next run."""


_claude_ok = None


def claude_available():
    """One cheap check per run, so an outage doesn't use up an episode's retry attempts."""
    global _claude_ok
    if _claude_ok is None:
        try:
            if not pc.find_claude():
                raise pc.Fail("Claude Code CLI not found")
            pc.ask_claude(pc.find_claude(), "Reply with just the word OK.", timeout=90)
            _claude_ok = True
        except (pc.Fail, subprocess.TimeoutExpired, OSError) as e:
            log(f"Claude unavailable this run: {str(e)[:150]}")
            _claude_ok = False
    return _claude_ok


def retry_due(rec):
    """Give episodes that failed repeatedly another chance after RETRY_AFTER_DAYS (reset their attempt count)."""
    if rec.get("attempts", 0) < MAX_ATTEMPTS:
        return True
    try:
        last = dt.datetime.fromisoformat(rec.get("last_attempt"))
    except (TypeError, ValueError):
        return False
    if now() - last > dt.timedelta(days=RETRY_AFTER_DAYS):
        rec["attempts"] = 0
        rec.pop("status", None) if rec.get("status") == "gave_up" else None
        return True
    return False


# ------------------------------------------------------------------------------ Post for Me API

class Ambiguous(Exception):
    """We can't tell whether the request took effect (timeout / 5xx). Never retry blindly."""


def api_key():
    key = os.environ.get("PURSUIT_POSTFORME_KEY")
    if key:
        return key
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise Stop("Post for Me API key not set up. Run: ./autopilot setup")
    return r.stdout.strip()


API_TIMEOUT = 60


def api(method, path, body=None, query=None, timeout=None):
    timeout = timeout or API_TIMEOUT
    url = API + path
    if query:
        url += "?" + "&".join(f"{k}={urllib.request.quote(str(v))}" for k, vals in query.items()
                              for v in (vals if isinstance(vals, list) else [vals]))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json", "User-Agent": "pursuit-autopilot"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")[:500]
        finally:
            e.close()
        if e.code in (401, 403):
            raise Stop(f"Post for Me rejected the API key ({e.code}). Run ./autopilot setup again.")
        if e.code >= 500:
            raise Ambiguous(f"Post for Me server error {e.code} on {method} {path}: {detail}")
        raise Stop(f"Post for Me refused {method} {path} ({e.code}): {detail}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise Ambiguous(f"No clear answer from Post for Me on {method} {path}: {e}")
    except json.JSONDecodeError:
        raise Ambiguous(f"Post for Me returned non-JSON on {method} {path}")


def upload_media(mp4):
    r = api("POST", "/v1/media/create-upload-url", {})
    if not r.get("upload_url") or not r.get("media_url"):
        raise Stop("Post for Me returned an incomplete upload-url response.")
    # curl streams the file and gives a clean exit code; -f fails on HTTP errors
    up = subprocess.run(["curl", "-sS", "-f", "-X", "PUT", "-H", "Content-Type: video/mp4",
                         "--upload-file", str(mp4), r["upload_url"]], capture_output=True, text=True, timeout=900)
    if up.returncode != 0:
        raise Stop(f"Uploading {mp4.name} failed: {up.stderr.strip()[:300]}")
    return r["media_url"]


def connected_accounts():
    r = api("GET", "/v1/social-accounts", query={"status": "connected", "limit": 50})
    return r.get("data", [])


def find_posts_by_external_id(external_id):
    return api("GET", "/v1/social-posts", query={"external_id": external_id, "limit": 10}).get("data", [])


def _handle(name):
    return (name or "").strip().lstrip("@").lower()


def active_platforms(config):
    """Platforms we're set up to post to (config order). Posting code never uses a platform outside this list."""
    active = [p for p in PLATFORMS if p in (config.get("accounts") or {})]
    if not active:
        raise Stop("No social accounts configured. Run ./autopilot setup")
    return active


def tiktok_user_info(account, fields):
    """Ask TikTok (not Post for Me) about this account. Returns (error_code, user). Token is never logged."""
    token = account.get("access_token")
    if not token:
        raise Stop("Post for Me didn't provide TikTok credentials to verify the account; not guessing.")
    req = urllib.request.Request(f"{TIKTOK_API}/v2/user/info/?fields={fields}",
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": "pursuit-autopilot"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            raise Ambiguous(f"TikTok returned HTTP {e.code} while verifying the account.")
        finally:
            e.close()
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
        raise Ambiguous(f"Couldn't reach TikTok to verify the account: {e}")
    return (data.get("error") or {}).get("code"), (data.get("data") or {}).get("user") or {}


def tiktok_identity(account):
    """TikTok's own view of the account: (open_id, display_name, handle or None).
    The handle only comes back if the connection happens to include user.info.profile."""
    code, user = tiktok_user_info(account, "open_id,display_name,username")
    if code == "ok" and user.get("open_id") and user.get("username"):
        return user["open_id"], user.get("display_name"), _handle(user["username"])
    code, user = tiktok_user_info(account, "open_id,display_name")      # user.info.basic: always granted
    if code != "ok" or not user.get("open_id"):
        raise Stop(f"TikTok didn't confirm which account this is ({code or 'no answer'}); not guessing.")
    return user["open_id"], user.get("display_name"), None


def confirm_pin(platform, acct, open_id, display_name, want):
    """Explicit human confirmation, used only when the platform won't tell us the handle."""
    if not sys.stdin.isatty():
        print(f"  {platform:9} NOT enabled: confirming the account needs an interactive Terminal.")
        return False
    print(f"\n  {platform.upper()} account connected in Post for Me:")
    print(f"    display name (Post for Me):  {acct.get('username')}")
    print(f"    display name (TikTok):       {display_name}")
    print(f"    label (external_id):         {acct.get('external_id')}   <- typed in when connecting; not proof")
    print(f"    Post for Me account id:      {acct.get('id')}")
    print(f"    TikTok account id (open_id): {open_id}   <- confirmed by TikTok to own this connection")
    print(f"    profile photo:               {acct.get('profile_photo_url')}")
    print(f"  {platform} won't share the @handle with Post for Me's app, so you have to confirm it.")
    print(f"  Only confirm if you connected this account while logged into TikTok as @{want}.")
    typed = input(f"  Type the handle of this account (without @) to confirm, or press Enter to skip: ").strip()
    if _handle(typed) != want:
        print(f"  {platform:9} NOT enabled (not confirmed as @{want}).")
        return False
    return True


def configured_accounts(config):
    """Return configured live accounts, failing closed on wrong-platform, wrong-handle or duplicate IDs."""
    wanted = config.get("accounts") or {}
    expected = dict(EXPECTED_USERNAMES, **(config.get("expected_usernames") or {}))
    platforms = active_platforms(config)
    live = connected_accounts()
    by_id = {}
    for account in live:
        account_id = account.get("id")
        if account_id in by_id:
            raise Stop(f"Post for Me returned duplicate account id {account_id}; not posting.")
        by_id[account_id] = account
    verified = {}
    for platform in platforms:
        account = by_id.get(wanted[platform])
        if not account:
            raise Stop(f"Post for Me says the configured {platform} account is disconnected. Run ./autopilot setup")
        if account.get("platform") != platform:
            raise Stop(f"Configured {platform} account is actually {account.get('platform')}; not posting. Run ./autopilot setup")
        if platform not in expected:
            raise Stop(f"No expected {platform} handle is set, so I won't post there (not guessing).")
        if platform in PINNED_PLATFORMS:
            pinned = (config.get("verified_ids") or {}).get(platform)
            if not pinned:
                raise Stop(f"The {platform} account hasn't been verified and pinned yet. Run ./autopilot setup")
            if account.get("user_id") != pinned:
                raise Stop(f"The connected {platform} account is not the one verified as @{_handle(expected[platform])}; not posting.")
        elif _handle(account.get("username")) != _handle(expected[platform]):
            raise Stop(f"Connected {platform} account is @{account.get('username')}, not @{_handle(expected[platform])}; not posting.")
        verified[platform] = account
    return verified


def _post_account_ids(post):
    result = []
    for account in post.get("social_accounts") or []:
        result.append(account.get("id") if isinstance(account, dict) else account)
    return result


def validate_post(post, external_id, account_ids, when, newly_created):
    """A response is usable only if it describes exactly the post we intended."""
    if not isinstance(post, dict) or not post.get("id"):
        raise Stop("Post for Me returned a post without an id; not continuing.")
    if post.get("external_id") != external_id:
        raise Stop("Post for Me returned a mismatched external_id; not continuing.")
    if set(_post_account_ids(post)) != set(account_ids):
        raise Stop("Post for Me returned different destination accounts; not continuing.")
    returned_at = post.get("scheduled_at")
    try:
        scheduled = dt.datetime.fromisoformat(returned_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise Stop("Post for Me did not confirm the scheduled time; not continuing.")
    if newly_created and abs((scheduled - when.astimezone(dt.timezone.utc)).total_seconds()) > 2:
        raise Stop("Post for Me confirmed a different scheduled time; not continuing.")
    allowed = {"scheduled"} if newly_created else {"scheduled", "processing", "processed"}
    if post.get("status") not in allowed:
        raise Stop(f"Post for Me returned status {post.get('status')!r}; not continuing.")
    return post


# ------------------------------------------------------------------------------ 1. episode detection

def latest_episodes(limit=5):
    r = subprocess.run(["yt-dlp", "--flat-playlist", "--playlist-end", str(limit), "-J", "--no-warnings", CHANNEL_URL],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise Stop("Couldn't read the PURSUIT channel (yt-dlp). Try: brew upgrade yt-dlp deno\n" + r.stderr[-300:])
    entries = json.loads(r.stdout).get("entries") or []
    eps = []
    for e in entries:
        if e.get("live_status") in ("is_upcoming", "is_live") or not e.get("duration"):
            continue   # premieres / livestreams: wait until they're normal videos
        if e["duration"] < MIN_EPISODE_MINUTES * 60:
            continue
        eps.append({"id": e["id"], "title": e.get("title", ""), "duration": e["duration"],
                    "url": f"https://www.youtube.com/watch?v={e['id']}"})
    return eps   # newest first


# ------------------------------------------------------------------------------ 2. render

BURNED_IN_BAND = "0.10:0.80"   # Anya's own captions sit in the bottom ~18% of the frame; use the band above them


def render_episode(url, band=None, ep_dir=None, force=False):
    summary = Path(tempfile.mkstemp(prefix="pursuit_summary_", suffix=".json")[1])
    summary.unlink()
    # call python directly (not the bash launcher): launchd may not let /bin/bash read files in ~/Documents
    cmd = [sys.executable, str(HERE / "pursuit_clips.py"), url, "--max", str(MAX_CLIPS_RENDER), "--out", str(OUT_DIR),
           "--summary-json", str(summary), "--keep-source"]
    if band:
        # same picks as before (analysis is cached); re-render the videos from the top part of the frame only
        cmd += ["--crop-band", band, "--force-render"]
    elif force:
        cmd += ["--force-render"]   # never trust MP4s left over from manual runs with older code
    log("Rendering clips: " + " ".join(cmd[1:]))
    env = dict(os.environ, PURSUIT_NO_OPEN="1",
               PATH="/opt/homebrew/bin:" + str(Path.home() / ".local/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"))
    # caffeinate: keep the Mac awake while this runs (it takes 15-25 minutes)
    r = subprocess.run(["caffeinate", "-i", *cmd], capture_output=True, text=True, env=env, timeout=4 * 3600)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(r.stdout[-6000:] + r.stderr[-3000:])
    if r.returncode != 0 or not summary.exists():
        err = (r.stderr.strip().splitlines() or ["unknown error"])
        raise Stop("Clip pipeline failed: " + " ".join(err[-4:])[:400])
    data = json.loads(summary.read_text())
    summary.unlink()
    return data


def cleanup_source(summary):
    """Remove the full episode after a completed verdict; failed runs keep it for resume."""
    work = Path(summary["ep_dir"]) / ".work"
    for source in work.glob("source.*"):
        source.unlink(missing_ok=True)


# ------------------------------------------------------------------------------ 3. quality checks

def technical_checks(ffmpeg, ffprobe, clip, ep_dir, words, captions):
    """Returns a list of problems (empty = passed)."""
    problems = []
    mp4 = Path(ep_dir) / clip["file"]
    if not mp4.exists() or mp4.stat().st_size < 200_000:
        return [f"missing or tiny file {mp4.name}"]
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(mp4)],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ["ffprobe could not read the file"]
    v = [s for s in d.get("streams", []) if s["codec_type"] == "video"]
    a = [s for s in d.get("streams", []) if s["codec_type"] == "audio"]
    if len(v) != 1 or v[0].get("codec_name") != "h264" or (v[0].get("width"), v[0].get("height")) != (1080, 1920):
        problems.append("video stream is not a single 1080x1920 H.264 track")
    if len(a) != 1 or a[0].get("codec_name") != "aac":
        problems.append("no AAC audio track")
    dur = float(d.get("format", {}).get("duration", 0))
    if not MIN_CLIP_SEC <= dur <= MAX_CLIP_SEC:
        problems.append(f"duration {dur:.1f}s outside {MIN_CLIP_SEC}-{MAX_CLIP_SEC}s")
    if abs(dur - float(clip["duration_sec"])) > 0.8:
        problems.append(f"duration {dur:.1f}s doesn't match planned {clip['duration_sec']}s")
    # full decode: catches corruption anywhere in the file
    dec = subprocess.run([ffmpeg, "-v", "error", "-i", str(mp4), "-f", "null", "-"], capture_output=True, text=True)
    if dec.returncode != 0 or dec.stderr.strip():
        problems.append("decode errors: " + dec.stderr.strip()[:150])
    # audio must not be silent
    vol = subprocess.run([ffmpeg, "-i", str(mp4), "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", vol.stderr)
    if not m or float(m.group(1)) < -40:
        problems.append("audio is silent or unreadable")
    # boundaries: no word may straddle the cut
    s, e = float(clip["start_sec"]), float(clip["end_sec"])
    for w in words:
        if w["s"] + 0.02 < s < w["e"] - 0.02:
            problems.append(f"starts mid-word ('{w['w']}')")
        if w["s"] + 0.02 < e < w["e"] - 0.02:
            problems.append(f"ends mid-word ('{w['w']}')")
    inside = [w for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05]
    wps = len(inside) / max(dur, 1)
    if not 1.2 <= wps <= 5.5:
        problems.append(f"odd speech rate ({wps:.1f} words/s): transcript may be wrong")
    toks = [re.sub(r"\W", "", w["w"].lower()) for w in inside]
    # Whisper glitches repeat whole phrases; people repeat short ones ("constantly, constantly, constantly")
    grams = [" ".join(toks[i:i + 6]) for i in range(len(toks) - 5)]
    if grams and max(grams.count(g) for g in set(grams)) >= 3:
        problems.append("repeated phrase loop: looks like a transcription glitch")
    copy = (Path(ep_dir) / clip["folder"] / "copy.txt")
    if not copy.exists():
        problems.append("copy.txt missing")
    return problems


def score_checks(clip, analysis_clip):
    problems = []
    overall = float(analysis_clip.get("overall") or 0)
    standalone = float((analysis_clip.get("scores") or {}).get("standalone") or 0)
    if overall < MIN_POST_SCORE:
        problems.append(f"score {overall:.0f} < {MIN_POST_SCORE}")
    if standalone < MIN_STANDALONE:
        problems.append(f"standalone {standalone:.0f} < {MIN_STANDALONE}")
    for field, limit in (("youtube_title", 95), ("caption", 1800)):
        val = (analysis_clip.get(field) or "").strip()
        if not val:
            problems.append(f"missing {field}")
        elif len(val) > limit:
            problems.append(f"{field} too long")
    return problems


QC_PROMPT = """You are the final quality gate before a short vertical video is published publicly under a creator's brand.
Read the image file {sheet}: 6 frames sampled evenly across the clip, left to right, top to bottom (each frame 1080x1920, shown smaller).
Burned-in captions {cap_note}. On-screen hook (first seconds only, may appear in frame 1): {hook!r}.
Transcript of the clip:
\"\"\"{transcript}\"\"\"

Only flag SERIOUS problems that would embarrass the creator or make the clip unwatchable:
- two different caption layers (e.g. captions already baked into the source video plus ours)
- the speaker's face is cut off or missing in most frames (for an audio-only logo layout this is expected and fine)
- black, frozen, glitched, or corrupted frames
- captions unreadable or wildly mismatched with the transcript
- transcript is incoherent/garbled or clearly not what a person would say
- anything offensive, private, or clearly not meant to be public
Do NOT flag normal things: hands or objects near captions, a caption caught mid-transition, framing that isn't perfect, casual speech.

Reply with ONLY JSON: {{"publish": true|false, "double_captions": true|false, "issues": ["..."]}}"""


def contact_sheet(ffmpeg, mp4, out_jpg, dur):
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(mp4), "-vf",
                    f"fps=6/{max(dur, 1):.2f},scale=360:-2,tile=3x2", "-frames:v", "1", str(out_jpg)],
                   capture_output=True, check=True)


def claude_visual_qc(ffmpeg, clip, ep_dir, analysis_clip, captions, transcript_text):
    claude = pc.find_claude()
    if not claude:
        raise Stop("Claude Code CLI not found (needed for the final quality check).")
    with tempfile.TemporaryDirectory(prefix="pursuit_qc_") as tmp:
        sheet = Path(tmp) / "sheet.jpg"
        contact_sheet(ffmpeg, Path(ep_dir) / clip["file"], sheet, float(clip["duration_sec"]))
        prompt = QC_PROMPT.format(sheet=sheet.name, hook=analysis_clip.get("onscreen_hook"),
                                  cap_note="were added by our tool (one layer expected)" if captions else "were NOT added by our tool",
                                  transcript=transcript_text[:3000])
        cmd = [claude, "-p", "--output-format", "json", "--no-session-persistence",
               "--tools", "Read", "--allowedTools", "Read", "--add-dir", tmp]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp, timeout=300)
    try:
        env = json.loads(r.stdout)
        if env.get("is_error"):
            raise ValueError(env.get("result"))
        text = env.get("result") or ""
        verdict = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
        if not isinstance(verdict.get("publish"), bool):
            raise ValueError("no publish field")
        return verdict
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise Stop(f"Claude's quality check reply was unusable ({e}); not publishing.")


def check_episode(summary, use_claude=True, check_all_visually=False):
    """Run every check on every clip. Returns (passed_clips, report_lines, double_caption_votes, broken_fraction)."""
    ffmpeg, ffprobe = pc.find_ffmpeg()
    ep_dir = Path(summary["ep_dir"])
    words = json.loads(Path(summary["transcript"]).read_text())["words"]
    analysis = json.loads((ep_dir / ".work" / "analysis.json").read_text())
    by_title = {c.get("clip_title", ""): c for c in analysis["clips"]}
    passed, report, doubles, eligible, broken = [], [], 0, 0, 0
    for clip in summary["clips"]:
        ac = by_title.get(clip["clip_title"])
        problems = ["can't match clip to Claude's analysis"] if ac is None else []
        if ac is not None:
            quality = score_checks(clip, ac)
            tech = technical_checks(ffmpeg, ffprobe, clip, ep_dir, words, summary["captions"])
            problems += quality + tech
            if not quality:
                eligible += 1
                broken += bool(tech)
        # visual QC on passing clips; the top-scored clip always gets looked at (it reveals burned-in captions early)
        if use_claude and (not problems or check_all_visually or clip is summary["clips"][0]) and ac is not None:
            text = " ".join(w["w"] for w in words if clip["start_sec"] - 0.05 <= w["s"] and w["e"] <= clip["end_sec"] + 0.05)
            v = claude_visual_qc(ffmpeg, clip, ep_dir, ac, summary["captions"], text)
            if v.get("double_captions"):
                doubles += 1
            if not v["publish"]:
                broken += not problems and not quality
                problems.append("visual QC: " + "; ".join(v.get("issues") or ["rejected"]))
        status = "PASS" if not problems else "SKIP"
        report.append(f"{status} {clip['folder']} (score {clip.get('score')}): " + ("ok" if not problems else "; ".join(problems)))
        log("  " + report[-1])
        if not problems:
            passed.append(dict(clip, analysis=ac))
    # "broken" = clips good enough to post that failed a technical/visual check: a sign the pipeline misbehaved
    return passed, report, doubles, (broken / eligible if eligible else 0.0)


# ------------------------------------------------------------------------------ 4. schedule + publish

def next_slots(ledger, n):
    """One slot per day at POST_HOUR, starting after the last already-scheduled post (never stacking)."""
    last = max((dt.datetime.fromisoformat(p["scheduled_at"]) for p in ledger["posts"]), default=None)
    t = now() + dt.timedelta(hours=1)
    day = t.date() if t.hour < POST_HOUR else t.date() + dt.timedelta(days=1)
    if last and last.astimezone(TZ).date() >= day:
        day = last.astimezone(TZ).date() + dt.timedelta(days=1)
    return [dt.datetime.combine(day + dt.timedelta(days=i), dt.time(POST_HOUR), TZ) for i in range(n)]


def hashtags(ac):
    return " ".join("#" + re.sub(r"\W", "", h.lstrip("#")) for h in (ac.get("hashtags") or [])[:5] if h)


def build_post(clip, meta, accounts, media_url, when, external_id, platforms=None):
    platforms = platforms or [p for p in PLATFORMS if p in accounts]
    ac = clip["analysis"]
    caption = (ac.get("caption") or "").strip()
    tags = hashtags(ac)
    social_caption = caption + (f"\n\n{tags}" if tags else "")
    yt_desc = (f"{caption}\n\nFull episode: {meta['title']}\n{meta['url']}\n\n"
               f"PURSUIT with Anya Postnikov\n{tags} #shorts").strip()
    configs = {
        "youtube": {"title": ac["youtube_title"][:95], "description": yt_desc[:4900], "privacy_status": "public",
                    "made_for_kids": False, "tags": [t.lstrip("#") for t in tags.split()][:10]},
        "instagram": {"placement": "reels", "share_to_feed": True},
        "tiktok": {"privacy_status": "public", "allow_comment": True, "allow_duet": True, "allow_stitch": True,
                   "disclose_your_brand": False, "disclose_branded_content": False, "is_ai_generated": False},
    }
    return {
        "caption": social_caption,
        "social_accounts": [accounts[p] for p in platforms],
        "media": [{"url": media_url}],
        "scheduled_at": when.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "external_id": external_id,
        "platform_configurations": {p: configs[p] for p in platforms},
    }


def schedule_clips(passed, summary, dry_run, slots=None):
    cfg = load(CONFIG_FILE, {})
    accounts = cfg.get("accounts") or {}
    platforms = [p for p in PLATFORMS if p in accounts] or PLATFORMS
    ledger = load(LEDGER_FILE, {"posts": []})
    meta = summary["meta"]
    todo = [c for c in sorted(passed, key=lambda c: -float(c.get("score") or 0))[:MAX_CLIPS_POST]]
    todo = [c for c in todo if not any(p["external_id"] == ext_id(meta, c) for p in ledger["posts"])]
    if not todo:
        log("Nothing new to schedule.")
        return []
    if not dry_run:
        platforms = list(configured_accounts(cfg))   # verified handles only
    account_ids = [accounts[p] for p in platforms] if not dry_run else []
    slots = slots or next_slots(ledger, len(todo))
    done = []
    for clip, when in zip(todo, slots):
        eid = ext_id(meta, clip)
        mp4 = Path(summary["ep_dir"]) / clip["file"]
        if dry_run:
            log(f"  [dry-run] would schedule {clip['folder']} for {when:%a %b %d %H:%M} -> {', '.join(platforms)}")
            done.append(eid)
            continue
        # idempotency: if a post with this id exists (e.g. an earlier run timed out), adopt it; never post twice
        existing = find_posts_by_external_id(eid)
        if len(existing) > 1:
            raise Stop(f"Multiple Post for Me posts have external_id {eid}; not posting or guessing.")
        if existing:
            post = validate_post(existing[0], eid, account_ids, when, newly_created=False)
            when = dt.datetime.fromisoformat(post["scheduled_at"].replace("Z", "+00:00")).astimezone(TZ)
            log(f"  found existing post for {eid}; not creating another")
        else:
            media_url = upload_media(mp4)
            body = build_post(clip, meta, accounts, media_url, when, eid, platforms)
            try:
                post = api("POST", "/v1/social-posts", body)
            except Ambiguous as e:
                # record it as unknown so the next run looks it up instead of re-posting
                ledger["posts"].append({"external_id": eid, "status": "unknown", "scheduled_at": when.isoformat(),
                                        "clip": clip["folder"], "episode": meta["id"], "platforms": platforms,
                                        "error": str(e)})
                save(LEDGER_FILE, ledger)
                raise Stop(f"Unclear whether {clip['folder']} was scheduled: {e}. Will check again next run.")
            post = validate_post(post, eid, account_ids, when, newly_created=True)
        ledger["posts"].append({"external_id": eid, "post_id": post["id"], "status": "scheduled",
                                "scheduled_at": when.isoformat(), "clip": clip["folder"], "episode": meta["id"],
                                "episode_title": meta["title"], "youtube_title": clip["analysis"]["youtube_title"],
                                "category": clip["analysis"].get("category", ""), "platforms": platforms,
                                "start_sec": clip.get("start_sec"), "end_sec": clip.get("end_sec"),
                                "file": str(mp4), "results": {}})
        save(LEDGER_FILE, ledger)
        log(f"  scheduled {clip['folder']} for {when:%a %b %d %H:%M} (post {post['id']})")
        done.append(eid)
    return done


def ext_id(meta, clip):
    return f"pursuit-{meta['id']}-{clip['folder'][:40]}"


# ------------------------------------------------------------------------------ 5. reconcile

def youtube_is_public(url):
    r = subprocess.run(["yt-dlp", "--simulate", "--no-warnings", "-q", url], capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def reconcile(dry_run=False):
    """Check posts whose time has passed. Records links; notifies on anything that didn't go out."""
    ledger = load(LEDGER_FILE, {"posts": []})
    cfg = load(CONFIG_FILE, {})
    accounts = cfg.get("accounts") or {}
    if any(p.get("status") in ("unknown", "scheduled") for p in ledger["posts"]):
        configured_accounts(cfg)
    changed, problems, published = False, [], []
    for p in ledger["posts"]:
        if p["status"] == "unknown":   # an earlier create timed out: look it up by external id
            found = find_posts_by_external_id(p["external_id"])
            if len(found) > 1:
                raise Stop(f"Multiple Post for Me posts have external_id {p['external_id']}; not guessing.")
            if found:
                post = validate_post(found[0], p["external_id"], [accounts[x] for x in p.get("platforms", PLATFORMS)],
                                     dt.datetime.fromisoformat(p["scheduled_at"]), newly_created=False)
                remote_when = dt.datetime.fromisoformat(post["scheduled_at"].replace("Z", "+00:00")).astimezone(TZ)
                p.update(post_id=post["id"], status="scheduled", scheduled_at=remote_when.isoformat(), results={})
                log(f"Resolved earlier timeout: {p['clip']} does exist ({found[0]['id']}).")
            else:
                p["status"] = "not_created"
                problems.append(f"{p['clip']}: earlier scheduling attempt didn't go through (not re-posting automatically).")
            changed = True
        if p["status"] != "scheduled":
            continue
        due = dt.datetime.fromisoformat(p["scheduled_at"])
        if now() < due + dt.timedelta(minutes=20):
            continue
        res = api("GET", "/v1/social-post-results", query={"post_id": p["post_id"], "limit": 10}).get("data", [])
        by_acct = {r["social_account_id"]: r for r in res}
        post_platforms = p.get("platforms", PLATFORMS)
        pending = [plat for plat in post_platforms if accounts.get(plat) not in by_acct]
        if pending:
            if now() > due + dt.timedelta(hours=6):
                problems.append(f"{p['clip']}: no result from {', '.join(pending)} 6h after posting time.")
                p["status"] = "unclear"
                changed = True
            continue
        for plat in post_platforms:
            r = by_acct[accounts[plat]]
            url = (r.get("platform_data") or {}).get("url")
            p["results"][plat] = {"success": bool(r.get("success")), "url": url, "error": r.get("error")}
            if not r.get("success"):
                problems.append(f"{p['clip']} failed on {plat}: {str(r.get('error'))[:120]}")
        yt = p["results"].get("youtube", {})
        if yt.get("success") and yt.get("url") and not youtube_is_public(yt["url"]):
            problems.append(f"{p['clip']}: YouTube upload exists but isn't publicly viewable ({yt['url']}).")
        p["status"] = "posted" if all(v["success"] for v in p["results"].values()) else "partial"
        published.append(p)
        changed = True
    if changed:
        save(LEDGER_FILE, ledger)
    for p in published:
        log(f"Posted: {p['clip']} -> " + ", ".join(f"{k}: {v.get('url') or v.get('error')}" for k, v in p["results"].items()))
    if problems:
        notify("PURSUIT autopilot: posting problem", " | ".join(problems))
    return published, problems


# ------------------------------------------------------------------------------ run

def render_and_check(ep, rec, use_claude=True):
    """Render + QC one episode (re-cropping if Anya's captions are burned in). Returns (summary, passed, report)."""
    summary = render_episode(ep["url"], force=not rec.get("rendered_by_autopilot"))
    rec["rendered_by_autopilot"] = True
    passed, report, doubles, broken_frac = check_episode(summary, use_claude)
    if doubles:
        log("QC saw captions burned into the source video: re-rendering from the top of the frame (drops her caption band).")
        summary = render_episode(ep["url"], band=BURNED_IN_BAND, ep_dir=summary["ep_dir"])
        passed, report, doubles, broken_frac = check_episode(summary, use_claude, check_all_visually=True)
        if doubles:
            passed = []
            report.append("Burned-in captions still visible after re-cropping.")
    n = len(summary["clips"])
    if n == 0 or not passed or broken_frac > MAX_FAIL_FRACTION:
        cleanup_source(summary)
        rec.update(status="rejected", report=report, finished=now().isoformat())   # a verdict: don't retry
        why = "No clip passed the checks" if n == 0 or not passed else \
              "Most postable clips failed checks, so the whole batch is suspect"
        raise Stop(f"{why} for '{summary['meta']['title']}'. Nothing queued.\n" + "\n".join(report))
    cleanup_source(summary)
    return summary, passed, report


def process_episode(ep, state, dry_run, use_claude=True, fresh=True):
    """Render, check and add the good clips to the approved queue. Posting happens from the queue (fill_schedule)."""
    if use_claude and not claude_available():
        raise Transient("Claude is unavailable right now; will process later (no attempt used).")
    rec = state["episodes"].setdefault(ep["id"], {"title": ep["title"], "attempts": 0})
    rec["attempts"] += 1
    rec["last_attempt"] = now().isoformat()
    if not dry_run:
        save(STATE_FILE, state)
    log(f"{'New' if fresh else 'Back-catalog'} episode: {ep['title']} ({ep['url']}), attempt {rec['attempts']}")
    try:
        summary, passed, report = render_and_check(ep, rec, use_claude)
    finally:
        if not dry_run:
            save(STATE_FILE, state)
    added = [] if dry_run else add_to_queue(passed, summary, fresh)
    n = len(summary["clips"])
    rec.update(status="dry_run" if dry_run else "queued", title=summary["meta"]["title"], clips_rendered=n,
               clips_passed=len(passed), queued=added, report=report, finished=now().isoformat())
    if not dry_run:
        if fresh:
            state["last_processed_video_id"] = ep["id"]
        save(STATE_FILE, state)
    msg = f"{len(passed)} approved clips {'found' if dry_run else 'queued'} from '{summary['meta']['title']}' ({n - len(passed)} held back)."
    notify("PURSUIT autopilot", msg)
    return msg


# ------------------------------------------------------------------------------ approved queue

def add_to_queue(passed, summary, fresh):
    queue = load(QUEUE_FILE, {"clips": []})
    ledger_ids = {p["external_id"] for p in load(LEDGER_FILE, {"posts": []})["posts"]}
    have = {q["external_id"] for q in queue["clips"]} | ledger_ids
    meta, added = summary["meta"], []
    for clip in passed:
        eid = ext_id(meta, clip)
        if eid in have:
            continue
        row = {k: v for k, v in clip.items() if k != "analysis"}
        queue["clips"].append({"external_id": eid, "status": "queued", "fresh": fresh, "added": now().isoformat(),
                               "score": float(clip.get("score") or 0), "category": clip["analysis"].get("category", ""),
                               "episode": meta["id"], "episode_title": meta["title"], "ep_dir": summary["ep_dir"],
                               "meta": meta, "clip": row, "analysis": clip["analysis"]})
        added.append(eid)
    save(QUEUE_FILE, queue)
    return added


def pick_next(queue, ledger, n):
    """Best clips first, but new-episode clips get a boost and we avoid repeating an episode/topic back to back."""
    posted = {p["external_id"] for p in ledger["posts"]}
    pool = [q for q in queue["clips"] if q["status"] == "queued" and q["external_id"] not in posted]
    history = sorted(ledger["posts"], key=lambda p: p["scheduled_at"])[-3:]
    recent_eps = [p.get("episode") for p in history]
    recent_cats = [p.get("category") for p in history]
    picks = []
    while pool and len(picks) < n:
        def value(q):
            return (q["score"] + (15 if q.get("fresh") else 0)
                    - (25 if q["episode"] in recent_eps[-2:] else 0) - (8 if q["category"] and q["category"] in recent_cats[-2:] else 0))
        best = max(pool, key=value)
        pool.remove(best)
        picks.append(best)
        recent_eps.append(best["episode"])
        recent_cats.append(best["category"])
    return picks


def queue_slots(ledger, n):
    """Next POST_HOURS slots, at least 30 min from now and at least 2h after the last scheduled post."""
    last = max((dt.datetime.fromisoformat(p["scheduled_at"]) for p in ledger["posts"]), default=None)
    earliest = now() + dt.timedelta(minutes=30)
    if last:
        earliest = max(earliest, last.astimezone(TZ) + dt.timedelta(hours=2))
    slots, day = [], earliest.date()
    while len(slots) < n:
        for h in POST_HOURS:
            t = dt.datetime.combine(day, dt.time(h), TZ)
            if t >= earliest and len(slots) < n:
                slots.append(t)
        day += dt.timedelta(days=1)
    return slots


def schedule_from_queue(item, when):
    """Schedule one queued clip through the normal (duplicate-safe, fail-closed) scheduling path."""
    queue = load(QUEUE_FILE, {"clips": []})
    mp4 = Path(item["ep_dir"]) / item["clip"]["file"]
    if not mp4.exists():   # you deleted it = you vetoed it
        for q in queue["clips"]:
            if q["external_id"] == item["external_id"]:
                q["status"] = "removed"
        save(QUEUE_FILE, queue)
        log(f"  {item['clip']['folder']} was deleted from disk; dropping it from the queue.")
        return None
    s0, e0 = float(item["clip"].get("start_sec", 0)), float(item["clip"].get("end_sec", 0))
    for p in load(LEDGER_FILE, {"posts": []})["posts"]:
        if p.get("episode") == item["episode"] and p.get("end_sec") is not None and p["status"] != "not_created":
            if min(e0, float(p["end_sec"])) - max(s0, float(p["start_sec"])) > 0:
                for q in queue["clips"]:
                    if q["external_id"] == item["external_id"]:
                        q["status"] = "duplicate"
                save(QUEUE_FILE, queue)
                log(f"  {item['clip']['folder']} overlaps a moment already posted ({p['clip']}); skipping it.")
                return None
    summary = {"ep_dir": item["ep_dir"], "meta": item["meta"]}
    done = schedule_clips([dict(item["clip"], analysis=item["analysis"])], summary, dry_run=False, slots=[when])
    if done != [item["external_id"]]:
        raise Stop(f"Post for Me did not confirm {item['clip']['folder']}; inspect the ledger before retrying.")
    for q in queue["clips"]:
        if q["external_id"] == item["external_id"]:
            q["status"] = "scheduled"
    save(QUEUE_FILE, queue)
    return done[0]


def fill_schedule():
    """Keep SCHEDULE_AHEAD queued clips scheduled on Post for Me. Only when auto-posting was explicitly enabled."""
    cfg = load(CONFIG_FILE, {})
    if not cfg.get("auto_posting"):
        return 0
    ledger = load(LEDGER_FILE, {"posts": []})
    ahead = [p for p in ledger["posts"] if p["status"] in ("scheduled", "unknown")
             and dt.datetime.fromisoformat(p["scheduled_at"]) > now()]
    need = SCHEDULE_AHEAD - len(ahead)
    if need <= 0:
        return 0
    configured_accounts(cfg)
    count, tried = 0, set()
    while count < need:
        picks = [q for q in pick_next(load(QUEUE_FILE, {"clips": []}), load(LEDGER_FILE, {"posts": []}), need)
                 if q["external_id"] not in tried][:1]
        if not picks:
            log("No approved clip available for the next slot; skipping it rather than posting something weak.")
            break
        tried.add(picks[0]["external_id"])
        when = queue_slots(load(LEDGER_FILE, {"posts": []}), 1)[0]
        if schedule_from_queue(picks[0], when):
            count += 1
    return count


def backlog_step(state, limit, use_claude=True):
    """Process up to `limit` old episodes that haven't been handled yet (newest first). Queue only, never posts."""
    waiting = sum(1 for q in load(QUEUE_FILE, {"clips": []})["clips"] if q["status"] == "queued")
    if waiting >= BACKLOG_TARGET:
        log(f"{waiting} approved clips waiting; back catalog can rest.")
        return [], None
    eps = latest_episodes(limit=300)
    todo = [e for e in eps if state["episodes"].get(e["id"], {}).get("status") not in
            ("queued", "done", "rejected", "live_test")
            and retry_due(state["episodes"].get(e["id"], {}))]
    msgs = []
    for ep in todo[:limit]:
        try:
            msgs.append(process_episode(ep, state, dry_run=False, use_claude=use_claude, fresh=False))
        except Transient as e:
            msgs.append(str(e))
            break
        except Stop as e:
            msgs.append(str(e).splitlines()[0])
            log(f"Back catalog: {e}")
    left = len(todo) - min(limit, len(todo))
    return msgs, left


def write_status(extra=""):
    ledger = load(LEDGER_FILE, {"posts": []})
    state = load(STATE_FILE, {})
    lines = [f"PURSUIT autopilot status, updated {now():%a %b %d %Y %H:%M}", ""]
    if PAUSE_FILE.exists():
        lines += ["*** PAUSED *** (run ./autopilot resume to turn back on)", ""]
    if extra:
        lines += [extra, ""]
    cfg = load(CONFIG_FILE, {})
    queue = [q for q in load(QUEUE_FILE, {"clips": []})["clips"] if q["status"] == "queued"]
    eps = state.get("episodes", {})
    lines.append(f"Posting to: {', '.join(f'{k} @{v}' for k, v in (cfg.get('usernames') or {}).items()) or '(no verified account)'}")
    lines.append(f"Auto-posting: {'ON' if cfg.get('auto_posting') else 'OFF (run the one-clip live test first)'}")
    lines.append(f"Episodes processed: {sum(1 for e in eps.values() if e.get('status') == 'queued')} queued, "
                 f"{sum(1 for e in eps.values() if e.get('status') == 'rejected')} rejected")
    lines.append(f"Last processed episode: {state.get('last_processed_video_id', '(none yet)')}")
    lines += ["", f"Approved queue ({len(queue)} clips, best first):"]
    for q in sorted(queue, key=lambda q: -q["score"])[:15]:
        lines.append(f"  {q['score']:>3.0f}  {q['clip']['folder'][:50]:50}  [{q['episode_title'][:40]}]")
    upcoming = [p for p in ledger["posts"] if p["status"] == "scheduled"]
    lines += ["", f"Scheduled ({len(upcoming)}):"]
    lines += [f"  {dt.datetime.fromisoformat(p['scheduled_at']):%a %b %d %H:%M}  {p['clip']}" for p in upcoming]
    recent = [p for p in ledger["posts"] if p["status"] != "scheduled"][-10:]
    lines += ["", "Recent:"]
    for p in recent:
        links = " ".join(f"{k}:{v.get('url') or 'FAILED'}" for k, v in (p.get("results") or {}).items())
        lines.append(f"  [{p['status']}] {p['clip']}  {links}")
    lines += ["", f"Full log: {LOG_FILE}"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text("\n".join(lines) + "\n")


def cmd_run(args, only_url=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another run is in progress; exiting.")
        lock.close()
        return
    if PAUSE_FILE.exists() and not only_url:
        log("Paused; exiting.")
        write_status()
        lock.close()
        return
    state = load(STATE_FILE, {"episodes": {}})
    state.setdefault("episodes", {})
    extra = ""
    try:
        if only_url:
            if not args.dry_run and load(LEDGER_FILE, {"posts": []})["posts"]:
                reconcile()
            vid = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", only_url)
            if not vid:
                raise Stop("That doesn't look like a YouTube video URL.")
            ep = {"id": vid.group(1), "title": vid.group(1), "url": only_url}
            extra = process_episode(ep, state, args.dry_run, not args.no_claude_qc, fresh=False)
            return
        live = not args.dry_run
        steps = [("posts", lambda: (reconcile(), None)[1] if live and load(LEDGER_FILE, {"posts": []})["posts"] else None),
                 ("scheduling", lambda: schedule_msg(fill_schedule()) if live else None),
                 ("new episode", lambda: check_new_episode(args, state)),
                 ("back catalog", lambda: backlog_msg(*backlog_step(state, BACKLOG_PER_RUN, not args.no_claude_qc))
                  if live and load(CONFIG_FILE, {}).get("backlog", True) else None),
                 ("scheduling", lambda: schedule_msg(fill_schedule()) if live else None)]
        lines = []
        for name, step in steps:
            try:
                msg = step()
                if msg:
                    lines.append(msg)
            except Transient as e:
                lines.append(f"{name}: waiting ({e})")
                log(lines[-1])
            except (Stop, Ambiguous) as e:
                lines.append(f"{name}: STOPPED (nothing questionable was posted): {e}")
                notify("PURSUIT autopilot stopped", f"{name}: {str(e).splitlines()[0]}")
                log(lines[-1])
        extra = "\n".join(lines)
    except Stop as e:
        extra = f"STOPPED (nothing questionable was posted): {e}"
        notify("PURSUIT autopilot stopped", str(e).splitlines()[0])
        log(extra)
    except Ambiguous as e:
        extra = f"STOPPED: unclear response from Post for Me: {e}"
        notify("PURSUIT autopilot stopped", "Unclear response from Post for Me. Will re-check next run.")
        log(extra)
    except Exception as e:   # any bug: fail closed and say so
        import traceback
        log(traceback.format_exc())
        extra = f"STOPPED on an unexpected error: {e!r}"
        notify("PURSUIT autopilot error", repr(e)[:200])
    finally:
        write_status(extra)
        lock.close()


def schedule_msg(n):
    return f"Scheduled {n} clip(s) from the approved queue." if n else None


def backlog_msg(msgs, left):
    if not msgs:
        return None
    return "Back catalog: " + " | ".join(msgs) + (f" ({left} episodes left)" if left is not None else "")


def check_new_episode(args, state):
    """Queue clips from a newly uploaded episode. Returns a status line (or None)."""
    eps = latest_episodes()
    if not eps:
        raise Stop("Found no episodes on the channel page. yt-dlp may need an update: brew upgrade yt-dlp deno")
    if "baseline_video_id" not in state:
        # first run ever: don't dig up old episodes; start with the next one Anya uploads
        state["baseline_video_id"] = state["last_processed_video_id"] = eps[0]["id"]
        if not args.dry_run:
            save(STATE_FILE, state)
        log(f"First run{' dry-run' if args.dry_run else ''}: baseline {'would be ' if args.dry_run else ''}set to newest episode '{eps[0]['title']}'.")
        return f"Watching for episodes newer than: {eps[0]['title']}"
    newest = eps[0]
    if newest["id"] == state.get("last_processed_video_id"):
        log("No new episode.")
        return None
    rec = state["episodes"].get(newest["id"], {})
    if rec.get("status") in ("done", "gave_up", "rejected", "queued", "live_test"):
        log(f"Newest episode already handled ({rec['status']}).")
        return None
    if not retry_due(rec):
        rec["status"] = "gave_up"
        save(STATE_FILE, state)
        notify("PURSUIT autopilot needs you", f"Gave up on '{newest['title']}' after {MAX_ATTEMPTS} tries. See {STATUS_FILE}")
        return None
    return process_episode(newest, state, args.dry_run, not args.no_claude_qc)


# ------------------------------------------------------------------------------ setup / misc

def setup_account_map():
    """Choose only an unambiguous account per platform, preferring our OAuth external_id."""
    live = connected_accounts()
    selected = {}
    for platform in PLATFORMS:
        candidates = [a for a in live if a.get("platform") == platform]
        owned = [a for a in candidates if a.get("external_id") == f"pursuit-{platform}"]
        if len(owned) == 1:
            selected[platform] = owned[0]
        elif len(owned) > 1 or len(candidates) > 1:
            labels = ", ".join(f"@{a.get('username') or '?'} ({a.get('id')})" for a in candidates)
            raise Stop(f"Multiple connected {platform} accounts found: {labels}. Disconnect extras in Post for Me; not guessing.")
        elif len(candidates) == 1:
            selected[platform] = candidates[0]
    return selected


def cmd_setup(args):
    print("\n== PURSUIT autopilot setup ==\n")
    have = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True).returncode == 0
    if not have or args.new_key:
        import getpass
        print("Paste your Post for Me API key (from https://app.postforme.dev > API keys). It won't be shown.")
        key = getpass.getpass("API key: ").strip()
        if not key:
            sys.exit("No key entered.")
        subprocess.run(["security", "add-generic-password", "-U", "-a", os.environ.get("USER", "pursuit"),
                        "-s", KEYCHAIN_SERVICE, "-w", key], check=True)
        print("Saved in your macOS Keychain.\n")
    cfg = load(CONFIG_FILE, {"accounts": {}})
    accounts = setup_account_map()
    for plat in PLATFORMS:
        if plat not in (args.connect or []) and not (args.reconnect and plat in accounts):
            continue
        body = {"platform": plat, "external_id": f"pursuit-{plat}"}
        if plat == "instagram":
            body["platform_data"] = {"instagram": {"connection_type": "instagram"}}
        url = api("POST", "/v1/social-accounts/auth-url", body)["url"]
        print(f"\n  Connect {plat.upper()}: a browser window is opening. Log in as PURSUIT's {plat} account and approve.")
        subprocess.run(["open", url])
        input("  Press Enter here once you've finished in the browser... ")
        accounts = setup_account_map()
        if plat not in accounts:
            print(f"  !! {plat} doesn't show as connected yet. Re-run ./autopilot setup to try again.")
        else:
            print(f"  {plat}: connected as @{accounts[plat].get('username')}")
    expected = dict(EXPECTED_USERNAMES, **(cfg.get("expected_usernames") or {}))
    old_accounts = dict(cfg.get("accounts") or {})
    old_pins = dict(cfg.get("verified_ids") or {})
    old_method = dict(cfg.get("verified_by") or {})
    cfg["accounts"], cfg["usernames"], cfg["verified_ids"], cfg["verified_by"] = {}, {}, {}, {}
    print("Accounts in Post for Me:")
    for plat in PLATFORMS:
        acct = accounts.get(plat)
        if not acct:
            print(f"  {plat:9} not connected")
            continue
        label = f"'{acct.get('username')}'"
        if plat not in expected:
            print(f"  {plat:9} {label}: NOT enabled (no expected handle set; not guessing)")
            continue
        want = _handle(expected[plat])
        if plat in PINNED_PLATFORMS:
            try:
                open_id, display_name, handle = tiktok_identity(acct)
            except (Stop, Ambiguous) as e:
                print(f"  {plat:9} {label}: NOT enabled: {e}")
                continue
            if open_id != acct.get("user_id"):
                print(f"  {plat:9} {label}: NOT enabled (TikTok and Post for Me disagree about which account this is)")
                continue
            if handle is not None and handle != want:
                print(f"  {plat:9} {label}: NOT enabled (TikTok says the handle is @{handle}, expected @{want})")
                continue
            same_as_before = (old_accounts.get(plat) == acct["id"] and old_pins.get(plat) == open_id
                              and old_method.get(plat) in ("platform", "you"))
            if handle == want:
                method = "platform"
            elif same_as_before:
                method = old_method[plat]          # you already confirmed exactly this account
            elif confirm_pin(plat, acct, open_id, display_name, want):
                method = "you"
            else:
                continue
            cfg["accounts"][plat], cfg["usernames"][plat], cfg["verified_ids"][plat] = acct["id"], want, open_id
            cfg["verified_by"][plat] = method
            how = "confirmed by TikTok" if method == "platform" else "confirmed by you, pinned"
            print(f"  {plat:9} @{want} (display name {label}): {how} ✓")
        elif _handle(acct.get("username")) != want:
            print(f"  {plat:9} {label}: NOT enabled (expected @{want})")
        else:
            cfg["accounts"][plat], cfg["usernames"][plat] = acct["id"], acct.get("username")
            print(f"  {plat:9} @{acct.get('username')}: verified ✓")
    save(CONFIG_FILE, cfg)
    if not cfg["accounts"]:
        print("\nNo verified account yet, so nothing can be posted.")
    else:
        print(f"\nWill post only to: {', '.join(cfg['accounts'])}")
        print("Next: ./autopilot test-post   (checks everything with a draft that is never published)\n")


def cmd_test_post(args):
    """Upload a real clip and create a *draft* post (isDraft: never processed/published), then delete the draft."""
    cfg = load(CONFIG_FILE, {})
    accounts = cfg.get("accounts") or {}
    if not accounts:
        sys.exit("Connect accounts first: ./autopilot setup")
    live = configured_accounts(cfg)
    for p in live:
        print(f"  {p}: connected and verified (@{live[p].get('username')})")
    clips = sorted(OUT_DIR.glob("*/[0-9][0-9]_*/*.mp4"))
    if not clips:
        sys.exit("No rendered clip found to test with.")
    mp4 = clips[0]
    print(f"  uploading {mp4.name} ...")
    media_url = upload_media(mp4)
    external_id = f"pursuit-test-{int(time.time())}"
    post = api("POST", "/v1/social-posts", {"caption": "PURSUIT autopilot connection test (draft, never published)",
                                            "social_accounts": [accounts[p] for p in live],
                                            "media": [{"url": media_url}], "isDraft": True,
                                            "external_id": external_id})
    print(f"  draft created: {post.get('id')} status={post.get('status')}")
    if (not post.get("id") or post.get("status") != "draft" or post.get("external_id") != external_id
            or set(_post_account_ids(post)) != {accounts[p] for p in live}):
        if post.get("id"):
            try:
                api("DELETE", f"/v1/social-posts/{post['id']}")
            except (Stop, Ambiguous):
                pass
        raise Stop("Post for Me did not confirm a safe draft exactly as requested. Check its dashboard; nothing else was created.")
    api("DELETE", f"/v1/social-posts/{post['id']}")
    print("  draft deleted. Everything works. ✓")


def cmd_live_test(args):
    """Explicitly schedule ONE clip from the approved queue, after showing it and asking for typed confirmation.
    Never called by launchd."""
    if not sys.stdin.isatty():
        raise Stop("The one-clip live test requires an interactive Terminal.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        for waited in range(45 * 6):          # background processing holds the lock for ~10-15 min at a time
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited == 0:
                    print("Background processing is running; waiting for it to finish (usually < 15 min)...")
                time.sleep(10)
        else:
            raise Stop("Background processing is still running after 45 minutes. Try again later.")
        cfg = load(CONFIG_FILE, {})
        live = configured_accounts(cfg)          # API key works + every destination is the verified handle
        ledger = load(LEDGER_FILE, {"posts": []})
        queue = load(QUEUE_FILE, {"clips": []})
        if args.clip:
            items = [q for q in queue["clips"] if q["status"] == "queued" and args.clip in q["clip"]["folder"]]
            if len(items) != 1:
                raise Stop(f"--clip must match exactly one queued clip (matched {len(items)}).")
            item = items[0]
        else:
            picks = pick_next(queue, ledger, 1)
            if not picks:
                raise Stop("The approved queue is empty. Build it first: ./autopilot backlog")
            item = picks[0]
        if any(p.get("external_id") == item["external_id"] for p in ledger["posts"]) or \
                find_posts_by_external_id(item["external_id"]):
            raise Stop("This clip already exists in the local or Post for Me ledger; not scheduling it again.")
        when = (now() + dt.timedelta(minutes=args.minutes)).replace(second=0, microsecond=0)
        mp4 = Path(item["ep_dir"]) / item["clip"]["file"]
        ac = item["analysis"]
        print("\n== CONTROLLED ONE-CLIP LIVE TEST ==")
        print(f"\nMP4: {mp4}")
        print(f"Episode: {item['episode_title']}")
        print(f"Score: {item['score']:.0f}   Title: {ac.get('youtube_title')}")
        print(f"Caption:\n{build_post(dict(item['clip'], analysis=ac), item['meta'], cfg['accounts'], '', when, '', list(live))['caption']}\n")
        print("Destinations (verified):")
        for platform, acct in live.items():
            print(f"  {platform}: @{acct.get('username')}")
        print(f"Goes live: {when:%A %B %d at %I:%M %p %Z}")
        print("\nThe video is opening for a final look. This command schedules only this one clip.")
        subprocess.run(["open", str(mp4)], check=False)
        confirmation = input('\nType POST ONE CLIP exactly to continue: ').strip()
        if confirmation != "POST ONE CLIP":
            print("Cancelled. Nothing was scheduled.")
            return
        eid = schedule_from_queue(item, when)
        if eid != item["external_id"]:
            raise Stop("Post for Me did not confirm exactly one scheduled clip; inspect the ledger before retrying.")
        write_status(f"Controlled live test scheduled: {item['clip']['folder']} at {when:%a %b %d %H:%M}")
        print(f"\nScheduled. ✓  Check TikTok after {when:%I:%M %p}, then run ./autopilot status")
        notify("PURSUIT live test scheduled", f"One clip scheduled for {when:%a %b %d at %I:%M %p}.")


def cmd_auto_post(args):
    cfg = load(CONFIG_FILE, {})
    if args.state == "off":
        cfg["auto_posting"] = False
        save(CONFIG_FILE, cfg)
        print("Auto-posting OFF. Clips keep going into the approved queue; nothing new gets scheduled.")
        return
    if load(LEDGER_FILE, {"posts": []})["posts"]:
        reconcile()                      # pick up the live test's result if it just went out
    ledger = load(LEDGER_FILE, {"posts": []})
    if not any(p["status"] == "posted" for p in ledger["posts"]):
        raise Stop("No post has been confirmed live yet. Do the one-clip live test first and let it go out "
                   "(./autopilot status shows it as [posted]).")
    configured_accounts(cfg)
    cfg["auto_posting"] = True
    cfg.pop("backlog", None)             # back catalog on (default)
    save(CONFIG_FILE, cfg)
    PAUSE_FILE.unlink(missing_ok=True)
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.pursuit.autopilot"],
                            capture_output=True).returncode == 0
    if not loaded:
        subprocess.run([str(HERE / "install_autopilot.sh")], check=False)
    n = fill_schedule()
    print(f"Scheduled {n} clip(s) now.")
    print(f"Auto-posting ON: approved clips go out at {', '.join(f'{h}:00' for h in POST_HOURS)} "
          f"(keeping {SCHEDULE_AHEAD} scheduled ahead).")


def cmd_backlog(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("Another autopilot run is in progress. Try again after it finishes.")
        state = load(STATE_FILE, {"episodes": {}})
        state.setdefault("episodes", {})
        msgs, left = backlog_step(state, args.limit)
        for m in msgs:
            print(" -", m)
        write_status(f"Back catalog: processed {len(msgs)}, {left} left")
        print(f"{left} old episodes left (the scheduled runs keep going, {BACKLOG_PER_RUN} per run).")


def cmd_status(args):
    write_status()
    print(STATUS_FILE.read_text())


def main():
    ap = argparse.ArgumentParser(description="PURSUIT autopilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="do everything except create posts")
    r.add_argument("--no-claude-qc", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("process")
    p.add_argument("url")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-claude-qc", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("setup")
    s.add_argument("--new-key", action="store_true")
    s.add_argument("--reconnect", action="store_true")
    s.add_argument("--connect", nargs="*", choices=PLATFORMS, help="open the connect flow for these platforms")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("test-post")
    live = sub.add_parser("live-test", help="interactively schedule exactly one clip from the approved queue")
    live.add_argument("--clip", help="part of a queued clip's folder name (default: the best one)")
    live.add_argument("--minutes", type=int, default=20, help="minutes from now (default 20)")
    b = sub.add_parser("backlog", help="process old episodes into the approved queue (never posts)")
    b.add_argument("--limit", type=int, default=5)
    a = sub.add_parser("auto-post", help="turn scheduled posting from the queue on/off")
    a.add_argument("state", choices=["on", "off"])
    args = ap.parse_args()
    try:
        if args.cmd == "run":
            cmd_run(args)
        elif args.cmd == "process":
            cmd_run(args, only_url=args.url)
        elif args.cmd == "setup":
            cmd_setup(args)
        elif args.cmd == "test-post":
            cmd_test_post(args)
        elif args.cmd == "live-test":
            cmd_live_test(args)
        elif args.cmd == "backlog":
            cmd_backlog(args)
        elif args.cmd == "auto-post":
            cmd_auto_post(args)
        elif args.cmd == "status":
            cmd_status(args)
        elif args.cmd == "pause":
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            PAUSE_FILE.touch()
            write_status()
            print("Paused: no new episodes will be processed. Posts already scheduled on Post for Me will still go out\n"
                  "(cancel those in the Post for Me dashboard if you need to).")
        elif args.cmd == "resume":
            PAUSE_FILE.unlink(missing_ok=True)
            write_status()
            print("Resumed.")
    except (Stop, Ambiguous) as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
