"""
check_tickets.py
---------------------------------------
定期檢查快達票(hkticketing)頁面是否有票可購買,
一旦狀態從「售罄/不明」變成「有票」,就用 Telegram 通知。

因為目標頁面是 JS 單頁應用(SPA),直接用 requests 抓不到內容,
所以這裡用 Playwright 啟動一個無頭瀏覽器把頁面「真的打開」再讀取文字。
"""

import os
import json
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

TICKET_URL = os.environ.get(
    "TICKET_URL",
    "https://hkt.hkticketing.com/hant/#/allEvents/detail/selectTicket"
    "?activityId=50000001563023&privilegeCodePrifixState=true",
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "status.txt"
SCREENSHOT_FILE = "latest_screenshot.png"
NETWORK_LOG_FILE = "network_log.json"

# ---------------------------------------------------------------
# 關鍵字設定 —— 這是「猜測」出來的常見字眼,第一次跑完之後
# 請對照 latest_screenshot.png 實際內容,依需要修改下面兩份清單。
# ---------------------------------------------------------------
SOLD_OUT_KEYWORDS = [
    "售罄", "已售完", "SOLD OUT", "Sold Out", "尚未開賣", "尚未開始",
    "敬請留意", "貨源已被搶購一空", "暫時沒有", "沒有可供選購",
    "No tickets available", "座位已滿",
]
AVAILABLE_KEYWORDS = [
    "選擇座位", "選擇票區", "加入購物車", "立即購買", "Buy Now",
    "選擇區域", "剩餘", "可選座位",
]


def send_telegram(text, photo_path=None):
    """用 Telegram Bot API 發送文字(附截圖更好判斷)。"""
    import urllib.request

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,略過通知")
        return

    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    if photo_path and os.path.exists(photo_path):
        boundary = "----ticketwatcherboundary"
        with open(photo_path, "rb") as f:
            photo_data = f.read()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{text}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="shot.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + photo_data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            f"{base}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    else:
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(f"{base}/sendMessage", data=data)

    try:
        urllib.request.urlopen(req, timeout=15)
        print("Telegram 通知已送出")
    except Exception as e:
        print(f"Telegram 發送失敗: {e}")


def load_last_state():
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE, encoding="utf-8").read().strip()
    return "unknown"


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(state)


def main():
    captured_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-HK",
        )
        page = context.new_page()

        # 順便把頁面在背後打的 API JSON 記錄下來,方便之後直接打 API
        # (不用再開瀏覽器),這樣監控可以更快更省資源。
        def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct and response.request.resource_type in ("xhr", "fetch"):
                    url = response.url
                    if any(k in url.lower() for k in
                           ["ticket", "activity", "stock", "seat", "event", "product"]):
                        try:
                            body = response.json()
                        except Exception:
                            body = None
                        captured_responses.append({"url": url, "body": body})
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(TICKET_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)  # 給 SPA 多一點時間把資料渲染出來

        page.screenshot(path=SCREENSHOT_FILE, full_page=True)
        body_text = page.inner_text("body")

        with open(NETWORK_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(captured_responses, f, ensure_ascii=False, indent=2)

        browser.close()

    lower_text = body_text.lower()
    is_sold_out = any(k.lower() in lower_text for k in SOLD_OUT_KEYWORDS)
    has_buy_signal = any(k.lower() in lower_text for k in AVAILABLE_KEYWORDS)

    if has_buy_signal and not is_sold_out:
        current_state = "available"
    elif is_sold_out and not has_buy_signal:
        current_state = "sold_out"
    else:
        current_state = "unclear"

    last_state = load_last_state()
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{now}] 上次狀態: {last_state} -> 這次狀態: {current_state}")

    if current_state == "available" and last_state != "available":
        send_telegram(
            f"🎫 快達票可能有票了!\n狀態: {current_state}\n時間: {now}\n{TICKET_URL}",
            photo_path=SCREENSHOT_FILE,
        )
    elif current_state == "unclear" and last_state == "unknown":
        # 只在第一次跑出「無法判斷」時通知一次,提醒你去調整關鍵字
        send_telegram(
            f"⚠️ 目前無法自動判斷票務狀態,請打開 Actions 的截圖確認,"
            f"並依 README 調整關鍵字。\n時間: {now}\n{TICKET_URL}",
            photo_path=SCREENSHOT_FILE,
        )

    save_state(current_state)


if __name__ == "__main__":
    main()
