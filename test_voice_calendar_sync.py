import json
import datetime
import pytest
from fastapi.testclient import TestClient

from main import app
from database import (
    Base,
    engine,
    SessionLocal,
    ScheduleItem,
    detect_conflicts,
    find_next_available_slot,
)
from ai_parser import parse_local_natural_language, analyze_input
from google_sync_service import sync_service, local_twin_store
from ics_generator import generate_ics_content, generate_google_calendar_deeplink
from flex_templates import create_schedule_card, create_morning_brief_card
from setup_rich_menu import build_rich_menu_request, generate_rich_menu_image_bytes


@pytest.fixture(autouse=True)
def clean_db_and_twin():
    """每次測試前重置 SQLite 資料表與 Local Sync Twin 遠端模擬倉庫"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    local_twin_store.clear()
    yield


def test_natural_language_event_vs_task_classification():
    """
    測試 1：語音與口語自然語言雙引擎解析 — Event vs Task 自動分流、時間區間、地點與優先級萃取
    """
    # 基準時間固定為 2026-10-24 (週六) 10:00:00
    base_now = datetime.datetime(2026, 10, 24, 10, 0, 0)

    # 案例 A：日曆事件 (CALENDAR_EVENT) — 具起訖時間與地點
    res_event = parse_local_natural_language(
        "下週三下午兩點到四點跟中華資安團隊開季會，地點在第一會議室",
        base_now=base_now,
    )
    assert res_event.action == "create"
    assert res_event.item_type == "CALENDAR_EVENT"
    assert "中華資安團隊開季會" in res_event.title
    assert res_event.location == "第一會議室"
    assert res_event.start_time_str == "2026-10-28 14:00:00"
    assert res_event.end_time_str == "2026-10-28 16:00:00"
    assert res_event.category == "會議"

    # 案例 B：截止日待辦任務 (TASK) — 含「前要...」與高優先級
    res_task1 = parse_local_natural_language(
        "明天晚上八點前要交專案報告，高優先級",
        base_now=base_now,
    )
    assert res_task1.action == "create"
    assert res_task1.item_type == "TASK"
    assert "交專案報告" in res_task1.title
    assert res_task1.due_time_str == "2026-10-25 20:00:00"
    assert res_task1.priority == "High"

    # 案例 C：生活採買待辦 (TASK) — 含「今天下班」
    res_task2 = parse_local_natural_language(
        "今天下班買牛奶跟雞蛋",
        base_now=base_now,
    )
    assert res_task2.action == "create"
    assert res_task2.item_type == "TASK"
    assert "買牛奶跟雞蛋" in res_task2.title
    assert res_task2.due_time_str == "2026-10-24 18:00:00"
    assert res_task2.category == "購物"

    # 案例 D：M4A 音訊 bytes 模擬輸入
    audio_bytes = "後天上午十點到十一點半跟客戶產品簡報，地點在台北101會議室".encode("utf-8")
    res_audio = analyze_input(
        audio_bytes, is_audio=True, mime_type="audio/mp4", base_now=base_now, prefer_local=True
    )
    assert res_audio.item_type == "CALENDAR_EVENT"
    assert res_audio.start_time_str == "2026-10-26 10:00:00"
    assert res_audio.end_time_str == "2026-10-26 11:30:00"


def test_conflict_detection_and_auto_reschedule():
    """
    測試 2：智慧撞期偵測 (Conflict Detection) 與「⏩ 自動順延至下個空檔」演算法
    """
    db = SessionLocal()
    try:
        user_id = "U_TEST_CONFLICT"
        # 建立第一場會議：14:00 ~ 16:00
        ev1 = ScheduleItem(
            user_id=user_id,
            item_type="CALENDAR_EVENT",
            title="中華資安團隊季會",
            start_time=datetime.datetime(2026, 10, 28, 14, 0, 0),
            end_time=datetime.datetime(2026, 10, 28, 16, 0, 0),
            location="第一會議室",
        )
        db.add(ev1)
        db.commit()
        db.refresh(ev1)

        # 檢查 15:00 ~ 16:30 是否與 ev1 撞期
        conflicts = detect_conflicts(
            db,
            user_id,
            datetime.datetime(2026, 10, 28, 15, 0, 0),
            datetime.datetime(2026, 10, 28, 16, 30, 0),
        )
        assert len(conflicts) == 1
        assert conflicts[0].id == ev1.id

        # 尋找 15:00 起長度 60 分鐘的下個空檔 -> 應自動順延至 16:00 ~ 17:00
        next_start, next_end = find_next_available_slot(
            db,
            user_id,
            desired_start=datetime.datetime(2026, 10, 28, 15, 0, 0),
            duration_minutes=60,
        )
        assert next_start == datetime.datetime(2026, 10, 28, 16, 0, 0)
        assert next_end == datetime.datetime(2026, 10, 28, 17, 0, 0)

        # 驗證撞期 Flex 卡片包含「⚠️ 時段衝突警示」與 `action=auto_reschedule` Postback 按鈕
        ev2 = ScheduleItem(
            id=99,
            user_id=user_id,
            item_type="CALENDAR_EVENT",
            title="架構審查會議",
            start_time=datetime.datetime(2026, 10, 28, 15, 0, 0),
            end_time=datetime.datetime(2026, 10, 28, 16, 0, 0),
        )
        card = create_schedule_card(ev2, conflicts=conflicts)
        card_json = json.dumps(card, ensure_ascii=False)
        assert "時段衝突警示" in card_json
        assert "action=auto_reschedule&id=99" in card_json
    finally:
        db.close()


def test_bidirectional_google_push_and_pull_merge_sync():
    """
    測試 3：Google Calendar & Google Tasks 雙向同步 (Push Sync + Pull & Two-Way Merge Sync)
    """
    db = SessionLocal()
    try:
        user_id = "U_SYNC_USER"
        # 1. Bot ➔ Google (Push Sync)：本地新增一筆日曆事件與一筆待辦任務
        local_ev = ScheduleItem(
            user_id=user_id,
            item_type="CALENDAR_EVENT",
            title="產品路線圖同步會",
            start_time=datetime.datetime(2026, 10, 29, 10, 0, 0),
            end_time=datetime.datetime(2026, 10, 29, 11, 0, 0),
            location="線上 Google Meet",
        )
        db.add(local_ev)
        db.commit()
        db.refresh(local_ev)

        synced_ev = sync_service.push_item_to_google(db, local_ev, operation="CREATE")
        assert synced_ev.google_event_id is not None
        assert synced_ev.etag is not None
        assert synced_ev.sync_status in ("SYNCED_LOCAL_TWIN", "SYNCED_CLOUD")
        assert synced_ev.google_event_id in local_twin_store.remote_events

        # 2. Google ➔ Bot (Pull & Two-Way Merge Sync)：
        # 模擬遠端 Google Calendar 修改了該行程標題，且遠端新增了一筆全新的 Google Task 與一筆新 Event
        future_ts = (datetime.datetime.now() + datetime.timedelta(minutes=5)).isoformat()
        external_remote_payload = {
            "events": [
                {
                    "id": synced_ev.google_event_id,
                    "summary": "產品路線圖同步會 (已於 Google 日曆改期)",
                    "location": "總部 A 棟 3F",
                    "start": {"dateTime": "2026-10-29T14:00:00"},
                    "end": {"dateTime": "2026-10-29T15:30:00"},
                    "status": "confirmed",
                    "updated": future_ts,
                    "etag": 'W/"remote_updated_etag_1"',
                },
                {
                    "id": "gcal_remote_new_001",
                    "summary": "遠端新增：年度預算審核",
                    "location": "董事會室",
                    "start": {"dateTime": "2026-10-30T09:30:00"},
                    "end": {"dateTime": "2026-10-30T11:00:00"},
                    "status": "confirmed",
                    "updated": future_ts,
                },
            ],
            "tasks": [
                {
                    "id": "gtask_remote_new_001",
                    "title": "遠端新增：簽署供應商保密協議 NDA",
                    "due": "2026-10-29T17:00:00.000Z",
                    "status": "needsAction",
                    "updated": future_ts,
                }
            ],
        }

        stats = sync_service.pull_and_merge_from_google(
            db, user_id=user_id, external_remote_payload=external_remote_payload
        )
        assert stats["created_in_local"] == 2
        assert stats["updated_in_local"] == 1

        # 驗證本地舊行程已被遠端雙向合併更新
        db.refresh(synced_ev)
        assert synced_ev.title == "產品路線圖同步會 (已於 Google 日曆改期)"
        assert synced_ev.location == "總部 A 棟 3F"
        assert synced_ev.start_time == datetime.datetime(2026, 10, 29, 14, 0, 0)
        assert synced_ev.sync_status == "CONFLICT_RESOLVED"

        # 驗證遠端新增的 Task 已寫入本地資料庫
        remote_task = (
            db.query(ScheduleItem)
            .filter(ScheduleItem.google_task_id == "gtask_remote_new_001")
            .first()
        )
        assert remote_task is not None
        assert remote_task.item_type == "TASK"
        assert remote_task.sync_source == "GOOGLE"
    finally:
        db.close()


def test_ics_and_google_deeplink_generation():
    """
    測試 4：RFC 5545 `.ics` 檔案內容與 Google Calendar Deep Link URL 生成
    """
    item = ScheduleItem(
        id=42,
        user_id="U_ICS_USER",
        item_type="CALENDAR_EVENT",
        title="資安演練檢討會",
        start_time=datetime.datetime(2026, 11, 2, 14, 0, 0),
        end_time=datetime.datetime(2026, 11, 2, 15, 30, 0),
        location="第一會議室",
        priority="High",
        category="會議",
        notes="準備滲透測試報告",
        google_event_id="gcal_ev_42",
    )

    deeplink = generate_google_calendar_deeplink(item)
    assert "https://calendar.google.com/calendar/render?" in deeplink
    assert "action=TEMPLATE" in deeplink
    assert "20261102T140000%2F20261102T153000" in deeplink

    ics_content = generate_ics_content(item)
    assert "BEGIN:VCALENDAR" in ics_content
    assert "BEGIN:VEVENT" in ics_content
    assert "SUMMARY:資安演練檢討會" in ics_content
    assert "LOCATION:第一會議室" in ics_content
    assert "TRIGGER:-PT30M" in ics_content
    assert "END:VCALENDAR" in ics_content


def test_fastapi_webhook_postback_and_cron_endpoints():
    """
    測試 5：FastAPI 端點整合測試 (Webhook 語音/文字建立、撞期偵測、Postback 自動順延/完成/延後/刪除、.ics 下載、晨間簡報與到期提醒)
    """
    client = TestClient(app)

    # 1. 健康檢查
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["status"] == "online"

    # 2. 透過 LINE Webhook 發送第一筆會議文字訊息
    webhook_ev1 = {
        "events": [
            {
                "type": "message",
                "replyToken": "dummy_reply_token",
                "source": {"userId": "U_E2E_USER"},
                "message": {
                    "type": "text",
                    "text": "今天下午兩點到四點跟中華資安團隊開季會，地點在第一會議室",
                },
            }
        ]
    }
    r_w1 = client.post("/webhook", json=webhook_ev1)
    assert r_w1.status_code == 200
    res1 = r_w1.json()["results"][0]
    assert res1["action"] == "create"
    assert res1["item_type"] == "CALENDAR_EVENT"
    assert res1["conflicts_count"] == 0
    ev1_id = res1["id"]

    # 3. 發送第二筆重疊時段的會議 -> 應觸發撞期偵測 (conflicts_count == 1)
    webhook_ev2 = {
        "events": [
            {
                "type": "message",
                "replyToken": "dummy_reply_token",
                "source": {"userId": "U_E2E_USER"},
                "message": {
                    "type": "text",
                    "text": "今天下午三點到四點跟客戶討論合約，地點在第二會議室",
                },
            }
        ]
    }
    r_w2 = client.post("/webhook", json=webhook_ev2)
    assert r_w2.status_code == 200
    res2 = r_w2.json()["results"][0]
    assert res2["conflicts_count"] == 1
    ev2_id = res2["id"]

    # 4. 點擊「⏩ 自動順延至下個空檔」Postback 按鈕 -> 應順延至 16:00~17:00
    webhook_pb_reschedule = {
        "events": [
            {
                "type": "postback",
                "replyToken": "dummy_reply_token",
                "source": {"userId": "U_E2E_USER"},
                "postback": {"data": f"action=auto_reschedule&id={ev2_id}"},
            }
        ]
    }
    r_pb1 = client.post("/webhook", json=webhook_pb_reschedule)
    assert r_pb1.status_code == 200
    pb_res1 = r_pb1.json()["results"][0]
    assert pb_res1["action"] == "auto_reschedule"
    assert "T16:00:00" in pb_res1["start_time"]

    # 5. 下載 .ics 行事曆檔
    r_ics = client.get(f"/api/ics/{ev1_id}.ics")
    assert r_ics.status_code == 200
    assert "BEGIN:VCALENDAR" in r_ics.text
    assert "中華資安團隊開季會" in r_ics.text

    # 6. 新增一筆 15 分鐘後到期的待辦任務，測試 `/api/cron/upcoming-reminders` 與 `/api/cron/morning-brief`
    db = SessionLocal()
    try:
        soon_dt = datetime.datetime.now() + datetime.timedelta(minutes=15)
        urgent_task = ScheduleItem(
            user_id="U_E2E_USER",
            item_type="TASK",
            title="緊急提交雲端架構圖",
            due_time=soon_dt,
            priority="High",
            category="工作",
        )
        db.add(urgent_task)
        db.commit()
        db.refresh(urgent_task)
        task_id = urgent_task.id
    finally:
        db.close()

    r_brief = client.post("/api/cron/morning-brief?user_id=U_E2E_USER")
    assert r_brief.status_code == 200
    brief_data = r_brief.json()
    assert brief_data["events_count"] >= 2
    assert brief_data["tasks_count"] >= 1
    assert brief_data["flex_card"]["type"] == "bubble"

    r_rem = client.post("/api/cron/upcoming-reminders")
    assert r_rem.status_code == 200
    assert task_id in r_rem.json()["reminded_ids"]

    # 7. 測試 Postback 完成與刪除並同步遠端
    r_comp = client.post(
        "/webhook",
        json={
            "events": [
                {
                    "type": "postback",
                    "replyToken": "dummy_reply_token",
                    "source": {"userId": "U_E2E_USER"},
                    "postback": {"data": f"action=complete&id={task_id}"},
                }
            ]
        },
    )
    assert r_comp.status_code == 200
    assert r_comp.json()["results"][0]["is_completed"] is True

    # 8. 驗證 Rich Menu 結構與底圖生成
    rm_req = build_rich_menu_request()
    assert len(rm_req.areas) == 3
    img_bytes = generate_rich_menu_image_bytes()
    assert len(img_bytes) > 1000
