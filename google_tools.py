"""Gmail (read-only) and Google Calendar helpers for canvas_agenda.py.

One-time setup:
  1. Google Cloud Console -> create a project -> enable "Gmail API" and "Google Calendar API".
  2. APIs & Services -> Credentials -> Create OAuth client ID -> "Desktop app".
     Download the JSON as credentials.json next to this file.
  3. pip install -r requirements.txt
The first run opens a browser for consent and stores token.json. Both files are git-ignored.

Note: a school Google Workspace account may block unapproved OAuth apps. If you get an
"access blocked" / admin_policy_enforced error, either ask IT to allow it, or use a personal
Google account for the calendar and forward school mail there.
"""
import base64
import hashlib
import json
import os
import re
import sys
from datetime import timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

URGENT_WORDS = re.compile(
    r"\b(urgent|asap|immediately|action required|deadline|due (today|tonight|tomorrow|soon)|"
    r"last chance|final notice|reminder|overdue|late|extension|cancel(l)?ed|rescheduled|"
    r"exam|midterm|quiz|missing|hold on your|registration|withdraw)\b",
    re.I,
)


def _service(name, version):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise SystemExit("Missing Google libraries. Run: pip install -r requirements.txt")

    token_path = os.path.join(HERE, "token.json")
    creds = None
    if os.environ.get("GOOGLE_TOKEN_JSON"):  # headless (GitHub Actions): token stored as a secret
        creds = Credentials.from_authorized_user_info(json.loads(os.environ["GOOGLE_TOKEN_JSON"]), SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as e:
                raise SystemExit(f"Google token refresh failed ({e}). Re-run `python google_tools.py` locally "
                                 "and update the GOOGLE_TOKEN_JSON secret.")
        return build(name, version, credentials=creds, cache_discovery=False)
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            cred_path = os.path.join(HERE, "credentials.json")
            if not os.path.exists(cred_path):
                raise SystemExit("credentials.json not found. See the setup steps in google_tools.py.")
            creds = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES).run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        os.chmod(token_path, 0o600)
    return build(name, version, credentials=creds, cache_discovery=False)


# Headers that still name the original UCSD mailbox after Gmail auto-forwarding.
# Forwarded mail keeps the original To/Cc and gets an X-Forwarded-For header.
ADDRESS_HEADERS = ["To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-For", "X-Forwarded-To"]
MAX_SCANNED = 300


def _from_school(headers, school):
    """True if the message was addressed to / forwarded from the school mailbox.

    `school` is a full address (you@ucsd.edu) or a domain (@ucsd.edu), case-insensitive.
    """
    blob = " ".join(headers.get(h, "") for h in ADDRESS_HEADERS).lower()
    return school.lower() in blob


def urgent_emails(days=7, limit=25, school="@ucsd.edu"):
    """Urgent-looking emails that arrived via the school mailbox (forwarded into this account).

    Recent emails that are flagged important, or unread and matching urgent keywords.
    """
    gmail = _service("gmail", "v1")
    ids, token = [], None
    while len(ids) < MAX_SCANNED:
        resp = gmail.users().messages().list(
            userId="me", q=f"newer_than:{days}d in:inbox -category:promotions -category:social",
            maxResults=100, pageToken=token,
        ).execute()
        ids += resp.get("messages", [])
        token = resp.get("nextPageToken")
        if not token:
            break

    found, matched = [], 0
    for m in ids:
        msg = gmail.users().messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "From"] + ADDRESS_HEADERS
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        if not _from_school(headers, school):
            continue
        matched += 1
        subject, sender = headers.get("Subject", "(no subject)"), headers.get("From", "")
        labels = set(msg.get("labelIds", []))
        text = f"{subject} {msg.get('snippet', '')}"

        score = 0
        score += 2 if "IMPORTANT" in labels else 0
        score += 2 if URGENT_WORDS.search(subject) else 0
        score += 1 if URGENT_WORDS.search(msg.get("snippet", "")) else 0
        score += 1 if "UNREAD" in labels else 0
        # Require a keyword hit or Gmail's own importance flag, not just unread.
        if score >= 3 or (score >= 2 and URGENT_WORDS.search(text)):
            found.append({
                "score": score,
                "subject": subject,
                "sender": re.sub(r"\s*<.*>", "", sender).strip('" '),
                "snippet": msg.get("snippet", "")[:120],
                "unread": "UNREAD" in labels,
                "url": f"https://mail.google.com/mail/u/0/#inbox/{m['id']}",
            })
    found.sort(key=lambda x: (-x["score"], x["subject"]))
    print(f"(scanned {len(ids)} emails, {matched} came via {school})", file=sys.stderr)
    return found[:limit]


def _event_id(a):
    # Deterministic id (Calendar allows a-v0-9) so re-running updates instead of duplicating.
    digest = hashlib.sha1(a["url"].encode()).digest()
    return "canvas" + base64.b32hexencode(digest).decode().lower().rstrip("=")


def sync_calendar(assignments, calendar_id="primary"):
    from googleapiclient.errors import HttpError

    cal = _service("calendar", "v3")
    created = updated = 0
    for a in assignments:
        start = a["due"].astimezone(timezone.utc)
        body = {
            "id": _event_id(a),
            "summary": f"[{a['course']}] {a['title']} due",
            "description": a["url"],
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 24 * 60},
                {"method": "popup", "minutes": 120},
            ]},
        }
        try:
            cal.events().insert(calendarId=calendar_id, body=body).execute()
            created += 1
        except HttpError as e:
            if e.resp.status != 409:
                raise
            # Already exists (possibly cancelled); overwrite with the latest due date.
            body["status"] = "confirmed"
            cal.events().update(calendarId=calendar_id, eventId=body["id"], body=body).execute()
            updated += 1
    return created, updated


if __name__ == "__main__":
    # Run once locally to do the browser consent and write token.json.
    _service("calendar", "v3")
    print("Saved token.json")
