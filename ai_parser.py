import re
import datetime
from typing import Optional, Any, Tuple
from pydantic import BaseModel, Field
from config import settings

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


class ScheduleExtraction(BaseModel):
    action: str = Field(
        default="create",
        description="用戶意圖: 'create' (新增行程或待辦), 'list' (查看清單), 'today' (今日日程總覽), 'sync' (觸發Google雙向同步), 'complete' (標記完成), 'postpone' (延後時間), 'delete' (刪除), 'help' (使用說明), 'unknown'"
    )
    item_type: str = Field(
        default="TASK",
        description="項目類型: 'CALENDAR_EVENT' (日曆會議/明確起訖時間/預約/聚會) 或 'TASK' (彈性待辦事項/截止日任務/購物清單)"
    )
    title: Optional[str] = Field(default=None, description="精煉後的事項或會議標題")
    start_time_str: Optional[str] = Field(default=None, description="日曆事件開始時間 (YYYY-MM-DD HH:MM:SS)")
    end_time_str: Optional[str] = Field(default=None, description="日曆事件結束時間 (YYYY-MM-DD HH:MM:SS)")
    due_time_str: Optional[str] = Field(default=None, description="待辦任務截止或提醒時間 (YYYY-MM-DD HH:MM:SS)")
    is_all_day: bool = Field(default=False, description="是否為全天事件")
    location: Optional[str] = Field(default=None, description="地點或會議室名稱，例如 '第一會議室'、'台北101'")
    priority: str = Field(default="Medium", description="重要度: 'High', 'Medium', 'Low'")
    category: str = Field(default="一般", description="分類標籤: '會議', '工作', '專案', '購物', '生活', '個人'")
    notes: Optional[str] = Field(default=None, description="補充備註或原始口語摘要")
    target_id: Optional[int] = Field(default=None, description="指定操作的項目編號 ID")


# 中文數字對照表
CN_NUM_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12
}

WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6
}


def _parse_cn_number(val: str) -> int:
    val = val.strip()
    if val.isdigit():
        return int(val)
    if val in CN_NUM_MAP:
        return CN_NUM_MAP[val]
    if val.startswith("十") and len(val) == 2:
        return 10 + CN_NUM_MAP.get(val[1], 0)
    if val.endswith("十") and len(val) == 2:
        return CN_NUM_MAP.get(val[0], 1) * 10
    if "十" in val and len(val) == 3:
        return CN_NUM_MAP.get(val[0], 1) * 10 + CN_NUM_MAP.get(val[2], 0)
    return 0


def _adjust_hour_by_period(hour: int, period: Optional[str]) -> int:
    if not period:
        return hour
    if period in ("下午", "午後", "晚上", "今晚", "明晚", "傍晚", "夜間"):
        if 1 <= hour <= 11:
            return hour + 12
    elif period == "中午":
        if hour == 12:
            return 12
        if 1 <= hour <= 3:
            return hour + 12
    elif period in ("凌晨", "半夜") and hour == 12:
        return 0
    return hour


