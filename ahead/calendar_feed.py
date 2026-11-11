"""
ahead/calendar_feed.py
======================
Medication reminders in the patient's own calendar (Phase 2b), instead of emailing every dose.

* A private subscription link, webcal://<site>/calendar/<token>.ics (server.py serves it). Calendar apps fetch it
  again every few hours, so a doctor's change to a plan reaches the calendar without the patient doing anything.
  "Make a new link" (Settings) stops the old one at once.
* A one-off .ics download of the same events, for calendars that can't subscribe.

Events say "AHEAD medicine reminder" unless the patient chooses to show medicine names (a calendar is often
shared or visible on a lock screen). Each dose time of each active plan is one weekly repeating event with a
stable UID, so re-importing or refreshing updates events instead of duplicating them. Paused, stopped and
finished plans drop out on the next refresh.
"""

import json
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ahead.storage import connection

ZONE_NAME = "Asia/Dubai"            # AHEAD's local time everywhere (platform_data); no daylight saving
ZONE = ZoneInfo(ZONE_NAME)
BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]   # plan days: 0 = Monday, as in My plan
GENERIC = "AHEAD medicine reminder"


def _now():
    return datetime.now(timezone.utc)


# =============================================================================
# THE PRIVATE LINK
# =============================================================================

def feed(user_id):
    """This patient's link settings ({'token', 'show_names', 'created_at', 'last_fetched_at'}) or None."""
    with connection() as db:
        row = db.execute("SELECT * FROM calendar_feeds WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def new_link(user_id):
    """Create the link, or replace it (the old address stops working). Keeps the show-names choice."""
    token = secrets.token_urlsafe(24)
    current = feed(user_id)
    with connection() as db:
        if current:
            db.execute("UPDATE calendar_feeds SET token=?,created_at=?,last_fetched_at=NULL WHERE user_id=?",
                       (token, _now().isoformat(), user_id))
        else:
            db.execute("INSERT INTO calendar_feeds(user_id,token,show_names,created_at) VALUES(?,?,0,?)",
                       (user_id, token, _now().isoformat()))
    return token


def set_show_names(user_id, show):
    if not feed(user_id):
        new_link(user_id)
    with connection() as db:
        db.execute("UPDATE calendar_feeds SET show_names=? WHERE user_id=?", (int(bool(show)), user_id))


def show_names(user_id):
    current = feed(user_id)
    return bool(current and current["show_names"])


def remove_link(user_id):
    with connection() as db:
        db.execute("DELETE FROM calendar_feeds WHERE user_id=?", (user_id,))


def urls(token, site):
    """(webcal link for one-tap subscribe, https link to paste into Google Calendar's 'From URL')."""
    https = f"{site}/calendar/{token}.ics"
    return "webcal://" + https.split("://", 1)[-1], https


def valid_token(token):
    return isinstance(token, str) and 20 <= len(token) <= 64 and all(c.isalnum() or c in "-_" for c in token)


def ics_for_token(token, site=""):
    """The calendar for a subscription link, or None for an unknown link (or an account that is switched off)."""
    if not valid_token(token):
        return None
    with connection() as db:
        row = db.execute("SELECT f.user_id,f.show_names,u.is_active,u.role FROM calendar_feeds f JOIN users u ON u.id=f.user_id "
                         "WHERE f.token=?", (token,)).fetchone()
        if not row or not row["is_active"] or row["role"] != "Patient":
            return None
    from ahead import consent
    if not consent.has_consent(row["user_id"]):   # withdrawn (or never given): the feed stops until they agree again
        return None
    with connection() as db:
        db.execute("UPDATE calendar_feeds SET last_fetched_at=? WHERE user_id=?", (_now().isoformat(), row["user_id"]))
    return build_ics(row["user_id"], bool(row["show_names"]), site)


# =============================================================================
# THE CALENDAR FILE (RFC 5545)
# =============================================================================

def _escape(text):
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line):
    """Lines longer than 75 bytes continue on the next line after a space (RFC 5545 §3.1)."""
    data, out = line.encode("utf-8"), []
    while len(data) > 75:
        cut = 75 if not out else 74
        while cut > 0 and (data[cut] & 0xC0) == 0x80:   # never split a UTF-8 character
            cut -= 1
        out.append(data[:cut].decode("utf-8"))
        data = data[cut:]
    out.append(data.decode("utf-8"))
    return "\r\n ".join(out)


def _utc(moment):
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _first_day(start, days):
    """The first scheduled day on or after the plan's start date."""
    for offset in range(7):
        day = start + timedelta(days=offset)
        if day.weekday() in days:
            return day
    return start


def _active_plans(patient_id, today):
    with connection() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM medication_plans WHERE patient_id=? AND status='active' AND end_date>=? ORDER BY id",
            (patient_id, today.isoformat())).fetchall()]
        edits = {r["plan_id"]: r["n"] for r in db.execute(
            "SELECT plan_id,COUNT(*) AS n FROM medication_audit WHERE plan_id IN (SELECT id FROM medication_plans "
            "WHERE patient_id=?) GROUP BY plan_id", (patient_id,)).fetchall()}
    return rows, edits


def build_ics(patient_id, names=False, site=""):
    """Every dose time of the patient's active plans as weekly repeating events, with a reminder at the dose time."""
    from ahead.platform_data import clock_time
    today = datetime.now(ZONE).date()
    stamp = _utc(_now())
    rows, edits = _active_plans(patient_id, today)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AHEAD//Medicine reminders//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:AHEAD reminders", f"X-WR-TIMEZONE:{ZONE_NAME}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT4H", "X-PUBLISHED-TTL:PT4H",
             "BEGIN:VTIMEZONE", f"TZID:{ZONE_NAME}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
             "TZOFFSETFROM:+0400", "TZOFFSETTO:+0400", "TZNAME:+04", "END:STANDARD", "END:VTIMEZONE"]
    link = f" Open AHEAD to log it: {site}" if site else " Open AHEAD to log it."
    for plan in rows:
        days = sorted({int(d) for d in json.loads(plan["days_json"])})
        if not days:
            continue
        start = max(date.fromisoformat(plan["start_date"]), today - timedelta(days=7))
        end = date.fromisoformat(plan["end_date"])
        until = _utc(datetime.combine(end, time(23, 59, 59), ZONE))
        title = f"{plan['name']} · {plan['dosage']}" if names else GENERIC
        details = (f"{plan['instructions']}\n" if names and plan["instructions"].strip() else "") + \
            "Time for a medicine in your AHEAD plan." + link
        modified = _utc(datetime.fromisoformat(plan["updated_at"]))
        for at in sorted({clock_time(t) for t in json.loads(plan["times_json"])}):
            first = _first_day(start, days)
            if first > end:
                continue
            hour, minute = (int(x) for x in at.split(":"))
            lines += ["BEGIN:VEVENT",
                      f"UID:ahead-med-{plan['id']}-{at.replace(':', '')}@ahead",
                      f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{modified}", f"SEQUENCE:{edits.get(plan['id'], 0)}",
                      f"DTSTART;TZID={ZONE_NAME}:{first:%Y%m%d}T{hour:02d}{minute:02d}00",
                      "DURATION:PT15M",
                      f"RRULE:FREQ=WEEKLY;BYDAY={','.join(BYDAY[d] for d in days)};UNTIL={until}",
                      f"SUMMARY:{_escape(title)}", f"DESCRIPTION:{_escape(details)}",
                      "TRANSP:TRANSPARENT", "CLASS:PRIVATE"]
            if site:
                lines.append(f"URL:{site}")
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(title)}", "TRIGGER:PT0M",
                      "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
