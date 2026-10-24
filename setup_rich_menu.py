import io
import requests
from PIL import Image, ImageDraw, ImageFont
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    RichMenuRequest,
    RichMenuSize,
    RichMenuArea,
    RichMenuBounds,
    MessageAction,
    PostbackAction,
)
from config import settings


def build_rich_menu_request() -> RichMenuRequest:
    """
    建立雙向同步專屬的三宮格 Rich Menu 設定：
    [ ☀️ 今日日程總覽 ]  [ 🔄 雙向同步 Google ]  [ 📋 全部待辦與秘訣 ]
    """
    return RichMenuRequest(
        size=RichMenuSize(width=2500, height=843),
        selected=True,
        name="VoiceCalendarSyncMenu",
        chat_bar_text="開啟日曆×待辦同步選單 📅",
        areas=[
            RichMenuArea(
                bounds=RichMenuBounds(x=0, y=0, width=833, height=843),
                action=MessageAction(text="今日行程"),
            ),
            RichMenuArea(
                bounds=RichMenuBounds(x=833, y=0, width=834, height=843),
                action=PostbackAction(
                    label="雙向同步 Google",
                    data="action=sync_google",
                    display_text="立即雙向同步 Google Calendar & Tasks",
                ),
            ),
            RichMenuArea(
                bounds=RichMenuBounds(x=1667, y=0, width=833, height=843),
                action=MessageAction(text="查看待辦清單"),
            ),
        ],
    )


def generate_rich_menu_image_bytes() -> bytes:
    img = Image.new("RGB", (2500, 843), color="#0F172A")
    draw = ImageDraw.Draw(img)

    draw.rectangle([20, 20, 813, 823], fill="#1E293B", outline="#3B82F6", width=5)
    draw.rectangle([853, 20, 1647, 823], fill="#1E293B", outline="#10B981", width=5)
    draw.rectangle([1687, 20, 2480, 823], fill="#1E293B", outline="#8B5CF6", width=5)

    try:
        font_title = ImageFont.truetype("msjh.ttc", 72)
        font_sub = ImageFont.truetype("msjh.ttc", 42)
    except Exception:
        font_title = font_sub = ImageFont.load_default()

    draw.text((416, 340), "☀️ 今日日程", fill="#60A5FA", font=font_title, anchor="mm")
    draw.text((416, 465), "Calendar + Tasks 總覽", fill="#CBD5E1", font=font_sub, anchor="mm")

    draw.text((1250, 340), "🔄 雙向同步", fill="#34D399", font=font_title, anchor="mm")
    draw.text((1250, 465), "即時同步 Google 雲端", fill="#CBD5E1", font=font_sub, anchor="mm")

    draw.text((2083, 340), "📋 全部清單", fill="#C084FC", font=font_title, anchor="mm")
    draw.text((2083, 465), "瀏覽與管理待辦事項", fill="#CBD5E1", font=font_sub, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def setup_rich_menu():
    token = settings.LINE_CHANNEL_ACCESS_TOKEN
    if not token:
        print("未設定 LINE_CHANNEL_ACCESS_TOKEN")
        return None

    config = Configuration(access_token=token)
    with ApiClient(config) as api_client:
        messaging_api = MessagingApi(api_client)
        req = build_rich_menu_request()
        res = messaging_api.create_rich_menu(req)
        rich_menu_id = res.rich_menu_id
        print(f"Rich Menu 建立成功 ID: {rich_menu_id}")

        img_bytes = generate_rich_menu_image_bytes()
        upload_url = f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content"
        upload_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "image/jpeg",
        }
        upload_res = requests.post(upload_url, headers=upload_headers, data=img_bytes, timeout=15)
        print("圖片上傳狀態:", upload_res.status_code)

        messaging_api.set_default_rich_menu(rich_menu_id)
        print("LINE Rich Menu 綁定成功！")
        return rich_menu_id


if __name__ == "__main__":
    setup_rich_menu()
