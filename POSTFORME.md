# Post for Me and Connecting YouTube

[Post for Me](https://www.postforme.dev/) is a hosted social-media publishing service operated by **Day Moon Development LLC**. It gives apps one interface for account connections, video uploads, scheduling, and publishing across **nine platforms**, including TikTok, Instagram, and YouTube. In this project, the Mac does the creative work: finding moments, editing, captioning, and checking clips. Post for Me handles delivery of approved TikTok posts and the weekly YouTube Shorts.

### Who runs it and how established is it?

Day Moon Development was started in **2023** by founders Caleb and Matt, who developed Post for Me from social-media integrations they built for clients. Its [public source repository](https://github.com/DayMoonDevelopment/post-for-me) contains the API, dashboard, and background-job code. As of **October 5, 2026**, GitHub showed **78 stars, 25 forks, and more than 1,100 commits**. These describe its public development footprint, not its number of customers. The official pages reviewed do not publish a verified customer or connected-account count. [Company background](https://www.postforme.dev/day-moon-development) · [Product background](https://www.postforme.dev/about)

### What connecting a YouTube channel means

YouTube connection uses **Google's OAuth authorization process**: the channel owner signs in with Google, chooses the intended account/channel, and reviews the requested permissions on Google's consent screen. The app receives authorization tokens rather than the owner's Google password. Those tokens let the service act within the permissions granted; the exact consent screen matters, since YouTube permissions can include video management, not just uploading. Connecting is a real authorization decision, and does not transfer ownership of the channel. [Google's OAuth explanation and permission scopes](https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps)

Post for Me's [YouTube integration](https://www.postforme.dev/integrations/youtube) supports uploads, scheduling, titles, thumbnails, and public/private/unlisted visibility. Its [privacy policy](https://www.postforme.dev/privacy) says it stores channel identifiers, account metadata, and OAuth tokens; encrypts data in transit and at rest; protects production access with least-privilege roles and mandatory two-factor authentication; and does not sell or rent user information. These are the provider's published commitments.

The owner can disconnect YouTube in Post for Me or revoke its access through [Google Account connections](https://myaccount.google.com/connections). Revocation stops future authorized access; it does not undo videos already published or automatically remove previously shared data. [Google's connection-management guide](https://support.google.com/accounts/answer/13533235)

### How PURSUIT currently uses YouTube

**As of October 7, 2026, automatic YouTube Shorts scheduling is enabled in the local deployment.** Post for Me is connected to **PURSUIT with Anya Postnikov (@AnyaPostnikov)**, pinned by channel ID `UCkw_7bkF1qSRrupIvN4SIAg`. Quality-approved Shorts are scheduled on **Monday, Wednesday, and Friday at 5 PM America/Denver**, independently of TikTok's three-per-day schedule. Up to three Shorts are submitted ahead, and empty slots are skipped. Creating clips happens locally; publishing uses the connected YouTube authorization. Optional Drive review delivery remains available separately.

