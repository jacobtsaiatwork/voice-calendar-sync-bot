/**
 * 🎙️ 語音待辦秘書 × Google Calendar / Tasks 雙向同步 LINE 智慧助理
 * Google Apps Script 100% 雲端全託管版 (24H 電腦關機免開機運作)
 */

const CONFIG = {
  DEFAULT_GEMINI_KEY: "",
  LINE_MESSAGING_API: "https://api.line.me/v2/bot/message/reply",
  LINE_PUSH_API: "https://api.line.me/v2/bot/message/push",
  LINE_CONTENT_API: "https://api-data.line.me/v2/bot/message"
};

function getProperty(key, fallback = "") {
  return PropertiesService.getScriptProperties().getProperty(key) || fallback;
}

function setProperty(key, value) {
  PropertiesService.getScriptProperties().setProperty(key, value);
}

function getLineToken() {
  return getProperty("LINE_CHANNEL_ACCESS_TOKEN", "");
}

function getGeminiKey() {
  return getProperty("GEMINI_API_KEY", CONFIG.DEFAULT_GEMINI_KEY);
}

// ==========================================
// 1. Webhook 核心：處理 LINE 訊息 (doPost)
// ==========================================
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput("OK");
    }
    const data = JSON.parse(e.postData.contents);
    const events = data.events || [];

    for (const event of events) {
      const replyToken = event.replyToken;
      const userId = event.source ? event.source.userId : "";
      if (userId && !getProperty("ADMIN_USER_ID")) {
        setProperty("ADMIN_USER_ID", userId);
      }

      if (event.type === "message") {
        handleMessageEvent(event, replyToken, userId);
      } else if (event.type === "postback") {
        handlePostbackEvent(event, replyToken, userId);
      }
    }
    return ContentService.createTextOutput(JSON.stringify({ status: "success" })).setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    console.error("doPost Error: " + err.toString());
    return ContentService.createTextOutput("Error: " + err.toString());
  }
}

// 處理訊息事件 (文字 / 語音)
function handleMessageEvent(event, replyToken, userId) {
  const msg = event.message;
  let userText = "";

  if (msg.type === "text") {
    userText = msg.text.trim();
  } else if (msg.type === "audio") {
    userText = transcribeAudioWithGemini(msg.id);
    if (!userText) {
      replyTextMessage(replyToken, "🎙️ 收到語音訊息，但語音辨識服務暫時無法處理，請重試或以文字輸入。");
      return;
    }
  } else {
    return;
  }

  // 指令比對
  if (userText === "今日日程" || userText === "今日行程" || userText.toLowerCase() === "today") {
    sendTodayBrief(replyToken);
    return;
  }
  if (userText === "本週行程" || userText === "本週日程" || userText.toLowerCase() === "week") {
    sendWeekBrief(replyToken);
    return;
  }
  if (userText === "待辦清單" || userText === "我的待辦" || userText === "tasks") {
    sendTasksList(replyToken);
    return;
  }
  if (userText === "雙向同步" || userText === "同步" || userText.toLowerCase() === "sync") {
    sendSyncStatus(replyToken);
    return;
  }

  // 自然語言分析：呼叫 Gemini 或 本地正則引擎
  const parsedItem = parseIntentWithGeminiOrLocal(userText);
  if (!parsedItem || !parsedItem.title) {
    replyTextMessage(replyToken, "抱歉，未能完全理解您的日程或待辦，您可以這樣說：\n「下週三下午兩點到四點開季會」或「明天晚上八點前交報告」");
    return;
  }

  if (parsedItem.type === "CALENDAR_EVENT") {
    handleCreateCalendarEvent(parsedItem, replyToken);
  } else {
    handleCreateTask(parsedItem, replyToken);
  }
}

