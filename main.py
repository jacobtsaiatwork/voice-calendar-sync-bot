import json
import re
import urllib.parse
import datetime
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, Header, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
)

from config import settings
from database import (
    init_db,
    get_db,
    ScheduleItem,
    detect_conflicts,
    find_next_available_slot,
)
from ai_parser import analyze_input, ScheduleExtraction
from google_sync_service import sync_service
from ics_generator import generate_ics_content, generate_google_calendar_deeplink
from flex_templates import (
    create_schedule_card,
    create_morning_brief_card,
    create_schedule_list_card,
)

app = FastAPI(
    title="Voice Secretary & Google Calendar/Tasks Bi-Directional Sync Bot",
    version="2.0.0",
)

init_db()

RECENT_LOGS = []


def add_log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    RECENT_LOGS.append(f"[{ts}] {msg}")
    if len(RECENT_LOGS) > 50:
        RECENT_LOGS.pop(0)


@app.get("/")
@app.get("/api")
@app.get("/api/")
def read_root():
    return {
        "status": "online",
        "service": "Voice Secretary & Google Calendar/Tasks Bi-Directional Sync Bot",
        "sync_mode": sync_service.sync_mode,
        "timestamp": datetime.datetime.now().isoformat(),
    }


@app.get("/logs")
@app.get("/api/logs")
def view_logs():
    return {"logs": RECENT_LOGS}


@app.get("/webhook")
@app.get("/api/webhook")
def webhook_health():
    return {"status": "ok", "message": "LINE Webhook Endpoint Ready"}


@app.get("/api/ics/{item_id}.ics")
@app.get("/ics/{item_id}.ics")
def download_ics(item_id: int, db: Session = Depends(get_db)):
    """
    提供一鍵下載 `.ics` 標準行事曆檔案，支援 iPhone / Android / Outlook / Apple Calendar 直接匯入。
    """
    item = db.query(ScheduleItem).filter(ScheduleItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail=f"找不到編號 #{item_id} 的行程或任務")

    ics_text = generate_ics_content(item)
    headers = {
        "Content-Disposition": f'attachment; filename="schedule_{item.id}.ics"',
        "Content-Type": "text/calendar; charset=utf-8",
    }
    return Response(content=ics_text, media_type="text/calendar", headers=headers)


@app.post("/api/sync/google")
@app.get("/api/sync/google")
def api_sync_google(
    payload: Optional[Dict[str, Any]] = None,
    user_id: str = "default_user",
    db: Session = Depends(get_db),
):
    """
    手動或透過 Google Apps Script / Cron 觸發 Google Calendar & Tasks 雙向合併同步 (Two-Way Merge)。
    """
    stats = sync_service.pull_and_merge_from_google(
        db, user_id=user_id, external_remote_payload=payload
    )
    add_log(f"執行 Google 雙向同步完成: {stats}")
    return {"status": "ok", "sync_stats": stats}


