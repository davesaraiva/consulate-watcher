#!/usr/bin/env python3
"""
consulate_watcher.py
--------------------
Watches the Embassy of Portugal (Washington DC) Acuity scheduling page for
appointment openings BEFORE a date you already hold, and alerts you when one
shows up.

Endpoint confirmed from the live booking page (DevTools capture):
  GET https://agendamentosconsulares.as.me/api/scheduling/v1/availability/month
      ?owner=303dd3be&appointmentTypeId=83817311&calendarId=12788063
      &timezone=America/New_York&month=YYYY-MM-01&queryParams=calendarID%3D12788063

Single-shot and cron-friendly: checks once, alerts on anything new, exits.

Notifications (set whichever you want via environment variables):
  - Pushover  -> phone push   (PUSHOVER_TOKEN, PUSHOVER_USER)   <- recommended
  - Email     -> SMTP         (SMTP_USER, SMTP_PASS, ALERT_TO)
  - Always prints to stdout regardless.

Requires: pip install requests
"""

import os
import sys
import json
import datetime as dt
import requests

# ---------------------------------------------------------------------------
# CONFIG  (extracted from your booking URL + DevTools capture)
# ---------------------------------------------------------------------------
SCHEDULE_SLUG    = "303dd3be"     # this is the "owner" the API wants
APPOINTMENT_TYPE = "83817311"
CALENDAR         = "12788063"
TIMEZONE         = "America/New_York"
BASE             = "https://agendamentosconsulares.as.me"

# You currently hold June 15. Alert on anything strictly earlier, within the
# next two weeks, and not in the past.
CURRENT_APPOINTMENT = dt.date(2026, 6, 15)
TODAY               = dt.date.today()
WINDOW_START        = TODAY + dt.timedelta(days=1)                       # tomorrow onward
WINDOW_END          = min(CURRENT_APPOINTMENT - dt.timedelta(days=1),    # before your slot
                          TODAY + dt.timedelta(days=14))                 # within 2 weeks

# Only check during these hours, Eastern time. No checks 10PM-6AM.
ACTIVE_START_HOUR = 6     # inclusive (6 AM)
ACTIVE_END_HOUR   = 22    # exclusive (10 PM)

# Don't re-alert on the same opening every run (local runs only).
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seen_slots.json")
DEBUG      = os.getenv("DEBUG") == "1"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/146.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/schedule/{SCHEDULE_SLUG}/appointment/"
               f"{APPOINTMENT_TYPE}/calendar/{CALENDAR}?calendarIds={CALENDAR}",
})
# Optional: if the endpoint ever returns empty/403, paste the Cookie header
# value from DevTools into ACUITY_COOKIE. Usually NOT needed.
if os.getenv("ACUITY_COOKIE"):
    SESSION.headers["Cookie"] = os.environ["ACUITY_COOKIE"]


# ---------------------------------------------------------------------------
# FETCHING AVAILABILITY  (exact request the browser makes)
# ---------------------------------------------------------------------------
def fetch_month(month: str) -> set[str]:
    """month = 'YYYY-MM'. Returns set of 'YYYY-MM-DD' dates with availability."""
    # Build the query string by hand so the already-encoded bits (%2F, %3D)
    # are sent verbatim, exactly like the browser.
    url = (f"{BASE}/api/scheduling/v1/availability/month"
           f"?owner={SCHEDULE_SLUG}"
           f"&appointmentTypeId={APPOINTMENT_TYPE}"
           f"&calendarId={CALENDAR}"
           f"&timezone=America%2FNew_York"
           f"&month={month}-01"
           f"&queryParams=calendarID%253D{CALENDAR}")
    try:
        r = SESSION.get(url, timeout=30)
        if DEBUG:
            print(f"[debug] {month} -> {r.status_code}\n{r.text}\n", file=sys.stderr)
        r.raise_for_status()
        return _extract_dates(r.json())
    except Exception as e:
        print(f"[warn] month {month} fetch failed: {e}", file=sys.stderr)
        return set()


def _extract_dates(data) -> set[str]:
    """Acuity's /month returns a list of day objects. Treat a day as open if
    it appears and (no count field present OR count > 0)."""
    out = set()
    rows = data if isinstance(data, list) else (
        data.get("dates") or data.get("data") or [] if isinstance(data, dict) else [])
    for item in rows:
        if isinstance(item, dict):
            d = item.get("date") or item.get("day")
            if not d:
                continue
            count = None
            for k in ("slotsAvailable", "slots", "available", "spotsAvailable", "openSlots"):
                if k in item:
                    count = item[k]
                    break
            if count is None or (isinstance(count, (int, float)) and count > 0):
                out.add(str(d)[:10])
        elif isinstance(item, str):
            out.add(item[:10])
    return out


