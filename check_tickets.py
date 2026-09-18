"""
check_tickets.py
---------------------------------------
定期檢查快達票(hkticketing)頁面是否有票可購買，
一旦狀態從「售罄/不明」變成「有票」，就用 Telegram 通知。

因為目標頁面是 JS 單頁應用(SPA)，直接用 requests 抓不到內容，
所以這裡用 Playwright 啟動一個無頭瀏覽器把頁面「真的打開」再讀取文字。

判斷原則：
1. 只要頁面出現任何「有票關鍵字」，直接判定 available。
2. 如果沒有任何有票關鍵字，但有「售罄/暫無可售」關鍵字，
   則判定 sold_out。
3. 兩者都沒有才判定 unclear。

這樣即使三個場次中只有一場釋票、另外兩場仍顯示「暫無可售」，
也會正確發送 Telegram 通知。
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
# 沒票 / 售罄關鍵字
# ---------------------------------------------------------------
SOLD_OUT_KEYWORDS = [
    "售罄",
    "已售完",
    "SOLD OUT",
    "Sold Out",
    "尚未開賣",
    "尚未開始",
    "敬請留意",
    "貨源已被搶購一空",
    "暫時沒有",
    "沒有可供選購",
    "No tickets available",
    "座位已滿",
    "暫無可售",
]


# ---------------------------------------------------------------
# 有票 / 可以進一步購買的關鍵字
# ---------------------------------------------------------------
AVAILABLE_KEYWORDS = [
    "選擇座位",
    "選擇票區",
    "加入購物車",
    "立即購買",
    "Buy Now",
    "選擇區域",
    "剩餘",
    "可選座位",
]


def send_telegram(text, photo_path=None):
    """用 Telegram Bot API 發送文字，若有截圖就一起傳。"""
    import urllib.request

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過通知")
        return

    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    if photo_path and os.path.exists(photo_path):
        boundary = "----ticketwatcherboundary"

        with open(photo_path, "rb") as f:
            photo_data = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f"{TELEGRAM_CHAT_ID}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{text}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="shot.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + photo_data + (
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{base}/sendPhoto",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            },
        )

    else:
        import urllib.parse

        data = urllib.parse.urlencode(
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{base}/sendMessage",
            data=data,
        )

    try:
        urllib.request.urlopen(req, timeout=15)
        print("Telegram 通知已送出")

    except Exception as e:
        print(f"Telegram 發送失敗: {e}")


def load_last_state():
    """讀取上一次票務狀態。"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip()

    return "unknown"


def save_state(state):
    """儲存這次票務狀態。"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(state)


def main():
    captured_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-HK",
        )

        page = context.new_page()

        # -------------------------------------------------------
        # 記錄頁面背後呼叫的 API JSON
        # 未來如果找到真正票量 API，
        # 可以改成直接打 API，不必每次開瀏覽器。
        # -------------------------------------------------------
        def on_response(response):
            try:
                content_type = response.headers.get("content-type", "")

                if (
                    "json" in content_type
                    and response.request.resource_type in ("xhr", "fetch")
                ):
                    url = response.url

                    if any(
                        keyword in url.lower()
                        for keyword in [
                            "ticket",
                            "activity",
                            "stock",
                            "seat",
                            "event",
                            "product",
                        ]
                    ):
                        try:
                            body = response.json()
                        except Exception:
                            body = None

                        captured_responses.append(
                            {
                                "url": url,
                                "body": body,
                            }
                        )

            except Exception:
                pass

        page.on("response", on_response)

        # -------------------------------------------------------
        # 開啟快達票頁面
        # -------------------------------------------------------
        page.goto(
            TICKET_URL,
            wait_until="networkidle",
            timeout=60000,
        )

        # 給 SPA 一點時間把場次資料渲染出來
        page.wait_for_timeout(5000)

        # 儲存整頁截圖
        page.screenshot(
            path=SCREENSHOT_FILE,
            full_page=True,
        )

        # 取得整頁文字
        body_text = page.inner_text("body")

        # 儲存攔截到的 API
        with open(
            NETWORK_LOG_FILE,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                captured_responses,
                f,
                ensure_ascii=False,
                indent=2,
            )

        browser.close()

    # -----------------------------------------------------------
    # 判斷票務狀態
    # -----------------------------------------------------------
    lower_text = body_text.lower()

    matched_available = [
        keyword
        for keyword in AVAILABLE_KEYWORDS
        if keyword.lower() in lower_text
    ]

    matched_sold_out = [
        keyword
        for keyword in SOLD_OUT_KEYWORDS
        if keyword.lower() in lower_text
    ]

    has_buy_signal = bool(matched_available)
    is_sold_out = bool(matched_sold_out)

    # -----------------------------------------------------------
    # 最重要的地方：
    #
    # 只要出現任何有票關鍵字，就優先判定為 available。
    #
    # 例如：
    # 12/18 暫無可售
    # 12/19 選擇座位
    # 12/20 暫無可售
    #
    # 雖然頁面同時有「暫無可售」，
    # 仍會判定 available。
    # -----------------------------------------------------------
    if has_buy_signal:
        current_state = "available"

    elif is_sold_out:
        current_state = "sold_out"

    else:
        current_state = "unclear"

    last_state = load_last_state()

    now = (
        datetime.now(timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )

    print(
        f"[{now}] "
        f"上次狀態: {last_state} "
        f"-> 這次狀態: {current_state}"
    )

    print(
        f"找到的有票關鍵字: "
        f"{matched_available if matched_available else '無'}"
    )

    print(
        f"找到的售罄關鍵字: "
        f"{matched_sold_out if matched_sold_out else '無'}"
    )

    # -----------------------------------------------------------
    # 有票通知
    # -----------------------------------------------------------
    if current_state == "available" and last_state != "available":

        matched_text = "、".join(matched_available)

        send_telegram(
            (
                f"🎫 快達票可能有票了！\n\n"
                f"偵測到：{matched_text}\n"
                f"時間：{now}\n\n"
                f"{TICKET_URL}"
            ),
            photo_path=SCREENSHOT_FILE,
        )

    # -----------------------------------------------------------
    # 第一次完全判斷不到時才通知
    # -----------------------------------------------------------
    elif (
        current_state == "unclear"
        and last_state == "unknown"
    ):

        send_telegram(
            (
                "⚠️ 目前無法自動判斷票務狀態。\n"
                "請打開 GitHub Actions 的截圖確認頁面文字，"
                "並調整關鍵字。\n\n"
                f"時間：{now}\n"
                f"{TICKET_URL}"
            ),
            photo_path=SCREENSHOT_FILE,
        )

    save_state(current_state)


if __name__ == "__main__":
    main()
