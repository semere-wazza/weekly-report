# weekly-report

Generates weekly work-update emails from your GitHub commit history, optionally
narrated by AI (Gemini free tier or Claude).

## Setup

```
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
GITHUB_TOKEN=ghp_xxx          # personal access token, scopes: repo, read:org
GITHUB_USERNAME=yourusername
REPORT_NAME=Your Name         # used in report header/sign-off
GEMINI_API_KEY=xxx            # optional, free tier: https://aistudio.google.com/apikey
GROQ_API_KEY=gsk_xxx          # optional, free tier fallback: https://console.groq.com/keys
ANTHROPIC_API_KEY=sk-ant-xxx  # optional, fallback if Gemini and Groq both fail
GMAIL_ADDRESS=you@gmail.com   # only needed for --send
GMAIL_APP_PASSWORD=xxxx       # only needed for --send, see below
REPORT_TO=a@x.com             # only needed for --send, comma-separated "To"
REPORT_CC=b@x.com,c@x.com     # optional, comma-separated "Cc"
```

Get a GitHub token: https://github.com/settings/tokens

### Gmail App Password (for `--send`)

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
3. Paste the 16-char password into `GMAIL_APP_PASSWORD` (no spaces)

## Usage

```
python3 weekly-report.py --orgs my-org another-org --days 7
python3 weekly-report.py --orgs my-org --since 2026-08-01
python3 weekly-report.py --orgs my-org --no-ai       # skip AI, print raw commits
python3 weekly-report.py --orgs my-org --send        # also emails the report via Gmail
python3 weekly-report.py --orgs my-org --format md   # save as markdown instead of plain text
python3 weekly-report.py --orgs my-org --format html # save as html
python3 weekly-report.py --orgs my-org --dry-run     # print only, skip saving/sending
python3 weekly-report.py --orgs my-org --no-cache    # force a fresh GitHub fetch, bypass the cache
python3 weekly-report.py --orgs my-org --cache-ttl 300  # cache freshness window in seconds (default: 3600)
```

Report prints to terminal and saves to `reports/<since-date>_to_<until-date>.<txt|md|html>`.

GitHub API calls auto-retry with backoff on rate limits (403/429) and server errors (5xx),
up to 4 attempts per request.

GitHub commit/PR responses are cached on disk under `.cache/`, keyed by (username, orgs,
since date, branch). Reruns with the same params within `--cache-ttl` reuse the cache
instead of re-hitting the API — useful when iterating on `--format`/`--dry-run` without
waiting on network calls each time.