// 處理 Postback 按鈕事件
function handlePostbackEvent(event, replyToken, userId) {
  const postbackData = event.postback.data || "";
  const params = parseQueryString(postbackData);
  const action = params.action;

  if (action === "complete_task") {
    const taskId = params.id;
    updateTaskStatus(taskId, "DONE");
    replyTextMessage(replyToken, "✅ 待辦事項已標記為【已完成】！");
  } else if (action === "delete_task") {
    const taskId = params.id;
    deleteTask(taskId);
    replyTextMessage(replyToken, "🗑️ 待辦事項已從清單中移除。");
  } else if (action === "delete_event") {
    const eventId = params.id;
    const deleted = deleteCalendarEventById(eventId);
    replyTextMessage(replyToken, deleted ? "🗑️ 行事曆行程已成功刪除。" : "找不到該行程或已被刪除。");
  } else if (action === "delay_1h") {
    const eventId = params.id;
    const res = delayCalendarEvent(eventId, 60);
    replyTextMessage(replyToken, res ? "⏰ 已成功將行程順延 1 小時！" : "順延失敗或找不到行程。");
  } else if (action === "auto_reschedule") {
    const eventId = params.id;
    const res = autoRescheduleEvent(eventId);
    if (res) {
      replyTextMessage(replyToken, `⏩ 已自動為您避開衝突，順延至空檔：\n📅 ${formatDateTime(res.start)} ~ ${formatTime(res.end)}`);
    } else {
      replyTextMessage(replyToken, "未能找到適當的後續空檔，請手動調整。");
    }
  }
}

// ==========================================
// 2. Google Calendar 原生整合與衝突偵測
// ==========================================
function handleCreateCalendarEvent(item, replyToken) {
  const cal = CalendarApp.getDefaultCalendar();
  const startTime = new Date(item.startTime);
  const endTime = new Date(item.endTime || (startTime.getTime() + 60 * 60 * 1000));

  // 衝突檢查 (Conflict Detection)
  const conflicts = cal.getEvents(startTime, endTime);
  const hasConflict = conflicts.length > 0;

  // 建立日曆活動
  const options = {
    description: (item.notes || "") + "\n\n[由 LINE 語音待辦秘書建立]",
    location: item.location || ""
  };
  const newEvent = cal.createEvent(item.title, startTime, endTime, options);
  const eventId = newEvent.getId();

  // 取得衝突名稱
  let conflictSummary = "";
  if (hasConflict) {
    conflictSummary = conflicts.map(c => `• ${c.getTitle()} (${formatTime(c.getStartTime())}-${formatTime(c.getEndTime())})`).join("\n");
  }

  // 產生 Flex Message 回傳
  const flex = buildEventFlexCard({
    id: eventId,
    title: item.title,
    start: startTime,
    end: endTime,
    location: item.location,
    notes: item.notes,
    hasConflict: hasConflict,
    conflictSummary: conflictSummary
  });

  replyFlexMessage(replyToken, `📅 行程已新增：${item.title}`, flex);
}

function autoRescheduleEvent(eventId) {
  const cal = CalendarApp.getDefaultCalendar();
  const ev = cal.getEventById(eventId);
  if (!ev) return null;

  const durationMs = ev.getEndTime().getTime() - ev.getStartTime().getTime();
  let candidateStart = new Date(ev.getEndTime().getTime());

  // 尋找未來 24 小時內的下一個空檔
  for (let i = 0; i < 48; i++) {
    const candidateEnd = new Date(candidateStart.getTime() + durationMs);
    const conflicts = cal.getEvents(candidateStart, candidateEnd);
    if (conflicts.length === 0) {
      ev.setTime(candidateStart, candidateEnd);
      return { start: candidateStart, end: candidateEnd };
    }
    candidateStart = new Date(candidateStart.getTime() + 30 * 60 * 1000); // 順延 30 分鐘再測
  }
  return null;
}

function delayCalendarEvent(eventId, minutes) {
  const cal = CalendarApp.getDefaultCalendar();
  const ev = cal.getEventById(eventId);
  if (!ev) return false;
  const newStart = new Date(ev.getStartTime().getTime() + minutes * 60 * 1000);
  const newEnd = new Date(ev.getEndTime().getTime() + minutes * 60 * 1000);
  ev.setTime(newStart, newEnd);
  return true;
}

