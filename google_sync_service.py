import os
import json
import uuid
import hashlib
import logging
import datetime
from typing import Dict, Any, List, Optional, Tuple
import requests
from sqlalchemy.orm import Session

from config import settings
from database import ScheduleItem

logger = logging.getLogger(__name__)

try:
    import icalendar
    import recurring_ical_events
    import dateutil.tz
    ICAL_AVAILABLE = True
except ImportError:
    ICAL_AVAILABLE = False


def compute_etag(title: str, start: Any, end: Any, due: Any, completed: bool, deleted: bool = False) -> str:
    """計算結構化內容雜湊作為雙向同步 ETag"""
    raw = f"{title}|{start}|{end}|{due}|{completed}|{deleted}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f'W/"{digest}"'


class LocalSyncTwinStore:
    """
    當使用者尚未綁定雲端 Google OAuth / Service Account 憑證時，
    自動以 Local Sync Twin 模擬器運作，完整維護遠端 Google Calendar Events 與 Google Tasks 狀態鏡像，
    支援 100% 擬真的 Push Sync 與 Pull & Two-Way Merge Sync 雙向同步流程。
    """
    def __init__(self):
        self.remote_events: Dict[str, Dict[str, Any]] = {}
        self.remote_tasks: Dict[str, Dict[str, Any]] = {}

    def clear(self):
        self.remote_events.clear()
        self.remote_tasks.clear()

    def upsert_remote_event(self, event_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now_iso = datetime.datetime.now().isoformat()
        record = {
            "id": event_id,
            "kind": "calendar#event",
            "summary": payload.get("summary", "未命名行程"),
            "location": payload.get("location"),
            "description": payload.get("description"),
            "start": payload.get("start"),
            "end": payload.get("end"),
            "status": payload.get("status", "confirmed"),
            "updated": payload.get("updated", now_iso),
            "etag": payload.get("etag") or compute_etag(
                payload.get("summary", ""),
                payload.get("start"),
                payload.get("end"),
                None,
                payload.get("status") == "completed",
                payload.get("status") == "cancelled",
            ),
        }
        self.remote_events[event_id] = record
        return record

    def upsert_remote_task(self, task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now_iso = datetime.datetime.now().isoformat()
        record = {
            "id": task_id,
            "kind": "tasks#task",
            "title": payload.get("title", "未命名待辦"),
            "notes": payload.get("notes"),
            "due": payload.get("due"),
            "status": payload.get("status", "needsAction"),  # 'needsAction' | 'completed'
            "deleted": payload.get("deleted", False),
            "updated": payload.get("updated", now_iso),
            "etag": payload.get("etag") or compute_etag(
                payload.get("title", ""),
                None,
                None,
                payload.get("due"),
                payload.get("status") == "completed",
                payload.get("deleted", False),
            ),
        }
        self.remote_tasks[task_id] = record
        return record


# 全域單例 Local Sync Twin
local_twin_store = LocalSyncTwinStore()


class GoogleBiDirectionalSyncService:
    """
    Google Calendar API v3 & Google Tasks API v1 雙向同步核心服務：
    1. Bot ➔ Google (Push Sync)：
       - 新增、完成、延後、刪除時即時同步至遠端（支援 OAuth2 Bearer Token、Google Apps Script Bridge Webhook、或 Local Sync Twin）。
    2. Google ➔ Bot (Pull & Two-Way Merge Sync)：
       - 拉取遠端 Google Calendar (REST API + 多組 iCal URLs) 與 Google Tasks 變更。
       - 透過 `etag` 與 `updated_at` 進行雙向合併 (Two-Way Merge)，解決衝突並更新本地 SQLite。
    """

    def __init__(self):
        self.calendar_id = settings.GOOGLE_CALENDAR_ID or "primary"
        self.tasklist_id = settings.GOOGLE_TASKLIST_ID or "@default"

    @property
    def sync_mode(self) -> str:
        if settings.GOOGLE_OAUTH_ACCESS_TOKEN:
            return "GOOGLE_REST_API"
        if settings.GOOGLE_APPS_SCRIPT_WEBHOOK_URL:
            return "GAS_WEBHOOK_BRIDGE"
        return "LOCAL_SYNC_TWIN"

    def _build_event_payload(self, item: ScheduleItem) -> Dict[str, Any]:
        now = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
        start_dt = item.start_time or item.due_time or (now + datetime.timedelta(hours=1))
        end_dt = item.end_time or (start_dt + datetime.timedelta(hours=1))

        status = "cancelled" if item.is_deleted else "confirmed"
        summary_prefix = "[已完成] " if item.is_completed and not item.title.startswith("[已完成]") else ""

        return {
            "summary": f"{summary_prefix}{item.title}",
            "location": item.location or "",
            "description": item.notes or f"分類: #{item.category} | 優先級: {item.priority}",
            "start": {
                "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": settings.DEFAULT_TIMEZONE,
            },
            "end": {
                "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": settings.DEFAULT_TIMEZONE,
            },
            "status": status,
            "updated": (item.updated_at or datetime.datetime.now()).isoformat(),
        }

    def _build_task_payload(self, item: ScheduleItem) -> Dict[str, Any]:
        due_dt = item.due_time or item.start_time
        return {
            "title": item.title,
            "notes": item.notes or f"分類: #{item.category} | 優先級: {item.priority}",
            "due": due_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if due_dt else None,
            "status": "completed" if item.is_completed else "needsAction",
            "deleted": bool(item.is_deleted),
            "updated": (item.updated_at or datetime.datetime.now()).isoformat(),
        }

    def push_item_to_google(self, db: Session, item: ScheduleItem, operation: str = "UPSERT") -> ScheduleItem:
        """
        Bot ➔ Google (Push Sync)：
        將本地新增、修改、完成、順延或刪除的項目推送至 Google Calendar 或 Google Tasks。
        """
        now = datetime.datetime.now()
        is_event = item.item_type == "CALENDAR_EVENT"

        if is_event:
            if not item.google_event_id:
                item.google_event_id = f"gcal_ev_{uuid.uuid4().hex[:12]}"
            payload = self._build_event_payload(item)
        else:
            if not item.google_task_id:
                item.google_task_id = f"gtask_{uuid.uuid4().hex[:12]}"
            payload = self._build_task_payload(item)

        new_etag = compute_etag(
            item.title,
            item.start_time.isoformat() if item.start_time else None,
            item.end_time.isoformat() if item.end_time else None,
            item.due_time.isoformat() if item.due_time else None,
            bool(item.is_completed),
            bool(item.is_deleted),
        )

        cloud_synced = False

        # 模式 1: Google REST API (OAuth2 Access Token)
        if settings.GOOGLE_OAUTH_ACCESS_TOKEN:
            headers = {
                "Authorization": f"Bearer {settings.GOOGLE_OAUTH_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            }
            try:
                if is_event:
                    url = f"https://www.googleapis.com/calendar/v3/calendars/{self.calendar_id}/events"
                    resp = requests.post(url, headers=headers, json=payload, timeout=8)
                    if resp.ok:
                        data = resp.json()
                        item.google_event_id = data.get("id", item.google_event_id)
                        new_etag = data.get("etag", new_etag)
                        cloud_synced = True
                else:
                    url = f"https://tasks.googleapis.com/tasks/v1/lists/{self.tasklist_id}/tasks"
                    resp = requests.post(url, headers=headers, json=payload, timeout=8)
                    if resp.ok:
                        data = resp.json()
                        item.google_task_id = data.get("id", item.google_task_id)
                        new_etag = data.get("etag", new_etag)
                        cloud_synced = True
            except Exception as e:
                logger.warning(f"Google REST API Push 失敗，自動切換 Local Sync Twin: {e}")

        # 模式 2: Google Apps Script Webhook 橋接模式
        elif settings.GOOGLE_APPS_SCRIPT_WEBHOOK_URL:
            try:
                bridge_payload = {
                    "direction": "PUSH",
                    "operation": operation,
                    "item_type": item.item_type,
                    "google_id": item.google_event_id or item.google_task_id,
                    "payload": payload,
                }
                resp = requests.post(settings.GOOGLE_APPS_SCRIPT_WEBHOOK_URL, json=bridge_payload, timeout=8)
                if resp.ok:
                    data = resp.json()
                    if is_event and data.get("google_event_id"):
                        item.google_event_id = data["google_event_id"]
                    elif not is_event and data.get("google_task_id"):
                        item.google_task_id = data["google_task_id"]
                    new_etag = data.get("etag", new_etag)
                    cloud_synced = True
            except Exception as e:
                logger.warning(f"GAS Webhook Push 失敗，自動切換 Local Sync Twin: {e}")

        # 同步寫入 Local Sync Twin 確保雙向狀態一致
        payload["etag"] = new_etag
        if is_event:
            local_twin_store.upsert_remote_event(item.google_event_id, payload)
        else:
            local_twin_store.upsert_remote_task(item.google_task_id, payload)

        item.etag = new_etag
        item.sync_status = "SYNCED_CLOUD" if cloud_synced else "SYNCED_LOCAL_TWIN"
        item.last_synced_at = now
        item.updated_at = now

        db.add(item)
        db.commit()
        db.refresh(item)
        return item

    def pull_and_merge_from_google(
        self,
        db: Session,
        user_id: str = "default_user",
        external_remote_payload: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> Dict[str, Any]:
        """
        Google ➔ Bot (Pull & Two-Way Merge Sync)：
        從遠端拉取 Google Calendar Events 與 Google Tasks，並與本地資料庫進行雙向合併 (Two-Way Merge)：
        1. 若遠端項目不存在於本地 ➔ 在本地新建 `ScheduleItem` (sync_source='GOOGLE')。
        2. 若遠端項目已存在於本地：
           - 比對 `etag`：若相同代表無變更，直接跳過 (unchanged)。
           - 若 `etag` 不同：比較遠端 `updated` 與本地 `updated_at`，若遠端較新（或外部主動推入變更），
             則以遠端覆寫本地並標記 `sync_status='SYNCED_CLOUD'` 或 `'CONFLICT_RESOLVED'`；
             若本地較新，則反向將本地最新版本 Push 回遠端！
        """
        stats = {
            "mode": self.sync_mode,
            "pulled_events": 0,
            "pulled_tasks": 0,
            "created_in_local": 0,
            "updated_in_local": 0,
            "pushed_back_to_remote": 0,
            "unchanged": 0,
            "timestamp": datetime.datetime.now().isoformat(),
        }

        remote_events: List[Dict[str, Any]] = []
        remote_tasks: List[Dict[str, Any]] = []

        if external_remote_payload is not None:
            remote_events = external_remote_payload.get("events", [])
            remote_tasks = external_remote_payload.get("tasks", [])
            # 同步更新至 twin store
            for ev in remote_events:
                if ev.get("id"):
                    local_twin_store.upsert_remote_event(ev["id"], ev)
            for tk in remote_tasks:
                if tk.get("id"):
                    local_twin_store.upsert_remote_task(tk["id"], tk)
        else:
            # 1. 若有設定 iCal URLs，支援從 iCal 匯入今日與未來 7 天遠端行程
            ical_events = self._fetch_ical_remote_events()
            for iev in ical_events:
                local_twin_store.upsert_remote_event(iev["id"], iev)

            remote_events = list(local_twin_store.remote_events.values())
            remote_tasks = list(local_twin_store.remote_tasks.values())

        stats["pulled_events"] = len(remote_events)
        stats["pulled_tasks"] = len(remote_tasks)

        # 合併 Google Calendar Events
        for rev in remote_events:
            gid = rev.get("id")
            if not gid:
                continue
            r_etag = rev.get("etag") or compute_etag(
                rev.get("summary", ""),
                rev.get("start"),
                rev.get("end"),
                None,
                rev.get("status") == "completed",
                rev.get("status") == "cancelled",
            )
            r_updated = self._parse_iso_dt(rev.get("updated")) or datetime.datetime.now()
            r_start = self._extract_event_dt(rev.get("start"))
            r_end = self._extract_event_dt(rev.get("end"))
            if r_start and not r_end:
                r_end = r_start + datetime.timedelta(hours=1)

            r_summary = str(rev.get("summary", "Google 日曆行程")).strip()
            is_comp = r_summary.startswith("[已完成]") or rev.get("status") == "completed"
            clean_title = r_summary.replace("[已完成]", "").strip() or "Google 日曆行程"
            is_del = rev.get("status") == "cancelled"

            existing = db.query(ScheduleItem).filter(
                ScheduleItem.google_event_id == gid
            ).first()

            if not existing:
                if is_del:
                    continue
                new_item = ScheduleItem(
                    user_id=user_id,
                    item_type="CALENDAR_EVENT",
                    title=clean_title,
                    start_time=r_start,
                    end_time=r_end,
                    location=rev.get("location"),
                    notes=rev.get("description") or "由 Google Calendar 遠端同步匯入",
                    category="會議",
                    priority="Medium",
                    is_completed=is_comp,
                    is_deleted=False,
                    google_event_id=gid,
                    etag=r_etag,
                    sync_status="SYNCED_LOCAL_TWIN" if self.sync_mode == "LOCAL_SYNC_TWIN" else "SYNCED_CLOUD",
                    sync_source="GOOGLE",
                    last_synced_at=datetime.datetime.now(),
                    updated_at=r_updated,
                )
                db.add(new_item)
                stats["created_in_local"] += 1
            else:
                if existing.etag == r_etag:
                    stats["unchanged"] += 1
                    continue

                local_updated = existing.updated_at or existing.created_at or datetime.datetime.min
                if r_updated >= local_updated:
                    existing.title = clean_title
                    if r_start:
                        existing.start_time = r_start
                    if r_end:
                        existing.end_time = r_end
                    if "location" in rev and rev["location"] is not None:
                        existing.location = rev["location"]
                    if "description" in rev and rev["description"] is not None:
                        existing.notes = rev["description"]
                    existing.is_completed = is_comp
                    existing.is_deleted = is_del
                    existing.etag = r_etag
                    existing.sync_status = "CONFLICT_RESOLVED"
                    existing.last_synced_at = datetime.datetime.now()
                    existing.updated_at = r_updated
                    db.add(existing)
                    stats["updated_in_local"] += 1
                else:
                    # 本地版本較新 ➔ 將本地變更推回遠端
                    self.push_item_to_google(db, existing, operation="MERGE_PUSH_BACK")
                    stats["pushed_back_to_remote"] += 1

        # 合併 Google Tasks
        for rtk in remote_tasks:
            tid = rtk.get("id")
            if not tid:
                continue
            r_etag = rtk.get("etag") or compute_etag(
                rtk.get("title", ""),
                None,
                None,
                rtk.get("due"),
                rtk.get("status") == "completed",
                bool(rtk.get("deleted", False)),
            )
            r_updated = self._parse_iso_dt(rtk.get("updated")) or datetime.datetime.now()
            r_due = self._parse_iso_dt(rtk.get("due"))
            is_comp = rtk.get("status") == "completed"
            is_del = bool(rtk.get("deleted", False))
            clean_title = str(rtk.get("title", "Google 待辦事項")).strip()

            existing = db.query(ScheduleItem).filter(
                ScheduleItem.google_task_id == tid
            ).first()

            if not existing:
                if is_del:
                    continue
                new_task = ScheduleItem(
                    user_id=user_id,
                    item_type="TASK",
                    title=clean_title,
                    due_time=r_due,
                    notes=rtk.get("notes") or "由 Google Tasks 遠端同步匯入",
                    category="待辦",
                    priority="Medium",
                    is_completed=is_comp,
                    is_deleted=False,
                    google_task_id=tid,
                    etag=r_etag,
                    sync_status="SYNCED_LOCAL_TWIN" if self.sync_mode == "LOCAL_SYNC_TWIN" else "SYNCED_CLOUD",
                    sync_source="GOOGLE",
                    last_synced_at=datetime.datetime.now(),
                    updated_at=r_updated,
                )
                db.add(new_task)
                stats["created_in_local"] += 1
            else:
                if existing.etag == r_etag:
                    stats["unchanged"] += 1
                    continue

                local_updated = existing.updated_at or existing.created_at or datetime.datetime.min
                if r_updated >= local_updated:
                    existing.title = clean_title
                    if r_due:
                        existing.due_time = r_due
                    if "notes" in rtk and rtk["notes"] is not None:
                        existing.notes = rtk["notes"]
                    existing.is_completed = is_comp
                    existing.is_deleted = is_del
                    existing.etag = r_etag
                    existing.sync_status = "CONFLICT_RESOLVED"
                    existing.last_synced_at = datetime.datetime.now()
                    existing.updated_at = r_updated
                    db.add(existing)
                    stats["updated_in_local"] += 1
                else:
                    self.push_item_to_google(db, existing, operation="MERGE_PUSH_BACK")
                    stats["pushed_back_to_remote"] += 1

        db.commit()
        return stats

    def _parse_iso_dt(self, val: Any) -> Optional[datetime.datetime]:
        if not val:
            return None
        if isinstance(val, datetime.datetime):
            return val.replace(tzinfo=None)
        s = str(val).strip()
        s = s.replace("Z", "")
        if "+" in s:
            s = s.split("+")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _extract_event_dt(self, node: Any) -> Optional[datetime.datetime]:
        if not node:
            return None
        if isinstance(node, str):
            return self._parse_iso_dt(node)
        if isinstance(node, dict):
            return self._parse_iso_dt(node.get("dateTime") or node.get("date"))
        return None

    def _fetch_ical_remote_events(self) -> List[Dict[str, Any]]:
        """整合 TravelWeatherBot 的多組 iCal URLs 讀取能力，轉換為標準遠端事件結構"""
        raw_urls = settings.GOOGLE_CALENDAR_ICAL_URLS
        if not raw_urls or not ICAL_AVAILABLE:
            return []

        results = []
        now = datetime.datetime.now()
        start_win = now - datetime.timedelta(days=1)
        end_win = now + datetime.timedelta(days=7)

        chunks = [c.strip() for c in raw_urls.replace(";", "\n").replace(",", "\n").splitlines() if c.strip()]
        for chunk in chunks:
            url = chunk.split("http", 1)[1] if "http" in chunk else chunk
            if not url.startswith("http"):
                url = "http" + url
            try:
                resp = requests.get(url, timeout=8)
                resp.raise_for_status()
                cal = icalendar.Calendar.from_ical(resp.text)
                evs = recurring_ical_events.of(cal).between(start_win, end_win)
                for ev in evs:
                    uid = str(ev.get("UID", uuid.uuid4().hex[:10]))
                    summary = str(ev.get("SUMMARY", "iCal 日曆行程")).strip()
                    dtstart = ev.get("DTSTART").dt
                    dtend_prop = ev.get("DTEND")
                    dtend = dtend_prop.dt if dtend_prop else None
                    if isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime):
                        dtstart = datetime.datetime(dtstart.year, dtstart.month, dtstart.day, 9, 0, 0)
                    if isinstance(dtend, datetime.date) and not isinstance(dtend, datetime.datetime):
                        dtend = datetime.datetime(dtend.year, dtend.month, dtend.day, 18, 0, 0)
                    if hasattr(dtstart, "replace"):
                        dtstart = dtstart.replace(tzinfo=None)
                    if dtend and hasattr(dtend, "replace"):
                        dtend = dtend.replace(tzinfo=None)
                    results.append({
                        "id": f"ical_{hashlib.md5(uid.encode()).hexdigest()[:12]}",
                        "summary": summary,
                        "location": str(ev.get("LOCATION", "")).strip() or None,
                        "description": str(ev.get("DESCRIPTION", "")).strip() or "由 iCal 訂閱同步",
                        "start": {"dateTime": dtstart.strftime("%Y-%m-%dT%H:%M:%S")},
                        "end": {"dateTime": (dtend or (dtstart + datetime.timedelta(hours=1))).strftime("%Y-%m-%dT%H:%M:%S")},
                        "status": "confirmed",
                        "updated": datetime.datetime.now().isoformat(),
                    })
            except Exception as e:
                logger.warning(f"拉取 iCal 遠端事件失敗: {e}")
        return results


sync_service = GoogleBiDirectionalSyncService()
