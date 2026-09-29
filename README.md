# Booboo Expansion Project

An automated podcast growth system built for **PURSUIT**.

The goal is simple: turn long-form podcast episodes into polished short-form discovery content with as little weekly work as possible.

```text
YouTube podcast episode
        -> detect and download
        -> transcribe with MLX Whisper
        -> Claude selects promising moments
        -> FFmpeg creates 9:16 clips
        -> captions and audio normalization
        -> technical and visual quality control
        -> approved buffer (never the same moment twice)
        -> Post for Me, up to 5 per day
        -> verified TikTok @pursuitthepod (Instagram Reels / YouTube Shorts once connected)
```

Old episodes keep it supplied; new uploads are detected automatically and get priority.

This is a small, local macOS command-line tool. It is not a web app and it does not create social accounts, bypass OAuth, manage comments, or modify existing channel content.

## What It Does

- Works through the PURSUIT back catalog and detects new uploads automatically.
- Downloads the source with `yt-dlp`.
- Transcribes locally with `mlx-whisper`, including word-level timestamps.
- Asks the Claude Code CLI to find interesting, standalone moments with fast openings and complete endings.
- Uses FFmpeg and OpenCV to create 1080x1920 H.264/AAC clips, frame the speaker, burn highlighted ASS captions, and normalize audio to -14 LUFS.
- Handles static-image/audio-only episodes with an audiogram layout.
- Rejects clips that fail technical, editorial, caption, framing, or audio checks.
- Can leave the finished clips for manual posting or schedule approved clips through Post for Me.

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS and Homebrew
- A Claude subscription with the Claude Code CLI logged in
- Approximately 2 GB for the Python environment and Whisper model, plus temporary space while an episode is processed
- For autopilot posting: your own Post for Me account/API key and your own YouTube, Instagram, and TikTok accounts
- Instagram must be a Professional account (Creator or Business) for API publishing

The core clipping workflow has no per-episode API bill beyond services you already use. Autopilot posting requires a Post for Me plan; pricing can change, so check its current pricing before subscribing. Claude Code usage is subject to your Claude plan limits.

## Installation

```bash
cd ~/Documents/PURSUIT_CLIPS_TOOL
./setup.sh
```

The setup script installs FFmpeg, `yt-dlp`, Deno, Python 3.12, the Python dependencies, and the Whisper model. It also installs the `pursuit-clips` launcher in `~/.local/bin`.

Then open a new Terminal window and log in to Claude Code once:

```bash
claude
```

Complete the browser login, then type `/exit`. If the project folder moves, run `./setup.sh` again to refresh the launcher symlink.

## Manual Workflow

The normal manual command is:

```bash
pursuit-clips "https://www.youtube.com/watch?v=VIDEO_ID"
```

Finished clips and posting copy are written under `~/Desktop/PURSUIT_CLIPS/`. Watch the clips, keep the good ones, and post them manually.

Useful existing options:

```bash
pursuit-clips "URL" --max 5
pursuit-clips "URL" --redo
pursuit-clips "URL" --video ~/Desktop/original.mp4
pursuit-clips ~/Desktop/local-episode.mp4
pursuit-clips "URL" --no-captions
pursuit-clips "URL" --keep-source
```

Running the same command after an interruption resumes from cached work. Completed source downloads are removed automatically; failed work keeps its source so it can resume.

## Autopilot Setup

Create the real social accounts before starting this section. Do not use personal accounts by mistake.

1. Create or finish the PURSUIT TikTok account.
2. Create or finish the PURSUIT Instagram account and make it a Professional Creator or Business account.
3. Confirm you can log in to the intended PURSUIT YouTube, Instagram, and TikTok accounts in the browser.
4. Create a Post for Me project and copy its API key.
5. Run setup:

```bash
cd ~/Documents/PURSUIT_CLIPS_TOOL
./autopilot setup
```

The API key is entered without echo and stored in macOS Keychain, not in this repository. Setup reads the accounts already connected in the Post for Me dashboard and enables a platform **only** if its handle matches the expected one (`EXPECTED_USERNAMES` in `autopilot.py`; currently TikTok `@pursuitthepod`). Anything else is listed but not used. To open a connect flow from here: `./autopilot setup --connect youtube instagram`. The tool refuses to guess when multiple accounts for one platform are connected.

The optional `PURSUIT_POSTFORME_KEY` environment variable is supported for development, but Keychain is the recommended local setup. Never commit a real `.env` file.

## Going Live (once)

1. Verify the account without publishing (uploads a clip, creates a Post for Me draft, deletes it):

   ```bash
   ./autopilot test-post
   ```

2. One controlled real post. It picks the best approved clip, shows the clip, caption, destination and time, opens the MP4, and schedules it 20 minutes out only if you type `POST ONE CLIP`:

   ```bash
   ./autopilot live-test
   ```

3. When the post appears on TikTok, turn on the hands-off system:

   ```bash
   ./autopilot auto-post on
   ```

   It first checks with Post for Me that the live test actually published, and refuses otherwise.

## What Runs Automatically

