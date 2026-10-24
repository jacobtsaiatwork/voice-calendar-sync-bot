import datetime
import urllib.parse
from database import ScheduleItem
from config import settings


def generate_google_calendar_deeplink(item: ScheduleItem) -> str:
    """
    生成「📅 一鍵加入 Google Calendar」Deep Link URL：
    https://calendar.google.com/calendar/render?action=TEMPLATE&text=...&dates=...&details=...&location=...
    """
    base_url = "https://calendar.google.com/calendar/render"
    now = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)

    start_dt = item.start_time or item.due_time or (now + datetime.timedelta(hours=1))
    end_dt = item.end_time or (start_dt + datetime.timedelta(hours=1))

    if item.is_all_day:
        dates_str = f"{start_dt.strftime('%Y%m%d')}/{(start_dt + datetime.timedelta(days=1)).strftime('%Y%m%d')}"
    else:
        dates_str = f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}"

    details_parts = []
    if item.notes:
        details_parts.append(item.notes)
    details_parts.append(f"分類: #{item.category} | 優先級: {item.priority}")
    if item.google_event_id or item.google_task_id:
        sync_id = item.google_event_id or item.google_task_id
        details_parts.append(f"Sync ID: {sync_id}")

    params = {
        "action": "TEMPLATE",
        "text": item.title,
        "dates": dates_str,
        "details": "\n".join(details_parts),
        "location": item.location or "",
        "ctz": settings.DEFAULT_TIMEZONE,
    }
    return f"{base_url}?{urllib.parse.urlencode(params)}"


def generate_ics_download_url(item: ScheduleItem) -> str:
    """
    回傳該項目的 `.ics` 行事曆下載網址 (`/api/ics/{item_id}.ics`)
    """
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/api/ics/{item.id}.ics"


def _escape_ics_text(val: str) -> str:
    if not val:
        return ""
    return (
        val.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def generate_ics_content(item: ScheduleItem) -> str:
    """
    生成符合 RFC 5545 標準的 `.ics` (iCalendar) 檔案內容，
    支援 iPhone (iOS Calendar)、Android、Google Calendar、Apple Calendar 與 Outlook 一鍵匯入。
    內含 30 分鐘前 VALARM 提醒通知。
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now_local = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)

    start_dt = item.start_time or item.due_time or (now_local + datetime.timedelta(hours=1))
    end_dt = item.end_time or (start_dt + datetime.timedelta(hours=1))

    uid = f"voice-sync-{item.id}-{item.google_event_id or item.google_task_id or 'local'}@voice-calendar-sync-bot"
    summary = _escape_ics_text(item.title)
    description = _escape_ics_text(
        f"{item.notes or ''}\n類型: {item.item_type} | 優先級: {item.priority} | 分類: #{item.category}".strip()
    )
    location = _escape_ics_text(item.location or "")

    priority_map = {"High": 1, "Medium": 5, "Low": 9}
    ics_priority = priority_map.get(item.priority, 5)
    status = "COMPLETED" if item.is_completed else "CONFIRMED"

    if item.is_all_day:
        dtstart_line = f"DTSTART;VALUE=DATE:{start_dt.strftime('%Y%m%d')}"
        dtend_line = f"DTEND;VALUE=DATE:{(start_dt + datetime.timedelta(days=1)).strftime('%Y%m%d')}"
    else:
        dtstart_line = f"DTSTART;TZID={settings.DEFAULT_TIMEZONE}:{start_dt.strftime('%Y%m%dT%H%M%S')}"
        dtend_line = f"DTEND;TZID={settings.DEFAULT_TIMEZONE}:{end_dt.strftime('%Y%m%dT%H%M%S')}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Voice Secretary//Google Calendar & Tasks Bi-Directional Sync Bot//TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:語音待辦秘書 ({settings.DEFAULT_TIMEZONE})",
        f"X-WR-TIMEZONE:{settings.DEFAULT_TIMEZONE}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc}",
        dtstart_line,
        dtend_line,
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"LOCATION:{location}",
        f"PRIORITY:{ics_priority}",
        f"STATUS:{status}",
        f"CATEGORIES:{_escape_ics_text(item.category)}",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:提醒：{summary} 即將於 30 分鐘後開始",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
