"""Manual YouTube Shorts delivery: finished Shorts + posting info -> a dedicated Google Drive folder.

This is a DELIVERY workflow, not publishing. It never calls Post for Me, never touches YouTube,
and never changes the TikTok ledger, queue, config or autopilot episode state. Its own state lives in
STATE_DIR/drive/ (drive_state.json + the OAuth token), separate from everything else.

    ./autopilot drive-setup --client-secret ~/Downloads/client_secret_XXXX.json   one-time Google sign-in
    ./autopilot drive-test                                                      one confirmed test upload
    ./autopilot export-shorts latest 3                                          build the package locally
    ./autopilot export-shorts latest 3 --deliver                                ...and upload it to Drive
    ./autopilot drive-status | cleanup-drive | drive-auto on|off

Google access uses the drive.file scope: this app can only see and change files it created itself.
Deletion is additionally restricted to files recorded in local state whose Drive metadata still proves
this tool created them inside its folder (fail closed otherwise). Expired files go to Drive's trash.
"""

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import autopilot as ap
import pursuit_clips as pc

DRIVE_DIR = ap.STATE_DIR / "drive"
DRIVE_STATE = DRIVE_DIR / "drive_state.json"
TOKEN_FILE = DRIVE_DIR / "google_token.json"          # OAuth refresh token (0600, never printed)
CLIENT_FILE = DRIVE_DIR / "google_client.json"        # OAuth client id/secret for this desktop app (0600)
STAGING_DIR = DRIVE_DIR / "staging"                   # private copies being delivered (production files may vanish)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_NAME = "PURSUIT - Shorts Ready to Post"
CHANNEL = "https://www.youtube.com/@AnyaPostnikov"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
TOOL_TAG = "pursuit-shorts"                           # appProperties.pursuit_tool on everything we create
RETENTION_DAYS = 14
MAX_SHORTS = 3
TEST_PHRASE = "UPLOAD TEST SHORT"


class DriveError(ap.Stop):
    """Drive problem: nothing further is uploaded or deleted this run."""


# ------------------------------------------------------------------------------ private state

def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def load_state():
    """Separate from the TikTok ledger/queue. A corrupt file stops everything (never guess)."""
    try:
        data = json.loads(DRIVE_STATE.read_text())
    except FileNotFoundError:
        return {"folder_id": None, "deliveries": {}}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise DriveError(f"{DRIVE_STATE} is corrupted ({e}). Nothing was uploaded or deleted. "
                         "Restore it or move it aside, then run ./autopilot drive-status.")
    if not isinstance(data, dict) or not isinstance(data.get("deliveries", {}), dict):
        raise DriveError(f"{DRIVE_STATE} has an unexpected shape. Nothing was uploaded or deleted.")
    data.setdefault("deliveries", {})
    return data


def save_state(state):
    _private_dir(DRIVE_DIR)
    ap.save(DRIVE_STATE, state)          # atomic + fsync + 0600


def _write_secret(path, text):
    _private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


# ------------------------------------------------------------------------------ Google auth + HTTP

def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN_FILE.exists():
        raise DriveError("Google Drive isn't set up yet. Run: ./autopilot drive-setup --client-secret <file>")
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except (ValueError, json.JSONDecodeError) as e:
        raise DriveError(f"Saved Google sign-in is unreadable ({type(e).__name__}). Run ./autopilot drive-setup again.")
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:  # google.auth.exceptions.RefreshError, network errors
            raise DriveError(f"Google sign-in needs renewing ({type(e).__name__}). Run ./autopilot drive-setup again.")
        _write_secret(TOKEN_FILE, creds.to_json())
    return creds


def session():
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(credentials())


