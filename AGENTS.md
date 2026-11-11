# Booboo Expansion Project: instructions for AI coding agents

Local PURSUIT podcast clip pipeline (Python): captioned vertical clips, TikTok scheduling, YouTube Shorts delivery, podcast distribution. It runs on the owner's Mac; see README.md and PROJECT_GUIDE.md.

This project has a stated monthly budget (README.md). Do not add a paid service, a paid API call path or recurring automation without stating its cost and getting approval. Never print or commit secrets (`.env.example` lists the variables).

## Development and CI policy (applies to every session, human or AI agent)

GitHub Actions minutes on private repositories are a limited resource shared across all of the owner's projects (2,000 a month on the free plan). A single careless push can cost more than a normal day's allowance. Follow these rules in every session:

1. **Deliver working, maintainable software first.** Tests exist to protect that goal, not to be the activity.
2. **Run the smallest meaningful set of tests during ordinary development**: the test file for the code you changed, then the type check and lint.
3. **Validate locally before pushing**, not by pushing and waiting for CI. Local runs cost nothing.
4. **Do not repeat full-suite runs without a specific reason** (a shared dependency, build or test-infrastructure change, a release, or a failure you cannot localize).
5. **Do not push small intermediate commits just to see CI.** Batch the work, check it locally, push once. Every push to a pull request branch can start a run.
6. **Preserve essential safeguards**: type checks, lint, security and secret scans, dependency audits, accessibility checks, deployment verification. Do not remove or weaken them to save minutes.
7. **Escalate testing with risk and scope.** A copy edit needs none; a feature needs its own tests; a migration, a shared layout, a dependency upgrade or a release needs the broad suite.
8. **Never bypass required checks, skip hooks, or misreport results.** If a test fails, say so with the output. If you did not run something, say that.
9. **Do not add recurring automation (schedules, new always-on workflows, matrix expansion) without stating its monthly minute cost** and getting approval.
10. **Treat Actions minutes as shared and finite.** Prefer local or manually triggered runs; use path filters, concurrency groups with cancel-in-progress, timeouts and caching in any workflow you write. Never enable paid usage or raise a spending limit.

### How this repository applies the policy
- This repository has **no GitHub Actions workflows**, and none should be added without approval and a stated minute cost. The automation (autopilot, scheduled posting) runs on the Mac and must keep working: do not change schedules, install scripts or posting behavior as a side effect of other work.
- Tests are in `tests/test_*.py`, one file per module. Run the file for the module you changed (for example `python -m pytest tests/test_shorts_delivery.py`); run all of `tests/` only after changing shared code such as `llm.py` or the pipeline entry points.
- Anything that publishes, schedules or deletes (TikTok, YouTube, Drive, Spotify, Cloudflare storage) is a high-risk change: test it with dry-run or fake clients and say so; never run it against live accounts as a test.

