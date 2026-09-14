import difflib
import requests
from bs4 import BeautifulSoup
import time
import hashlib
import json
import os
from datetime import datetime
from typing import Optional, List  # Added for backwards-compatible type hints
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from office365_login import login_and_get_cookies, cookies_to_requests_session

load_dotenv()  # reads .env locally; no-op in CI where secrets are injected directly


headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}
BASELINE_FILE = "baselines.json"

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
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump(baselines, f, indent=2, ensure_ascii=False)

# Fixed: Using Optional[requests.Session] instead of requests.Session | None
def fetch_meaningful_content(
    url: str,
    selector: Optional[str] = None,
    timeout: int = 15,  
    session: Optional[requests.Session] = None,
) -> str:
    requester = session if session is not None else requests
    resp = requester.get(url, headers=headers, timeout=timeout)
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

def send_text_email_alert(url: str, changes: dict):
    sender_email = os.environ["GMAIL_SENDER"]
    receiver_email = os.environ["GMAIL_RECEIVER"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]


    subject = "[HUST Event Monitor] New Upcoming Events Found!"
    
    body = f"""
NEW EVENT UPDATE DETECTED
====================================================

New events or updates were just published on the CTSV HUST portal.

Page URL: {url}
Content Similarity: {changes.get('similarity', 'N/A')}%

--- DETECTED CHANGES ---
{changes.get('diff', 'No detailed diff available.')}

====================================================
Visit the link above to view details and register!
"""

    msg = MIMEText(body, "plain")
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = receiver_email

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, app_password)
            server.send_message(msg)
        print("Text email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")

def content_hash(content: str) -> str:
    return hashlib.md5(content.encode('utf-8')).hexdigest()


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

# Fixed: Using List[str] and Optional[...] for Python 3.7-3.9 compatibility
def monitor_website(
    urls: List[str], 
    threshold: float = 99.0, 
    selector: Optional[str] = None, 
    delay: float = 3.0, 
    session: Optional[requests.Session] = None
):
    baselines = load_baselines()
    
    for url in urls:
        try:
            # Fixed: Fetch content INSIDE the url loop
            current = fetch_meaningful_content(url, selector=selector, session=session)
            current_h = content_hash(current)

            if url not in baselines:
                print(f"New URL detected - saving baseline for {url}")
                save_baseline(url, current, current_h)
                continue

            if current_h == baselines[url]["hash"]:
                print(f"No changes on {url} (hash match)")
                continue

            changes = detect_changes(baselines[url]["content"], current)
            
            if changes["changed"] and changes["similarity"] < threshold:
                print(f"CHANGED! {url} Similarity: {changes['similarity']}%")

                send_text_email_alert(url, changes)
            else:
                print(f"Minor change ignored on {url} (similarity {changes['similarity']}%)")

            save_baseline(url, current, current_h)

        except Exception as e:
            print(f"Error checking {url}: {e}")

        time.sleep(delay)

if __name__ == "__main__":
    urls_to_monitor = [
        "https://ctsv.hust.edu.vn/danh-sach-su-kien"
    ]

    username = os.environ["HUST_USERNAME"]
    password = os.environ["HUST_PASSWORD"]

    print("Logging in...")
    cookies = login_and_get_cookies(username, password, headless=True)
    session = cookies_to_requests_session(cookies)
    print(f"Logged in, got {len(cookies)} cookies.")

    monitor_website(urls_to_monitor, threshold=98.0, session=session)


