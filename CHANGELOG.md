# Changelog

Short, plain notes. Newest first.

## 2026-11-10

- **Clips are now 20–60 seconds.** Shorter or longer clips are never posted.
- **New `unschedule` command.** Cancels a scheduled clip on TikTok and YouTube and keeps it from coming back.
- **Claude picks and checks clips.** Codex is the backup when Claude is busy.
- **QC can now see the frame.** Claude is told which image file to open.
- **Messy AI replies no longer crash a run.** If the AI corrects itself mid-reply, the last valid answer is used.
- **Bigger buffer.** Schedules about a week ahead (21 posts) and builds a 30-clip backlog.
- **Installer** sets the AI choice for the daily job.
- **Whole library is eligible.** Episodes up to a year old can be clipped, not just the last 30 days. Moving-video only.
- **2 older episodes per run** (was 1) while the backlog fills.
- **Low-disk guard.** Heavy work waits if the Mac has under 3 GB free.
- **AI must give 20–60s clips.** Out-of-range clips are dropped before rendering; the AI gets one more try if under 3 are left.
- **One bad upload no longer blocks the rest.** A clip that fails to upload is skipped, then retired after 3 runs.
- **Clips moved off the Desktop.** The background job can't read the Desktop, so old clips failed to upload for weeks. They now live in the autopilot folder.
- **Health check.** Status file shows days of buffer and each step's last good run. Alerts only for real stalls: 3 failed runs in a row, or almost no posts left.
- **Picks stay 21–58s** so the final cut lands inside 20–60s.
- **Tests:** 171 pass.
