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
        -> Post for Me
        -> TikTok + Instagram Reels + YouTube Shorts
```

This is a small, local macOS command-line tool. It is not a web app and it does not create social accounts, bypass OAuth, manage comments, or modify existing channel content.

## What It Does

- Accepts a YouTube episode URL or detects a new PURSUIT upload.
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

The API key is entered without echo and stored in macOS Keychain, not in this repository. Three Post for Me OAuth windows open. In each window, carefully verify that you are authorizing the intended PURSUIT account. The tool refuses to guess when multiple accounts for one platform are connected.

The optional `PURSUIT_POSTFORME_KEY` environment variable is supported for development, but Keychain is the recommended local setup. Never commit a real `.env` file.

## Safe Testing

First render an episode manually so a test clip exists. Then verify all three connections without publishing:

```bash
./autopilot test-post
```

This uploads one existing clip, creates a Post for Me **system draft**, validates it, and deletes the draft. It does not publish the draft.

Rehearse a complete episode without creating any posts or changing production state:

```bash
./autopilot process "YOUTUBE_URL" --dry-run
```

For the first controlled live test:

```bash
./autopilot live-test "YOUTUBE_URL"
```

The command performs the full pipeline and QC, chooses exactly one passing clip, shows the destination accounts and schedule, and opens the MP4 for review. Nothing is scheduled unless you type `POST ONE CLIP` exactly in an interactive Terminal.

## Autopilot Behavior

Install the LaunchAgent after setup and testing:

```bash
./install_autopilot.sh
```

It checks the PURSUIT YouTube channel at 7:15, 10:15, 13:15, 16:15, 19:15, and 22:15 local time. The first run records the newest episode as its baseline; later uploads are processed automatically. Passing clips are scheduled strongest-first, one per day at 5:00 PM, to all three configured platforms.

```bash
./autopilot status
./autopilot pause
./autopilot resume
./autopilot process "YOUTUBE_URL"
./install_autopilot.sh --remove
```

Runtime data is stored outside the repository:

- State and account IDs: `~/Library/Application Support/PURSUIT_AUTOPILOT/`
- API key: macOS Keychain
- Logs: `~/Library/Logs/pursuit-autopilot.log`
- Clips/status: `~/Desktop/PURSUIT_CLIPS/`

## Safety

- **Fail closed:** unexpected pipeline or API responses stop posting.
- **Strict accounts:** every configured ID must still be connected and match its expected platform.
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
- Generated clips remain on disk until you remove them; full source downloads are cleaned after completed jobs.

## Tests

Tests use generated media and a local fake Post for Me server. They do not contact real social accounts:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Credentials Warning

You must supply and authorize your own API credentials and social accounts. Never commit API keys, OAuth tokens, cookies, `.env` files, runtime state, logs, transcripts, source media, or generated clips. The included `.env.example` contains placeholders only; the default setup uses macOS Keychain.
