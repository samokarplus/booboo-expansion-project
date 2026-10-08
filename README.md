# Booboo Expansion Project

## TL;DR

A local **podcast-to-short-form content engine** for PURSUIT. It watches the podcast's YouTube channel, transcribes episodes, uses Claude to find standalone moments, renders captioned vertical clips, and checks their quality. The current local deployment schedules approved clips through Post for Me to **TikTok @pursuitthepod, up to 3 posts per day**, and to the connected **[PURSUIT with Anya Postnikov YouTube channel (@AnyaPostnikov)](https://www.youtube.com/@AnyaPostnikov), up to 3 Shorts per week**.

**YouTube automatic publishing is now enabled:** Monday, Wednesday, and Friday at **5 PM America/Denver**. The destination is pinned to channel ID `UCkw_7bkF1qSRrupIvN4SIAg`. The first Short is confirmed as scheduled in Post for Me for **Friday, October 9, 2026 at 5 PM Denver time**; that is a scheduling confirmation, not a claim it has already published.

TikTok uses separate **9 AM, 3 PM, and 9 PM Denver-time** slots and a **12-clip target buffer**. YouTube maintains its own weekly schedule and duplicate-protection ledger. These are ceilings, not quotas: slots remain empty when no eligible clip is available. New episodes get priority. Submitted posts can publish from Post for Me's cloud while the Mac is off; the Mac must be on to generate, check, and submit more clips. Google Drive review delivery remains an optional, separate workflow. TikTok/Instagram captions link viewers to @AnyaPostnikov on YouTube.

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
- Targets up to **three TikTok posts per day** at **9 AM, 3 PM, and 9 PM America/Denver**. Three is a ceiling, not a quota: if nothing good passes QC, the slot is skipped.
- Automatically schedules **three YouTube Shorts per week** to the connected **PURSUIT with Anya Postnikov (@AnyaPostnikov)** channel, on **Monday, Wednesday, and Friday at 5 PM America/Denver**.
- Checks for new episodes six times per day and works toward a **12-clip approved-buffer target**.
- Recovers safely from interrupted processing, Mac sleep/restarts, network failures, and ambiguous publishing responses.
- For each **new** PURSUIT episode, automatically prepares up to **3** strong, non-overlapping YouTube Shorts after the normal processing/QC pass and uploads the finished 1080x1920 MP4s to the shared `PURSUIT - Shorts Ready to Post` Google Drive folder.
- Generates a `POSTING_INFO.txt` alongside each Shorts batch with ready-to-copy YouTube titles/descriptions and the full-episode link.
- Keeps weekly YouTube publishing separate from TikTok scheduling. The optional Drive workflow also supports reviewing finished files; Drive sign-in is not required for Post for Me publishing.
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
        -> YouTube Shorts: Post for Me -> @AnyaPostnikov -> Mon/Wed/Fri at 5 PM Denver
        -> Optional review copies: shared Google Drive
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
             -> YouTube Shorts: Post for Me -> automatic publishing, 3 per week
             -> Instagram Reels: available as a future Post for Me destination once the intended account is connected/tested