A LaunchAgent (`./install_autopilot.sh`, already installed) runs at 7:15, 10:15, 13:15, 16:15, 19:15 and 22:15 local time. It is loaded again at login, runs immediately when loaded, and launchd coalesces missed calendar runs into one run after wake. Each run does these steps independently, so one failing step never blocks the others:

1. **Checks earlier posts.** It asks Post for Me what happened and records the links. Failures trigger a Mac notification.
2. **New episodes:** detects new PURSUIT uploads before filling the schedule and turns their good moments into approved clips. Fresh clips get priority.
3. **Keeps 5 posts scheduled ahead** (auto-posting only) in the 9:00, 12:00, 15:00, 18:00 and 21:00 slots, up to 5 TikToks a day. Five is a ceiling, not a quota: an empty buffer leaves the slot empty. The posts sit on Post for Me's servers, so they go out even if the Mac is asleep or offline.
4. **Back catalog:** processes one old episode per run (newest first) whenever fewer than 21 approved clips (about four days at the ceiling) are waiting.
5. **Tops the schedule up again** with anything new.

For every clip:

- **Quality control:** a clip only becomes "approved" if it passes the full quality control (Claude's score ≥ 70 and standalone ≥ 7, technical checks, and Claude's visual check of the finished video).
- **Choosing what to post:** the best approved clip goes first. The same episode or topic is avoided back to back.
- **When nothing good is left:** if nothing approved is available, the slot is skipped. Nothing weak is ever posted to fill it.
- **Never twice:** a moment is never posted twice. Each post records the episode and time range, and anything overlapping an earlier post is dropped.
- **Removing a clip:** deleting a clip's folder removes it before it's posted.

**Resilience:**
- **Resuming work:** interrupted downloads, transcripts and renders resume from cache.
- **Outages:** a Claude or internet outage skips processing for that run without using up an episode's retry attempts. Episodes that fail 3 times get retried after 7 days.
- **Unclear replies:** an ambiguous Post for Me response is recorded and looked up by its unique ID next run, never re-posted blindly.

**Adding Instagram/YouTube later:** connect the account in Post for Me, add its expected handle to `expected_usernames` in `~/Library/Application Support/PURSUIT_AUTOPILOT/config.json`, and run `./autopilot setup`. The same clips then go to every verified account.

```bash
./autopilot status            # what's scheduled / posted / waiting (also ~/Desktop/PURSUIT_CLIPS/AUTOPILOT_STATUS.txt)
./autopilot pause | resume    # stop/start new work (already-scheduled posts still go out)
./autopilot auto-post off     # stop scheduling new posts
./install_autopilot.sh --remove
```

Runtime data is stored outside the repository:

- State, approved queue, post ledger, pinned account IDs: `~/Library/Application Support/PURSUIT_AUTOPILOT/`
- API key: macOS Keychain
- Logs: `~/Library/Logs/pursuit-autopilot.log`
- Clips/status: `~/Desktop/PURSUIT_CLIPS/`

## Safety

- **Fail closed:** unexpected pipeline or API responses stop posting.
- **Strict accounts:** every configured ID must still be connected, match its platform, and have the expected handle. Platforms without a verified handle are never posted to.
- **Duplicate protection:** a local atomic ledger, deterministic external IDs, remote lookup, and a process lock prevent blind reposting.
- **Safe retries:** ambiguous network results are recorded and reconciled before another create attempt.
- **Private atomic state:** state and ledger files are written atomically with mode `0600`.
- **Real dry runs:** dry-run processing does not alter production state or call posting endpoints.
- **Explicit live test:** the one-clip live test requires an exact interactive confirmation.
- **No channel administration:** the tool does not use YouTube write/delete APIs and cannot edit or delete existing channel videos.

Posts already scheduled on Post for Me are controlled by Post for Me. Pausing or uninstalling this local tool does not cancel them; use the Post for Me dashboard when cancellation is required.

## Quality Control

Every candidate must pass codec, dimensions, duration, full-decode, audio, transcript-boundary, repetition, speech-rate, score, and standalone checks. Claude also inspects frames from the finished clip. If source captions are already burned in, the pipeline rerenders from a safer crop and checks again.

The system is deliberately conservative, but editorial quality is subjective. Watch the first controlled live-test clip before enabling unattended posting. YouTube's **Related video** field still needs to be set manually in YouTube Studio after each Short is published.

## Current Limitations

- Built for Apple Silicon and the PURSUIT channel configuration in `autopilot.py`.
- Claude and platform OAuth sessions can expire and require interactive login again.
- `yt-dlp` occasionally needs an update when YouTube changes its site.
- Automated framing works best with a clearly visible primary speaker.
- Post for Me and social platforms may change APIs, pricing, or publishing rules.
- Full source downloads and rejected clips are cleaned after completed jobs. An approved MP4 is retained until Post for Me confirms it was published, then removed automatically.

## Tests

Tests use generated media and a local fake Post for Me server. They do not contact real social accounts:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Credentials Warning

You must supply and authorize your own API credentials and social accounts. Never commit API keys, OAuth tokens, cookies, `.env` files, runtime state, logs, transcripts, source media, or generated clips. The included `.env.example` contains placeholders only; the default setup uses macOS Keychain.