class Drive:
    """The handful of Drive v3 calls we need. `http` is a requests-like session (faked in tests)."""

    def __init__(self, http):
        self.http = http

    def _call(self, method, url, expect=(200,), **kw):
        try:
            r = self.http.request(method, url, timeout=120, **kw)
        except Exception as e:
            raise ap.Ambiguous(f"No clear answer from Google Drive ({type(e).__name__}).")
        if r.status_code in (401, 403):
            raise DriveError(f"Google Drive refused the request ({r.status_code}). Run ./autopilot drive-setup again.")
        if r.status_code == 404:
            return None
        if r.status_code >= 500 or r.status_code == 429:
            raise ap.Ambiguous(f"Google Drive is having trouble ({r.status_code}); will retry later.")
        if r.status_code not in expect:
            raise DriveError(f"Google Drive returned {r.status_code} for {method}.")
        return r

    FIELDS = "id,name,parents,appProperties,size,md5Checksum,trashed,createdTime,mimeType"

    def get(self, file_id):
        r = self._call("GET", f"{DRIVE}/files/{file_id}", params={"fields": self.FIELDS})
        return None if r is None else r.json()

    def find(self, parent, key, value, name=None):
        q = [f"appProperties has {{ key='{key}' and value='{value}' }}", "trashed = false"]
        if parent:
            q.append(f"'{parent}' in parents")
        r = self._call("GET", f"{DRIVE}/files", params={"q": " and ".join(q), "fields": f"files({self.FIELDS})",
                                                        "pageSize": 20})
        files = r.json().get("files", [])
        return [f for f in files if name is None or f.get("name") == name]

    def create_folder(self, name, props):
        r = self._call("POST", f"{DRIVE}/files", params={"fields": self.FIELDS},
                       json={"name": name, "mimeType": "application/vnd.google-apps.folder", "appProperties": props})
        return r.json()

    def upload(self, path, name, parent, props, mime):
        meta = {"name": name, "parents": [parent], "appProperties": props}
        r = self._call("POST", f"{UPLOAD}/files", params={"uploadType": "resumable", "fields": self.FIELDS},
                       json=meta, headers={"X-Upload-Content-Type": mime})
        location = r.headers.get("Location")
        if not location:
            raise DriveError("Google Drive didn't return an upload location.")
        with open(path, "rb") as f:
            r = self._call("PUT", location, expect=(200, 201), data=f, headers={"Content-Type": mime})
        return r.json()

    def trash(self, file_id):
        r = self._call("PATCH", f"{DRIVE}/files/{file_id}", params={"fields": self.FIELDS}, json={"trashed": True})
        return None if r is None else r.json()


# ------------------------------------------------------------------------------ folder

def ensure_folder(drive, state):
    """Our one dedicated folder. Created by this app (so drive.file can manage it) and tagged."""
    fid = state.get("folder_id")
    if fid:
        f = drive.get(fid)
        if f and not f.get("trashed") and (f.get("appProperties") or {}).get("pursuit_tool") == TOOL_TAG + "-root":
            return fid
        raise DriveError("The delivery folder recorded locally is missing, trashed, or not ours. "
                         "Nothing was uploaded. Check Drive, then run ./autopilot drive-setup.")
    found = drive.find(None, "pursuit_tool", TOOL_TAG + "-root", FOLDER_NAME)
    if len(found) > 1:
        raise DriveError("More than one PURSUIT delivery folder exists; not guessing which to use.")
    folder = found[0] if found else drive.create_folder(FOLDER_NAME, {"pursuit_tool": TOOL_TAG + "-root"})
    state["folder_id"] = folder["id"]
    save_state(state)
    return folder["id"]


# ------------------------------------------------------------------------------ choosing the Shorts

def _ranges_overlap(a, b):
    return min(a[1], b[1]) - max(a[0], b[0]) > 0


def select_best(candidates, n):
    """Top-scoring QC-passed clips, at most n, never overlapping in time. Shorts need a visible speaker,
    so audio-only (logo card) layouts are left out."""
    chosen = []
    for c in sorted(candidates, key=lambda c: -float(c.get("score") or 0)):
        if c.get("layout", "video") != "video":
            continue
        rng = (float(c["start_sec"]), float(c["end_sec"]))
        if any(_ranges_overlap(rng, (float(x["start_sec"]), float(x["end_sec"]))) for x in chosen):
            continue
        chosen.append(c)
        if len(chosen) == n:
            break
    return chosen


