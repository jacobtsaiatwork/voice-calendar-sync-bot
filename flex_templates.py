import datetime
from typing import List, Optional, Dict, Any
from database import ScheduleItem
from ics_generator import generate_google_calendar_deeplink, generate_ics_download_url


def create_schedule_card(
    item: ScheduleItem,
    conflicts: Optional[List[ScheduleItem]] = None,
    header_override: Optional[str] = None
) -> dict:
    """
    建立或更新單一日曆事件 (CALENDAR_EVENT) 或待辦任務 (TASK) 的互動式 Flex Message 卡片：
    - 自動區分 Google Calendar 藍色主題 vs Google Tasks 翠綠主題（若有撞期則切換為琥珀紅警示主題）
    - 若有撞期 (conflicts)，醒目顯示「⚠️ 時段衝突警示」區塊，並提供「⏩ 自動順延至下個空檔」Postback 按鈕
    - 內建「📅 一鍵加入 Google Calendar (Deep Link)」與「📥 下載 .ics 行事曆檔」按鈕
    - 內建「✅ 完成」、「⏰ 延後 1 小時」、「🗑️ 刪除」雙向同步 Postback 按鈕
    """
    is_event = item.item_type == "CALENDAR_EVENT"
    has_conflict = bool(conflicts)

    if has_conflict:
        header_bg = "#DC2626"
        header_title = header_override or "⚠️ 已建立行程（偵測到時段衝突！）"
    elif is_event:
        header_bg = "#2563EB"
        header_title = header_override or "📅 已同步至 Google Calendar"
    else:
        header_bg = "#059669"
        header_title = header_override or "✅ 已同步至 Google Tasks"

    if is_event and item.start_time:
        end_str = item.end_time.strftime("%H:%M") if item.end_time else ""
        time_display = f"{item.start_time.strftime('%Y-%m-%d %H:%M')}" + (f" ~ {end_str}" if end_str else "")
    elif item.due_time:
        time_display = f"{item.due_time.strftime('%Y-%m-%d %H:%M')} 前截止"
    else:
        time_display = "彈性時間（未指定期限）"

    priority_colors = {"High": "#EF4444", "Medium": "#F59E0B", "Low": "#10B981"}
    badge_color = priority_colors.get(item.priority, "#F59E0B")
    type_label = "📅 日曆事件" if is_event else "✅ 待辦任務"
    sync_id = item.google_event_id or item.google_task_id or "local"

    body_contents: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": item.title,
            "weight": "bold",
            "size": "xl",
            "wrap": True,
            "color": "#1E293B"
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "margin": "sm",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#EFF6FF" if is_event else "#ECFDF5",
                    "cornerRadius": "sm",
                    "paddingAll": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": type_label,
                            "color": "#1D4ED8" if is_event else "#047857",
                            "size": "xxs",
                            "weight": "bold",
                            "align": "center"
                        }
                    ]
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": badge_color,
                    "cornerRadius": "sm",
                    "paddingAll": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"優先級: {item.priority}",
                            "color": "#FFFFFF",
                            "size": "xxs",
                            "align": "center"
                        }
                    ]
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F1F5F9",
                    "cornerRadius": "sm",
                    "paddingAll": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"#{item.category}",
                            "color": "#475569",
                            "size": "xxs",
                            "align": "center"
                        }
                    ]
                }
            ]
        },
        {
            "type": "box",
            "layout": "vertical",
            "margin": "lg",
            "spacing": "sm",
            "contents": [
                {
                    "type": "box",
                    "layout": "baseline",
                    "contents": [
                        {"type": "text", "text": "⏰ 時段", "color": "#64748B", "size": "sm", "flex": 2},
                        {"type": "text", "text": time_display, "color": "#0F172A", "size": "sm", "flex": 6, "weight": "bold", "wrap": True}
                    ]
                },
                {
                    "type": "box",
                    "layout": "baseline",
                    "contents": [
                        {"type": "text", "text": "📍 地點", "color": "#64748B", "size": "sm", "flex": 2},
                        {"type": "text", "text": item.location or "未指定地點", "color": "#334155", "size": "sm", "flex": 6, "wrap": True}
                    ]
                },
                {
                    "type": "box",
                    "layout": "baseline",
                    "contents": [
                        {"type": "text", "text": "🔄 同步", "color": "#64748B", "size": "sm", "flex": 2},
                        {"type": "text", "text": f"{item.sync_status} ({sync_id[:14]})", "color": "#0284C7", "size": "xs", "flex": 6, "wrap": True}
                    ]
                }
            ]
        }
    ]

    # 若偵測到撞期衝突，插入醒目紅框警示與一鍵自動順延按鈕
    if has_conflict and conflicts:
        conflict_names = "、".join(
            f"「{c.title} ({c.start_time.strftime('%H:%M')}~{c.end_time.strftime('%H:%M') if c.end_time else ''})」"
            for c in conflicts[:2]
        )
        body_contents.append({
            "type": "box",
            "layout": "vertical",
            "margin": "lg",
            "paddingAll": "md",
            "backgroundColor": "#FEF2F2",
            "borderColor": "#EF4444",
            "borderWidth": "1px",
            "cornerRadius": "md",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": "⚠️ 時段衝突警示 (Conflict Detected)",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#B91C1C"
                },
                {
                    "type": "text",
                    "text": f"此行程與現有 {conflict_names} 時段重疊！",
                    "size": "xs",
                    "color": "#7F1D1D",
                    "wrap": True
                },
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#DC2626",
                    "margin": "sm",
                    "action": {
                        "type": "postback",
                        "label": "⏩ 自動順延至下個空檔",
                        "data": f"action=auto_reschedule&id={item.id}",
                        "displayText": f"自動順延 #{item.id} 至下個空檔"
                    }
                }
            ]
        })

    gcal_url = generate_google_calendar_deeplink(item)
    ics_url = generate_ics_download_url(item)

    footer_contents: List[Dict[str, Any]] = [
        # 第一排：雙向同步快捷操作按鈕 (Postback)
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "xs",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#10B981",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "✅ 完成",
                        "data": f"action=complete&id={item.id}",
                        "displayText": f"完成 #{item.id}"
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "⏰ 延後1h",
                        "data": f"action=postpone&id={item.id}&minutes=60",
                        "displayText": f"延後 #{item.id}"
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "🗑️ 刪除",
                        "data": f"action=delete&id={item.id}",
                        "displayText": f"刪除 #{item.id}"
                    }
                }
            ]
        },
        # 第二排：Google Calendar Deep Link 與 .ics 檔案下載
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "xs",
            "margin": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "uri",
                        "label": "📅 加入 Google 日曆",
                        "uri": gcal_url
                    }
                },
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "uri",
                        "label": "📥 下載 .ics 檔",
                        "uri": ics_url
                    }
                }
            ]
        }
    ]

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": header_bg,
            "contents": [
                {
                    "type": "text",
                    "text": header_title,
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "md"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": body_contents
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "contents": footer_contents
        }
    }


