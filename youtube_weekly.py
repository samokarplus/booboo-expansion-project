"""Independent three-per-week YouTube Shorts scheduling through Post for Me."""
import datetime as dt
import json
import re
from pathlib import Path

import autopilot as ap

WEEKDAYS = (0, 2, 4)
POST_TIME = dt.time(5, 0)
SCHEDULE_AHEAD = 12
MAX_RETRIES = 3
LEDGER_FILE = ap.STATE_DIR / "youtube-weekly-ledger.json"
RETRYABLE_ERROR_PATTERNS = ("429", "quota exceeded", "ratelimitexceeded", "resource_exhausted")


def slots(posts, count, current=None):
    current = current or ap.now()
    earliest = current + dt.timedelta(minutes=30)
    if posts:
        earliest = max(earliest, max(dt.datetime.fromisoformat(p["scheduled_at"])
                                     for p in posts) + dt.timedelta(minutes=1))
    day, result = earliest.astimezone(ap.TZ).date(), []
    while len(result) < count:
        when = dt.datetime.combine(day, POST_TIME, ap.TZ)
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


def base_external_id(external_id):
    return re.sub(r"-r\d+$", "", external_id)


def related_posts(posts, base_id):
    return [p for p in posts if base_external_id(p.get("external_id", "")) == base_id]


def retry_count(posts, base_id):
    return sum(1 for p in related_posts(posts, base_id) if p.get("status") == "retry")


def retry_external_id(item, posts):
    base_id = external_id(item)
    count = retry_count(posts, base_id)
    return base_id if count == 0 else f"{base_id}-r{count}"


def active_or_final(post):
    return post.get("status") in {"unknown", "scheduled", "posted", "failed", "unclear"}


def quota_retryable(error):
    try:
        text = json.dumps(error, sort_keys=True)
    except TypeError:
        text = str(error)
    text = text.lower()
    return any(pattern in text for pattern in RETRYABLE_ERROR_PATTERNS)


def retire_remote_post(post):
    if not post.get("post_id"):
        return
    ap.api("DELETE", f"/v1/social-posts/{post['post_id']}")


def local_scheduled_at(remote):
    return dt.datetime.fromisoformat(remote["scheduled_at"].replace("Z", "+00:00")).astimezone(ap.TZ).isoformat()


def candidates(queue, posts):
    result = []
    for item in sorted(queue["clips"], key=lambda q: -q["score"]):
        clip = item["clip"]
        base_id = external_id(item)
        if item["status"] not in ("queued", "scheduled") or \
                any(active_or_final(p) for p in related_posts(posts, base_id)):
            continue
        if not ap.recent_visual_clip(item["meta"], clip):
            continue
        if not (Path(item["ep_dir"]) / clip["file"]).is_file():
            continue
        start, end = float(clip["start_sec"]), float(clip["end_sec"])
        if any(active_or_final(p) and p["episode"] == item["episode"] and
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
            post.update(post_id=remote["id"], status="scheduled", scheduled_at=local_scheduled_at(remote))
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
        url = (result.get("platform_data") or {}).get("url")
        error = result.get("error")
        if result.get("success"):
            post.update(status="posted", url=url, error=error)
            ap.save(LEDGER_FILE, ledger)
            continue
        base_id = base_external_id(post["external_id"])
        if quota_retryable(error):
            if retry_count(ledger["posts"], base_id) >= MAX_RETRIES:
                post.update(status="failed", url=url, error=error)
                ap.save(LEDGER_FILE, ledger)
                ap.notify("PURSUIT YouTube publishing problem",
                          f"{post['clip']}: quota retry limit reached after {MAX_RETRIES} retries. "
                          f"{str(error)[:160]}")
                continue
            retire_remote_post(post)
            post.update(status="retry", url=url, error=error, retryable=True,
                        retry_at=ap.now().isoformat())
            ap.save(LEDGER_FILE, ledger)
            ap.log(f"YouTube Short will retry after quota failure: {post['clip']}")
            continue
        post.update(status="failed", url=url, error=error)
        ap.save(LEDGER_FILE, ledger)
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
        eid = retry_external_id(item, ledger["posts"])
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
        record.update(status="scheduled", post_id=remote["id"], scheduled_at=local_scheduled_at(remote))
        ap.save(LEDGER_FILE, ledger)
        ap.log(f"YouTube Short scheduled: {record['clip']} for {when:%a %b %d %H:%M}")
        count += 1
    return f"YouTube weekly: {count} Short(s) added; Monday/Wednesday/Friday at 05:00."