def verify_mp4(path):
    """Valid, fully decodable 1080x1920 H.264 + AAC file."""
    ffmpeg, ffprobe = pc.find_ffmpeg()
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ["not a readable video"]
    v = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    a = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    problems = []
    if len(v) != 1 or v[0].get("codec_name") != "h264" or (v[0].get("width"), v[0].get("height")) != (1080, 1920):
        problems.append("not a 1080x1920 H.264 video")
    if len(a) != 1 or a[0].get("codec_name") != "aac":
        problems.append("no AAC audio")
    dec = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    if dec.returncode != 0 or dec.stderr.strip():
        problems.append("decode errors")
    return problems


def find_ep_dir(ep_id):
    """The production folder for an episode, identified by the video id in its saved metadata."""
    hits = [p.parent.parent for p in ap.OUT_DIR.glob("*/.work/meta.json")
            if ap.load(p, {}).get("id") == ep_id]
    return hits[0] if len(hits) == 1 else None


def qc_passed_from_production(ep_id):
    """Reuse the production QC verdict (read-only) if the autopilot already processed this episode."""
    rec = ap.load(ap.STATE_FILE, {"episodes": {}}).get("episodes", {}).get(ep_id) or {}
    passed_folders = [line.split()[1] for line in rec.get("report") or [] if line.startswith("PASS ")]
    if not passed_folders:
        return None
    ep_dir = find_ep_dir(ep_id)
    if ep_dir is None:
        return None
    rows = {r["folder"]: r for r in _read_csv(ep_dir / "clips.csv")}
    analysis = ap.load(ep_dir / ".work" / "analysis.json", {"clips": []})
    by_title = {c.get("clip_title", ""): c for c in analysis.get("clips", [])}
    meta = ap.load(ep_dir / ".work" / "meta.json", {})
    out = []
    for folder in passed_folders:
        row = rows.get(folder)
        if row and by_title.get(row["clip_title"]):
            out.append(dict(row, start_sec=float(row["start_sec"]), end_sec=float(row["end_sec"]),
                            score=float(row.get("score") or 0), analysis=by_title[row["clip_title"]]))
    return {"ep_dir": str(ep_dir), "meta": meta, "clips": out} if out else None


def _read_csv(path):
    import csv
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _with_lock(fn):
    """Rendering shares the autopilot's lock so it never collides with a scheduled run."""
    _private_dir(ap.STATE_DIR)
    with open(ap.LOCK_FILE, "w") as lock:
        for waited in range(45 * 6):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited == 0:
                    ap.log("Background processing is running; waiting for it to finish...")
                time.sleep(10)
        else:
            raise ap.Stop("Background processing is still running after 45 minutes. Try again later.")
        return fn()


def gather_shorts(ep, n, lock_held=False):
    """Up to n QC-passed, non-overlapping Shorts for this episode with their MP4s on disk.
    Never mutates the TikTok queue, ledger or autopilot episode state."""
    found = qc_passed_from_production(ep["id"])
    if found:
        chosen = select_best(found["clips"], n)
        if all((Path(found["ep_dir"]) / c["file"]).exists() for c in chosen):
            return found["meta"], found["ep_dir"], chosen, None
    # Not processed yet, or chosen MP4s were cleaned up after posting: run the normal render + QC.
    # Transcript and Claude's analysis are cached on disk, so this only (re)renders what's missing.
    def work():
        rec = {"rendered_by_autopilot": True}            # a throwaway record: autopilot state isn't touched
        try:
            summary, passed, report = ap.render_and_check(ep, rec)
        except ap.Transient:
            raise
        except ap.Stop as e:
            if "passed the checks" in str(e) or "suspect" in str(e):
                return None, []                           # a QC verdict: nothing good in this episode
            raise
        return summary, passed
    # auto_step runs inside autopilot's process lock already. Trying to flock the same
    # file through a second descriptor would make the process wait on itself on macOS.
    summary, passed = work() if lock_held else _with_lock(work)
    if summary is None:
        return {"title": ep.get("title", ep["id"]), "id": ep["id"]}, None, [], None
    chosen = select_best(passed, n)
    return summary["meta"], summary["ep_dir"], chosen, summary


# ------------------------------------------------------------------------------ package