def create_morning_brief_card(
    events: List[ScheduleItem],
    tasks: List[ScheduleItem],
    date_label: Optional[str] = None
) -> dict:
    """
    晨間日程總覽 (Morning Brief) 時間軸 Flex 卡片：
    彙整今日 Google Calendar 行程 + 未完成 Google Tasks 待辦清單。
    """
    today_str = date_label or datetime.datetime.now().strftime("%Y-%m-%d (%a)")
    body_rows: List[Dict[str, Any]] = []

    # 區塊 1: 今日日曆行程 (Google Calendar Events)
    body_rows.append({
        "type": "text",
        "text": f"📅 今日日曆行程 ({len(events)} 場)",
        "weight": "bold",
        "size": "sm",
        "color": "#1D4ED8"
    })

    if not events:
        body_rows.append({
            "type": "text",
            "text": "🎉 今日無排定會議，適合深度專注工作！",
            "size": "xs",
            "color": "#64748B",
            "margin": "sm"
        })
    else:
        for ev in events[:6]:
            t_str = ev.start_time.strftime("%H:%M") if ev.start_time else "全天"
            loc_str = f" · 📍{ev.location}" if ev.location else ""
            body_rows.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": t_str, "size": "xs", "weight": "bold", "color": "#2563EB", "flex": 2},
                    {"type": "text", "text": f"{ev.title}{loc_str}", "size": "xs", "color": "#1E293B", "flex": 6, "wrap": True},
                    {
                        "type": "button",
                        "style": "link",
                        "height": "sm",
                        "flex": 2,
                        "action": {
                            "type": "postback",
                            "label": "完成",
                            "data": f"action=complete&id={ev.id}",
                            "displayText": f"完成 #{ev.id}"
                        }
                    }
                ]
            })

    body_rows.append({"type": "separator", "margin": "lg"})

    # 區塊 2: 待辦任務清單 (Google Tasks)
    body_rows.append({
        "type": "text",
        "text": f"✅ 未完成 Google Tasks ({len(tasks)} 項)",
        "weight": "bold",
        "size": "sm",
        "color": "#047857",
        "margin": "md"
    })

    if not tasks:
        body_rows.append({
            "type": "text",
            "text": "✨ 所有待辦任務皆已結案！",
            "size": "xs",
            "color": "#64748B",
            "margin": "sm"
        })
    else:
        for tk in tasks[:6]:
            due_str = tk.due_time.strftime("%m/%d %H:%M") if tk.due_time else "無期限"
            p_icon = "🔴" if tk.priority == "High" else ("🟡" if tk.priority == "Medium" else "🟢")
            body_rows.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": f"{p_icon} #{tk.id}", "size": "xs", "color": "#475569", "flex": 2},
                    {"type": "text", "text": f"{tk.title} ({due_str})", "size": "xs", "color": "#1E293B", "flex": 6, "wrap": True},
                    {
                        "type": "button",
                        "style": "link",
                        "height": "sm",
                        "flex": 2,
                        "action": {
                            "type": "postback",
                            "label": "完成",
                            "data": f"action=complete&id={tk.id}",
                            "displayText": f"完成 #{tk.id}"
                        }
                    }
                ]
            })

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F172A",
            "contents": [
                {
                    "type": "text",
                    "text": f"☀️ 晨間雙向日程總覽 · {today_str}",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "md"
                },
                {
                    "type": "text",
                    "text": "Google Calendar × Google Tasks 即時同步",
                    "color": "#94A3B8",
                    "size": "xxs",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": body_rows
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#2563EB",
                    "action": {
                        "type": "postback",
                        "label": "🔄 立即雙向同步 Google",
                        "data": "action=sync_google",
                        "displayText": "立即雙向同步 Google"
                    }
                }
            ]
        }
    }


def create_schedule_list_card(items: List[ScheduleItem]) -> dict:
    """區分日曆事件與待辦事項的總覽清單卡片"""
    events = [i for i in items if i.item_type == "CALENDAR_EVENT"]
    tasks = [i for i in items if i.item_type != "CALENDAR_EVENT"]
    return create_morning_brief_card(events, tasks, date_label="全部未完成清單")