def fetch_times(date_str: str) -> list[str]:
    """Best-effort human-readable time slots for a date (same host/pattern)."""
    url = (f"{BASE}/api/scheduling/v1/availability/times"
           f"?owner={SCHEDULE_SLUG}"
           f"&appointmentTypeId={APPOINTMENT_TYPE}"
           f"&calendarId={CALENDAR}"
           f"&timezone=America%2FNew_York"
           f"&date={date_str}"
           f"&queryParams=calendarID%253D{CALENDAR}")
    try:
        r = SESSION.get(url, timeout=30)
        if not r.ok:
            return []
        data = r.json()
        rows = data if isinstance(data, list) else data.get("times", [])
        slots = []
        for it in rows:
            t = it.get("time") if isinstance(it, dict) else it
            if t:
                # Acuity times are ISO; show just HH:MM if possible
                slots.append(str(t)[11:16] if "T" in str(t) else str(t))
        return slots
    except Exception:
        return []


# ---------------------------------------------------------------------------
# NOTIFICATIONS
# ---------------------------------------------------------------------------
def _try(name, fn):
    try:
        fn()
    except Exception as e:
        print(f"[warn] {name} notify failed: {e}", file=sys.stderr)


def notify(title: str, body: str) -> None:
    """Sends via every channel you've configured (env vars). Set as many or
    as few as you like. Always prints to stdout."""
    print(f"\n=== {title} ===\n{body}\n")
    book_url = f"{BASE}/schedule/{SCHEDULE_SLUG}"

    # --- Pushover (phone push). PUSHOVER_PRIORITY=2 => repeats until you ack ---
    token, user = os.getenv("PUSHOVER_TOKEN"), os.getenv("PUSHOVER_USER")
    if token and user:
        prio = int(os.getenv("PUSHOVER_PRIORITY", "1"))
        data = {"token": token, "user": user, "title": title, "message": body,
                "priority": prio, "url": book_url, "url_title": "Open booking page"}
        if prio == 2:                      # emergency priority needs these
            data.update({"retry": 60, "expire": 3600})
        _try("pushover", lambda: requests.post(
            "https://api.pushover.net/1/messages.json", data=data, timeout=15))

    # --- ntfy.sh (free, no account: install the ntfy app, subscribe a topic) ---
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        _try("ntfy", lambda: requests.post(
            f"{server}/{topic}", data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent",
                     "Tags": "calendar", "Click": book_url}, timeout=15))

    # --- Telegram bot (free: @BotFather for token, then your chat id) ---
    tg_token, tg_chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        _try("telegram", lambda: requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            data={"chat_id": tg_chat, "parse_mode": "Markdown",
                  "text": f"*{title}*\n{body}\n{book_url}"}, timeout=15))

    # --- Slack OR Discord incoming webhook (free) ---
    hook = os.getenv("WEBHOOK_URL")
    if hook:
        key = "content" if "discord" in hook else "text"
        _try("webhook", lambda: requests.post(
            hook, json={key: f"*{title}*\n{body}\n{book_url}"}, timeout=15))

    # --- Email, or free text-to-SMS via your carrier gateway ---
    # T-Mobile: set ALERT_TO=YOURNUMBER@tmomail.net to get a real text.
    smtp_user, smtp_pass = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    alert_to = os.getenv("ALERT_TO", smtp_user or "")
    if smtp_user and smtp_pass and alert_to:
        def _send_mail():
            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = title, smtp_user, alert_to
            msg.set_content(body + f"\n\nBook: {book_url}")
            with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"),
                              int(os.getenv("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)
        _try("email", _send_mail)


# ---------------------------------------------------------------------------
# DEDUPE STATE
# ---------------------------------------------------------------------------
def load_seen() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(seen), f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def months_to_check() -> list[str]:
    months, cur = [], TODAY.replace(day=1)
    while cur <= WINDOW_END:
        months.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
    return months or [TODAY.strftime("%Y-%m")]


def within_active_hours() -> bool:
    """True only between 6 AM and 10 PM Eastern."""
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = dt.datetime.now()   # assume the machine's clock is Eastern
    return ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR


def main() -> int:
    if "--test" in sys.argv:
        notify("Watcher test",
                "If this reached your phone, notifications are working.")
        return 0

    if not within_active_hours():
        print("Outside active hours (10PM-6AM ET); skipping.")
        return 0

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] checking openings "
          f"{WINDOW_START} .. {WINDOW_END} (before {CURRENT_APPOINTMENT})")

    available = set()
    for month in months_to_check():
        available |= fetch_month(month)

    if DEBUG:
        print(f"[debug] all available dates: {sorted(available)}", file=sys.stderr)

    qualifying = sorted(
        d for d in available
        if WINDOW_START.isoformat() <= d <= WINDOW_END.isoformat()
    )
    if not qualifying:
        print("No earlier openings right now.")
        return 0

    seen = load_seen()
    new = [d for d in qualifying if d not in seen]
    if not new:
        print(f"Earlier slots exist but already alerted: {qualifying}")
        return 0

    lines = []
    for d in new:
        times = fetch_times(d)
        when = ", ".join(times[:6]) + ("..." if len(times) > 6 else "")
        lines.append(f"  - {d}" + (f"  ({when})" if when else ""))

    notify("Earlier consulate appointment available!",
           "Openings before your June 15 slot:\n" + "\n".join(lines) +
           f"\n\nBook fast: {BASE}/schedule/{SCHEDULE_SLUG}")
    save_seen(seen | set(new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