function deleteCalendarEventById(eventId) {
  try {
    const cal = CalendarApp.getDefaultCalendar();
    const ev = cal.getEventById(eventId);
    if (ev) {
      ev.deleteEvent();
      return true;
    }
  } catch (e) {}
  return false;
}

// ==========================================
// 3. 雲端待辦事項清單管理 (ScriptProperties)
// ==========================================
function getStoredTasks() {
  const raw = getProperty("TASKS_JSON", "[]");
  try {
    return JSON.parse(raw);
  } catch (e) {
    return [];
  }
}

function saveStoredTasks(tasks) {
  setProperty("TASKS_JSON", JSON.stringify(tasks));
}

function handleCreateTask(item, replyToken) {
  const tasks = getStoredTasks();
  const taskId = "task_" + new Date().getTime();
  const newTask = {
    id: taskId,
    title: item.title,
    dueDate: item.dueDate || "",
    priority: item.priority || "Normal",
    status: "TODO",
    notes: item.notes || "",
    createdAt: new Date().toISOString()
  };
  tasks.push(newTask);
  saveStoredTasks(tasks);

  const flex = buildTaskFlexCard(newTask);
  replyFlexMessage(replyToken, `✅ 待辦事項已記錄：${item.title}`, flex);
}

function updateTaskStatus(taskId, status) {
  const tasks = getStoredTasks();
  for (const t of tasks) {
    if (t.id === taskId) {
      t.status = status;
      break;
    }
  }
  saveStoredTasks(tasks);
}

function deleteTask(taskId) {
  let tasks = getStoredTasks();
  tasks = tasks.filter(t => t.id !== taskId);
  saveStoredTasks(tasks);
}

// ==========================================
// 4. 今日與本週日程總覽推播
// ==========================================
function sendTodayBrief(replyToken) {
  const cal = CalendarApp.getDefaultCalendar();
  const today = new Date();
  const events = cal.getEventsForDay(today);
  const tasks = getStoredTasks().filter(t => t.status === "TODO");

  const flex = buildScheduleOverviewFlex("☀️ 今日日程總覽", today, events, tasks);
  replyFlexMessage(replyToken, "☀️ 今日日程總覽", flex);
}

function sendWeekBrief(replyToken) {
  const cal = CalendarApp.getDefaultCalendar();
  const today = new Date();
  const nextWeek = new Date(today.getTime() + 7 * 24 * 60 * 60 * 1000);
  const events = cal.getEvents(today, nextWeek);
  const tasks = getStoredTasks().filter(t => t.status === "TODO");

  const flex = buildScheduleOverviewFlex("📅 未來 7 天行程總覽", today, events, tasks);
  replyFlexMessage(replyToken, "📅 未來 7 天行程總覽", flex);
}

function sendTasksList(replyToken) {
  const tasks = getStoredTasks().filter(t => t.status === "TODO");
  if (tasks.length === 0) {
    replyTextMessage(replyToken, "🎉 目前沒有未完成的待辦任務！您可以直接用語音說「明天中午前交簡報」來新增。");
    return;
  }
  const flex = buildAllTasksFlex(tasks);
  replyFlexMessage(replyToken, "📋 未完成待辦清單", flex);
}

function sendSyncStatus(replyToken) {
  const cal = CalendarApp.getDefaultCalendar();
  const today = new Date();
  const eventsCount = cal.getEventsForDay(today).length;
  const pendingTasksCount = getStoredTasks().filter(t => t.status === "TODO").length;

  replyTextMessage(replyToken, `🔄 雙向同步狀態正常！\n\n• 雲端日曆：${cal.getName()}\n• 今日日曆活動：${eventsCount} 項\n• 待辦清單：${pendingTasksCount} 項未完成\n• 同步模式：Google Apps Script 原生 24H 雙向直連`);
}