```

This is a small, local macOS command-line tool. It is not a web app and it does not create social accounts, bypass OAuth, manage comments, or modify existing channel content.

## About Post for Me and Connecting YouTube

[Post for Me](https://www.postforme.dev/) is a hosted social-media publishing service operated by **Day Moon Development LLC**. It gives apps one interface for account connections, video uploads, scheduling, and publishing across **nine platforms**, including TikTok, Instagram, and YouTube. In this project, the Mac does the creative work: finding moments, editing, captioning, and checking clips. Post for Me handles delivery of approved TikTok posts and the weekly YouTube Shorts.

### Who runs it and how established is it?

Day Moon Development was started in **2023** by founders Caleb and Matt, who developed Post for Me from social-media integrations they built for clients. Its [public source repository](https://github.com/DayMoonDevelopment/post-for-me) contains the API, dashboard, and background-job code. As of **October 5, 2026**, GitHub showed **78 stars, 25 forks, and more than 1,100 commits**. These describe its public development footprint, not its number of customers. The official pages reviewed do not publish a verified customer or connected-account count. [Company background](https://www.postforme.dev/day-moon-development) · [Product background](https://www.postforme.dev/about)

### What connecting a YouTube channel means

YouTube connection uses **Google's OAuth authorization process**: the channel owner signs in with Google, chooses the intended account/channel, and reviews the requested permissions on Google's consent screen. The app receives authorization tokens rather than the owner's Google password. Those tokens let the service act within the permissions granted; the exact consent screen matters, since YouTube permissions can include video management, not just uploading. Connecting is a real authorization decision, and does not transfer ownership of the channel. [Google's OAuth explanation and permission scopes](https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps)

Post for Me's [YouTube integration](https://www.postforme.dev/integrations/youtube) supports uploads, scheduling, titles, thumbnails, and public/private/unlisted visibility. Its [privacy policy](https://www.postforme.dev/privacy) says it stores channel identifiers, account metadata, and OAuth tokens; encrypts data in transit and at rest; protects production access with least-privilege roles and mandatory two-factor authentication; and does not sell or rent user information. These are the provider's published commitments.

The owner can disconnect YouTube in Post for Me or revoke its access through [Google Account connections](https://myaccount.google.com/connections). Revocation stops future authorized access; it does not undo videos already published or automatically remove previously shared data. [Google's connection-management guide](https://support.google.com/accounts/answer/13533235)

### How PURSUIT currently uses YouTube

**As of October 7, 2026, automatic YouTube Shorts scheduling is enabled in the local deployment.** Post for Me is connected to **PURSUIT with Anya Postnikov (@AnyaPostnikov)**, pinned by channel ID `UCkw_7bkF1qSRrupIvN4SIAg`. Quality-approved Shorts are scheduled on **Monday, Wednesday, and Friday at 5 PM America/Denver**, independently of TikTok's three-per-day schedule. Up to three Shorts are submitted ahead, and empty slots are skipped. Creating clips happens locally; publishing uses the connected YouTube authorization. Optional Drive review delivery remains available separately.

## What It Does

### Built end-to-end automation

The current local build goes beyond one-off clip generation. It can run as a scheduled, hands-off content engine:

- Works through PURSUIT's existing YouTube back catalog automatically.
- Checks for new PURSUIT episodes six times per day and gives fresh episodes priority.
- Uses Claude to select promising, standalone short-form moments.
- Renders 9:16 clips with captions, framing, and normalized audio.
- Runs technical, editorial, caption, framing, and audio quality control; weak candidates are rejected rather than posted just to fill a slot.
- Maintains an internal buffer of approved clips without requiring the user to manage the queue.
- Processes another back-catalog episode roughly every three hours when the buffer needs content, and pauses backlog work when roughly four days of clips are ready.
- Targets up to three TikTok posting slots per day at **9 AM, 3 PM, and 9 PM America/Denver**. A slot is skipped when no clip passes QC.
- Schedules YouTube Shorts independently on **Monday, Wednesday, and Friday at 5 PM America/Denver** to **PURSUIT with Anya Postnikov (@AnyaPostnikov)**.
- Tracks processed episodes, used time ranges, queued clips, and posts to prevent duplicate content.
- Uses persistent state, retries, locking, and reconciliation so interruptions, restarts, and ambiguous API failures do not blindly create duplicate posts.
- Keeps unattended posting OFF until a controlled real post has been confirmed live.
- Uses verified TikTok and pinned YouTube destinations for unattended publishing. Instagram Reels can be added later through the controlled account-pinning/live-test process. Google Drive review delivery is an optional additional output.

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
- Can leave finished clips for manual posting, schedule approved TikTok clips and weekly YouTube Shorts through Post for Me, and optionally deliver review copies to Google Drive.

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS and Homebrew
- A Claude subscription with the Claude Code CLI logged in
- Approximately 2 GB for the Python environment and Whisper model, plus temporary space while an episode is processed
- For automatic publishing: your own Post for Me account/API key, the intended TikTok account, and the intended connected YouTube channel
- For optional Drive review delivery: a Google OAuth Desktop client with Drive access and a shared Drive folder
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

It checks the PURSUIT YouTube channel six times per day. New episodes jump ahead of the older catalog. When the approved buffer needs content, the scheduled worker can process another back-catalog episode roughly every three hours; it pauses backlog processing when roughly four days of approved clips are already waiting.

After the controlled live-post gate has succeeded and unattended TikTok posting is enabled, the target is up to three TikTok posts per day at **09:00, 15:00, and 21:00 America/Denver**. Separately, YouTube weekly publishing is enabled for **PURSUIT with Anya Postnikov (@AnyaPostnikov)** on **Monday, Wednesday, and Friday at 17:00 America/Denver**. A slot is skipped rather than publishing a clip that did not pass quality control. The queue is an internal reliability mechanism and does not require daily manual management.

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

## Optional YouTube Shorts Review Delivery

In addition to the active weekly Post for Me publishing schedule, Google Drive supports an optional **human-review workflow**. Once Drive delivery is enabled, each newly uploaded PURSUIT episode is processed by the existing episode/transcription/Claude/QC pipeline. After that processing finishes, the delivery layer prepares up to **3** of the strongest approved, non-overlapping, on-camera clips. Fewer are delivered when fewer clips meet the quality bar.

The finished 1080x1920 H.264/AAC MP4s and a `POSTING_INFO.txt` file are uploaded automatically to the shared Google Drive folder **PURSUIT - Shorts Ready to Post**. Anya can open that folder on her phone, review the finished videos, and manually upload whichever ones she wants to YouTube Shorts. The Drive delivery module does not publish to YouTube; the separate weekly publisher uses the connected YouTube account through Post for Me.

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

## Spotify: Full Episodes as an Audio Podcast

The Spotify workflow takes the **full podcast episode**, extracts its audio, and publishes an MP3 through a podcast host and RSS feed. Spotify reads that feed and makes the episode available to listeners. This is a separate destination from the short clips published through Post for Me.

**Current status:** the podcast module and CLI integration are implemented locally and passed automated tests. They have not yet been pushed to this repository or activated for production. This documentation update describes that local implementation; a fresh clone will not yet include the `podcast-*` commands below. A podcast host, credentials, RSS registration, and a verified live episode are still required before calling Spotify distribution operational.

```text
New full-length YouTube episode
        -> program finds the episode's source media
        -> program extracts audio and converts it to MP3
        -> program uploads and publishes through Transistor
        -> Transistor updates the podcast RSS feed
        -> Spotify reads the feed and lists the episode
