#!/usr/bin/env python3
"""PURSUIT clips: turn a full episode into ready-to-post vertical clips.

    ./pursuit-clips "https://www.youtube.com/watch?v=..."
    ./pursuit-clips "https://www.youtube.com/watch?v=..." --video ~/Desktop/original.mp4
    ./pursuit-clips ~/Desktop/episode.mp4

Pipeline: download (yt-dlp) -> transcribe (mlx-whisper, word timings) ->
pick moments (Codex CLI; Claude optional fallback) -> cut, reframe, caption, normalize (FFmpeg).
Every step is cached in <episode>/.work, so re-running resumes where it stopped.
"""

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import llm

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"
PROMPT_FILE = HERE / "clip_prompt.md"
DEFAULT_OUT = Path.home() / "Desktop" / "PURSUIT_CLIPS"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@AnyaPostnikov"
YOUTUBE_CTA = f"Watch PURSUIT on YouTube: {YOUTUBE_CHANNEL_URL}"

MAX_CLIPS = 10          # never render more than this
MIN_CLIPS = 5           # render at least this many if the AI found them...
MIN_SCORE = 60          # ...otherwise only clips scoring at least this

W, H = 1080, 1920       # output size

# Caption look. Colors are ASS format: &HAABBGGRR (alpha, blue, green, red).
CAPTION_FONT = "Montserrat ExtraBold"
CAPTION_SIZE = 86
CAPTION_Y = 1340                     # vertical center of the caption line (px from top)
CAPTION_MAX_WORDS = 4
CAPTION_MAX_CHARS = 18
HIGHLIGHT = "&H0AD6FF&"              # current word: warm yellow (#FFD60A)
HOOK_SECONDS = 3.2

FFMPEG_CANDIDATES = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg"]


# ----------------------------------------------------------------------------- utils

class Fail(Exception):
    """An error with a message meant for a human."""


def add_youtube_cta(caption):
    """Give every social caption one consistent route back to PURSUIT on YouTube."""
    caption = (caption or "").strip()
    caption = re.sub(r"\s*Full episode of PURSUIT on YouTube\.?\s*$", "", caption, flags=re.IGNORECASE).strip()
    if YOUTUBE_CHANNEL_URL in caption:
        return caption
    return f"{caption}\n\n{YOUTUBE_CTA}" if caption else YOUTUBE_CTA


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-15:])
        raise Fail(f"Command failed: {Path(cmd[0]).name}\n{tail}")
    return r