// ==========================================
// 5. Gemini 語音辨識與自然語言意圖解析
// ==========================================
function transcribeAudioWithGemini(messageId) {
  const token = getLineToken();
  const geminiKey = getGeminiKey();
  if (!token || !geminiKey) return "";

  try {
    const audioUrl = `${CONFIG.LINE_CONTENT_API}/${messageId}/content`;
    const resp = UrlFetchApp.fetch(audioUrl, {
      headers: { Authorization: `Bearer ${token}` }
    });
    const blob = resp.getBlob();
    const base64Audio = Utilities.base64Encode(blob.getBytes());

    const geminiUrl = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${geminiKey}`;
    const payload = {
      contents: [{
        parts: [
          { text: "請將這段語音轉譯為繁體中文文字。只需輸出轉譯後的字句，不要有任何多餘解釋。" },
          { inlineData: { mimeType: "audio/mp4", data: base64Audio } }
        ]
      }]
    };

    const res = UrlFetchApp.fetch(geminiUrl, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    const json = JSON.parse(res.getContentText());
    if (json.candidates && json.candidates[0] && json.candidates[0].content && json.candidates[0].content.parts) {
      return json.candidates[0].content.parts[0].text.trim();
    }
  } catch (err) {
    console.error("Gemini Transcribe Error: " + err);
  }
  return "";
}

function parseIntentWithGeminiOrLocal(text) {
  const geminiKey = getGeminiKey();
  const now = new Date();
  const nowStr = Utilities.formatDate(now, "Asia/Taipei", "yyyy-MM-dd'T'HH:mm:ss");

  if (geminiKey) {
    try {
      const prompt = `現在時間是 ${nowStr} (Asia/Taipei)。
請分析使用者的自然語言輸入：「${text}」，並以 JSON 格式輸出：
{
  "type": "CALENDAR_EVENT" 或 "TASK",
  "title": "事件或待辦標題",
  "startTime": "YYYY-MM-DDTHH:mm:ss", // 若為 CALENDAR_EVENT
  "endTime": "YYYY-MM-DDTHH:mm:ss",   // 若有結束時間
  "dueDate": "YYYY-MM-DDTHH:mm:ss",   // 若為 TASK 且有截止日期
  "location": "地點（若有）",
  "priority": "High" | "Medium" | "Normal",
  "notes": "備註內容"
}
只需輸出合法 JSON，不要有 Markdown code block。`;

      const geminiUrl = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${geminiKey}`;
      const res = UrlFetchApp.fetch(geminiUrl, {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
        muteHttpExceptions: true
      });
      const resJson = JSON.parse(res.getContentText());
      if (resJson.candidates && resJson.candidates[0]) {
        let content = resJson.candidates[0].content.parts[0].text.trim();
        content = content.replace(/^```json/i, "").replace(/```$/i, "").trim();
        return JSON.parse(content);
      }
    } catch (e) {
      console.warn("Gemini parse failed, fallback to local regex: " + e);
    }
  }

  // 本地正則 Fallback
  return parseLocalNaturalLanguage(text, now);
}

function parseLocalNaturalLanguage(text, now) {
  const isTask = /買|記得|繳|待辦|任務|準備|交/i.test(text);
  const targetDate = new Date(now.getTime());

  if (text.includes("明天")) {
    targetDate.setDate(targetDate.getDate() + 1);
  } else if (text.includes("後天")) {
    targetDate.setDate(targetDate.getDate() + 2);
  }

  let hour = 14;
  let minute = 0;
  const timeMatch = text.match(/(早上|上午|下午|晚上)?\s*([0-9]{1,2})點([0-9]{1,2})?分?/);
  if (timeMatch) {
    let h = parseInt(timeMatch[2]);
    const period = timeMatch[1] || "";
    if ((period === "下午" || period === "晚上") && h < 12) h += 12;
    hour = h;
    if (timeMatch[3]) minute = parseInt(timeMatch[3]);
  }
  targetDate.setHours(hour, minute, 0, 0);

  const end = new Date(targetDate.getTime() + 60 * 60 * 1000);

  if (isTask) {
    return {
      type: "TASK",
      title: text,
      dueDate: Utilities.formatDate(targetDate, "Asia/Taipei", "yyyy-MM-dd'T'HH:mm:ss"),
      priority: text.includes("急") || text.includes("重要") ? "High" : "Normal"
    };
  } else {
    return {
      type: "CALENDAR_EVENT",
      title: text,
      startTime: Utilities.formatDate(targetDate, "Asia/Taipei", "yyyy-MM-dd'T'HH:mm:ss"),
      endTime: Utilities.formatDate(end, "Asia/Taipei", "yyyy-MM-dd'T'HH:mm:ss"),
      location: ""
    };
  }
}

