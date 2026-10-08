"""Publish the owned podcast feed and media to Cloudflare R2 using its S3 API."""

import getpass
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import podcast_distribution as pd


def credentials_file():
    return pd.ap.STATE_DIR / "podcast-r2-credentials.json"


def client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise pd.PodcastError("Install podcast storage dependencies: .venv/bin/pip install -r requirements.txt")
    credentials = pd.ap.load(credentials_file(), {})
    if not credentials.get("access_key_id") or not credentials.get("secret_access_key"):
        raise pd.PodcastError("R2 credentials missing; run ./autopilot podcast-setup.")
    account = pd.setting("r2_account_id", "")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise pd.PodcastError("R2 account ID must be the 32-character Cloudflare account ID.")
    return boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                        aws_access_key_id=credentials["access_key_id"],
                        aws_secret_access_key=credentials["secret_access_key"], region_name="auto",
                        config=Config(connect_timeout=15, read_timeout=120,
                                      request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required",
                                      retries={"max_attempts": 3, "mode": "standard"}))


def public_base():
    base = pd.setting("r2_public_url", "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise pd.PodcastError("R2 public URL must be an HTTPS bucket/custom-domain URL without query parameters.")
    return base


def validate_settings():
    public_base()
    if not pd.setting("r2_bucket") or not pd.setting("owner_email") or not pd.setting("artwork_url"):
        raise pd.PodcastError("R2 bucket, podcast owner email and public cover-art URL are required; run podcast-setup.")
    artwork = urllib.parse.urlparse(pd.setting("artwork_url"))
    if artwork.scheme != "https" or not artwork.hostname:
        raise pd.PodcastError("Podcast cover art must have a public HTTPS URL.")


def read_remote_feed(storage, bucket):
    try:
        response = storage.get_object(Bucket=bucket, Key="feed.xml")
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            return []
        raise pd.PodcastTransient("Unable to read the existing R2 feed; leaving it unchanged.") from e
    body = response["Body"]
    try:
        root = ET.fromstring(body.read())
    except ET.ParseError as e:
        raise pd.PodcastError("Existing R2 feed is invalid; refusing to replace it.") from e
    finally:
        body.close()
    if root.tag != "rss" or root.find("channel") is None:
        raise pd.PodcastError("Existing R2 feed is not a podcast RSS feed.")
    rows = []
    for item in root.findall("./channel/item"):
        guid = item.findtext("guid", "")
        enclosure = item.find("enclosure")
        if not guid.startswith("pursuit:") or enclosure is None:
            raise pd.PodcastError("Existing R2 feed contains unrecognized episodes; refusing to replace it.")
        rows.append({"video_id": guid.removeprefix("pursuit:"), "guid": guid,
                     "title": item.findtext("title", ""), "pubDate": item.findtext("pubDate", ""),
                     "description": item.findtext("description", ""),
                     "explicit": item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit", "false"),
                     "enclosure_url": enclosure.attrib["url"], "length": int(enclosure.attrib["length"])})
    return rows


def check_audio(url, expected_length):
    request = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 206 or not response.headers.get("Content-Range", "").endswith(f"/{expected_length}"):
            raise pd.PodcastError("Public audio must support byte-range requests and expose the correct length.")
        if len(response.read(1024)) != min(expected_length, 1024):
            raise pd.PodcastError("Public audio is incomplete.")


def check_artwork():
    import cv2
    import numpy as np
    with urllib.request.urlopen(pd.setting("artwork_url"), timeout=30) as response:
        data = response.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024 or not data.startswith((b"\xff\xd8", b"\x89PNG\r\n\x1a\n")):
        raise pd.PodcastError("Cover art must be a public JPEG or PNG no larger than 10 MB.")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[0] != image.shape[1] or not 1400 <= image.shape[0] <= 3000:
        raise pd.PodcastError("Cover art must be a square JPEG/PNG between 1400 and 3000 pixels per side.")


def publish(ep, mp3):
    validate_settings()
    storage, bucket = client(), pd.setting("r2_bucket")
    base = public_base()
    # The remote feed is authoritative, including after loss of local state.
    rows = read_remote_feed(storage, bucket)
    key = f"media/{ep['id']}.mp3"
    audio_url = f"{base}/{key}"
    row = {"video_id": ep["id"], "title": ep["title"], "guid": f"pursuit:{ep['id']}",
           "pubDate": pd.rss_pub_date(ep.get("upload_date")), "published_at": pd.ap.now().isoformat(),
           "description": pd.clean_description(ep.get("description")) + f"\n\nYouTube version: {ep['url']}",
           "enclosure_url": audio_url, "length": mp3.stat().st_size,
           "explicit": pd.setting("explicit", "false")}
    existing = next((r for r in rows if r["video_id"] == ep["id"]), None)
    try:
        check_artwork()
        if not existing:
            storage.upload_file(str(mp3), bucket, key,
                                ExtraArgs={"ContentType": "audio/mpeg", "CacheControl": "public, max-age=3600"})
            check_audio(audio_url, row["length"])
            rows.append(row)
            feed = pd.render_self_hosted_rss(rows)
            storage.upload_file(str(feed), bucket, "feed.xml",
                                ExtraArgs={"ContentType": "application/rss+xml", "CacheControl": "no-cache, max-age=0"})
        else:
            check_audio(existing["enclosure_url"], existing["length"])
        # Feed upload timeouts can be retried: the next run adopts the stable GUID.
        with urllib.request.urlopen(f"{base}/feed.xml", timeout=30) as response:
            public_feed = ET.fromstring(response.read())
        remote_guids = {item.findtext("guid") for item in public_feed.findall("./channel/item")}
        if not {r["guid"] for r in rows}.issubset(remote_guids):
            raise pd.PodcastTransient("Public RSS has not refreshed yet; retry on the next run.")
    except (pd.PodcastError, pd.PodcastTransient):
        raise
    except Exception as e:
        raise pd.PodcastTransient("R2 publication or public verification failed; retry on the next run.") from e
    pd.ap.save(pd.ledger_file(), {"episodes": rows})
    return {"provider": "r2", "feed_url": f"{base}/feed.xml", "audio_url": audio_url}


def setup(args):
    if not re.fullmatch(r"[a-fA-F0-9]{32}", args.account_id):
        raise pd.PodcastError("Use the 32-character Cloudflare account ID.")
    parsed = urllib.parse.urlparse(args.public_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise pd.PodcastError("Use an HTTPS public bucket URL without a query or fragment.")
    artwork = urllib.parse.urlparse(args.artwork_url)
    if artwork.scheme != "https" or not artwork.hostname or "@" not in args.owner_email:
        raise pd.PodcastError("Supply a public HTTPS cover URL and the show owner's email.")
    print("Enter the bucket-scoped R2 S3 credentials (input is hidden).")
    key = getpass.getpass("Access Key ID: ").strip()
    secret = getpass.getpass("Secret Access Key: ").strip()
    if not key or not secret:
        raise pd.PodcastError("Both R2 credentials are required.")
    pd.ap.save(credentials_file(), {"access_key_id": key, "secret_access_key": secret})
    config = pd.ap.load(pd.ap.CONFIG_FILE, {})
    pod = config.setdefault("podcast", {})
    pod.update(mode="r2", r2_account_id=args.account_id, r2_bucket=args.bucket,
               r2_public_url=args.public_url.rstrip("/"), owner_email=args.owner_email,
               artwork_url=args.artwork_url, allow_ytdlp=True, enabled=False)
    pd.ap.save(pd.ap.CONFIG_FILE, config)
    state = pd.load_state()
    for record in state["episodes"].values():
        if record.get("status") == "needs_source":
            record["status"] = "source_ready"
    pd.ap.save(pd.state_file(), state)
    print(f"Configured. Automation is OFF. Feed URL after first publication: {args.public_url.rstrip('/')}/feed.xml")
    print("Next: ./autopilot podcast-publish latest --dry-run, then podcast-publish latest")