```

### What the User Does Once

1. **Create the podcast at a host.** The implemented default is [Transistor](https://transistor.fm/). Set the show name, description, author, category, explicit-content setting, cover artwork, and owner email. The host subscription is an additional cost; check its current pricing before subscribing.
2. **Configure access on the Mac.** Supply the Transistor API key and show ID through local configuration or the environment. Keep these credentials out of GitHub. Scheduled runs must have access to the same settings; exporting variables in one Terminal session alone does not configure the macOS LaunchAgent.
3. **Choose how source media is supplied.** By default, provide an exported episode file. Register it with `podcast-source`, or configure a source folder with filenames containing the episode's YouTube video ID. Optionally enable the existing YouTube download path with `PURSUIT_PODCAST_ALLOW_YTDLP=1` for the owner's uploads.
4. **Initialize episode watching.** The first podcast run records the latest YouTube episode as its starting point. It does not publish that episode or backfill older episodes. Later runs handle episodes newer than that starting point, one per run, oldest first.
5. **Check and publish a first eligible episode.** Run the dry run to check source retrieval and MP3 conversion, then run the publishing command. The dry run writes local working files and status, but does not upload or publish. Check the resulting episode and RSS feed in the host.
6. **Connect the RSS feed to Spotify.** In [Spotify for Creators](https://creators.spotify.com/), submit the host's RSS feed as an existing podcast and complete ownership verification using the feed's owner email. Check that Spotify accepts the show and displays the first episode.
7. **Enable recurring distribution.** Turn `podcast-auto on` after configuration and testing. The local scheduled worker needs to be installed and the Mac needs to be awake, online, and able to access the source media and credentials. The podcast step runs during live autopilot runs; the existing autopilot pause controls also apply.

### What the Program Does Automatically

| Step | Program behavior |
| --- | --- |
| Discover | Reuses autopilot's full-episode detection and selects an eligible upload newer than the initial starting point. |
| Find media | Looks for the registered export or a matching file in the source folder; uses the YouTube download path only when explicitly configured. |
| Convert | Extracts the complete audio track and creates a stereo MP3, defaulting to 160 kbps. |
| Publish | Uploads the MP3 to Transistor, creates the episode with its title, description and YouTube link, and asks the host to publish it. |
| Record | Saves per-episode progress and a publication ledger to avoid publishing an already recorded episode again. |
| Report | Shows podcast progress and errors in `podcast-status` and the normal autopilot status output. |

**Transistor hosts the audio and updates the RSS feed. Spotify controls when it imports and displays the episode.** A program status of `published` confirms host/feed publication, not that Spotify has already refreshed. The program does not create the hosting account, complete Spotify ownership verification, or confirm Spotify listing availability. See [Spotify's RSS explanation](https://support.spotify.com/bd-en/creators/article/your-rss-feed/) and the [Transistor API documentation](https://developers.transistor.fm/).

### What the User Does for Each New Episode

Publish the full episode to YouTube as usual. With the default source-file workflow, also provide the exported media and register its YouTube video ID, or place it in the configured source folder with that ID in its filename. The program handles conversion and host publication on subsequent scheduled runs. If the optional YouTube download path is enabled and works, a separate export is not required.

Check status when an episode does not appear. A `needs_source` episode requires source registration before it resumes. An `unknown` result means the host's create/publish response was unclear: inspect Transistor before attempting another publication to avoid duplicates. Reconnect expired credentials and manage hosting billing when needed.

### Commands in the Local Implementation

These commands become available from a fresh clone once the implementation is published to GitHub:

```bash
# Register an exported source for a specific YouTube episode.
./autopilot podcast-source VIDEO_ID ~/Movies/full_episode_export.mp4