def write_text_atomic(path, text):
    """Do not leave a valid-looking cache file behind if the process is interrupted."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path, data, **kwargs):
    write_text_atomic(path, json.dumps(data, **kwargs))


def fmt_ts(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def slugify(text, maxlen=60):
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", text)[:maxlen].strip("-") or "clip"


def safe_folder_name(text, maxlen=80):
    text = re.sub(r'[\\/:*?"<>|]+', "", text).strip()
    return re.sub(r"\s+", " ", text)[:maxlen].strip() or "episode"


def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


# ----------------------------------------------------------------------------- deps

def find_ffmpeg():
    for c in FFMPEG_CANDIDATES:
        if os.path.exists(c):
            return c, str(Path(c).with_name("ffprobe"))
    ff = shutil.which("ffmpeg")
    if ff:
        filters = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if re.search(r"\bass\b", filters):
            return ff, shutil.which("ffprobe") or "ffprobe"
    raise Fail("FFmpeg with subtitle support is missing.\nFix: brew install ffmpeg-full   (or re-run ./setup.sh)")


def check_deps(need_download):
    ffmpeg, ffprobe = find_ffmpeg()
    if need_download:
        if not shutil.which("yt-dlp"):
            raise Fail("yt-dlp is missing.\nFix: brew install yt-dlp   (or re-run ./setup.sh)")
        if not shutil.which("deno"):
            log("WARNING: deno is not installed; YouTube downloads may fail. Fix: brew install deno")
    try:
        import mlx_whisper  # noqa: F401
        import cv2  # noqa: F401
    except ImportError as e:
        raise Fail(f"Python package missing ({e.name}). Run ./setup.sh (and use ./pursuit-clips, not python3 directly).")
    if not (FONTS_DIR / "Montserrat-ExtraBold.ttf").exists():
        raise Fail(f"Caption font missing from {FONTS_DIR}. Re-download the project folder.")
    return ffmpeg, ffprobe


# ----------------------------------------------------------------------------- 1. source

def fetch_info(url):
    log("Reading episode info from YouTube...")
    r = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--no-warnings", url], capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise Fail(f"Couldn't open that YouTube link.\n{msg}\nCheck the URL. If it's correct, update the downloader: brew upgrade yt-dlp deno")
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise Fail(f"yt-dlp returned invalid episode metadata ({e}). Try: brew upgrade yt-dlp deno")
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Episode",
        "url": info.get("webpage_url") or url,
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "description": (info.get("description") or "")[:2500],
        "chapters": [{"start": c.get("start_time"), "title": c.get("title")} for c in (info.get("chapters") or [])],
    }


def download_video(url, work, ffmpeg):
    existing = [p for p in glob.glob(str(work / "source.*")) if not p.endswith((".part", ".ytdl"))]
    if existing:
        return Path(existing[0])
    log("Downloading best-quality video (this can take a few minutes)...")
    cmd = ["yt-dlp", "--no-playlist", "--newline", "--no-warnings",
           "-f", "bv*[height<=2160]+ba/b", "--merge-output-format", "mkv",
           "--ffmpeg-location", str(Path(ffmpeg).parent),
           "-o", str(work / "source.%(ext)s"), url]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-8:])
        raise Fail("Download failed. Usually fixed by updating yt-dlp:  brew upgrade yt-dlp deno\n" + tail)
    files = [p for p in glob.glob(str(work / "source.*")) if not p.endswith((".part", ".ytdl"))]
    if not files:
        raise Fail("Download finished but no video file was found.")
    return Path(files[0])


def probe(ffprobe, path):
    r = run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if not a:
        raise Fail(f"{path.name} has no audio track.")
    if not v:
        raise Fail(f"{path.name} has no video track. (Audio-only files aren't supported; use the YouTube URL.)")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return {"w": w, "h": h, "fps": fps, "duration": float(d["format"]["duration"])}


# ----------------------------------------------------------------------------- 2. transcript

def transcribe(src, work, ffmpeg):
    out = work / "transcript.json"
    if out.exists():
        return json.loads(out.read_text())
    wav = work / "audio16k.wav"
    log("Extracting audio...")
    run([ffmpeg, "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    log("Transcribing with Whisper on the Mac's GPU (~7 min per hour of audio)...")
    import mlx_whisper
    t0 = time.time()
    res = mlx_whisper.transcribe(
        str(wav), path_or_hf_repo=WHISPER_MODEL, word_timestamps=True, language="en",
        condition_on_previous_text=False,
        initial_prompt="PURSUIT with Anya Postnikov. Guests have included Samo Karplus. A podcast about ambition, running, ultramarathons, Ironman, relationships and purpose.",
    )
    segments, words = [], []
    for seg in res["segments"]:
        text = seg["text"].strip()
        if not text:
            continue
        segments.append({"start": round(float(seg["start"]), 2), "end": round(float(seg["end"]), 2), "text": text})
        for w in seg.get("words", []):
            t = w["word"].strip()
            if t:
                words.append({"w": t, "s": round(float(w["start"]), 3), "e": round(float(w["end"]), 3)})
    if len(words) < 50:
        raise Fail("Transcription produced almost no words. Is there speech in this video?")
    data = {"segments": segments, "words": words}
    write_json_atomic(out, data)
    wav.unlink(missing_ok=True)
    log(f"Transcribed {len(words)} words in {time.time() - t0:.0f}s.")
    return data


# ----------------------------------------------------------------------------- 3. analysis

def build_prompt(meta, transcript):
    lines = [f"Title: {meta['title']}"]
    if meta.get("description"):
        lines.append(f"Description:\n{meta['description']}")
    if meta.get("chapters"):
        lines.append("Chapters: " + "; ".join(f"{fmt_ts(c['start'] or 0)} {c['title']}" for c in meta["chapters"]))
    lines.append("\n## Transcript\n")
    lines += [f"[{s['start']:.1f}] {s['text']}" for s in transcript["segments"]]
    return PROMPT_FILE.read_text() + "\n" + "\n".join(lines) + "\n"


def extract_json(text):
    """Find and validate the first complete clip object in a CLI or pasted reply."""
    decoder = json.JSONDecoder()
    last_error = None
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError as e:
            last_error = e
            continue
        return validate_analysis(data)
    raise ValueError(f"no valid clip JSON object in reply{f' ({last_error})' if last_error else ''}")


def validate_analysis(data):
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list) or not data["clips"]:
        raise ValueError("reply has no clips")
    cleaned = []
    for n, clip in enumerate(data["clips"], 1):
        if not isinstance(clip, dict):
            raise ValueError(f"clip {n} is not an object")
        try:
            start, end = float(clip["start"]), float(clip["end"])
            overall = float(clip.get("overall", 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"clip {n} needs numeric start, end, and overall values")
        if not all(math.isfinite(x) for x in (start, end, overall)) or start < 0 or end <= start:
            raise ValueError(f"clip {n} has invalid timing or score")
        if not isinstance(clip.get("start_words"), str) or not isinstance(clip.get("end_words"), str):
            raise ValueError(f"clip {n} needs start_words and end_words")
        normalized = dict(clip)
        normalized.update(start=start, end=end, overall=overall)
        normalized["rank"] = clip.get("rank") if isinstance(clip.get("rank"), int) else n
        normalized["scores"] = clip.get("scores") if isinstance(clip.get("scores"), dict) else {}
        normalized["hashtags"] = clip.get("hashtags") if isinstance(clip.get("hashtags"), list) else []
        cleaned.append(normalized)
    result = dict(data)
    result["clips"] = cleaned
    return result


def ask_llm(prompt, timeout=900, image=None):
    """One prompt to the configured AI (Codex by default, Claude as optional fallback). See llm.py."""
    try:
        return llm.ask(prompt, timeout=timeout, image=image, log=log)
    except llm.LLMUnavailable as e:
        raise Fail(f"No AI is available right now ({e}).\n"
                   "Codex: install it and run `codex login` (choose 'Sign in with ChatGPT'). "
                   "If you hit your Codex usage limit, try again after it resets.")
    except llm.LLMError as e:
        raise Fail(str(e))


def preflight_llm():
    """Fail in seconds (not after a 10-minute download) if no AI can be used. Makes no model call."""
    if not llm.available():
        raise Fail(f"No AI is available right now ({llm.explain_unavailable()}).\n"
                   "Fix: install Codex and run `codex login` (choose 'Sign in with ChatGPT'), then re-run.")


def analyze(meta, transcript, work, mode):
    out = work / "analysis.json"
    if out.exists():
        try:
            return extract_json(out.read_text())   # tolerant of pasted replies with extra text/```fences
        except (ValueError, json.JSONDecodeError) as e:
            raise Fail(f"{out} isn't valid clip JSON ({e}). Fix or delete it, then run again.")
    prompt = build_prompt(meta, transcript)
    prompt_file = work / "clip_prompt.txt"
    write_text_atomic(prompt_file, prompt)
    if mode == "manual" or not llm.available():
        raise Fail(
            "No AI is available, so I can't pick the clips automatically "
            f"({llm.explain_unavailable()}).\n"
            "Either install Codex (./setup.sh does this, then run `codex login` once),\n"
            f"or do it by hand: paste the contents of\n  {prompt_file}\n"
            f"into any AI chat, save its JSON reply as\n  {out}\nand run the same command again.")
    log("Asking the AI to find the best moments (1–3 min)...")
    last_err = None
    for attempt in range(2):
        reply = ask_llm(prompt if attempt == 0 else
                        prompt + "\n\nIMPORTANT: respond with ONLY the JSON object, nothing else.")
        try:
            data = extract_json(reply)
            write_json_atomic(out, data, indent=2)
            return data
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            write_text_atomic(work / "llm_reply_unparsed.txt", reply)
    raise Fail(f"Couldn't read the AI's reply as JSON ({last_err}). Raw reply saved in {work}.")


# ----------------------------------------------------------------------------- 4. timing

def _norm(t):
    return re.sub(r"[^a-z0-9']", "", t.lower().replace("’", "'"))


def _match_at(words, i, target):
    """Fraction of `target` tokens matching words starting at index i."""
    if i < 0 or i + len(target) > len(words):
        return 0.0
    return sum(_norm(words[i + k]["w"]) == target[k] for k in range(len(target))) / len(target)


def _find(words, approx_t, phrase, window, want_end):
    target = [t for t in (_norm(x) for x in (phrase or "").split()) if t][:8]
    if want_end:
        target = [t for t in (_norm(x) for x in (phrase or "").split()) if t][-8:]
    nearest = min(range(len(words)), key=lambda i: abs((words[i]["e"] if want_end else words[i]["s"]) - approx_t))
    if not target:
        return nearest
    best, best_score = nearest, 0.0
    for i in range(len(words)):
        t = words[i]["s"]
        if abs(t - approx_t) > window:
            continue
        j = i - len(target) + 1 if want_end else i
        sc = _match_at(words, j, target) - abs(t - approx_t) / (window * 20)  # tie-break: closer wins
        if sc > best_score:
            best, best_score = i, sc
    return best if best_score >= 0.5 else nearest


def snap_clip(clip, words):
    """Turn the AI's approximate times into exact word boundaries with a little breathing room."""
    i = _find(words, float(clip["start"]), clip.get("start_words"), 25, want_end=False)
    j = _find(words, float(clip["end"]), clip.get("end_words"), 25, want_end=True)
    if j <= i:
        return None
    prev_end = words[i - 1]["e"] if i > 0 else 0.0
    start = max(0.0, min(words[i]["s"], max(words[i]["s"] - 0.30, prev_end + 0.02)))
    next_start = words[j + 1]["s"] if j + 1 < len(words) else words[j]["e"] + 1.0
    end = max(words[j]["e"] + 0.02, min(words[j]["e"] + 0.35, next_start - 0.03))
    return start, end, words[i:j + 1]


