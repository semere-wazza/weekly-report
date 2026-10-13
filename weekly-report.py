#!/usr/bin/env python3
"""
weekly_report.py

Generates a concise weekly report of your GitHub activity across
specific orgs/accounts, printed to the terminal.

SETUP
-----
1. pip install requests python-dotenv
2. Set env vars (or put them in a .env file):
     export GITHUB_TOKEN=ghp_xxx          # personal access token (repo/read:org scopes)
     export GITHUB_USERNAME=yourusername  # your GitHub username(s), comma-separated if you commit under multiple accounts (e.g. "personal-login,work-login")
     export REPORT_NAME="Your Name"       # optional: display name used in report header/sign-off
     export GEMINI_API_KEY=xxx            # optional: enables AI narrative summaries (free tier)
     export GROQ_API_KEY=gsk_xxx          # optional: fallback if Gemini fails/unset (free tier, console.groq.com/keys)
     export ANTHROPIC_API_KEY=sk-ant-xxx  # optional: fallback if Gemini and Groq both fail/unset
     export GMAIL_ADDRESS=you@gmail.com   # required for --send: sending Gmail address
     export GMAIL_APP_PASSWORD=xxxx       # required for --send: Gmail App Password (myaccount.google.com/apppasswords)
     export REPORT_TO=a@x.com             # required for --send: comma-separated "To" recipient list
     export REPORT_CC=b@x.com,c@x.com     # optional for --send: comma-separated "Cc" recipient list

USAGE
-----
  python weekly_report.py --orgs my-org another-org --days 7
  python weekly_report.py --orgs my-org --since 2026-08-01
  python weekly_report.py --orgs my-org --send            # also emails the report via Gmail
  python weekly_report.py --orgs my-org --format md       # save as reports/<range>.md
  python weekly_report.py --orgs my-org --dry-run         # print only, skip save + send
  python weekly_report.py --orgs my-org --branch dev            # also count commits on 'dev' (in addition to default branch)
  python weekly_report.py --orgs my-org --branch dev master     # also count commits on multiple branches, deduped by sha
  python weekly_report.py --orgs my-org --no-cache        # bypass the on-disk GitHub API cache, force a fresh fetch
  python weekly_report.py --orgs my-org --cache-ttl 300   # treat cached data older than 5 min as stale (default: 3600s)
  python weekly_report.py --orgs my-org --username a-login b-login  # count commits/PRs authored by either GitHub login

GitHub commit/PR responses are cached on disk under .cache/, keyed by (username, orgs,
since date, branch). Reruns with the same params within --cache-ttl reuse the cache
instead of re-hitting the GitHub API — handy for --dry-run iteration or tweaking
--format without waiting on network calls again.
"""

import os
import sys
import time
import json
import hashlib
import argparse
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

GITHUB_API = "https://api.github.com"
MAX_RETRIES = 4
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL_SECONDS = 3600  # 1 hour — rerunning within the same hour reuses fetched data