def _clean_caption(text):
    """The viewer is already on YouTube: drop 'on YouTube' style call-to-action sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(s for s in sentences if s and not re.search(r"(?i)\bon youtube\b|\bwatch (it|this) on\b", s)).strip()


def youtube_copy(clip, meta):
    ac = clip["analysis"]
    title = (ac.get("youtube_title") or clip.get("clip_title") or "PURSUIT").strip()[:100]
    tags = [re.sub(r"\W", "", h.lstrip("#")) for h in (ac.get("hashtags") or [])][:5]
    tags = [t for t in tags if t] + ["Shorts"]
    ep_url = f"https://www.youtube.com/watch?v={meta['id']}" if meta.get("id") else None
    lines = [_clean_caption(ac.get("caption")), "", "From PURSUIT with Anya Postnikov."]
    if ep_url:
        lines += [f"Full episode: {meta['title']}", ep_url]
    lines += ["", f"More PURSUIT: {CHANNEL}", "", " ".join("#" + t for t in tags)]
    return title, "\n".join(lines).strip(), tags, ep_url


def build_package(meta, ep_dir, chosen, delivery_id, delivered_on):
    """Private copies of the MP4s + POSTING_INFO.txt, so production cleanup can't pull files out from under us."""
    _private_dir(STAGING_DIR)
    stage = STAGING_DIR / delivery_id
    _private_dir(stage)
    raw = str(meta.get("upload_date") or "")
    prefix = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else delivered_on   # unique names in a flat folder
    if delivery_id.endswith("-test"):
        prefix = "TEST " + prefix
    items, blocks = [], []
    expires = (dt.date.fromisoformat(delivered_on) + dt.timedelta(days=RETENTION_DAYS)).isoformat()
    for i, clip in enumerate(chosen, 1):
        src = Path(ep_dir) / clip["file"]
        name = f"{prefix} {i:02d}_{pc.slugify(clip.get('clip_title') or 'short', 50)}.mp4"
        dst = stage / name
        if not dst.exists():
            shutil.copy2(src, dst)
        problems = verify_mp4(dst)
        if problems:
            raise DriveError(f"{name} failed its final check ({'; '.join(problems)}). Nothing uploaded.")
        title, desc, tags, ep_url = youtube_copy(clip, meta)
        items.append({"item": f"{i:02d}", "name": name, "path": str(dst), "md5": _md5(dst), "size": dst.stat().st_size,
                      "start_sec": clip["start_sec"], "end_sec": clip["end_sec"], "clip": clip.get("folder")})
        blocks += [
            "━" * 28, f"SHORT {i} of {len(chosen)}", f"File: {name}", "",
            "TITLE (copy this):", title, "",
            "DESCRIPTION (copy this):", desc, "",
            "Hashtags: " + " ".join("#" + t for t in tags),
            f"From: {meta['title']}",
            f"Episode link: {ep_url or '(unknown)'}",
            f"Moment: {pc.fmt_ts(clip['start_sec'])} – {pc.fmt_ts(clip['end_sec'])}", ""]
    info_name = f"{prefix} POSTING_INFO.txt"
    header = [f"PURSUIT Shorts ready to post ({len(chosen)})", f"Episode: {meta['title']}",
              f"Full episode: {'https://www.youtube.com/watch?v=' + meta['id'] if meta.get('id') else '(unknown)'}",
              f"Prepared {delivered_on}. Files are removed from this folder {RETENTION_DAYS} days after delivery "
              f"(around {expires}).", "",
              "How to post from your phone:",
              "1. Tap a video file below > ⋮ > Download (or Send a copy > Save video).",
              "2. YouTube app > + > Short > pick the video.",
              "3. Copy the TITLE and DESCRIPTION below into YouTube.",
              "4. Optional: under 'Related video', pick the full episode.", ""]
    info = stage / info_name
    info.write_text("\n".join(header + blocks) + "\n", encoding="utf-8")
    items.append({"item": "info", "name": info_name, "path": str(info), "md5": _md5(info), "size": info.stat().st_size})
    return items


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------ upload (idempotent)

