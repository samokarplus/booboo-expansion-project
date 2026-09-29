# Booboo Expansion Project

## TL;DR

A local **podcast-to-short-form content engine** for PURSUIT with two production outputs. It watches the podcast's YouTube channel, transcribes old and new episodes, uses Claude to find coherent standalone moments, turns them into polished vertical clips with captions/framing/audio normalization, and quality-checks them. Approved TikTok clips can be queued and published automatically through Post for Me; for every new PURSUIT episode, the system also prepares up to **3 review-ready YouTube Shorts** and automatically delivers the finished MP4s plus posting copy to a shared Google Drive folder for manual review and upload. New episodes get priority, duplicate moments are avoided, failures recover safely, and the system targets up to **5 posts per day** without routine human editing, clip selection, queue management, or posting. The TikTok side continuously replenishes approved content toward a **21-clip target buffer**; this is a target, not a claim that 21 clips are currently ready. Only clips that have actually passed QC count as available inventory. Once TikTok clips have been submitted/scheduled with Post for Me, those scheduled posts can publish from the cloud even if the Mac is powered off; the Mac must be on to create, QC, replenish, submit new TikTok clips, and prepare/deliver new Shorts to Drive. YouTube Shorts are **not automatically published**: Drive is the review/delivery layer, and Anya chooses which finished Shorts to upload manually. Every automated TikTok/Instagram caption includes a fixed CTA to **@AnyaPostnikov on YouTube** before the hashtags, creating a funnel from short-form clips to full PURSUIT episodes. Until the TikTok account reaches **1,000 followers** and can add a clickable website link, the profile bio directs viewers to `@AnyaPostnikov`; once eligible, the YouTube channel URL can be added as the clickable website.

An automated podcast growth system built for **PURSUIT**.