@app.post("/api/cron/morning-brief")
@app.get("/api/cron/morning-brief")
def cron_morning_brief(
    user_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    每日晨間排程推播 (Morning Brief)：
    1. 先執行一次 Google Calendar & Tasks 雙向同步拉取最新狀態
    2. 彙整今日 Google Calendar 行程 + 未完成 Google Tasks 待辦清單，生成時間軸 Flex 卡片推播
    """
    now = datetime.datetime.now()
    start_of_day = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)
    end_of_day = datetime.datetime(now.year, now.month, now.day, 23, 59, 59)

    # 先從遠端拉取最新更新
    sync_stats = sync_service.pull_and_merge_from_google(
        db, user_id=user_id or "default_user"
    )

    events_q = db.query(ScheduleItem).filter(
        ScheduleItem.item_type == "CALENDAR_EVENT",
        ScheduleItem.is_deleted == False,
        ScheduleItem.is_completed == False,
        ScheduleItem.start_time >= start_of_day,
        ScheduleItem.start_time <= end_of_day,
    )
    tasks_q = db.query(ScheduleItem).filter(
        ScheduleItem.item_type == "TASK",
        ScheduleItem.is_deleted == False,
        ScheduleItem.is_completed == False,
    )
    if user_id:
        events_q = events_q.filter(ScheduleItem.user_id == user_id)
        tasks_q = tasks_q.filter(ScheduleItem.user_id == user_id)

    today_events = events_q.order_by(ScheduleItem.start_time.asc()).all()
    pending_tasks = tasks_q.order_by(ScheduleItem.due_time.asc().nullslast()).all()

    flex_dict = create_morning_brief_card(
        today_events, pending_tasks, date_label=now.strftime("%Y-%m-%d")
    )

    pushed_users = []
    target_users = (
        [user_id]
        if user_id
        else list({i.user_id for i in (today_events + pending_tasks) if i.user_id and i.user_id != "default_user"})
    )

    if target_users and settings.LINE_CHANNEL_ACCESS_TOKEN:
        try:
            cfg = Configuration(access_token=settings.LINE_CHANNEL_ACCESS_TOKEN)
            with ApiClient(cfg) as api_client:
                messaging_api = MessagingApi(api_client)
                for uid in target_users:
                    try:
                        messaging_api.push_message(
                            PushMessageRequest(
                                to=uid,
                                messages=[
                                    FlexMessage(
                                        alt_text=f"☀️ 晨間日程總覽 ({len(today_events)} 場行程 / {len(pending_tasks)} 項待辦)",
                                        contents=FlexContainer.from_dict(flex_dict),
                                    )
                                ],
                            )
                        )
                        pushed_users.append(uid)
                    except Exception as pe:
                        add_log(f"晨間推播失敗 ({uid}): {pe}")
        except Exception as e:
            add_log(f"MessagingApi 初始化異常: {e}")

    return {
        "status": "ok",
        "date": now.strftime("%Y-%m-%d"),
        "events_count": len(today_events),
        "tasks_count": len(pending_tasks),
        "pushed_users": pushed_users,
        "sync_stats": sync_stats,
        "flex_card": flex_dict,
    }


@app.post("/api/cron/upcoming-reminders")
@app.get("/api/cron/upcoming-reminders")
@app.post("/cron/remind")
@app.get("/cron/remind")
def cron_upcoming_reminders(db: Session = Depends(get_db)):
    """
    會議與待辦到期前 30 分鐘主動推播提醒：
    檢查未來 30 分鐘內即將開始的 `CALENDAR_EVENT` 或即將截止的 `TASK`（且尚未提醒過），
    主動發送 LINE Flex 卡片通知。
    """
    now = datetime.datetime.now()
    threshold = now + datetime.timedelta(minutes=30)
    window_start = now - datetime.timedelta(minutes=30)

    candidates = (
        db.query(ScheduleItem)
        .filter(
            ScheduleItem.is_deleted == False,
            ScheduleItem.is_completed == False,
            ScheduleItem.reminded_at == None,
        )
        .all()
    )

    due_items = []
    for item in candidates:
        target_dt = item.start_time if item.item_type == "CALENDAR_EVENT" else item.due_time
        if target_dt and window_start <= target_dt <= threshold:
            due_items.append(item)

    notified_count = 0
    if due_items:
        cfg = Configuration(access_token=settings.LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(cfg) as api_client:
            messaging_api = MessagingApi(api_client)
            for item in due_items:
                item.reminded_at = now
                db.add(item)
                flex_dict = create_schedule_card(
                    item, header_override="⏰ 即將開始 / 到期提醒 (30分鐘內)"
                )
                if item.user_id and item.user_id not in ("default_user", "test_user"):
                    try:
                        messaging_api.push_message(
                            PushMessageRequest(
                                to=item.user_id,
                                messages=[
                                    FlexMessage(
                                        alt_text=f"⏰ 提醒：{item.title}",
                                        contents=FlexContainer.from_dict(flex_dict),
                                    )
                                ],
                            )
                        )
                    except Exception as pe:
                        add_log(f"提醒推播失敗 (#{item.id}): {pe}")
                notified_count += 1
        db.commit()

    return {
        "status": "ok",
        "notified_count": notified_count,
        "checked_at": now.isoformat(),
        "reminded_ids": [i.id for i in due_items],
    }


@app.post("/webhook")
@app.post("/api/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None, alias="X-Line-Signature"),
    db: Session = Depends(get_db),
):
    body = (await request.body()).decode("utf-8")
    if not body:
        return JSONResponse(content={"status": "ok", "message": "Verified"})

    try:
        payload = json.loads(body)
    except Exception:
        payload = {}

    raw_events = payload.get("events", [])
    if not raw_events:
        return JSONResponse(status_code=200, content={"status": "ok", "message": "Verify passed"})

    current_config = Configuration(access_token=settings.LINE_CHANNEL_ACCESS_TOKEN)
    processed_results = []

    with ApiClient(current_config) as api_client:
        messaging_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)

        for event in raw_events:
            event_type = event.get("type")
            reply_token = event.get("replyToken", "dummy_reply_token")
            user_id = event.get("source", {}).get("userId", "default_user")

            # 1. 處理 Postback 按鈕互動事件 (完成 / 延後1小時 / 自動順延空檔 / 刪除 / 雙向同步)
            if event_type == "postback":
                pb_data = event.get("postback", {}).get("data", "")
                res = handle_postback_action(pb_data, user_id, reply_token, messaging_api, db)
                processed_results.append(res)
                continue

            if event_type != "message":
                continue

            msg_obj = event.get("message", {})
            msg_type = msg_obj.get("type")

            # 2. 處理語音訊息 (M4A)
            if msg_type == "audio":
                message_id = msg_obj.get("id")
                try:
                    audio_content = blob_api.get_message_content(message_id)
                except Exception:
                    audio_content = b""
                extraction = analyze_input(audio_content, is_audio=True, mime_type="audio/mp4")
                res = execute_schedule_extraction(extraction, user_id, reply_token, messaging_api, db)
                processed_results.append(res)

            # 3. 處理文字訊息
            elif msg_type == "text":
                user_text = msg_obj.get("text", "").strip()
                extraction = analyze_input(user_text, is_audio=False)
                res = execute_schedule_extraction(extraction, user_id, reply_token, messaging_api, db)
                processed_results.append(res)

    return JSONResponse(content={"status": "ok", "results": processed_results})


def handle_postback_action(
    pb_data: str,
    user_id: str,
    reply_token: str,
    messaging_api: MessagingApi,
    db: Session,
) -> Dict[str, Any]:
    """
    處理 Flex Message 上的 Postback 按鈕並即時雙向同步至 Google Calendar / Tasks：
    - action=complete&id=X
    - action=postpone&id=X&minutes=60
    - action=auto_reschedule&id=X
    - action=delete&id=X
    - action=sync_google
    """
    params = dict(urllib.parse.parse_qsl(pb_data))
    action = params.get("action", "")
    item_id = int(params["id"]) if params.get("id", "").isdigit() else None

    if action == "sync_google":
        stats = sync_service.pull_and_merge_from_google(db, user_id=user_id)
        msg = (
            f"🔄 Google Calendar & Tasks 雙向同步完成！\n"
            f"• 同步模式：{stats['mode']}\n"
            f"• 遠端日曆事件：{stats['pulled_events']} 筆\n"
            f"• 遠端待辦任務：{stats['pulled_tasks']} 筆\n"
            f"• 本地新增/更新：{stats['created_in_local'] + stats['updated_in_local']} 筆"
        )
        _safe_reply_text(messaging_api, reply_token, msg)
        return {"action": "sync_google", "stats": stats}

    if not item_id:
        return {"action": action, "error": "missing_id"}

    item = db.query(ScheduleItem).filter(ScheduleItem.id == item_id).first()
    if not item:
        _safe_reply_text(messaging_api, reply_token, f"⚠️ 找不到編號 #{item_id} 的項目。")
        return {"action": action, "error": "not_found"}

    if action == "complete":
        item.is_completed = True
        sync_service.push_item_to_google(db, item, operation="COMPLETE")
        _safe_reply_text(
            messaging_api,
            reply_token,
            f"🎉 已完成 #{item.id}「{item.title}」並同步更新至 Google ({item.google_event_id or item.google_task_id})！",
        )
        return {"action": "complete", "id": item.id, "is_completed": True}

    elif action == "postpone":
        minutes = int(params.get("minutes", "60"))
        delta = datetime.timedelta(minutes=minutes)
        if item.item_type == "CALENDAR_EVENT":
            base_s = item.start_time or datetime.datetime.now()
            base_e = item.end_time or (base_s + datetime.timedelta(hours=1))
            item.start_time = base_s + delta
            item.end_time = base_e + delta
        else:
            base_d = item.due_time or datetime.datetime.now()
            item.due_time = base_d + delta

        sync_service.push_item_to_google(db, item, operation="POSTPONE")
        conflicts = detect_conflicts(db, item.user_id, item.start_time, item.end_time, exclude_id=item.id)
        flex_dict = create_schedule_card(item, conflicts=conflicts, header_override=f"⏰ 已順延 {minutes} 分鐘並同步 Google")
        _safe_reply_flex(messaging_api, reply_token, f"已順延：{item.title}", flex_dict)
        return {"action": "postpone", "id": item.id, "new_time": str(item.effective_time)}

    elif action == "auto_reschedule":
        desired_start = item.start_time or datetime.datetime.now()
        duration_mins = 60
        if item.start_time and item.end_time:
            duration_mins = max(15, int((item.end_time - item.start_time).total_seconds() // 60))
        new_start, new_end = find_next_available_slot(
            db, item.user_id, desired_start, duration_minutes=duration_mins, exclude_id=item.id
        )
        item.start_time = new_start
        item.end_time = new_end
        sync_service.push_item_to_google(db, item, operation="AUTO_RESCHEDULE")
        flex_dict = create_schedule_card(item, conflicts=[], header_override="✨ 已自動排入無衝突空檔並同步 Google")
        _safe_reply_flex(messaging_api, reply_token, f"已自動順延：{item.title}", flex_dict)
        return {
            "action": "auto_reschedule",
            "id": item.id,
            "start_time": new_start.isoformat(),
            "end_time": new_end.isoformat(),
        }

    elif action == "delete":
        item.is_deleted = True
        sync_service.push_item_to_google(db, item, operation="DELETE")
        _safe_reply_text(
            messaging_api,
            reply_token,
            f"🗑️ 已刪除 #{item.id}「{item.title}」並從遠端 Google 同步移除！",
        )
        return {"action": "delete", "id": item.id, "is_deleted": True}

    return {"action": action, "status": "ignored"}


def execute_schedule_extraction(
    extraction: ScheduleExtraction,
    user_id: str,
    reply_token: str,
    messaging_api: MessagingApi,
    db: Session,
) -> Dict[str, Any]:
    action = extraction.action

    if action == "create":
        start_dt = _parse_dt(extraction.start_time_str)
        end_dt = _parse_dt(extraction.end_time_str)
        due_dt = _parse_dt(extraction.due_time_str)

        if extraction.item_type == "CALENDAR_EVENT" and start_dt and not end_dt:
            end_dt = start_dt + datetime.timedelta(hours=1)

        new_item = ScheduleItem(
            user_id=user_id,
            item_type=extraction.item_type,
            title=extraction.title or "未命名事項",
            start_time=start_dt,
            end_time=end_dt,
            due_time=due_dt,
            is_all_day=extraction.is_all_day,
            location=extraction.location,
            priority=extraction.priority or "Medium",
            category=extraction.category or "一般",
            notes=extraction.notes,
            is_completed=False,
            is_deleted=False,
            sync_source="LINE",
        )
        db.add(new_item)
        db.commit()
        db.refresh(new_item)

        # 推送至 Google Calendar / Google Tasks
        new_item = sync_service.push_item_to_google(db, new_item, operation="CREATE")

        # 智慧撞期偵測 (若為 CALENDAR_EVENT)
        conflicts = []
        if new_item.item_type == "CALENDAR_EVENT" and new_item.start_time:
            conflicts = detect_conflicts(
                db, user_id, new_item.start_time, new_item.end_time, exclude_id=new_item.id
            )

        flex_dict = create_schedule_card(new_item, conflicts=conflicts)
        _safe_reply_flex(messaging_api, reply_token, f"已建立並同步：{new_item.title}", flex_dict)
        return {
            "action": "create",
            "id": new_item.id,
            "item_type": new_item.item_type,
            "conflicts_count": len(conflicts),
            "google_id": new_item.google_event_id or new_item.google_task_id,
        }

    elif action == "today":
        now = datetime.datetime.now()
        start_of_day = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)
        end_of_day = datetime.datetime(now.year, now.month, now.day, 23, 59, 59)
        sync_service.pull_and_merge_from_google(db, user_id=user_id)
        events = (
            db.query(ScheduleItem)
            .filter(
                ScheduleItem.user_id == user_id,
                ScheduleItem.item_type == "CALENDAR_EVENT",
                ScheduleItem.is_deleted == False,
                ScheduleItem.is_completed == False,
                ScheduleItem.start_time >= start_of_day,
                ScheduleItem.start_time <= end_of_day,
            )
            .order_by(ScheduleItem.start_time.asc())
            .all()
        )
        tasks = (
            db.query(ScheduleItem)
            .filter(
                ScheduleItem.user_id == user_id,
                ScheduleItem.item_type == "TASK",
                ScheduleItem.is_deleted == False,
                ScheduleItem.is_completed == False,
            )
            .order_by(ScheduleItem.due_time.asc().nullslast())
            .all()
        )
        flex_dict = create_morning_brief_card(events, tasks)
        _safe_reply_flex(messaging_api, reply_token, "☀️ 今日日程與待辦總覽", flex_dict)
        return {"action": "today", "events": len(events), "tasks": len(tasks)}

    elif action == "list":
        sync_service.pull_and_merge_from_google(db, user_id=user_id)
        items = (
            db.query(ScheduleItem)
            .filter(
                ScheduleItem.user_id == user_id,
                ScheduleItem.is_deleted == False,
                ScheduleItem.is_completed == False,
            )
            .order_by(ScheduleItem.start_time.asc().nullslast(), ScheduleItem.due_time.asc().nullslast())
            .all()
        )
        flex_dict = create_schedule_list_card(items)
        _safe_reply_flex(messaging_api, reply_token, "📋 日曆行程與待辦清單", flex_dict)
        return {"action": "list", "count": len(items)}

    elif action == "sync":
        return handle_postback_action("action=sync_google", user_id, reply_token, messaging_api, db)

    elif action in ("complete", "delete", "postpone") and extraction.target_id:
        return handle_postback_action(
            f"action={action}&id={extraction.target_id}", user_id, reply_token, messaging_api, db
        )

    else:
        help_text = (
            "🎙️【語音待辦秘書 × Google Calendar/Tasks 雙向同步助理】\n"
            "您可以直接傳送語音或口語文字：\n"
            "1️⃣ 日曆會議（自動歸入 Google Calendar）：\n"
            "「下週三下午兩點到四點跟中華資安團隊開季會，地點在第一會議室」\n"
            "2️⃣ 待辦任務（自動歸入 Google Tasks）：\n"
            "「明天晚上八點前要交專案報告，高優先級」\n"
            "3️⃣ 智慧防撞期與一鍵匯出：\n"
            "支援衝突警示、自動順延空檔、一鍵加入 Google Calendar 與下載 .ics 行事曆！"
        )
        _safe_reply_text(messaging_api, reply_token, help_text)
        return {"action": "help"}


def _parse_dt(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    if not dt_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return None


def _safe_reply_text(messaging_api: MessagingApi, reply_token: str, text: str):
    if not reply_token or reply_token == "dummy_reply_token":
        return
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )
    except Exception as e:
        add_log(f"Reply Text 略過或失敗: {e}")


def _safe_reply_flex(messaging_api: MessagingApi, reply_token: str, alt_text: str, flex_dict: dict):
    if not reply_token or reply_token == "dummy_reply_token":
        return
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text=alt_text, contents=FlexContainer.from_dict(flex_dict))],
            )
        )
    except Exception as e:
        add_log(f"Reply Flex 略過或失敗: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.PORT, reload=True)