// ==========================================
// 6. LINE Flex Message 視覺模板
// ==========================================
function buildEventFlexCard(item) {
  const startStr = formatDateTime(item.start);
  const endStr = formatTime(item.end);

  const headerColor = item.hasConflict ? "#D32F2F" : "#1B5E20";
  const statusTitle = item.hasConflict ? "⚠️ 已排入但偵測到時段衝突！" : "✅ 成功加入 Google 行事曆";

  const contents = [
    { type: "text", text: statusTitle, weight: "bold", color: "#FFFFFF", size: "sm" },
    { type: "text", text: item.title, weight: "bold", color: "#FFFFFF", size: "xl", margin: "md" }
  ];

  const bodyContents = [
    {
      type: "box", layout: "vertical", spacing: "sm",
      contents: [
        { type: "text", text: `🕒 時間：${startStr} ~ ${endStr}`, size: "sm", color: "#333333" },
        item.location ? { type: "text", text: `📍 地點：${item.location}`, size: "sm", color: "#666666" } : null,
        item.hasConflict ? {
          type: "box", layout: "vertical", margin: "md", backgroundColor: "#FFEBEE", paddingAll: "sm", cornerRadius: "md",
          contents: [
            { type: "text", text: "⚠️ 衝突行程：", size: "xs", weight: "bold", color: "#C62828" },
            { type: "text", text: item.conflictSummary, size: "xs", color: "#C62828", wrap: true }
          ]
        } : null
      ].filter(Boolean)
    }
  ];

  const footerContents = [
    item.hasConflict ? {
      type: "button", style: "primary", color: "#D32F2F", height: "sm",
      action: { type: "postback", label: "⏩ 自動順延至下個空檔", data: `action=auto_reschedule&id=${item.id}` }
    } : null,
    {
      type: "button", style: "secondary", height: "sm", margin: "sm",
      action: { type: "postback", label: "⏰ 順延 1 小時", data: `action=delay_1h&id=${item.id}` }
    },
    {
      type: "button", style: "link", height: "sm",
      action: { type: "postback", label: "🗑️ 刪除此行程", data: `action=delete_event&id=${item.id}` }
    }
  ].filter(Boolean);

  return {
    type: "bubble",
    header: { type: "box", layout: "vertical", backgroundColor: headerColor, contents: contents },
    body: { type: "box", layout: "vertical", contents: bodyContents },
    footer: { type: "box", layout: "vertical", contents: footerContents }
  };
}

function buildTaskFlexCard(task) {
  return {
    type: "bubble",
    header: {
      type: "box", layout: "vertical", backgroundColor: "#0288D1",
      contents: [
        { type: "text", text: "📌 已收錄至待辦清單", weight: "bold", color: "#FFFFFF", size: "sm" },
        { type: "text", text: task.title, weight: "bold", color: "#FFFFFF", size: "lg", margin: "md" }
      ]
    },
    body: {
      type: "box", layout: "vertical", spacing: "sm",
      contents: [
        task.dueDate ? { type: "text", text: `⏰ 截止時間：${task.dueDate.replace("T", " ")}`, size: "sm", color: "#555555" } : null,
        { type: "text", text: `⚡ 優先級：${task.priority === "High" ? "🔥 高" : "普通"}`, size: "sm", color: "#555555" }
      ].filter(Boolean)
    },
    footer: {
      type: "box", layout: "horizontal", spacing: "sm",
      contents: [
        {
          type: "button", style: "primary", color: "#00C300", height: "sm",
          action: { type: "postback", label: "✅ 完成", data: `action=complete_task&id=${task.id}` }
        },
        {
          type: "button", style: "secondary", height: "sm",
          action: { type: "postback", label: "🗑️ 刪除", data: `action=delete_task&id=${task.id}` }
        }
      ]
    }
  };
}