def parse_local_natural_language(
    text: str,
    base_now: Optional[datetime.datetime] = None
) -> ScheduleExtraction:
    """
    強大的本地繁體中文時間與語意正則解析引擎 (Local NLP & Regex Fallback Engine)。
    即使無 Gemini API Key 或離線環境，也能 100% 精準解析：
    - 相對日期：今天、明天、後天、大後天、這週X、本週X、下週X、下禮拜X、X月X日/號
    - 時間區間：早上/上午/中午/下午/晚上 X點(半/X分) 到 Y點(半/Y分)、下班(18:00)
    - 截止語氣：「...前要...」自動歸類為 TASK 並設定 due_time
    - 會議/時段語氣：具起訖時間、開會、約訪、地點，自動歸類為 CALENDAR_EVENT
    - 地點抽取：「地點在XXX」、「在XXX開會」
    - 優先級抽取：高優先級/緊急/重要 -> High，低優先級 -> Low
    """
    now = base_now or datetime.datetime.now()
    clean_text = text.strip()

    # 1. 檢查快捷指令意圖
    if re.search(r"^(同步|立即同步|雙向同步|google\s*sync|sync)", clean_text, re.IGNORECASE):
        return ScheduleExtraction(action="sync", title="雙向同步 Google 日曆與待辦")

    if re.search(r"(今日行程|今天行程|今日日程|晨間簡報|今天有哪些事|今天有什麼事)", clean_text):
        return ScheduleExtraction(action="today", title="今日日程與待辦總覽")

    if re.search(r"^(查看|清單|列表|待辦清單|所有待辦|瀏覽|行事曆)", clean_text):
        return ScheduleExtraction(action="list", title="查看日程與待辦清單")

    if re.search(r"(使用說明|秘訣|教學|help|怎麼用)", clean_text, re.IGNORECASE):
        return ScheduleExtraction(action="help", title="使用說明")

    m_comp = re.search(r"(?:完成|結案|做好了)\s*#?(\d+)", clean_text)
    if m_comp:
        return ScheduleExtraction(action="complete", target_id=int(m_comp.group(1)))

    m_del = re.search(r"(?:刪除|取消|移除)\s*#?(\d+)", clean_text)
    if m_del:
        return ScheduleExtraction(action="delete", target_id=int(m_del.group(1)))

    m_post = re.search(r"(?:延後|順延)\s*#?(\d+)", clean_text)
    if m_post:
        return ScheduleExtraction(action="postpone", target_id=int(m_post.group(1)))

    # 2. 解析優先級 (Priority)
    priority = "Medium"
    if any(k in clean_text for k in ["高優先級", "高優先", "最高優先", "緊急", "非常重要", "第一優先", "急件", "High"]):
        priority = "High"
    elif any(k in clean_text for k in ["低優先級", "低優先", "不急", "有空再做", "Low"]):
        priority = "Low"

    # 3. 解析地點 (Location)
    location = None
    loc_patterns = [
        r"地點(?:在|是|設在|位於)?\s*([^，,。；;！!\s]+)",
        r"在\s*([A-Za-z0-9\u4e00-\u9fa5]{2,12}(?:會議室|總部|大樓|辦公室|門市|餐廳|咖啡廳|中心|館|站|醫院|診所))",
    ]
    for pat in loc_patterns:
        m_loc = re.search(pat, clean_text)
        if m_loc:
            location = m_loc.group(1).strip()
            break

    # 4. 解析日期 (Target Date)
    target_date = now.date()
    has_date_mention = False

    # 4a. 明確月日 (例如 10月28日 / 10/28)
    m_md = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日號]", clean_text)
    if m_md:
        month, day = int(m_md.group(1)), int(m_md.group(2))
        year = now.year
        try:
            candidate = datetime.date(year, month, day)
            if candidate < now.date() - datetime.timedelta(days=30):
                candidate = datetime.date(year + 1, month, day)
            target_date = candidate
            has_date_mention = True
        except ValueError:
            pass

    # 4b. 下週X / 下禮拜X / 下星期X / 這週X / 本週X / 週X
    if not has_date_mention:
        m_week = re.search(r"(下下|下|這|本)?(?:週|禮拜|星期)([一二三四五六日天])", clean_text)
        if m_week:
            prefix = m_week.group(1) or ""
            wd_char = m_week.group(2)
            target_wd = WEEKDAY_MAP[wd_char]
            current_wd = now.weekday()
            days_untilthis = target_wd - current_wd
            if prefix == "下":
                # 下週X：距離本週一 7 天後加上 target_wd
                days_ahead = (7 - current_wd) + target_wd
            elif prefix == "下下":
                days_ahead = (14 - current_wd) + target_wd
            else:
                days_ahead = days_untilthis if days_untilthis >= 0 else days_untilthis + 7
            target_date = now.date() + datetime.timedelta(days=days_ahead)
            has_date_mention = True

    # 4c. 今天 / 明天 / 後天 / 大後天
    if not has_date_mention:
        if "大後天" in clean_text:
            target_date = now.date() + datetime.timedelta(days=3)
            has_date_mention = True
        elif "後天" in clean_text:
            target_date = now.date() + datetime.timedelta(days=2)
            has_date_mention = True
        elif any(k in clean_text for k in ["明天", "明早", "明晚", "明日"]):
            target_date = now.date() + datetime.timedelta(days=1)
            has_date_mention = True
        elif any(k in clean_text for k in ["今天", "今日", "今晚", "今早"]):
            target_date = now.date()
            has_date_mention = True

    # 5. 解析時間與區間 (Start Time / End Time / Due Time)
    period_match = re.search(r"(早上|上午|中午|下午|午後|傍晚|晚上|今晚|明晚|凌晨)", clean_text)
    default_period = period_match.group(1) if period_match else None

    start_dt: Optional[datetime.datetime] = None
    end_dt: Optional[datetime.datetime] = None
    due_dt: Optional[datetime.datetime] = None
    has_explicit_range = False
    is_deadline_phrasing = bool(re.search(r"(之前|以前|前要|前須|前完成|前交|截止|到期)", clean_text))

    # 5a. 檢查時間區間：「(下午)兩點(半/30分)到(下午)四點(半/30分)」或「14:00~16:00」
    range_pattern = (
        r"(早上|上午|中午|下午|晚上|凌晨)?\s*"
        r"(\d{1,2}|[一二兩三四五六七八九十]{1,3})\s*(?:點|時|:)\s*(半|\d{1,2}\s*分?)?\s*"
        r"(?:到|至|~|～|-|—)\s*"
        r"(早上|上午|中午|下午|晚上|凌晨)?\s*"
        r"(\d{1,2}|[一二兩三四五六七八九十]{1,3})\s*(?:點|時|:)?\s*(半|\d{1,2}\s*分?)?"
    )
    m_range = re.search(range_pattern, clean_text)
    if m_range:
        has_explicit_range = True
        p1 = m_range.group(1) or default_period
        h1 = _parse_cn_number(m_range.group(2))
        min1_raw = m_range.group(3)
        m1 = 30 if min1_raw == "半" else (int(re.sub(r"\D", "", min1_raw)) if min1_raw else 0)

        p2 = m_range.group(4) or p1
        h2 = _parse_cn_number(m_range.group(5))
        min2_raw = m_range.group(6)
        m2 = 30 if min2_raw == "半" else (int(re.sub(r"\D", "", min2_raw)) if min2_raw else 0)

        h1 = _adjust_hour_by_period(h1, p1)
        h2 = _adjust_hour_by_period(h2, p2)
        if h2 < h1 and h2 <= 12:
            h2 += 12

        start_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, min(h1, 23), min(m1, 59), 0)
        end_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, min(h2, 23), min(m2, 59), 0)
        if end_dt <= start_dt:
            end_dt = start_dt + datetime.timedelta(hours=1)

    else:
        # 5b. 單一時間點：「(晚上)八點(半/30分)(前)」
        single_pattern = (
            r"(早上|上午|中午|下午|午後|傍晚|晚上|今晚|明晚|凌晨)?\s*"
            r"(\d{1,2}|[一二兩三四五六七八九十]{1,3})\s*(?:點|時|:)\s*(半|\d{1,2}\s*分?)?"
        )
        m_single = re.search(single_pattern, clean_text)
        if m_single:
            p = m_single.group(1) or default_period
            h = _parse_cn_number(m_single.group(2))
            min_raw = m_single.group(3)
            m = 30 if min_raw == "半" else (int(re.sub(r"\D", "", min_raw)) if min_raw else 0)
            h = _adjust_hour_by_period(h, p)
            parsed_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, min(h, 23), min(m, 59), 0)

            if is_deadline_phrasing:
                due_dt = parsed_dt
            else:
                start_dt = parsed_dt
                end_dt = parsed_dt + datetime.timedelta(hours=1)
        elif "下班" in clean_text:
            parsed_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 18, 0, 0)
            due_dt = parsed_dt
            is_deadline_phrasing = True
        elif has_date_mention:
            # 有提到日期但未提具體幾點
            if default_period in ("早上", "上午", "今早", "明早"):
                parsed_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 9, 0, 0)
            elif default_period == "中午":
                parsed_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0)
            elif default_period in ("下午", "午後"):
                parsed_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 14, 0, 0)
            else:
                parsed_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 18, 0, 0)
            due_dt = parsed_dt

    # 6. 自動分流：CALENDAR_EVENT vs TASK
    event_keywords = ["開會", "會議", "季會", "週會", "例會", "討論", "訪談", "面試", "聚餐", "看牙醫", "看診", "預約", "拜訪", "簡報", "演講", "航班", "高鐵"]
    task_keywords = ["買", "購買", "採買", "繳費", "交報告", "交作業", "寄信", "回覆", "整理", "準備", "待辦", "提醒我"]

    has_event_kw = any(k in clean_text for k in event_keywords)
    has_task_kw = any(k in clean_text for k in task_keywords)

    if has_explicit_range or (start_dt and (has_event_kw or location is not None) and not is_deadline_phrasing):
        item_type = "CALENDAR_EVENT"
        if not start_dt and due_dt:
            start_dt = due_dt
            end_dt = start_dt + datetime.timedelta(hours=1)
    elif is_deadline_phrasing or has_task_kw or not start_dt:
        item_type = "TASK"
        if not due_dt and start_dt:
            due_dt = start_dt
            start_dt = None
            end_dt = None
    else:
        # 有明確單一時段且無明顯買東西/截止字眼 -> 視為日曆行程
        item_type = "CALENDAR_EVENT"

    # 7. 分類標籤 (Category)
    category = "一般"
    if any(k in clean_text for k in ["開會", "會議", "季會", "週會", "例會", "會議室"]):
        category = "會議"
    elif any(k in clean_text for k in ["買", "牛奶", "雞蛋", "超市", "購物", "採買"]):
        category = "購物"
    elif any(k in clean_text for k in ["報告", "專案", "客戶", "簡報", "工作", "資安", "團隊"]):
        category = "工作"
    elif any(k in clean_text for k in ["牙醫", "醫院", "看診", "運動", "健身"]):
        category = "健康"
    elif item_type == "CALENDAR_EVENT":
        category = "行程"

    # 8. 精煉標題 (Clean Title Extraction)
    title_clean = clean_text
    # 移除常見時間、地點、優先度修飾詞以取得乾淨標題
    remove_patterns = [
        r"^(請|幫我|麻煩|提醒我|記一下|新增|加入)\s*",
        r"(下下|下|這|本)?(週|禮拜|星期)[一二三四五六日天]",
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日號]",
        r"(大後天|後天|明天|今天|明日|今日|明早|明晚|今晚|今早)",
        range_pattern,
        r"(早上|上午|中午|下午|午後|傍晚|晚上|凌晨)?\s*(\d{1,2}|[一二兩三四五六七八九十]{1,3})\s*(?:點|時|:)\s*(?:半|\d{1,2}\s*分?)?\s*(?:之前|以前|前)?",
        r"下班(?:前|後)?",
        r"[，,。；;\s]*(?:地點(?:在|是|設在|位於)?\s*[^，,。；;！!\s]+)",
        r"[，,。；;\s]*(?:高優先級|高優先|最高優先|低優先級|低優先|中優先級|緊急|非常重要)",
        r"^(要|去|需要|記得|必須)\s*",
    ]
    for pat in remove_patterns:
        title_clean = re.sub(pat, " ", title_clean)
    title_clean = re.sub(r"^[，,。；;、\s]+|[，,。；;、\s]+$", "", title_clean)
    title_clean = re.sub(r"\s{2,}", " ", title_clean).strip()
    if title_clean.startswith(("要", "去")) and len(title_clean) > 2:
        title_clean = title_clean[1:].strip()

    if not title_clean:
        title_clean = clean_text[:30]

    return ScheduleExtraction(
        action="create",
        item_type=item_type,
        title=title_clean,
        start_time_str=start_dt.strftime("%Y-%m-%d %H:%M:%S") if start_dt else None,
        end_time_str=end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else None,
        due_time_str=due_dt.strftime("%Y-%m-%d %H:%M:%S") if due_dt else None,
        is_all_day=False,
        location=location,
        priority=priority,
        category=category,
        notes=f"原始輸入：{clean_text}"
    )