# ----------------------------------------------------------------------------- 5. framing

def sample_frames(ffmpeg, src, start, dur, n=10):
    """n small grayscale frames spread across the clip, as numpy arrays."""
    import numpy as np
    frames = []
    for k in range(n):
        t = start + dur * (k + 0.5) / n
        r = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", "scale=640:-2,format=gray", "-f", "rawvideo", "-"], capture_output=True)
        if r.returncode == 0 and r.stdout:
            frames.append(r.stdout)
    if not frames:
        return []
    # figure out height from byte count (width is 640)
    return [np.frombuffer(f, dtype=np.uint8).reshape(-1, 640) for f in frames]


def choose_layout(ffmpeg, src, info, start, dur, band=(0.0, 1.0)):
    """Returns ("audio", None) for static-image episodes, else ("video", crop_x_in_source_pixels)."""
    import cv2
    import numpy as np
    frames = sample_frames(ffmpeg, src, start, dur)
    if len(frames) >= 2:
        diffs = [float(np.mean(cv2.absdiff(frames[k], frames[k + 1]))) for k in range(len(frames) - 1)]
        if max(diffs) < 1.5:
            return "audio", None
    crop_w = info["h"] * (band[1] - band[0]) * 9 / 16
    if info["w"] <= crop_w + 2:  # already vertical (or narrower)
        return "video", None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    centers = []
    for f in frames:
        faces = cascade.detectMultiScale(f, scaleFactor=1.1, minNeighbors=6, minSize=(f.shape[0] // 10,) * 2)
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda b: b[2] * b[3])
            centers.append((x + fw / 2) / f.shape[1])
    cx = float(np.median(centers)) if len(centers) >= max(2, len(frames) // 3) else 0.5
    x = cx * info["w"] - crop_w / 2
    return "video", int(max(0, min(info["w"] - crop_w, x)))


# ----------------------------------------------------------------------------- 6. captions

def _ass_escape(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def _ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def group_words(words):
    groups, cur = [], []
    for k, w in enumerate(words):
        cur.append(w)
        text_len = len(" ".join(x["w"] for x in cur))
        nxt = words[k + 1] if k + 1 < len(words) else None
        brk = (
            nxt is None
            or len(cur) >= CAPTION_MAX_WORDS
            or w["w"][-1] in ".?!"
            or (w["w"][-1] in ",;:" and len(cur) >= 2)
            or nxt["s"] - w["e"] > 0.6
            or text_len + 1 + len(nxt["w"]) > CAPTION_MAX_CHARS and len(cur) >= 2
        )
        if brk:
            groups.append(cur)
            cur = []
    return groups


def display_word(t):
    t = t.strip()
    t = re.sub(r"^i(?=$|['’][a-z]+$|[,.?!]$)", "I", t)   # whisper sometimes writes "i", "i'm"
    return t.rstrip(",.;:") if len(t) > 1 else t   # keep ? and !, drop trailing commas/periods


def build_ass(words, clip_start, clip_dur, hook, audio_mode, captions=True):
    ws = [{"w": w["w"], "s": w["s"] - clip_start, "e": w["e"] - clip_start} for w in words]
    if audio_mode:
        primary, outline, shadow, bord, shad, base_hl, y = "&H00141414", "&H00FFFFFF", "&H00FFFFFF", 0, 0, "&H2C34D4&", 1010
    else:
        primary, outline, shadow, bord, shad, base_hl, y = "&H00FFFFFF", "&H00000000", "&H96000000", 6, 3, HIGHLIGHT, CAPTION_Y
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{CAPTION_FONT},{CAPTION_SIZE + (10 if audio_mode else 0)},{primary},{primary},{outline},{shadow},0,0,0,0,100,100,0,0,1,{bord},{shad},5,90,90,0,1
Style: Hook,{CAPTION_FONT},68,&H00141414,&H00141414,&H00FFFFFF,&H64000000,0,0,0,0,100,100,0,0,3,26,0,8,100,100,{230 if not audio_mode else 190},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    groups = group_words(ws) if captions else []
    for gi, g in enumerate(groups):   # capitalize the first word of each sentence
        prev = groups[gi - 1][-1]["w"] if gi else "."
        if prev[-1] in ".?!" and g[0]["w"][:1].islower():
            g[0]["w"] = g[0]["w"][0].upper() + g[0]["w"][1:]
    for gi, g in enumerate(groups):
        # the next group appears 0.08s before its first word, so end this one before that (no overlapping text)
        g_end_limit = max(0.0, groups[gi + 1][0]["s"] - 0.08) if gi + 1 < len(groups) else clip_dur
        for k, w in enumerate(g):
            s = w["s"] if k > 0 else max(0.0, w["s"] - 0.08)
            if k + 1 < len(g):
                e = g[k + 1]["s"]
            else:  # last word of group: hold briefly, but never overlap the next group
                e = min(max(w["e"], w["s"] + 0.25) + 0.35, g_end_limit)
            if e - s < 0.04:
                e = s + 0.04
            parts = []
            for m, x in enumerate(g):
                t = _ass_escape(display_word(x["w"]))
                parts.append(f"{{\\c{base_hl}}}{t}{{\\r}}" if m == k else t)
            events.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{{\\pos({W // 2},{y})}}" + " ".join(parts))
    if hook:
        hook_end = min(HOOK_SECONDS, clip_dur - 0.5)
        events.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(hook_end)},Hook,,0,0,0,,{{\\fad(120,250)}}{_ass_escape(hook.strip())}")
    return header + "\n".join(events) + "\n"


# ----------------------------------------------------------------------------- 7. render

def measure_loudness(ffmpeg, src, start, dur):
    r = subprocess.run([ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-vn",
                        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    try:
        valid = all(math.isfinite(float(d.get(k))) for k in required)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return None
    return d


def render_clip(ffmpeg, src, info, start, end, ass_path, layout, crop_x, out_path, band=(0.0, 1.0)):
    dur = end - start
    fps = min(60.0, info["fps"] or 30.0)
    if layout == "audio":
        # static-image episode: brand card on a light background + animated waveform + big captions
        vf = (f"color=c=0xF7F5F0:s={W}x{H}:r={fps:.3f}:d={dur:.3f}[bg];"
              f"[0:v]scale={W}:-2:flags=lanczos,format=yuv420p[card];"
              f"[0:a]aformat=channel_layouts=mono,volume=4,showwaves=s=860x220:mode=cline:rate={fps:.3f}:colors=0x141414:scale=sqrt,format=rgba,colorchannelmixer=aa=0.8[wave];"
              f"[bg][card]overlay=0:{int(H * 0.16)}:shortest=1[t1];"
              f"[t1][wave]overlay=(W-w)/2:{int(H * 0.66)}:shortest=1[t2];"
              f"[t2]ass='{ass_path}':fontsdir='{ass_path.parent}',format=yuv420p[v]")
    else:
        # band = (top, bottom) fractions of the frame height to use, e.g. (0.10, 0.80) drops burned-in captions
        crop_h = int(round(info["h"] * (band[1] - band[0]) / 2)) * 2
        crop_w = int(round(crop_h * 9 / 16 / 2)) * 2
        crop_y = int(round(info["h"] * band[0] / 2)) * 2
        if crop_x is None:  # source already vertical-ish: retain its width, but honor any requested vertical band
            band_crop = "" if band == (0.0, 1.0) else f"crop=iw:{crop_h}:0:{crop_y},"
            geo = f"{band_crop}scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
        else:
            geo = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={W}:{H}:flags=lanczos"
        vf = f"[0:v]{geo},setsar=1,ass='{ass_path}':fontsdir='{ass_path.parent}',format=yuv420p[v]"
    ln = measure_loudness(ffmpeg, src, start, dur)
    if ln:
        af = (f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:"
              f"measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:offset={ln['target_offset']}:linear=true")
    else:
        af = "loudnorm=I=-14:TP=-1.5:LRA=11"
    af += ",aresample=48000,afade=t=in:d=0.015," + f"afade=t=out:st={max(0, dur - 0.12):.3f}:d=0.12"
    vf += f";[0:a]{af}[a]"
    cmd = [ffmpeg, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]", "-map", "[a]",
           "-r", f"{fps:.3f}", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", "-t", f"{dur:.3f}", str(out_path)]
    run(cmd)


def verify_output(ffprobe, path, expected_dur):
    info = probe(ffprobe, path)
    if (info["w"], info["h"]) != (W, H):
        raise Fail(f"{path.name}: wrong size {info['w']}x{info['h']}")
    if abs(info["duration"] - expected_dur) > 0.6:
        raise Fail(f"{path.name}: duration {info['duration']:.1f}s, expected {expected_dur:.1f}s")


# ----------------------------------------------------------------------------- 8. copy

def write_copy(folder, clip, meta, start, end, transcript_text):
    url = meta.get("url")
    ts_url = f"https://www.youtube.com/watch?v={meta['id']}&t={int(start)}s" if meta.get("id") else None
    tags = " ".join("#" + re.sub(r"[^\w]", "", h.lstrip("#")) for h in clip.get("hashtags") or [])
    lines = [
        "CLIP TITLE:", clip.get("clip_title", ""), "",
        "YOUTUBE SHORT TITLE:", clip.get("youtube_title", ""), "",
        "INSTAGRAM/TIKTOK CAPTION:", add_youtube_cta(clip.get("caption", "")), "",
        "OPTIONAL HASHTAGS:", tags, "",
        "ON-SCREEN HOOK:", clip.get("onscreen_hook") or "(none, the spoken opening is the hook)", "",
        "SOURCE:", meta["title"],
    ]
    if url:
        lines.append(url)
    lines.append(f"Timestamp: {fmt_ts(start)} – {fmt_ts(end)} ({end - start:.0f}s)")
    if ts_url:
        lines.append(f"Jump to moment: {ts_url}")
    lines += ["", "WHY THIS CLIP:", clip.get("why", ""), "",
              f"AI SCORE: {clip.get('overall', '?')}/100   (hook {clip.get('scores', {}).get('hook', '?')}, "
              f"standalone {clip.get('scores', {}).get('standalone', '?')}, value {clip.get('scores', {}).get('value', '?')}, "
              f"retention {clip.get('scores', {}).get('retention', '?')}, curiosity {clip.get('scores', {}).get('curiosity', '?')})",
              "", "TRANSCRIPT:", transcript_text, ""]
    if url:
        lines += ["TIP: on YouTube, set this Short's \"Related video\" to the full episode (YouTube Studio > the Short > Related video).", ""]
    write_text_atomic(folder / "copy.txt", "\n".join(lines))
    return ts_url


# ----------------------------------------------------------------------------- main

def select_clips(analysis):
    clips = sorted(analysis["clips"], key=lambda c: (-(c.get("overall") or 0), c.get("rank") or 99))
    good = [c for c in clips if (c.get("overall") or 0) >= MIN_SCORE]
    if len(good) < MIN_CLIPS:
        good = clips[:MIN_CLIPS]
    return good[:MAX_CLIPS]


def main():
    ap = argparse.ArgumentParser(description="Turn a PURSUIT episode into vertical short-form clips.")
    ap.add_argument("source", help="YouTube URL of the episode, or a local video file")
    ap.add_argument("--video", help="local original video file to use instead of downloading (with a YouTube URL)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"output folder (default: {DEFAULT_OUT})")
    ap.add_argument("--max", type=int, default=MAX_CLIPS, help="maximum clips to render (default 10)")
    ap.add_argument("--redo", action="store_true", help="ask the AI again for new clip picks (keeps download + transcript)")
    ap.add_argument("--no-captions", action="store_true", help="don't add captions (for videos that already have captions burned in)")
    ap.add_argument("--crop-band", default="0:1",
                    help="use only part of the frame height, TOP:BOTTOM, e.g. 0.10:0.80 to drop captions burned into the video")
    ap.add_argument("--force-render", action="store_true", help=argparse.SUPPRESS)  # used for autopilot crop correction
    ap.add_argument("--keep-source", action="store_true", help="keep the downloaded full video afterwards (~1-3 GB)")
    ap.add_argument("--summary-json", help=argparse.SUPPRESS)  # used by autopilot.py
    ap.add_argument("--llm", default="auto", choices=["auto", "codex", "claude", "manual"], help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        try:
            band = tuple(float(x) for x in args.crop_band.split(":"))
            assert len(band) == 2 and 0 <= band[0] < band[1] <= 1 and band[1] - band[0] >= 0.5
        except (ValueError, AssertionError):
            raise Fail("--crop-band must look like 0.10:0.80 (top:bottom, at least half the frame).")
        if not 1 <= args.max <= MAX_CLIPS:
            raise Fail(f"--max must be between 1 and {MAX_CLIPS}.")
        url = args.source if is_url(args.source) else None
        local = Path(args.video or (None if url else args.source)).expanduser() if (args.video or not url) else None
        if local and not local.exists():
            raise Fail(f"File not found: {local}")
        ffmpeg, ffprobe = check_deps(need_download=bool(url))

        meta = fetch_info(url) if url else {"id": None, "title": local.stem, "url": None, "upload_date": None,
                                            "description": "", "chapters": []}
        date = meta.get("upload_date")
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if date else dt.date.today().isoformat()
        ep_dir = Path(args.out).expanduser() / f"{date} {safe_folder_name(meta['title'])}"
        work = ep_dir / ".work"
        work.mkdir(parents=True, exist_ok=True)
        write_json_atomic(work / "meta.json", meta, indent=2)
        log(f"Episode: {meta['title']}")
        log(f"Output:  {ep_dir}")

        if args.redo:
            (work / "analysis.json").unlink(missing_ok=True)
        if args.llm in ("codex", "claude"):
            os.environ["PURSUIT_LLM"] = args.llm
        if args.llm != "manual" and not (work / "analysis.json").exists():
            preflight_llm()

        src = local if local else download_video(url, work, ffmpeg)
        info = probe(ffprobe, src)
        log(f"Source: {info['w']}x{info['h']} @ {info['fps']:.0f}fps, {fmt_ts(info['duration'])}")

        transcript = transcribe(src, work, ffmpeg)
        analysis = analyze(meta, transcript, work, args.llm)
        chosen = select_clips(analysis)[: args.max]
        log(f"The AI found {len(analysis['clips'])} candidates; rendering the best {len(chosen)}.")

        # FFmpeg filter strings choke on quotes/colons in paths, so subtitles + font live in a plain temp folder
        temp_context = tempfile.TemporaryDirectory(prefix="pursuit_")
        safe_tmp = Path(temp_context.name)
        shutil.copy(FONTS_DIR / "Montserrat-ExtraBold.ttf", safe_tmp)
        words, rows, used = transcript["words"], [], []
        for n, clip in enumerate(chosen, 1):
            snapped = snap_clip(clip, words)
            if not snapped:
                log(f"  skipping '{clip.get('clip_title')}': couldn't locate it in the transcript")
                continue
            start, end, cw = snapped
            if end - start < 8 or end - start > 150:
                log(f"  skipping '{clip.get('clip_title')}': odd length {end - start:.0f}s")
                continue
            if any(start < ue and end > us for us, ue in used):
                log(f"  skipping '{clip.get('clip_title')}': overlaps another clip")
                continue
            used.append((start, end))
            num = len(rows) + 1
            name = f"{num:02d}_{slugify(clip.get('clip_title') or 'clip', 50)}"
            folder = ep_dir / name
            folder.mkdir(parents=True, exist_ok=True)
            layout, crop_x = choose_layout(ffmpeg, src, info, start, end - start, band)
            ass_path = safe_tmp / f"{num:02d}.ass"
            write_text_atomic(ass_path, build_ass(cw, start, end - start, clip.get("onscreen_hook"), layout == "audio",
                                                  captions=not args.no_captions))
            mp4 = folder / f"{name}.mp4"
            log(f"  [{num}/{len(chosen)}] {clip.get('clip_title')}  ({fmt_ts(start)}, {end - start:.0f}s, "
                f"score {clip.get('overall')}, {'audio-only layout' if layout == 'audio' else 'vertical crop'})")
            reuse = False
            if mp4.exists() and not args.redo and not args.force_render:
                try:
                    verify_output(ffprobe, mp4, end - start)
                    reuse = True
                    log("    reusing finished MP4 from the previous run")
                except Fail:
                    pass
            if not reuse:
                partial = mp4.with_name(mp4.stem + ".part.mp4")
                partial.unlink(missing_ok=True)
                render_clip(ffmpeg, src, info, start, end, ass_path, layout, crop_x, partial, band)
                verify_output(ffprobe, partial, end - start)
                partial.replace(mp4)
            text = " ".join(w["w"] for w in cw)
            ts_url = write_copy(folder, clip, meta, start, end, text)
            rows.append({
                "rank": num, "folder": name, "file": f"{name}/{name}.mp4", "score": clip.get("overall"),
                "start": fmt_ts(start), "end": fmt_ts(end), "start_sec": round(start, 2), "end_sec": round(end, 2),
                "duration_sec": round(end - start, 1), "category": clip.get("category", ""),
                "clip_title": clip.get("clip_title", ""), "youtube_title": clip.get("youtube_title", ""),
                "onscreen_hook": clip.get("onscreen_hook") or "", "layout": layout,
                "link_at_moment": ts_url or "", "why": clip.get("why", ""),
            })

        if not rows:
            raise Fail("No clips could be rendered. Try again with --redo.")
        csv_tmp = work / "clips.csv.tmp"
        with open(csv_tmp, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)
        csv_tmp.replace(ep_dir / "clips.csv")
        keep_folders = {row["folder"] for row in rows}
        for old in ep_dir.glob("[0-9][0-9]_*"):
            if old.is_dir() and old.name not in keep_folders:
                shutil.rmtree(old)
        if url and not args.keep_source and not local:
            for p in glob.glob(str(work / "source.*")):
                os.remove(p)
        temp_context.cleanup()
        log(f"Done: {len(rows)} clips in {ep_dir}")
        if args.summary_json:
            write_json_atomic(args.summary_json, {
                "ep_dir": str(ep_dir), "meta": meta, "clips": rows, "captions": not args.no_captions, "crop_band": args.crop_band,
                "candidates": len(analysis["clips"]), "transcript": str(work / "transcript.json"),
            }, indent=2)
        if not os.environ.get("PURSUIT_NO_OPEN"):
            subprocess.run(["open", str(ep_dir)], capture_output=True)
    except Fail as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to resume.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