**Live TikTok:** [@pursuitthepod](https://www.tiktok.com/@pursuitthepod)

## What I Built

This is a **local autonomous podcast-to-short-form content engine**. The human creates the long-form podcast; the system handles essentially the entire repetitive distribution workflow from episode discovery to publishing.

In plain English: **publish a podcast episode, and the Mac can turn it into short-form social content without someone manually finding moments, editing clips, captioning them, maintaining a queue, choosing posting times, or pressing Post.**

The system automatically:

- Detects new PURSUIT YouTube episodes and works through the existing back catalog.
- Downloads and transcribes episodes locally with Whisper.
- Uses Claude to find coherent, standalone moments such as advice, stories, arguments, or complete thoughts rather than arbitrary timestamp windows.
- Renders polished 9:16 vertical clips with speaker framing, burned captions, and normalized audio.
- Runs automated technical and editorial quality control and rejects weak, incomplete, or awkward clips.
- Tracks source episodes and time ranges to avoid recycling the same moments.
- Maintains its own approved-content buffer and replenishes it from the back catalog.
- Gives newly published podcast episodes priority over older material.
- Schedules and publishes approved clips through Post for Me.
- Targets up to **five posts per day** at **9 AM, 12 PM, 3 PM, 6 PM, and 9 PM local time**. Five is a ceiling, not a quota: if nothing good passes QC, the slot is skipped.
- Checks for new episodes six times per day and works toward a **21-clip approved-buffer target**.
- Recovers safely from interrupted processing, Mac sleep/restarts, network failures, and ambiguous publishing responses.
- For each **new** PURSUIT episode, automatically prepares up to **3** strong, non-overlapping YouTube Shorts after the normal processing/QC pass and uploads the finished 1080x1920 MP4s to the shared `PURSUIT - Shorts Ready to Post` Google Drive folder.
- Generates a `POSTING_INFO.txt` alongside each Shorts batch with ready-to-copy YouTube titles/descriptions and the full-episode link.
- Keeps YouTube human-in-the-loop: the system does **not** connect to or publish to Anya's YouTube account; she reviews the Drive files and manually posts whichever Shorts she wants.
- Catches up on multiple new episodes after Mac sleep/offline time, retries failed Drive deliveries without losing the prepared package, and prevents duplicate batches per episode.
- Cleans tool-owned Drive delivery files after **14 days** while preserving source/transcript/analysis data and social-production assets.

The unattended system also includes production safeguards: automated tests, resumable processing, serialized jobs/locking, atomic state writes, credential validation, pinned social-account identity, duplicate-post protection, API reconciliation, bounded media cleanup, corruption handling, and fail-closed behavior when an upstream service or credential stops working.

The result is not just a clip maker. It is an **autonomous local social-media pipeline:**

```text
Long-form PURSUIT podcast
        -> episode discovery
        -> local transcription
        -> AI moment selection
        -> 9:16 editing + captions + audio/framing
        -> automated quality control
        -> approved content buffer
        -> quality-approved clips
        -> TikTok: queue + scheduled publishing through Post for Me
        -> YouTube Shorts: up to 3 finished MP4s + posting copy -> shared Drive -> human review/manual upload
        -> repeat
```

Unattended publishing is deliberately protected by a one-time controlled live-post gate before auto-posting can be enabled.

```text
YouTube podcast episode
        -> detect and download
        -> transcribe with MLX Whisper
        -> Claude selects promising moments
        -> FFmpeg creates 9:16 clips
        -> captions and audio normalization
        -> technical and visual quality control
        -> output routing
             -> TikTok: Post for Me -> automatic publishing
             -> YouTube Shorts: Google Drive -> Anya reviews -> manual publishing
             -> Instagram Reels: available as a future Post for Me destination once the intended account is connected/tested
```

This is a small, local macOS command-line tool. It is not a web app and it does not create social accounts, bypass OAuth, manage comments, or modify existing channel content.

## What It Does

### Built end-to-end automation

The current local build goes beyond one-off clip generation. It can run as a scheduled, hands-off content engine:

- Works through PURSUIT's existing YouTube back catalog automatically.
- Checks for new PURSUIT episodes six times per day and gives fresh episodes priority.
- Uses Claude to select promising, standalone short-form moments.
- Renders 9:16 clips with captions, framing, and normalized audio.
- Runs technical, editorial, caption, framing, and audio quality control; weak candidates are rejected rather than posted just to fill a slot.
- Maintains an internal buffer of approved clips without requiring the user to manage the queue.
- Processes another back-catalog episode roughly every three hours when the buffer needs content, and pauses backlog work when about a week's worth of clips is ready.
- Targets up to five TikTok posting slots per day at **9 AM, 12 PM, 3 PM, 6 PM, and 9 PM local time**. Five is a ceiling, not a quota; a slot is skipped when no clip passes QC.
- Tracks processed episodes, used time ranges, queued clips, and posts to prevent duplicate content.
- Uses persistent state, retries, locking, and reconciliation so interruptions, restarts, and ambiguous API failures do not blindly create duplicate posts.
- Keeps unattended posting OFF until a controlled real post has been confirmed live.
- Currently supports a verified/pinned TikTok destination for unattended publishing. Instagram Reels can be added later through the same controlled account-pinning/live-test process. YouTube Shorts intentionally use the separate Drive review workflow rather than unattended publishing.

In short:

```text
PURSUIT old + new YouTube episodes
        -> scheduled discovery
        -> transcription
        -> Claude selects strong unused moments
        -> vertical render + captions + audio/framing
        -> quality control
        -> internal approved buffer
        -> automatic scheduled publishing
        -> repeat without daily clip management
```

- Accepts a YouTube episode URL or detects a new PURSUIT upload.
- Downloads the source with `yt-dlp`.
- Transcribes locally with `mlx-whisper`, including word-level timestamps.
- Asks the Claude Code CLI to find interesting, standalone moments with fast openings and complete endings.
- Uses FFmpeg and OpenCV to create 1080x1920 H.264/AAC clips, frame the speaker, burn highlighted ASS captions, and normalize audio to -14 LUFS.
- Handles static-image/audio-only episodes with an audiogram layout.
- Rejects clips that fail technical, editorial, caption, framing, or audio checks.
- Can leave finished clips for manual posting, schedule approved TikTok clips through Post for Me, and automatically deliver review-ready YouTube Shorts to Google Drive.

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS and Homebrew
- A Claude subscription with the Claude Code CLI logged in
- Approximately 2 GB for the Python environment and Whisper model, plus temporary space while an episode is processed
- For TikTok autopilot posting: your own Post for Me account/API key and the intended TikTok account
- For automated Shorts delivery: a Google OAuth Desktop client with Drive access and a shared Drive folder
- Instagram is optional; if automatic Reels publishing is added later, connect and verify the intended eligible Instagram account before enabling it

In the current deployment, Claude is used through the logged-in Claude Code subscription rather than a separately configured Anthropic API key, so the pipeline consumes normal Claude plan usage rather than a separate per-call API bill. Local Whisper/FFmpeg processing and Google Drive API delivery add no per-episode software charge; Drive files use the account's normal storage. TikTok autopilot uses Post for Me, which is the main incremental recurring service cost in this deployment (currently $10/month for the account in use; pricing can change).

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
./autopilot live-test
```

The automated queue supplies the candidate; a URL can still be processed manually when needed.

The command chooses exactly one passing clip, shows the destination account and schedule, and opens the MP4 for review. Nothing is scheduled unless you type `POST ONE CLIP` exactly in an interactive Terminal. Unattended posting remains locked until a real live-test post is confirmed as posted; after that, `./autopilot auto-post on` enables the hands-off schedule.

## Autopilot Behavior

Install the LaunchAgent after setup and testing:

```bash
./install_autopilot.sh
```

It checks the PURSUIT YouTube channel six times per day. New episodes jump ahead of the older catalog. When the approved buffer needs content, the scheduled worker can process another back-catalog episode roughly every three hours; it pauses backlog processing when about a week's worth of approved clips is already waiting.

After the controlled live-post gate has succeeded and unattended posting is explicitly enabled, the current target is up to five TikTok posting slots per day at **9:00, 12:00, 15:00, 18:00, and 21:00 local time**. A slot is skipped rather than publishing a clip that did not pass quality control. The queue is an internal reliability mechanism and does not require daily manual management.

```bash
./autopilot status
./autopilot pause
./autopilot resume
./autopilot auto-post on
./autopilot process "YOUTUBE_URL"
./install_autopilot.sh --remove
```

Runtime data is stored outside the repository:

- State and account IDs: `~/Library/Application Support/PURSUIT_AUTOPILOT/`
- API key: macOS Keychain
- Logs: `~/Library/Logs/pursuit-autopilot.log`
- Clips/status: `~/Desktop/PURSUIT_CLIPS/`

## YouTube Shorts Review Delivery

YouTube Shorts deliberately use a **human-review workflow** instead of automatic publishing. Once Drive delivery is enabled, each newly uploaded PURSUIT episode is processed by the existing episode/transcription/Claude/QC pipeline. After that processing finishes, the delivery layer prepares up to **3** of the strongest approved, non-overlapping, on-camera clips. Fewer are delivered when fewer clips meet the quality bar.

The finished 1080x1920 H.264/AAC MP4s and a `POSTING_INFO.txt` file are uploaded automatically to the shared Google Drive folder **PURSUIT - Shorts Ready to Post**. Anya can open that folder on her phone, review the finished videos, and manually upload whichever ones she wants to YouTube Shorts. The pipeline has no YouTube publishing permission in this workflow.

The Drive path is designed to be unattended but fail-safe: one batch per episode, stable Drive IDs/checksums, duplicate reconciliation after interrupted uploads, local preservation before upload, retry after authentication/network failure, catch-up when multiple episodes arrive while the Mac is asleep, and 14-day cleanup limited to files the tool can prove it owns. If the configured folder disappears or its identity cannot be verified, the tool fails closed rather than silently creating/switching to another folder.

Useful commands:

```bash
./autopilot drive-status
./autopilot export-shorts latest 3
./autopilot export-shorts latest 3 --deliver
./autopilot drive-test
./autopilot drive-auto on
./autopilot drive-auto off
./autopilot cleanup-drive
```

A controlled real-world Drive test and a real three-Short batch were successfully uploaded and downloaded back for integrity verification before automatic delivery was enabled. The recurring new-episode path is covered by automated tests; the first completely hands-off future episode remains the final real-world validation of that recurring path.

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
- Post for Me, Google, and social platforms may change APIs, pricing, authentication, or publishing rules.
- Google OAuth/Claude sessions can require interactive re-authentication.
- Drive review packages are intentionally temporary and are cleaned after 14 days when provenance can be verified.
- The first future episode processed completely hands-off after enabling automatic Drive delivery is still the final real-world validation of that recurring path.
- Generated production clips/source assets follow the pipeline's bounded cleanup rules; Drive cleanup does not delete the underlying transcript/analysis merely because a review copy expires.

## Tests

Tests use generated media and a local fake Post for Me server. They do not contact real social accounts:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Credentials Warning

You must supply and authorize your own API credentials and social accounts. Never commit API keys, OAuth tokens, cookies, `.env` files, runtime state, logs, transcripts, source media, or generated clips. The included `.env.example` contains placeholders only; the default setup uses macOS Keychain.
