#!/usr/bin/env python3
"""Pull upcoming assignments and readings from Canvas and print an agenda.

Setup:
    export CANVAS_BASE_URL=https://yourschool.instructure.com
    export CANVAS_TOKEN=...   # Canvas > Account > Settings > New Access Token

Usage:
    python canvas_agenda.py                 # next 14 days
    python canvas_agenda.py --days 30
    python canvas_agenda.py --ics out.ics   # also write a calendar file
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

READING_WORDS = re.compile(
    r"\b(read|reading|readings|chapter|ch\.?\s*\d+|article|pp?\.\s*\d+|pages?\s+\d+)\b",
    re.I,
)
READING_TYPES = {"Page", "File", "ExternalUrl", "ExternalTool"}


class Canvas:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.token = token

    def get(self, path, **params):
        """GET a Canvas endpoint, following Link-header pagination."""
        query = "&".join(
            f"{k}={v}" if not isinstance(v, list) else "&".join(f"{k}[]={i}" for i in v)
            for k, v in params.items()
        )
        url = f"{self.base}/api/v1{path}?per_page=100" + (f"&{query}" if query else "")
        out = []
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req) as resp:
                    out.extend(json.load(resp))
                    link = resp.headers.get("Link", "")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    # 403 also happens for courses that hide modules/assignments.
                    if e.code == 401:
                        sys.exit("Canvas rejected the token (401). Check CANVAS_TOKEN.")
                    return out
                raise
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None
        return out


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def looks_like_reading(item):
    if item["type"] not in READING_TYPES:
        return False
    # Files are usually readings; pages/links/tools only if the title says so (avoids "Syllabus", "Zoom").
    return item["type"] == "File" or bool(READING_WORDS.search(item["title"]))


def collect(canvas, days):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    assignments, readings = [], []

    courses = canvas.get("/courses", enrollment_state="active")
    for c in courses:
        if "name" not in c:  # restricted/concluded course stubs
            continue
        cname = c.get("course_code") or c["name"]

        for a in canvas.get(f"/courses/{c['id']}/assignments", include=["submission"], order_by="due_at"):
            due = parse_dt(a.get("due_at"))
            sub = a.get("submission") or {}
            done = sub.get("workflow_state") in ("submitted", "graded") or sub.get("submitted_at")
            if due and now <= due <= horizon and not done:
                assignments.append({"course": cname, "title": a["name"], "due": due, "url": a["html_url"]})

        for mod in canvas.get(f"/courses/{c['id']}/modules"):
            unlock = parse_dt(mod.get("unlock_at"))
            if mod.get("state") == "completed":
                continue
            for it in canvas.get(f"/courses/{c['id']}/modules/{mod['id']}/items"):
                if not looks_like_reading(it):
                    continue
                if (it.get("completion_requirement") or {}).get("completed"):
                    continue
                readings.append({
                    "course": cname,
                    "module": mod["name"],
                    "title": it["title"],
                    # Canvas has no due date for readings; the module unlock date is the best proxy.
                    "when": unlock,
                    "url": it.get("html_url", ""),
                })

    assignments.sort(key=lambda x: x["due"])
    readings.sort(key=lambda x: (x["when"] is None, x["when"] or now, x["course"]))
    return assignments, readings


def fmt(dt):
    return dt.astimezone().strftime("%a %b %d, %I:%M %p")


def print_agenda(assignments, readings, days):
    print(f"=== Assignments due in the next {days} days ===")
    if not assignments:
        print("  (none)")
    for a in assignments:
        print(f"  {fmt(a['due'])}  [{a['course']}] {a['title']}")

    print("\n=== Readings (not yet marked done) ===")
    if not readings:
        print("  (none found)")
    last = None
    for r in readings:
        key = (r["course"], r["module"])
        if key != last:
            when = f" — module opens {fmt(r['when'])}" if r["when"] else ""
            print(f"  [{r['course']}] {r['module']}{when}")
            last = key
        print(f"      - {r['title']}")


def write_ics(path, assignments):
    def stamp(dt):
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AssignmentCalender//EN"]
    for i, a in enumerate(assignments):
        lines += [
            "BEGIN:VEVENT",
            f"UID:{stamp(a['due'])}-{i}@assignmentcalender",
            f"DTSTAMP:{stamp(datetime.now(timezone.utc))}",
            f"DTSTART:{stamp(a['due'])}",
            f"DTEND:{stamp(a['due'] + timedelta(minutes=30))}",
            f"SUMMARY:[{a['course']}] {a['title']} due".replace(",", "\\,"),
            f"URL:{a['url']}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    with open(path, "w") as f:
        f.write("\r\n".join(lines) + "\r\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--ics", help="also write assignment due dates to this .ics file")
    args = p.parse_args()

    base, token = os.environ.get("CANVAS_BASE_URL"), os.environ.get("CANVAS_TOKEN")
    if not base or not token:
        sys.exit("Set CANVAS_BASE_URL and CANVAS_TOKEN (see the docstring at the top of this file).")

    assignments, readings = collect(Canvas(base, token), args.days)
    print_agenda(assignments, readings, args.days)
    if args.ics:
        write_ics(args.ics, assignments)
        print(f"\nWrote {args.ics}")


if __name__ == "__main__":
    main()