def deliver(drive, state, delivery_id, items):
    """Upload each item exactly once. State is saved before and after every upload; a retry first looks the file
    up by its unique tag in Drive and adopts it instead of uploading a second copy."""
    folder = ensure_folder(drive, state)
    rec = state["deliveries"][delivery_id]
    rec.setdefault("files", {})
    for it in items:
        key = f"{delivery_id}:{it['item']}"
        done = rec["files"].get(it["item"])
        if done and done.get("file_id") and done.get("verified"):
            continue
        existing = drive.find(folder, "delivery_key", key)
        if len(existing) > 1:
            raise DriveError(f"Drive has {len(existing)} copies of {it['name']}; not guessing. Remove extras by hand.")
        if existing:
            f = existing[0]
        else:
            rec["files"][it["item"]] = {"name": it["name"], "status": "uploading", "started": ap.now().isoformat()}
            save_state(state)
            mime = "video/mp4" if it["name"].endswith(".mp4") else "text/plain"
            f = drive.upload(it["path"], it["name"], folder,
                             {"pursuit_tool": TOOL_TAG, "delivery_id": delivery_id, "delivery_key": key}, mime)
        f = drive.get(f["id"]) or {}
        ok = (f.get("md5Checksum") == it["md5"] and folder in (f.get("parents") or [])
              and (f.get("appProperties") or {}).get("delivery_key") == key)
        rec["files"][it["item"]] = {"name": it["name"], "file_id": f.get("id"), "size": int(f.get("size") or 0),
                                    "created": f.get("createdTime"), "verified": ok,
                                    "status": "uploaded" if ok else "unverified"}
        save_state(state)
        if not ok:
            raise DriveError(f"{it['name']} is in Drive but doesn't match what was sent; stopping.")
    rec.update(status="delivered", delivered_at=ap.now().isoformat())
    save_state(state)
    shutil.rmtree(STAGING_DIR / delivery_id, ignore_errors=True)       # local copies no longer needed
    return rec


# ------------------------------------------------------------------------------ retention (fail closed)

def expired(rec, days):
    try:
        anchor = rec.get("delivered_at") or rec["created"]
        return ap.now() - dt.datetime.fromisoformat(anchor) > dt.timedelta(days=days)
    except (KeyError, TypeError, ValueError):
        return False


def cleanup(drive, state, days=RETENTION_DAYS):
    """Trash expired files that this tool provably created in its folder. Anything uncertain is left alone."""
    folder = state.get("folder_id")
    removed, kept = [], []
    folder_meta = drive.get(folder) if folder else None
    folder_proven = bool(folder_meta and not folder_meta.get("trashed") and
                         (folder_meta.get("appProperties") or {}).get("pursuit_tool") == TOOL_TAG + "-root")
    for did, rec in state["deliveries"].items():
        if rec.get("status") not in ("delivered", "packaged") or not expired(rec, days):
            continue
        for item, f in rec.get("files", {}).items():
            if f.get("status") == "removed":
                continue
            file_id = f.get("file_id")
            if not file_id and folder_proven:
                # The upload may have succeeded just before a crash/timeout. Reconcile by the
                # unique appProperty key; never use a filename as deletion evidence.
                matches = drive.find(folder, "delivery_key", f"{did}:{item}")
                if len(matches) > 1:
                    f["status"] = "provenance_uncertain"
                    kept.append(f.get("name"))
                    continue
                if matches:
                    file_id = matches[0].get("id")
                    f["file_id"] = file_id
                else:
                    f["status"] = "removed"  # no completed Drive file exists for this item
                    continue
            if not file_id:
                f["status"] = "provenance_uncertain"
                kept.append(f.get("name"))
                continue
            meta = drive.get(file_id)
            if meta is None or meta.get("trashed"):
                f["status"] = "removed"                       # already gone
                continue
            props = meta.get("appProperties") or {}
            provable = (folder_proven and folder in (meta.get("parents") or [])
                        and props.get("pursuit_tool") == TOOL_TAG and props.get("delivery_id") == did
                        and props.get("delivery_key") == f"{did}:{item}" and meta.get("name") == f.get("name"))
            if not provable:
                f["status"] = "provenance_uncertain"
                kept.append(f.get("name"))
                continue
            trashed = drive.trash(file_id)
            if trashed is not None and not trashed.get("trashed"):
                raise DriveError(f"Drive did not confirm that {f.get('name')} was moved to trash; stopping cleanup.")
            f.update(status="removed", removed_at=ap.now().isoformat())
            removed.append(f.get("name"))
        if all(f.get("status") == "removed" for f in rec.get("files", {}).values()):
            rec["status"] = "expired"
        save_state(state)
    if kept:
        ap.notify("PURSUIT Drive cleanup", f"Left {len(kept)} file(s) alone: couldn't prove this tool created them.")
    return removed, kept