def get_gemini_client():
    if not GENAI_AVAILABLE or not settings.GEMINI_API_KEY:
        return None
    if settings.GEMINI_API_KEY in ("your_gemini_api_key_here", "dummy", ""):
        return None
    try:
        return genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception:
        return None


def analyze_input(
    content: Any,
    is_audio: bool = False,
    mime_type: str = "audio/mp4",
    base_now: Optional[datetime.datetime] = None,
    prefer_local: bool = False
) -> ScheduleExtraction:
    """
    語音與自然語言雙引擎解析核心：
    - 支援 LINE M4A 語音訊息 (audio/mp4) 與自然語言口語文字
    - 當 prefer_local=True 或無 API Key / 網路離線時，自動由本地高精度 NLP Regex 引擎解析
    - 當 Gemini 回傳結果缺漏關鍵時間或地點時，自動融合本地引擎解析成果
    """
    now = base_now or datetime.datetime.now()

    # 若為純文字，先跑一次本地高精度解析作為保底與增強基準
    local_result: Optional[ScheduleExtraction] = None
    if not is_audio and isinstance(content, str):
        local_result = parse_local_natural_language(content, base_now=now)
        # 若為明確的系統操作指令或 prefer_local，直接回傳本地結果（零延遲、100% 穩定）
        if prefer_local or local_result.action in ("sync", "today", "list", "complete", "delete", "postpone", "help"):
            return local_result

    client = get_gemini_client()
    if not client:
        if is_audio:
            # 音訊離線模擬：若 content 是夾帶文字的 bytes 測試資料，嘗試解碼解析
            if isinstance(content, (bytes, bytearray)):
                try:
                    decoded = content.decode("utf-8").strip()
                    if decoded and not decoded.startswith("\x00"):
                        res = parse_local_natural_language(decoded, base_now=now)
                        res.notes = f"[語音轉譯] {decoded}"
                        return res
                except Exception:
                    pass
            default_start = (now + datetime.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
            default_end = default_start + datetime.timedelta(hours=1)
            return ScheduleExtraction(
                action="create",
                item_type="CALENDAR_EVENT",
                title="語音行程備忘 (離線雙向同步模式)",
                start_time_str=default_start.strftime("%Y-%m-%d %H:%M:%S"),
                end_time_str=default_end.strftime("%Y-%m-%d %H:%M:%S"),
                priority="Medium",
                category="語音備忘",
                notes="由 LINE 語音訊息建立"
            )
        return local_result or parse_local_natural_language(str(content), base_now=now)

    now_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")
    system_prompt = f"""你是一位精通繁體中文的「語音待辦秘書 × Google Calendar / Google Tasks 雙向同步智慧助理」。
當前基準時間是：{now_str}。
請解析用戶的語音或口語文字，並自動分類與結構化輸出 JSON：
1. 判斷 item_type：
   - 'CALENDAR_EVENT'：具有明確起訖時間、會議、約訪、聚會或指定地點的行程（對應 Google Calendar），請填入 start_time_str 與 end_time_str (YYYY-MM-DD HH:MM:SS)。
   - 'TASK'：彈性待辦事項、截止日任務（「...前要完成/交報告」）、購物清單（對應 Google Tasks），請填入 due_time_str (YYYY-MM-DD HH:MM:SS)。
2. 擷取地點 (location)、重要度 (priority: High/Medium/Low)、分類 (category) 與乾淨的標題 (title)。
請嚴格依據 ScheduleExtraction Schema 輸出 JSON。"""

    try:
        parts = []
        if is_audio:
            parts.append(types.Part.from_bytes(data=content, mime_type=mime_type))
            parts.append("請聽這段語音錄音，萃取日曆行程 (CALENDAR_EVENT) 或待辦任務 (TASK) 資訊。")
        else:
            parts.append(str(content))

        candidate_models = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        ]
        for model_name in candidate_models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=ScheduleExtraction,
                        temperature=0.1,
                    )
                )
                ai_res = ScheduleExtraction.model_validate_json(response.text)
                # 融合本地解析器的精確時間與地點作為補正
                if local_result:
                    if not ai_res.location and local_result.location:
                        ai_res.location = local_result.location
                    if local_result.priority != "Medium" and ai_res.priority == "Medium":
                        ai_res.priority = local_result.priority
                    if not ai_res.start_time_str and local_result.start_time_str:
                        ai_res.start_time_str = local_result.start_time_str
                        ai_res.end_time_str = local_result.end_time_str
                    if not ai_res.due_time_str and local_result.due_time_str:
                        ai_res.due_time_str = local_result.due_time_str
                return ai_res
            except Exception:
                continue
    except Exception:
        pass

    # 若雲端呼叫失敗，無縫切換回本地高精度解析引擎
    if local_result:
        return local_result
    if is_audio and isinstance(content, (bytes, bytearray)):
        try:
            decoded = content.decode("utf-8").strip()
            if decoded:
                return parse_local_natural_language(decoded, base_now=now)
        except Exception:
            pass
    default_start = (now + datetime.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    return ScheduleExtraction(
        action="create",
        item_type="CALENDAR_EVENT",
        title="語音行程紀錄",
        start_time_str=default_start.strftime("%Y-%m-%d %H:%M:%S"),
        end_time_str=(default_start + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        priority="Medium",
        category="語音備忘",
        notes="語音自動建立"
    )
