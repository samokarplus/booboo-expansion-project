# Automatic PURSUIT Episodes on Spotify

**Stop before migration:** the signed-in show's redirect dialog says this move removes Spotify-hosted ads monetization and converts video episodes to audio. The user paused to review that tradeoff; the final redirect was cancelled and automation is OFF. Creator sign-in is complete. Read [Spotify Hosting and Monetization](PROJECT_GUIDE.md#spotify-hosting-and-monetization) before applying the setup instructions below. They describe the Cloudflare route, not a recommendation to give up Spotify hosting.

The program downloads the full YouTube episode, converts it to MP3, uploads it to Cloudflare R2, and updates a public podcast RSS feed. After Anya connects that feed to Spotify once, Spotify imports future episodes. No per-episode Spotify upload is needed. Spotify controls the import delay; `published` in this tool means the public feed has been verified, not that Spotify has finished importing it.

**Live setup status, October 8, 2026:** the Cloudflare account, private bucket, publishing credential, cover upload and server deployment are complete. The first approved real episode is published to our [public feed](https://pursuit-podcast.endlesspursuits-co.workers.dev/feed.xml), and public audio/feed verification passed. Spotify has not yet been connected to this new feed; recurring publication is OFF. Start with [Finish Spotify Once](PROJECT_GUIDE.md#finish-spotify-once), not the account-creation steps already completed below.

For the plain-English overview and monthly budget, read [Services, Costs, and What You Do](PROJECT_GUIDE.md). The following instructions remain useful for an initial setup or another machine.

## What Anya Needs to Create

1. A [Cloudflare account](https://dash.cloudflare.com/sign-up), with R2 enabled. Cloudflare may require billing details even when usage fits the free allowance. Create a **Standard** storage bucket named `pursuit-podcast`. Use a dedicated bucket, with no automatic expiry rules for podcast audio. Leave bucket public access disabled; the included Worker serves only the feed, audio and cover.
2. In R2, create S3 credentials with **Object Read & Write** permission restricted to this bucket. Keep the Account ID, Access Key ID and Secret Access Key private. Enter the keys in the local setup prompt, not in messages or GitHub. They are saved outside the repository with owner-only file permissions.
3. A square podcast cover image, ideally 1400-3000 pixels per side. Upload it to the bucket as `cover.jpg` or `cover.png`. Choose the show title, description, explicit-content setting and the email Anya can receive Spotify verification at. The owner email will appear in the public RSS feed.
4. A [Spotify for Creators account](https://creators.spotify.com/) for the intended owner. For this automatic route, **submit an existing RSS show**, rather than creating a second show hosted by Spotify. If PURSUIT already exists there, check its current hosting/feed before creating another listing.

No domain purchase is needed: the included Cloudflare Worker exposes a `workers.dev` HTTPS address. Cloudflare recommends custom domains for business-critical use; a domain can be connected later. This route avoids a podcast-host subscription, but is not unlimited free hosting. R2 Standard currently includes **10 GB** and operation allowances; Workers Free includes **100,000 requests/day**. Usage above allowances can incur charges or hit limits. Set billing notifications and check usage. A one-hour episode at 160 kbps uses approximately 72 MB before small overhead.

Sources: [R2 pricing](https://developers.cloudflare.com/r2/pricing/), [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [workers.dev routing](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/).

## Deploy the Public Feed Server Once

On the Mac running this project, install the added Python dependency:

```bash
cd ~/Documents/PURSUIT_CLIPS_TOOL
.venv/bin/pip install -r requirements.txt
```

Use Node.js and Cloudflare's Wrangler CLI to deploy the included server. Sign in with the Cloudflare account that owns the bucket:

```bash
cd ~/Documents/PURSUIT_CLIPS_TOOL/cloudflare-podcast
npx wrangler login --scopes user:read account:read workers_scripts:write
npx wrangler deploy
```

The deployment prints an address like `https://pursuit-podcast.YOUR-SUBDOMAIN.workers.dev`. Keep that address stable. If the bucket has another name, update `bucket_name` in `wrangler.jsonc` before deploying. Only one Mac should write to this show's feed; local locking does not coordinate multiple computers.

The Worker permits only GET/HEAD requests for `feed.xml`, `cover.jpg`, `cover.png` and `media/VIDEO_ID.mp3`. It streams audio, supports byte ranges for podcast players, and serves the feed without stale caching. The private bucket's write credentials are used by the local publisher, not exposed through the Worker.

## Configure and Test Once

Return to the project root and substitute the actual non-secret settings:

```bash
cd ~/Documents/PURSUIT_CLIPS_TOOL
./autopilot podcast-setup \
  --account-id YOUR_CLOUDFLARE_ACCOUNT_ID \
  --bucket pursuit-podcast \
  --public-url https://pursuit-podcast.YOUR-SUBDOMAIN.workers.dev \
  --owner-email OWNER_EMAIL \
  --artwork-url https://pursuit-podcast.YOUR-SUBDOMAIN.workers.dev/cover.jpg
```

Enter the R2 keys at the hidden prompts. Setup selects automatic R2 publication, allows downloading the owner's YouTube episodes, and leaves automation off. Settings and credentials persist for scheduled runs. Show settings can be changed in the `podcast` section of `~/Library/Application Support/PURSUIT_AUTOPILOT/config.json`: `title`, `description`, `author`, `category`, `language` and `explicit`. Defaults describe PURSUIT with Anya Postnikov; review them before the first publication.

Check conversion without uploading, then publish the first full episode:

```bash
./autopilot podcast-publish latest --dry-run
./autopilot podcast-publish latest
./autopilot podcast-status
```

These commands can take time while downloading and converting a full episode. The dry run creates local MP3/status files but does not upload. You can use a specific YouTube URL in place of `latest`, including an older episode. If downloading fails, provide the original export with `--source /path/to/episode.mp4`.

The publisher uploads the audio first, verifies public byte-range access, and then uploads the feed. Stable video-ID filenames and RSS GUIDs make retries idempotent. It reads the remote feed before each update to preserve existing episodes even after local state is lost. Upload/verification failures do not record successful publication. It refuses to overwrite a feed with unrecognized episode GUIDs.

## Anya Connects Spotify Once

1. Open the printed feed URL, ending in `/feed.xml`, and verify it is publicly readable. Open the cover URL and test the MP3 from the feed. Check the show's details and owner email; validate the RSS with a podcast-feed validator.
2. For this deployment, sign in to the account owning the existing PURSUIT show. Inspect its current hosting/feed settings, then use the applicable feed update or host-migration procedure to connect our feed. Review and approve any migration before applying it; preserve existing episodes/listeners and do not create a duplicate listing. See [Spotify feed updates](https://support.spotify.com/dj-en/podcasters/article/updating-an-rss-feed-link-or-hosting-provider/) and [moving a Spotify-hosted show](https://support.spotify.com/ws/creators/article/switching-away-from-spotify-for-creators-with-a-301-redirect/). A genuinely new show without an existing listing can instead be added/claimed using its RSS URL. Complete any ownership verification sent to the feed owner's email.
3. Confirm the first episode appears on Spotify. [Spotify's ownership instructions](https://support.spotify.com/us/creators/article/claiming-your-podcast-on-spotify-for-creators/).
4. Start watching for future uploads, then enable recurring publication:

   ```bash
   ./autopilot podcast-run --once --dry-run
   ./autopilot podcast-auto on
   ```

The first watching run records the current latest YouTube episode as its starting point. Older episodes are not automatically backfilled; use `podcast-publish URL` for selected older episodes. Later scheduled live autopilot runs publish newer full-length episodes, oldest first, one per run.

## Thereafter

**Anya:** publish the full episode to YouTube as usual. Maintain the Cloudflare/Spotify accounts and respond if credentials or downloads fail. There is no routine Spotify upload step.

**Program:** detect the new episode, retrieve media, convert MP3, upload audio, update/verify RSS and record status. The Mac must be awake and online; existing autopilot pause controls apply. Hosting remains available when the Mac is off, but new episodes cannot be processed until it runs again.

**Spotify:** import the submitted RSS feed on its own schedule. The program does not control or verify that import timing.

Use `./autopilot podcast-status` for errors and `./autopilot podcast-auto off` to stop future publication. Already hosted audio and the feed stay online; this command does not remove existing episodes.