function buildScheduleOverviewFlex(title, date, events, tasks) {
  const eventBoxes = events.map(ev => ({
    type: "box", layout: "vertical", margin: "sm", paddingAll: "sm", backgroundColor: "#F5F5F5", cornerRadius: "md",
    contents: [
      { type: "text", text: `${formatTime(ev.getStartTime())} - ${formatTime(ev.getEndTime())}`, size: "xs", color: "#1976D2", weight: "bold" },
      { type: "text", text: ev.getTitle(), size: "sm", weight: "bold", color: "#222222" },
      ev.getLocation() ? { type: "text", text: `📍 ${ev.getLocation()}`, size: "xs", color: "#777777" } : null
    ].filter(Boolean)
  }));

  const taskBoxes = tasks.slice(0, 5).map(t => ({
    type: "box", layout: "horizontal", margin: "xs",
    contents: [
      { type: "text", text: "•", size: "sm", flex: 1, color: "#E65100" },
      { type: "text", text: t.title, size: "sm", flex: 9, color: "#333333" }
    ]
  }));

  return {
    type: "bubble",
    header: {
      type: "box", layout: "vertical", backgroundColor: "#37474F",
      contents: [
        { type: "text", text: title, weight: "bold", color: "#FFFFFF", size: "lg" },
        { type: "text", text: formatDate(date), size: "xs", color: "#B0BEC5" }
      ]
    },
    body: {
      type: "box", layout: "vertical", spacing: "md",
      contents: [
        { type: "text", text: `📅 今日 Google 行程 (${events.length})`, weight: "bold", size: "sm", color: "#1976D2" },
        events.length > 0 ? { type: "box", layout: "vertical", contents: eventBoxes } : { type: "text", text: "今日無行事曆行程", size: "xs", color: "#999999" },
        { type: "separator", margin: "md" },
        { type: "text", text: `📋 待辦任務 (${tasks.length})`, weight: "bold", size: "sm", color: "#E65100", margin: "md" },
        tasks.length > 0 ? { type: "box", layout: "vertical", contents: taskBoxes } : { type: "text", text: "目前無未完成待辦", size: "xs", color: "#999999" }
      ]
    }
  };
}

function buildAllTasksFlex(tasks) {
  const items = tasks.slice(0, 10).map(t => ({
    type: "box", layout: "horizontal", margin: "sm", alignItems: "center",
    contents: [
      { type: "text", text: t.priority === "High" ? "🔥" : "📌", size: "sm", flex: 1 },
      { type: "text", text: t.title, size: "sm", flex: 7, wrap: true },
      {
        type: "button", style: "primary", color: "#00C300", height: "sm", flex: 3,
        action: { type: "postback", label: "完成", data: `action=complete_task&id=${t.id}` }
      }
    ]
  }));

  return {
    type: "bubble",
    header: {
      type: "box", layout: "vertical", backgroundColor: "#0288D1",
      contents: [{ type: "text", text: `📋 未完成待辦清單 (${tasks.length})`, weight: "bold", color: "#FFFFFF", size: "md" }]
    },
    body: { type: "box", layout: "vertical", contents: items }
  };
}

// ==========================================
// 7. LINE API 呼叫工具
// ==========================================
function replyTextMessage(replyToken, text) {
  sendLineReply(replyToken, [{ type: "text", text: text }]);
}

function replyFlexMessage(replyToken, altText, flexBubble) {
  sendLineReply(replyToken, [{
    type: "flex",
    altText: altText,
    contents: flexBubble
  }]);
}

function sendLineReply(replyToken, messages) {
  const token = getLineToken();
  if (!token) return;
  UrlFetchApp.fetch(CONFIG.LINE_MESSAGING_API, {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: `Bearer ${token}` },
    payload: JSON.stringify({ replyToken: replyToken, messages: messages }),
    muteHttpExceptions: true
  });
}

