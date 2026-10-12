# Haber Ajansı

Automated News Intelligence Pipeline that monitors YouTube news channels daily and delivers AI-generated reports to Discord.

## What it does
- Checks YouTube news channels for new videos every day
- Fetches transcripts via Apify
- Analyzes transcripts with Google Gemini AI
- Sends structured reports to a Discord channel

## Setup

### Requirements
- Python 3
- YouTube Data API key
- Gemini API key(s)
- Apify API key
- Discord Webhook URL

### GitHub Secrets
Add these to your repository secrets:
- `YOUTUBE_API_KEY`
- `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`
- `APIFY_API_KEY`
- `DISCORD_WEBHOOK_URL`

## Deployment
Runs automatically every day via GitHub Actions.

---

# Twitter Haber Ajansı

Second pipeline in this repo (`twitter_main.py`, workflow `twitter.yml`). It scans the posts of the X (Twitter) accounts you follow, drops the noise (jokes, greetings, ads, fan chatter, reaction posts) with Gemini, groups the newsworthy posts by topic and sends a "Twitter Gündem Raporu" to Discord. Runs daily (last 24 h) and every Monday as a weekly summary (last 7 days).

## How it gets the accounts
1. `twitter/accounts.txt` — if it lists at least one handle, only those are scanned.
2. Otherwise the following list of the `X_HANDLE` account is fetched through Apify and cached in `twitter/followings.json` for 7 days.

## Setup
Secrets (shared with the YouTube pipeline): `APIFY_API_KEY`, `GEMINI_API_KEY_1..3`, `DISCORD_WEBHOOK_URL`.
Optional secret `TWITTER_DISCORD_WEBHOOK_URL` sends Twitter reports to a different Discord channel.

Repository **variables** (Settings → Secrets and variables → Actions → Variables):
- `X_HANDLE` — your X username without `@`. Defaults to `SadettinZ_` in the workflow; set the variable to override.
- `DISCORD_MENTION_USER_ID` — optional, Discord user id to ping at the top of the report.

Tuning via env in `twitter.yml`: `MAX_TWEETS_PER_ACCOUNT` (40), `INCLUDE_REPLIES` (0), `HANDLES_PER_RUN` (25), `APIFY_TWEET_ACTOR` (`apidojo~tweet-scraper`), `APIFY_FOLLOWING_ACTOR` (`apidojo~twitter-user-scraper`).

## Manual run
Actions → "Twitter Haber Ajansı" → Run workflow. `mode=scrape` only downloads the posts to `twitter/tweets.json` (no Gemini, no Discord). `hours` sets the window. `refresh_followings` re-fetches the following list.

## Output
- Discord message (chunked), with source links per topic.
- `twitter/reports/YYYY-MM-DD.md` (daily) or `YYYY-MM-DD-7gun.md` (weekly), committed by the workflow.
- `twitter/tweets.json` — the filtered posts of the last run.