def prune_local_staging(state, days=RETENTION_DAYS):
    """Bounded local cleanup: staging copies only (never production clips)."""
    if not STAGING_DIR.exists():
        return 0
    n = 0
    for d in STAGING_DIR.iterdir():
        rec = state["deliveries"].get(d.name, {})
        old = time.time() - d.stat().st_mtime > days * 86400
        if rec.get("status") in ("delivered", "expired") or old:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


# ------------------------------------------------------------------------------ commands

def _latest():
    eps = ap.latest_episodes(limit=5)
    if not eps:
        raise ap.Stop("Couldn't find any PURSUIT episodes on the channel.")
    return eps[0]


def export(ep, n, deliver_now, drive=None, lock_held=False):
    state = load_state()
    delivery_id = ep["id"]
    rec = state["deliveries"].get(delivery_id)
    if rec and rec.get("status") in ("delivered", "expired"):
        return f"Already delivered '{rec.get('title')}' on {rec['delivered_at'][:10]}; not making another copy.", rec
    if rec and rec.get("status") == "no_shorts":
        return f"'{rec.get('title')}' had no clip good enough for a Short; nothing to deliver.", None
    if deliver_now and not state.get("test_verified"):
        raise ap.Stop("Do the one-time confirmed test first: ./autopilot drive-test")
    if rec and rec.get("items") and all(Path(STAGING_DIR / delivery_id / it["name"]).exists() for it in rec["items"]):
        # an interrupted delivery: resume from the exact package that was built (no re-selection, no re-render)
        items = [dict(it, path=str(STAGING_DIR / delivery_id / it["name"])) for it in rec["items"]]
    else:
        meta, ep_dir, chosen, rendered = gather_shorts(ep, n, lock_held=lock_held)
        if not chosen:
            state["deliveries"][delivery_id] = {"episode": ep["id"], "title": meta.get("title", ep["id"]),
                                                "status": "no_shorts", "created": ap.now().isoformat()}
            save_state(state)
            return f"No clip from '{meta.get('title', ep['id'])}' passed QC; nothing to deliver.", None
        rec = state["deliveries"].setdefault(delivery_id, {"episode": ep["id"], "title": meta["title"],
                                                            "status": "packaged", "created": ap.now().isoformat()})
        items = build_package(meta, ep_dir, chosen, delivery_id, rec["created"][:10])
        rec["items"] = [{k: v for k, v in it.items() if k != "path"} for it in items]
        save_state(state)
        if rendered:
            ap.cleanup_unqueued_media(rendered)   # private copies exist; don't leave extra renders on the Mac
    rec = state["deliveries"][delivery_id]
    shorts = sum(1 for it in items if it["name"].endswith(".mp4"))
    if not deliver_now:
        return f"{shorts} Short(s) packaged locally in {STAGING_DIR / delivery_id} (not uploaded).", rec
    drive = drive or Drive(session())
    deliver(drive, state, delivery_id, items)
    return f"Delivered {shorts} Short(s) from '{rec['title']}' to Drive › {FOLDER_NAME}.", rec


def cmd_export_shorts(args):
    if args.which != "latest":
        raise ap.Stop("Only 'latest' is supported: ./autopilot export-shorts latest 3")
    n = max(1, min(MAX_SHORTS, args.count))
    ep = _latest()
    print(f"Latest PURSUIT episode: {ep['title']} ({ep['url']})")
    msg, rec = export(ep, n, args.deliver)
    print(msg)
    for it in (rec or {}).get("items", []):
        print(f"  {it['name']}  ({it['size'] / 1e6:.1f} MB)")


