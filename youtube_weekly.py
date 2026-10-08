"""Independent three-per-week YouTube Shorts scheduling through Post for Me."""
import datetime as dt
from pathlib import Path

import autopilot as ap

WEEKDAYS = (0, 2, 4)
POST_HOUR = 17
SCHEDULE_AHEAD = 3
LEDGER_FILE = ap.STATE_DIR / "youtube-weekly-ledger.json"


def slots(posts, count, current=None):
    current = current or ap.now()
    earliest = current + dt.timedelta(minutes=30)
    if posts:
        earliest = max(earliest, max(dt.datetime.fromisoformat(p["scheduled_at"])
                                     for p in posts) + dt.timedelta(minutes=1))
    day, result = earliest.astimezone(ap.TZ).date(), []
    while len(result) < count:
        when = dt.datetime.combine(day, dt.time(POST_HOUR), ap.TZ)
        if day.weekday() in WEEKDAYS and when >= earliest:
            result.append(when)
        day += dt.timedelta(days=1)
    return result


def verified_account(settings):
    matches = [a for a in ap.connected_accounts() if a.get("id") == settings["account_id"]]
    if len(matches) != 1 or matches[0].get("platform") != "youtube" or \
            matches[0].get("user_id") != settings["channel_id"]:
        raise ap.Stop("YouTube weekly: the pinned PURSUIT channel is not connected; nothing scheduled.")
    return matches[0]


def external_id(item):
    return item["external_id"] + "-youtube-weekly"


def candidates(queue, posts):
    used = {p["external_id"] for p in posts}
    result = []
    for item in sorted(queue["clips"], key=lambda q: -q["score"]):
        clip = item["clip"]
        if item["status"] not in ("queued", "scheduled") or external_id(item) in used:
            continue
        if not ap.recent_visual_clip(item["meta"], clip):
            continue
        if not (Path(item["ep_dir"]) / clip["file"]).is_file():
            continue
        start, end = float(clip["start_sec"]), float(clip["end_sec"])
        if any(p["episode"] == item["episode"] and
               min(end, p["end_sec"]) > max(start, p["start_sec"]) for p in posts):
            continue
        result.append(item)
    return result


def reconcile(ledger, account_id):
    for post in ledger["posts"]:
        when = dt.datetime.fromisoformat(post["scheduled_at"])
        if post["status"] == "unknown":
            found = ap.find_posts_by_external_id(post["external_id"])
            if len(found) != 1:
                raise ap.Stop("YouTube weekly: an unconfirmed submission needs review; not resubmitting it.")
            remote = ap.validate_post(found[0], post["external_id"], [account_id], when, False)
            post.update(post_id=remote["id"], status="scheduled", scheduled_at=remote["scheduled_at"])
            ap.save(LEDGER_FILE, ledger)
        if post["status"] != "scheduled" or ap.now() < when + dt.timedelta(minutes=20):
            continue
        results = ap.api("GET", "/v1/social-post-results",
                         query={"post_id": post["post_id"], "limit": 10}).get("data", [])
        matches = [r for r in results if r.get("social_account_id") == account_id]
        if len(matches) > 1:
            raise ap.Stop("YouTube weekly: multiple publishing results returned; needs review.")
        if not matches:
            if ap.now() > when + dt.timedelta(hours=6):
                post["status"] = "unclear"
                ap.save(LEDGER_FILE, ledger)
                ap.notify("PURSUIT YouTube publishing problem", "No result six hours after the scheduled Short.")
            continue
        result = matches[0]
        post.update(status="posted" if result.get("success") else "failed",
                    url=(result.get("platform_data") or {}).get("url"), error=result.get("error"))
        ap.save(LEDGER_FILE, ledger)
        if post["status"] == "failed":
            ap.notify("PURSUIT YouTube publishing problem", str(post.get("error"))[:200])


def fill_schedule():
    settings = ap.load(ap.CONFIG_FILE, {}).get("youtube_weekly") or {}
    if not settings.get("enabled"):
        return None
    account = verified_account(settings)
    ledger = ap.load(LEDGER_FILE, {"posts": []})
    reconcile(ledger, account["id"])
    ahead = sum(p["status"] in ("scheduled", "unknown") and
                dt.datetime.fromisoformat(p["scheduled_at"]) > ap.now() for p in ledger["posts"])
    count = 0
    while ahead + count < SCHEDULE_AHEAD:
        picks = candidates(ap.load(ap.QUEUE_FILE, {"clips": []}), ledger["posts"])
        if not picks:
            break
        item = picks[0]
        when = slots(ledger["posts"], 1)[0]
        eid = external_id(item)
        existing = ap.find_posts_by_external_id(eid)
        if len(existing) > 1:
            raise ap.Stop("YouTube weekly: duplicate remote posts found; needs review.")
        record = {"external_id": eid, "status": "unknown", "scheduled_at": when.isoformat(),
                  "episode": item["episode"], "clip": item["clip"]["folder"],
                  "start_sec": float(item["clip"]["start_sec"]),
                  "end_sec": float(item["clip"]["end_sec"])}
        if existing:
            remote = ap.validate_post(existing[0], eid, [account["id"]], when, False)
        else:
            media = ap.upload_media(Path(item["ep_dir"]) / item["clip"]["file"])
            body = ap.build_post(dict(item["clip"], analysis=item["analysis"]), item["meta"],
                                 {"youtube": account["id"]}, media, when, eid, ["youtube"])
            # Persist intent before submission so an interruption cannot duplicate a Short.
            ledger["posts"].append(record)
            ap.save(LEDGER_FILE, ledger)
            remote = ap.api("POST", "/v1/social-posts", body)
            remote = ap.validate_post(remote, eid, [account["id"]], when, True)
        if record not in ledger["posts"]:
            ledger["posts"].append(record)
        record.update(status="scheduled", post_id=remote["id"], scheduled_at=remote["scheduled_at"])
        ap.save(LEDGER_FILE, ledger)
        ap.log(f"YouTube Short scheduled: {record['clip']} for {when:%a %b %d %H:%M}")
        count += 1
    return f"YouTube weekly: {count} Short(s) added; Monday/Wednesday/Friday at 17:00."