# First run initializes watching; subsequent runs handle one newer episode.
./autopilot podcast-run --once --dry-run
./autopilot podcast-run --once

# Enable or disable the podcast step in scheduled live autopilot runs.
./autopilot podcast-auto on
./autopilot podcast-auto off
./autopilot podcast-status
```

`--once` permits a manual podcast run while podcast automation is off; it does not override the initial starting point or select a back-catalog episode. Status and publication records live under `~/Library/Application Support/PURSUIT_AUTOPILOT/`, outside GitHub.

### Optional Self-Hosted Feed

The local implementation also supports `PURSUIT_PODCAST_MODE=self_hosted`. In that mode the program copies MP3 files into a local public folder and generates `feed.xml`. **The user must provide the HTTPS hosting**, public media/feed URLs, artwork URL, and owner email, and keep the feed and audio reachable. Generating local files does not deploy them to a web server. Spotify registration and verification still happen manually, once, against the public RSS URL.

## Safety

- **Fail closed:** unexpected pipeline or API responses stop posting.
- **Strict accounts:** every configured ID must still be connected and match its expected platform.
- **Duplicate protection:** a local atomic ledger, deterministic external IDs, remote lookup, and a process lock prevent blind reposting.
- **Safe retries:** ambiguous network results are recorded and reconciled before another create attempt.
- **Private atomic state:** state and ledger files are written atomically with mode `0600`.
- **Real dry runs:** dry-run processing does not alter production state or call posting endpoints.
- **Explicit live test:** the one-clip live test requires an exact interactive confirmation.
- **YouTube publishing scope:** the weekly publisher submits new Shorts through Post for Me. Its implementation does not edit or delete existing YouTube videos; the Google consent screen defines the service's authorized access.

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