def cmd_drive_setup(args):
    """One-time Google sign-in in your browser (no password is ever typed here)."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if args.client_secret:
        raw = Path(args.client_secret).expanduser().read_text()
        cfg = json.loads(raw)
        if "installed" not in cfg:
            raise ap.Stop("That file isn't a 'Desktop app' OAuth client. Create one of type Desktop app.")
        _write_secret(CLIENT_FILE, raw)
        print(f"OAuth client saved privately. You can delete {args.client_secret} now.")
    if not CLIENT_FILE.exists():
        raise ap.Stop("Run: ./autopilot drive-setup --client-secret ~/Downloads/client_secret_XXXX.json")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    print("A browser window is opening. Sign in with YOUR Google account and allow access.")
    print("(This app can only see and manage files it creates itself.)")
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="", success_message="Done. You can close this tab.")
    _write_secret(TOKEN_FILE, creds.to_json())
    state = load_state()
    fid = ensure_folder(Drive(session()), state)
    print(f"\nGoogle Drive connected. Delivery folder: '{FOLDER_NAME}'")
    print(f"  https://drive.google.com/drive/folders/{fid}")
    print("Share that folder with Anya (Drive > folder > Share). Next: ./autopilot drive-test")


def cmd_drive_test(args, drive=None, ask=input):
    """Controlled test: one Short, shown first, uploaded only after typed confirmation, then verified."""
    import sys
    if ask is input and not sys.stdin.isatty():
        raise ap.Stop("The test upload needs an interactive Terminal.")
    state = load_state()
    ep = _latest()
    meta, ep_dir, chosen, rendered = gather_shorts(ep, 1)
    if not chosen:
        raise ap.Stop("No clip from the latest episode passed QC, so there's nothing to test with.")
    drive = drive or Drive(session())
    folder = ensure_folder(drive, state)
    delivery_id = f"{ep['id']}-test"
    rec = state["deliveries"].setdefault(delivery_id, {"episode": ep["id"], "title": meta["title"] + " (test)",
                                                        "status": "packaged", "created": ap.now().isoformat(),
                                                        "test": True})
    items = build_package(meta, ep_dir, chosen, delivery_id, rec["created"][:10])
    print("\n== CONTROLLED DRIVE TEST (nothing is published anywhere) ==")
    for it in items:
        print(f"  will upload: {it['path']}  ({it['size'] / 1e6:.1f} MB)")
    print(f"  destination: Google Drive › {FOLDER_NAME}  (https://drive.google.com/drive/folders/{folder})")
    if ask(f"\nType {TEST_PHRASE} exactly to upload: ").strip() != TEST_PHRASE:
        print("Cancelled. Nothing was uploaded.")
        return False
    deliver(drive, state, delivery_id, items)
    ok = all(f.get("verified") for f in state["deliveries"][delivery_id]["files"].values())
    if ok:
        state["test_verified"] = ap.now().isoformat()
        save_state(state)
        print("Uploaded and verified in Drive ✓  (the test files follow the normal 14-day retention)")
    return ok


def drive_status_lines(drive=None):
    state = load_state()
    lines = [f"Delivery folder: {FOLDER_NAME}" + (f"  (https://drive.google.com/drive/folders/{state['folder_id']})"
                                                   if state.get("folder_id") else "  (not created yet)")]
    try:
        credentials()
        lines.append("Google Drive auth: OK")
    except ap.Stop as e:
        lines.append(f"Google Drive auth: NOT READY ({e})")
    lines.append(f"Test delivery: {'verified ' + state['test_verified'][:10] if state.get('test_verified') else 'not done'}")
    lines.append(f"Auto delivery on new episodes: {'ON' if state.get('auto') else 'OFF'}")
    live = [(did, r) for did, r in state["deliveries"].items() if r.get("status") == "delivered"]
    files = [f for _, r in live for f in r.get("files", {}).values() if f.get("status") == "uploaded"]
    shorts = [f for f in files if f.get("name", "").endswith(".mp4")]
    lines.append(f"Shorts available in Drive: {len(shorts)}  (~{sum(f.get('size', 0) for f in files) / 1e6:.0f} MB)")
    if live:
        oldest = min(live, key=lambda x: x[1]["delivered_at"])
        lines.append(f"Oldest delivery: {oldest[1]['delivered_at'][:10]}  {oldest[1].get('title', '')}")
        due = sorted(live, key=lambda x: x[1]["delivered_at"])
        lines.append(f"Next cleanup ({RETENTION_DAYS} days after delivery):")
        for did, r in due[:5]:
            when = (dt.datetime.fromisoformat(r["delivered_at"]) + dt.timedelta(days=RETENTION_DAYS)).date()
            lines.append(f"  {when}  {r.get('title', did)}")
    uncertain = [f["name"] for r in state["deliveries"].values() for f in r.get("files", {}).values()
                 if f.get("status") == "provenance_uncertain"]
    if uncertain:
        lines.append(f"Left alone (couldn't prove ownership): {', '.join(uncertain)}")
    return lines


def cmd_drive_status(args):
    print("\n".join(drive_status_lines()))


def cmd_cleanup_drive(args, drive=None):
    state = load_state()
    removed, kept = cleanup(drive or Drive(session()), state)
    pruned = prune_local_staging(state)
    print(f"Moved {len(removed)} expired file(s) to Drive's trash; left {len(kept)} alone; "
          f"removed {pruned} local staging folder(s).")


def cmd_drive_auto(args):
    state = load_state()
    if args.state == "off":
        state["auto"] = False
        save_state(state)
        print("Automatic Drive delivery OFF.")
        return
    if not state.get("test_verified"):
        raise ap.Stop("Do the confirmed test first: ./autopilot drive-test")
    credentials()
    state["auto"] = True
    state["auto_baseline"] = _latest()["id"]          # only episodes uploaded after this get automatic batches
    save_state(state)
    print("Automatic Drive delivery ON: each NEW PURSUIT episode gets one batch of up to 3 Shorts.")


DONE_STATUSES = ("delivered", "expired", "no_shorts")


def pending_episodes(state):
    """New episodes (uploaded after auto-delivery was enabled) that haven't had their one batch yet, oldest first.
    Covers several uploads in a row, e.g. while the Mac was asleep. Test deliveries ("<id>-test") don't count."""
    eps = ap.latest_episodes(limit=10)
    ids = [e["id"] for e in eps]
    base = state.get("auto_baseline")
    newer = eps[:ids.index(base)] if base in ids else eps[:1]   # baseline scrolled off: stay conservative
    return [e for e in reversed(newer) if (state["deliveries"].get(e["id"]) or {}).get("status") not in DONE_STATUSES]