function pushLineMessage(userId, messages) {
  const token = getLineToken();
  if (!token || !userId) return;
  UrlFetchApp.fetch(CONFIG.LINE_PUSH_API, {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: `Bearer ${token}` },
    payload: JSON.stringify({ to: userId, messages: messages }),
    muteHttpExceptions: true
  });
}

// ==========================================
// 8. 雲端定時觸發器 (Cloud Triggers)
// ==========================================
function setupCloudTriggers() {
  // 清除舊觸發器
  const triggers = ScriptApp.getProjectTriggers();
  for (const t of triggers) {
    ScriptApp.deleteTrigger(t);
  }

  // 1. 每日早上 08:00 自動推播晨間日程
  ScriptApp.newTrigger("dailyMorningBrief")
    .timeBased()
    .everyDays(1)
    .atHour(8)
    .create();

  // 2. 每 15 分鐘檢查即將開始的會議提醒
  ScriptApp.newTrigger("checkUpcomingReminders")
    .timeBased()
    .everyMinutes(15)
    .create();

  return "✅ 雲端定時觸發器已設定完成！（每日 08:00 晨報 + 每 15 分鐘會議提醒）";
}

function dailyMorningBrief() {
  const adminId = getProperty("ADMIN_USER_ID");
  if (!adminId) return;

  const cal = CalendarApp.getDefaultCalendar();
  const today = new Date();
  const events = cal.getEventsForDay(today);
  const tasks = getStoredTasks().filter(t => t.status === "TODO");

  const flex = buildScheduleOverviewFlex("☀️ 早安！今日日程總覽", today, events, tasks);
  pushLineMessage(adminId, [{ type: "flex", altText: "☀️ 早安！今日日程總覽", contents: flex }]);
}

function checkUpcomingReminders() {
  const adminId = getProperty("ADMIN_USER_ID");
  if (!adminId) return;

  const cal = CalendarApp.getDefaultCalendar();
  const now = new Date();
  const in30Min = new Date(now.getTime() + 30 * 60 * 1000);
  const events = cal.getEvents(now, in30Min);

  for (const ev of events) {
    const key = "reminded_" + ev.getId();
    if (!getProperty(key)) {
      setProperty(key, "true");
      const msg = `⏰【行前 30 分鐘提醒】\n\n📌 ${ev.getTitle()}\n🕒 時間：${formatTime(ev.getStartTime())} ~ ${formatTime(ev.getEndTime())}\n${ev.getLocation() ? '📍 地點：' + ev.getLocation() : ''}`;
      pushLineMessage(adminId, [{ type: "text", text: msg }]);
    }
  }
}

// ==========================================
// 9. 雲端 Web 管理面板 (doGet)
// ==========================================
function doGet(e) {
  const action = e && e.parameter ? e.parameter.action : "";

  if (action === "setupTriggers") {
    const msg = setupCloudTriggers();
    return ContentService.createTextOutput(msg);
  }

  if (action === "saveConfig") {
    if (e.parameter.lineToken) setProperty("LINE_CHANNEL_ACCESS_TOKEN", e.parameter.lineToken);
    if (e.parameter.geminiKey) setProperty("GEMINI_API_KEY", e.parameter.geminiKey);
    return ContentService.createTextOutput("✅ 設定已儲存至 Google 雲端！");
  }

  return HtmlService.createHtmlOutputFromFile("index")
    .setTitle("語音日曆同步秘書 - 雲端中控台")
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// ==========================================
// 10. 輔助函式
// ==========================================
function formatDateTime(d) {
  return Utilities.formatDate(d, "Asia/Taipei", "MM/dd HH:mm");
}
function formatDate(d) {
  return Utilities.formatDate(d, "Asia/Taipei", "yyyy/MM/dd (E)");
}
function formatTime(d) {
  return Utilities.formatDate(d, "Asia/Taipei", "HH:mm");
}
function parseQueryString(query) {
  const res = {};
  const parts = query.split("&");
  for (const p of parts) {
    const pair = p.split("=");
    if (pair.length === 2) res[pair[0]] = decodeURIComponent(pair[1]);
  }
  return res;
}