def get_env_or_die(var_name):
    val = os.environ.get(var_name)
    if not val:
        print(f"ERROR: {var_name} environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def cache_key(label, *parts):
    raw = json.dumps(parts, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{label}_{digest}.json")


def cache_load(path, ttl):
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > ttl:
        return None
    try:
        with open(path) as f:
            return json.load(f)["data"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def cache_save(path, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"cached_at": time.time(), "data": data}, f)


def cached_fetch(label, key_parts, ttl, use_cache, fetch_fn):
    """Load `fetch_fn()`'s result from disk cache if fresh, else fetch and cache it."""
    path = cache_key(label, *key_parts)
    if use_cache:
        cached = cache_load(path, ttl)
        if cached is not None:
            print(f"Using cached {label} (age < {ttl}s)...", file=sys.stderr)
            return cached

    data = fetch_fn()
    cache_save(path, data)
    return data


def get_with_retry(url, headers, params, label, quiet_statuses=()):
    """GET with exponential backoff on rate limits (403/429) and server errors (5xx).

    `quiet_statuses` are returned as-is without a warning or retry (e.g. 404 for a
    branch that doesn't exist in a given repo — expected, not worth logging).
    """
    delay = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"WARNING: {label} network error (attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            resp = None
            if attempt < MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return None

        if resp.status_code == 200:
            return resp

        if resp.status_code in quiet_statuses:
            return resp

        if resp.status_code in (403, 429):
            reset = resp.headers.get("X-RateLimit-Reset")
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = int(retry_after)
            elif reset:
                wait = max(int(reset) - int(time.time()), delay)
            else:
                wait = delay
            print(f"WARNING: {label} rate-limited (attempt {attempt}/{MAX_RETRIES}), waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            delay *= 2
            continue

        if resp.status_code >= 500:
            print(f"WARNING: {label} server error {resp.status_code} (attempt {attempt}/{MAX_RETRIES}), retrying in {delay}s...", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue

        print(f"WARNING: {label} failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return resp

    return None


def fetch_commits_search(token, usernames, orgs, since_iso):
    """Search commits authored by any of `usernames` within given orgs since `since_iso`.

    Note: GitHub's commit search only indexes each repo's default branch.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    seen_shas = set()
    all_items = []

    for org in orgs:
        for username in usernames:
            query = f"author:{username} org:{org} committer-date:>={since_iso}"
            page = 1
            while True:
                resp = get_with_retry(
                    f"{GITHUB_API}/search/commits",
                    headers,
                    {"q": query, "per_page": 100, "page": page, "sort": "committer-date"},
                    f"commit search for org '{org}' author '{username}'",
                )
                if resp is None or resp.status_code != 200:
                    break
                data = resp.json()
                items = data.get("items", [])
                for item in items:
                    if item["sha"] in seen_shas:
                        continue
                    seen_shas.add(item["sha"])
                    all_items.append(item)
                if len(items) < 100:
                    break
                page += 1

    return all_items


def list_org_repos(token, org):
    """List all repo full_names in an org."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    repos = []
    page = 1
    while True:
        resp = get_with_retry(
            f"{GITHUB_API}/orgs/{org}/repos",
            headers,
            {"per_page": 100, "page": page, "type": "all"},
            f"repo list for org '{org}'",
        )
        if resp is None or resp.status_code != 200:
            break
        data = resp.json()
        if not data:
            break
        repos.extend(r["full_name"] for r in data)
        if len(data) < 100:
            break
        page += 1
    return repos


def fetch_commits_on_branches(token, usernames, orgs, branches, since_iso):
    """Fetch commits authored by any of `usernames` on any of `branches`, across all repos in each org.

    Commits are deduped by sha (the same commit can exist on multiple branches, e.g. a
    branch merged into another, or be found under more than one author identity).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    since_full = f"{since_iso}T00:00:00Z"
    seen_shas = set()
    all_items = []

    for org in orgs:
        repo_names = list_org_repos(token, org)
        for repo_full_name in repo_names:
            for branch in branches:
                for username in usernames:
                    page = 1
                    while True:
                        resp = get_with_retry(
                            f"{GITHUB_API}/repos/{repo_full_name}/commits",
                            headers,
                            {"sha": branch, "author": username, "since": since_full, "per_page": 100, "page": page},
                            f"commits on branch '{branch}' for {repo_full_name} author '{username}'",
                            quiet_statuses=(404, 409),
                        )
                        if resp is None or resp.status_code != 200:
                            break
                        items = resp.json()
                        for item in items:
                            if item["sha"] in seen_shas:
                                continue
                            seen_shas.add(item["sha"])
                            all_items.append({
                                "sha": item["sha"],
                                "commit": {"message": item["commit"]["message"]},
                                "repository": {"full_name": repo_full_name},
                            })
                        if len(items) < 100:
                            break
                        page += 1

    return all_items


def fetch_commits(token, usernames, orgs, since_iso, branches=None):
    """Search-based fetch (default branch, all orgs) plus any extra `branches` requested, deduped by sha."""
    search_items = fetch_commits_search(token, usernames, orgs, since_iso)
    if not branches:
        return search_items

    branch_items = fetch_commits_on_branches(token, usernames, orgs, branches, since_iso)
    seen_shas = {item["sha"] for item in search_items}
    for item in branch_items:
        if item["sha"] not in seen_shas:
            seen_shas.add(item["sha"])
            search_items.append(item)
    return search_items


def fetch_prs(token, usernames, orgs, since_iso):
    """Search PRs authored by any of `usernames` within given orgs, created since `since_iso`."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    seen_ids = set()
    all_items = []

    for org in orgs:
        for username in usernames:
            query = f"author:{username} org:{org} type:pr created:>={since_iso}"
            page = 1
            while True:
                resp = get_with_retry(
                    f"{GITHUB_API}/search/issues",
                    headers,
                    {"q": query, "per_page": 100, "page": page, "sort": "created"},
                    f"PR search for org '{org}' author '{username}'",
                )
                if resp is None or resp.status_code != 200:
                    break
                data = resp.json()
                items = data.get("items", [])
                for item in items:
                    if item["id"] in seen_ids:
                        continue
                    seen_ids.add(item["id"])
                    all_items.append(item)
                if len(items) < 100:
                    break
                page += 1

    return all_items


# Commit messages that carry no real signal for a narrative summary.
NOISE_PREFIXES = ("merge branch", "merge pull request", "wip", "typo", "fixup", "minor")


def is_noise_commit(message):
    lowered = message.strip().lower()
    return any(lowered.startswith(p) for p in NOISE_PREFIXES)


def group_by_repo(commit_items):
    grouped = defaultdict(list)
    for item in commit_items:
        message = item["commit"]["message"].split("\n")[0]  # first line only
        if is_noise_commit(message):
            continue
        repo_full_name = item["repository"]["full_name"]
        sha = item["sha"][:7]
        grouped[repo_full_name].append({"sha": sha, "message": message})
    return grouped


def group_prs_by_repo(pr_items):
    grouped = defaultdict(list)
    for item in pr_items:
        repo_full_name = "/".join(item["repository_url"].split("/")[-2:])
        grouped[repo_full_name].append({
            "number": item["number"],
            "title": item["title"],
            "body": (item.get("body") or "").strip(),
            "merged": item.get("pull_request", {}).get("merged_at") is not None,
        })
    return grouped


def build_email_prompt(grouped, pr_grouped):
    repo_names = sorted(set(grouped) | set(pr_grouped))
    sections = []
    for repo_name in repo_names:
        parts = [f"Repo '{repo_name}':"]

        prs = pr_grouped.get(repo_name, [])
        if prs:
            for pr in prs:
                status = "merged" if pr["merged"] else "open"
                pr_text = f"- PR #{pr['number']} ({status}): {pr['title']}"
                if pr["body"]:
                    pr_text += f"\n  {pr['body'][:400]}"
                parts.append(pr_text)

        commits = grouped.get(repo_name, [])
        if commits:
            parts.append("Commits:")
            parts.extend(f"- {c['message']}" for c in commits)

        sections.append("\n".join(parts))

    all_activity = "\n\n".join(sections)

    return (
        "Here is my GitHub activity (pull requests and commits) across repos for the past week:\n\n"
        f"{all_activity}\n\n"
        "Write a weekly work-update email body summarizing what I worked on. "
        "Prefer the PR titles/descriptions as the source of truth for what each change was about "
        "(they carry more context than raw commit messages); use commits only to fill in detail. "
        "Group related work into 2-4 flowing paragraphs by theme/repo (not per-item bullet points), "
        "written in first person, professional but natural tone, like a status update to a manager. "
        "No greeting, no sign-off, no subject line — just the paragraphs of body text."
    )


def post_with_retry(url, label, **kwargs):
    """POST with exponential backoff on rate limits (429), server errors (5xx), and timeouts."""
    delay = 2
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, timeout=60, **kwargs)
        except requests.RequestException as e:
            print(f"WARNING: {label} network error (attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            return resp

        if resp.status_code == 429 or resp.status_code >= 500:
            print(f"WARNING: {label} returned {resp.status_code} (attempt {attempt}/{MAX_RETRIES}), retrying in {delay}s...", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue

        print(f"WARNING: {label} failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return resp

    return resp


def generate_email_with_gemini(grouped, pr_grouped):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    prompt = build_email_prompt(grouped, pr_grouped)

    try:
        resp = post_with_retry(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
            "Gemini summarization",
            headers={"content-type": "application/json"},
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        if resp is None or resp.status_code != 200:
            return None
        candidates = resp.json().get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        return text.strip()
    except Exception as e:
        print(f"WARNING: Gemini summarization failed: {e}", file=sys.stderr)
        return None


def generate_email_with_groq(grouped, pr_grouped):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

    prompt = build_email_prompt(grouped, pr_grouped)

    try:
        resp = post_with_retry(
            "https://api.groq.com/openai/v1/chat/completions",
            "Groq summarization",
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-120b",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if resp is None or resp.status_code != 200:
            return None
        text = resp.json()["choices"][0]["message"]["content"]
        return text.strip()
    except Exception as e:
        print(f"WARNING: Groq summarization failed: {e}", file=sys.stderr)
        return None


def generate_email_with_claude(grouped, pr_grouped):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    prompt = build_email_prompt(grouped, pr_grouped)

    try:
        resp = post_with_retry(
            "https://api.anthropic.com/v1/messages",
            "Claude summarization",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if resp is None or resp.status_code != 200:
            return None
        content = resp.json()["content"]
        text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
        return text.strip()
    except Exception as e:
        print(f"WARNING: Claude summarization failed: {e}", file=sys.stderr)
        return None


def generate_email_body(grouped, pr_grouped):
    """Try Groq first (free tier), then Gemini (free tier), then Claude if configured."""
    return (
        generate_email_with_groq(grouped, pr_grouped)
        or generate_email_with_gemini(grouped, pr_grouped)
        or generate_email_with_claude(grouped, pr_grouped)
    )


def build_report(grouped, pr_grouped, since_date, until_date, use_ai, author_name):
    lines = []
    lines.append(f"Weekly Update — {author_name} — {since_date} to {until_date}")
    lines.append("")

    if not grouped and not pr_grouped:
        lines.append("No activity found in this period.")
        return lines

    body = generate_email_body(grouped, pr_grouped) if use_ai else None
    if body:
        lines.append(body)
    else:
        repo_names = sorted(set(grouped) | set(pr_grouped))
        for repo_name in repo_names:
            prs = pr_grouped.get(repo_name, [])
            commits = grouped.get(repo_name, [])
            lines.append(f"## {repo_name}  ({len(prs)} PR(s), {len(commits)} commit(s))")
            for pr in prs:
                status = "merged" if pr["merged"] else "open"
                lines.append(f"  - PR #{pr['number']} ({status}): {pr['title']}")
            for c in commits:
                lines.append(f"  - [{c['sha']}] {c['message']}")
            lines.append("")

    lines.append("")
    lines.append(author_name)
    return lines


def render_markdown(lines, since_date, until_date, author_name):
    header, _blank, *rest = lines
    body_lines = rest[:-2] if len(rest) >= 2 else rest  # drop trailing blank + sign-off, re-added below
    md = [f"# {header}", ""]
    md.extend(body_lines)
    md.append("")
    md.append(f"*{author_name}*")
    return "\n".join(md) + "\n"


def render_html(lines, since_date, until_date, author_name):
    header, _blank, *rest = lines
    body_lines = rest[:-2] if len(rest) >= 2 else rest
    paragraphs = "\n".join(f"<p>{p}</p>" for p in body_lines if p.strip())
    return (
        "<html><body style=\"font-family: sans-serif; line-height: 1.5;\">\n"
        f"<h2>{header}</h2>\n"
        f"{paragraphs}\n"
        f"<p><em>{author_name}</em></p>\n"
        "</body></html>\n"
    )


def render_report(fmt, lines, since_date, until_date, author_name):
    if fmt == "md":
        return render_markdown(lines, since_date, until_date, author_name)
    if fmt == "html":
        return render_html(lines, since_date, until_date, author_name)
    return "\n".join(lines) + "\n"


def save_report(fmt, lines, since_date, until_date, author_name):
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    ext = {"txt": "txt", "md": "md", "html": "html"}[fmt]
    path = os.path.join(reports_dir, f"{since_date}_to_{until_date}.{ext}")
    content = render_report(fmt, lines, since_date, until_date, author_name)
    with open(path, "w") as f:
        f.write(content)
    return path


def send_email(lines, since_date, until_date, author_name):
    """Send the report via Gmail SMTP using an App Password."""
    gmail_address = get_env_or_die("GMAIL_ADDRESS")
    gmail_app_password = get_env_or_die("GMAIL_APP_PASSWORD")
    report_to = get_env_or_die("REPORT_TO")
    to_recipients = [r.strip() for r in report_to.split(",") if r.strip()]
    cc_recipients = [r.strip() for r in os.environ.get("REPORT_CC", "").split(",") if r.strip()]

    body = "\n".join(lines[2:])  # skip the "Weekly Update — ..." header + blank line, email has its own subject
    msg = MIMEText(body, "plain")
    msg["Subject"] = f"Weekly Update — {author_name} — {since_date} to {until_date}"
    msg["From"] = gmail_address
    msg["To"] = ", ".join(to_recipients)
    if cc_recipients:
        msg["Cc"] = ", ".join(cc_recipients)

    all_recipients = to_recipients + cc_recipients

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, all_recipients, msg.as_string())

    return to_recipients, cc_recipients


def main():
    parser = argparse.ArgumentParser(description="Generate a weekly GitHub activity report.")
    parser.add_argument("--orgs", nargs="+", required=True, help="GitHub orgs/accounts to scan, e.g. --orgs my-org another-org")
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back (default: 7)")
    parser.add_argument("--since", type=str, default=None, help="Explicit start date YYYY-MM-DD (overrides --days)")
    parser.add_argument("--branch", nargs="+", default=None, help="Also count commits on these branch(es) (in addition to the default-branch search), e.g. --branch dev master main")
    parser.add_argument("--username", nargs="+", default=None, help="Override GITHUB_USERNAME; accepts multiple GitHub logins if you commit under more than one account, e.g. --username semere-wazza SemereT627")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI summarization even if GEMINI_API_KEY/ANTHROPIC_API_KEY is set")
    parser.add_argument("--send", action="store_true", help="Email the report via Gmail (requires GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_TO)")
    parser.add_argument("--format", choices=["txt", "md", "html"], default="txt", help="Saved report file format (default: txt)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print the report, but skip saving to disk and sending email")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the on-disk GitHub API cache and force a fresh fetch")
    parser.add_argument("--cache-ttl", type=int, default=CACHE_TTL_SECONDS, help=f"Cache freshness window in seconds (default: {CACHE_TTL_SECONDS})")
    args = parser.parse_args()

    token = get_env_or_die("GITHUB_TOKEN")
    if args.username:
        usernames = args.username
    else:
        usernames = [u.strip() for u in get_env_or_die("GITHUB_USERNAME").split(",") if u.strip()]
    username = usernames[0]  # primary identity, used for report header/sign-off fallback

    until_date = datetime.now(timezone.utc)

    if args.since:
        since_date = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        since_date = until_date - timedelta(days=args.days)

    since_iso = since_date.strftime("%Y-%m-%d")
    until_iso = until_date.strftime("%Y-%m-%d")

    branch_note = f" plus branches {args.branch}" if args.branch else ""
    print(f"Fetching commits for {usernames} in orgs {args.orgs}{branch_note} from {since_iso} to {until_iso}...", file=sys.stderr)
    commit_items = cached_fetch(
        "commits",
        (sorted(usernames), sorted(args.orgs), since_iso, sorted(args.branch) if args.branch else None),
        args.cache_ttl,
        not args.no_cache,
        lambda: fetch_commits(token, usernames, args.orgs, since_iso, branches=args.branch),
    )
    grouped = group_by_repo(commit_items)

    print(f"Fetching PRs for {usernames} in orgs {args.orgs} from {since_iso} to {until_iso}...", file=sys.stderr)
    pr_items = cached_fetch(
        "prs",
        (sorted(usernames), sorted(args.orgs), since_iso),
        args.cache_ttl,
        not args.no_cache,
        lambda: fetch_prs(token, usernames, args.orgs, since_iso),
    )
    pr_grouped = group_prs_by_repo(pr_items)

    use_ai = (not args.no_ai) and bool(
        os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    )
    author_name = os.environ.get("REPORT_NAME", username)
    lines = build_report(grouped, pr_grouped, since_iso, until_iso, use_ai, author_name)
    print("\n".join(lines))

    if args.dry_run:
        print("\n(dry run — report not saved, email not sent)", file=sys.stderr)
        return

    path = save_report(args.format, lines, since_iso, until_iso, author_name)
    print(f"\nSaved report to {path}", file=sys.stderr)

    if args.send:
        to_recipients, cc_recipients = send_email(lines, since_iso, until_iso, author_name)
        cc_note = f" (cc: {', '.join(cc_recipients)})" if cc_recipients else ""
        print(f"Emailed report to {', '.join(to_recipients)}{cc_note}", file=sys.stderr)


if __name__ == "__main__":
    main()