def auto_step():
    """Scheduled-run hook (runs inside the autopilot's lock). Off unless enabled after a verified test.
    1. For each new episode the autopilot has finished processing: package up to 3 QC-passed Shorts as private
       local copies FIRST (so nothing is lost if Drive or Google sign-in is down).
    2. Then connect to Drive, upload what's packaged (idempotent), and run the 14-day cleanup.
    Never renders on its own for an episode the autopilot hasn't processed yet; it simply waits for that."""
    try:
        state = load_state()
    except DriveError as e:
        return f"Drive delivery: STOPPED ({e})"
    if not state.get("auto"):
        return None
    episodes = ap.load(ap.STATE_FILE, {"episodes": {}}).get("episodes", {})
    msgs, ready = [], []
    for ep in pending_episodes(state):
        rec = state["deliveries"].get(ep["id"]) or {}
        if rec.get("status") == "packaged":
            ready.append(ep)
            continue
        prod = episodes.get(ep["id"]) or {}
        if prod.get("status") == "rejected":
            state["deliveries"][ep["id"]] = {"episode": ep["id"], "title": ep.get("title", ep["id"]),
                                             "status": "no_shorts", "created": ap.now().isoformat()}
            save_state(state)
            msgs.append(f"'{ep.get('title')}': no clip passed QC, so no Shorts.")
            continue
        if qc_passed_from_production(ep["id"]) is None:
            continue                                   # autopilot hasn't processed it yet: wait, don't render
        msg, rec = export(ep, MAX_SHORTS, False, lock_held=True)
        state = load_state()
        if (state["deliveries"].get(ep["id"]) or {}).get("status") == "packaged":
            ready.append(ep)
        else:
            msgs.append(msg)                           # e.g. nothing suitable for a Short
    drive = Drive(session())                           # a sign-in problem stops here; packages stay staged
    for ep in ready:
        msg, _ = export(ep, MAX_SHORTS, True, drive=drive, lock_held=True)
        ap.notify("PURSUIT Shorts delivered", msg)
        msgs.append(msg)
    state = load_state()
    cleanup(drive, state)
    prune_local_staging(state)
    return " | ".join(msgs) or None
