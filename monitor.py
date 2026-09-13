"""
Website change monitor.
Fetches pages, detects meaningful changes, stores baselines, sends alerts.
"""

import difflib
import hashlib
import json
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

BASELINE_FILE = "baselines.json"

DEFAULT_HEADERS = {
    # A realistic User-Agent avoids some basic bot-blocking.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# STEP 1: Fetch and clean the page
# ---------------------------------------------------------------------------
def fetch_and_clean(
    url: str,
    selector: str | None = None,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> str:
    """
    Download a page and return clean, comparable text.

    - Strips <script>, <style>, <nav>, <header>, <footer> — these change
      constantly (menus, ads, tracking) without the actual content changing.
    - If `selector` is given (a CSS selector like ".price" or "#job-list"),
      only that element is compared. This is the "precision" trick from the
      article's tips section — watching one div instead of the whole page
      means far fewer false alarms.
    - If `session` is given (e.g. from use_saved_cookies.build_session()),
      it's used instead of a plain request — this is how logged-in pages
      get fetched.
    """
    requester = session if session is not None else requests
    resp = requester.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    if selector:
        matched = soup.select(selector)
        if not matched:
            raise ValueError(f"CSS selector {selector!r} matched nothing on {url}")
        soup = BeautifulSoup("".join(str(el) for el in matched), "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]  # drop blank lines
    return "\n".join(lines)


def content_hash(content: str) -> str:
    """Cheap fingerprint so unchanged pages skip the more expensive diff step."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# STEP 2: Detect changes with difflib
# ---------------------------------------------------------------------------
def detect_changes(old_content: str, new_content: str) -> dict:
    if old_content == new_content:
        return {"changed": False}

    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    diff_text = "\n".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile="previous", tofile="current", lineterm=""
        )
    )

    ratio = difflib.SequenceMatcher(None, old_content, new_content).ratio()

    return {
        "changed": True,
        "similarity": round(ratio * 100, 2),
        "diff": diff_text,
        "additions": sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++")),
        "deletions": sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---")),
    }


# ---------------------------------------------------------------------------
# STEP 3: Store baselines
# ---------------------------------------------------------------------------
def load_baselines() -> dict:
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE) as f:
            return json.load(f)
    return {}


def save_baseline(url: str, content: str, hash_val: str) -> None:
    baselines = load_baselines()
    baselines[url] = {
        "hash": hash_val,
        "content": content,
        "last_checked": datetime.now().isoformat(),
        "last_changed": datetime.now().isoformat(),
    }
    with open(BASELINE_FILE, "w") as f:
        json.dump(baselines, f, indent=2)


# ---------------------------------------------------------------------------
# STEP 4: Alerts
# ---------------------------------------------------------------------------
def send_email_alert(url: str, changes: dict, smtp_config: dict) -> None:
    """
    smtp_config = {
        "host": "smtp.yourdomain.com", "port": 587,
        "user": "monitor@yourdomain.com", "password": "your-app-password",
        "to": "you@yourdomain.com",
    }
    """
    msg = MIMEText(
        f"Changes detected on {url}\n\n"
        f"Similarity: {changes['similarity']}%\n"
        f"Additions: {changes['additions']}\n"
        f"Deletions: {changes['deletions']}\n\n"
        f"Diff:\n{changes['diff'][:2000]}"
    )
    msg["Subject"] = f"Change detected: {url[:50]}"
    msg["From"] = smtp_config["user"]
    msg["To"] = smtp_config["to"]

    with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as server:
        server.starttls()
        server.login(smtp_config["user"], smtp_config["password"])
        server.send_message(msg)


def send_slack_alert(webhook_url: str, url: str, changes: dict) -> None:
    """
    This is the part the article gated behind an affiliate link — it's just
    a POST request. Create a Slack "Incoming Webhook" in your workspace
    settings and paste that URL here.
    """
    text = (
        f"*Change detected:* {url}\n"
        f"Similarity: {changes['similarity']}% "
        f"(+{changes['additions']} / -{changes['deletions']})\n"
        f"```{changes['diff'][:1500]}```"
    )
    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Use-case helpers
# ---------------------------------------------------------------------------
def extract_price(content: str) -> float | None:
    """Grabs the first $-formatted number, e.g. from a product page."""
    match = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", content)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def check_new_listings(old_content: str, new_content: str, keywords: list[str]) -> list[str]:
    """Returns newly-added lines that mention any of the given keywords."""
    old_lines = set(old_content.splitlines())
    new_lines = set(new_content.splitlines())
    additions = new_lines - old_lines
    return [line for line in additions if any(kw.lower() in line.lower() for kw in keywords)]


# ---------------------------------------------------------------------------
# STEP 5: Put it all together
# ---------------------------------------------------------------------------
def monitor(
    urls: list[str],
    threshold: float = 99.0,
    selector: str | None = None,
    alert_fn=None,
    delay_seconds: float = 3.0,
    session: requests.Session | None = None,
) -> None:
    """
    alert_fn(url, changes) is called whenever a real change is found.
    Pass in send_email_alert / send_slack_alert (wrapped with functools.partial
    to fill in your config) or your own function.

    session: pass an authenticated requests.Session (e.g. from
    use_saved_cookies.build_session()) to monitor pages that require login.
    """
    baselines = load_baselines()

    for url in urls:
        print(f"Checking {url}...")
        try:
            current = fetch_and_clean(url, selector=selector, session=session)
            current_h = content_hash(current)

            if url not in baselines:
                print("  New URL - saving baseline")
                save_baseline(url, current, current_h)
                continue

            if current_h == baselines[url]["hash"]:
                print("  No changes (hash match)")
                continue

            changes = detect_changes(baselines[url]["content"], current)

            if changes["changed"] and changes["similarity"] < threshold:
                print(f"  CHANGED! Similarity: {changes['similarity']}%")
                if alert_fn:
                    alert_fn(url, changes)
            else:
                print(f"  Minor change ignored (similarity {changes['similarity']}%)")

            save_baseline(url, current, current_h)

        except Exception as e:
            print(f"  Error: {e}")

        time.sleep(delay_seconds)  # be polite - don't hammer the server


if __name__ == "__main__":
    urls_to_monitor = [
        "https://example.com/products/widget",
        "https://example.com/jobs",
    ]

    # Swap this for send_email_alert / send_slack_alert once configured.
    def print_alert(url, changes):
        print(f"ALERT: {url} changed ({changes['similarity']}% similar)")

    monitor(urls_to_monitor, threshold=98.0, alert_fn=print_alert)