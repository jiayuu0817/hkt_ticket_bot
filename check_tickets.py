"""
check_tickets.py
---------------------------------------
監控香港快達票 HK Ticketing 指定活動是否釋票。

判斷方式：
1. 找出頁面上的所有演出場次。
2. 檢查每個場次自己的區塊是否顯示「暫無可售」。
3. 只要其中一個場次沒有「暫無可售」，就視為疑似釋票。
4. 再嘗試點擊該場次，確認是否出現：
   - 票價類別
   - 下一步
   - 其他購票相關文字
5. 偵測到疑似有票時，用 Telegram 通知。

這樣不需要網站真的顯示「有票」兩個字。
"""

import os
import re
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
# 場次顯示「沒票」時會看到的文字
# ---------------------------------------------------------------
SOLD_OUT_KEYWORDS = [
    "暫無可售",
    "售罄",
    "已售完",
    "SOLD OUT",
    "Sold Out",
    "尚未開賣",
    "尚未開始",
    "沒有可供選購",
    "No tickets available",
]


# ---------------------------------------------------------------
# 點進疑似有票的場次後，可用來再次確認的文字
# ---------------------------------------------------------------
AVAILABLE_KEYWORDS = [
    "票價類別",
    "下一步",
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
    """透過 Telegram Bot API 發送通知。"""
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
            f'Content-Disposition: form-data; name="photo"; filename="shot.png"\r\n'
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
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip()

    return "unknown"


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(state)


def main():

    captured_responses = []

    available_sessions = []
    sold_out_sessions = []
    detected_confirmation = []

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
        # 記錄網站背景 API
        # -------------------------------------------------------
        def on_response(response):
            try:
                content_type = response.headers.get(
                    "content-type",
                    "",
                )

                if (
                    "json" in content_type
                    and response.request.resource_type
                    in ("xhr", "fetch")
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
        # 開啟快達票
        # -------------------------------------------------------
        page.goto(
            TICKET_URL,
            wait_until="networkidle",
            timeout=60000,
        )

        # 等 SPA 資料載入
        page.wait_for_timeout(5000)

        # -------------------------------------------------------
        # 找出每個場次區塊
        #
        # 原理：
        # 從包含「2026年12月18日」這類日期的元素，
        # 往上找到只包含一個日期的場次容器。
        #
        # 這樣就可以判斷：
        #
        # 12/18
        # 暫無可售
        #
        # 跟
        #
        # 12/18
        #
        # 的差別。
        # -------------------------------------------------------
        session_blocks = page.evaluate(
            """
            () => {

                const dateRegex =
                    /20\\d{2}年\\d{1,2}月\\d{1,2}日/g;

                const allElements =
                    Array.from(document.querySelectorAll("*"));

                const results = [];
                const seen = new Set();

                for (const el of allElements) {

                    const ownText =
                        (el.innerText || "").trim();

                    const dates =
                        ownText.match(dateRegex);

                    if (!dates || dates.length === 0) {
                        continue;
                    }

                    // 避免整個頁面的大容器
                    if (ownText.length > 200) {
                        continue;
                    }

                    let node = el;

                    // 往父層找：
                    // 只要父層仍然只包含一個場次日期，
                    // 就把它視為同一個場次卡片。
                    for (let i = 0; i < 5; i++) {

                        if (!node.parentElement) {
                            break;
                        }

                        const parentText =
                            (node.parentElement.innerText || "")
                            .trim();

                        const parentDates =
                            parentText.match(dateRegex) || [];

                        if (
                            parentDates.length === 1 &&
                            parentText.length < 300
                        ) {
                            node = node.parentElement;
                        } else {
                            break;
                        }
                    }

                    const text =
                        (node.innerText || "").trim();

                    const foundDates =
                        text.match(dateRegex) || [];

                    if (foundDates.length !== 1) {
                        continue;
                    }

                    const date = foundDates[0];

                    if (seen.has(date)) {
                        continue;
                    }

                    seen.add(date);

                    results.push({
                        date: date,
                        text: text
                    });
                }

                return results;
            }
            """
        )

        print("========== 場次偵測 ==========")

        for session in session_blocks:

            date = session["date"]
            text = session["text"]

            print(f"場次：{date}")
            print(f"內容：{text}")

            has_sold_out_text = any(
                keyword.lower() in text.lower()
                for keyword in SOLD_OUT_KEYWORDS
            )

            if has_sold_out_text:

                sold_out_sessions.append(date)

                print("結果：暫無可售")

            else:

                available_sessions.append(date)

                print("結果：疑似有票")

        print("==============================")

        # -------------------------------------------------------
        # 如果發現某場次沒有「暫無可售」
        # 嘗試點進去進一步確認
        # -------------------------------------------------------
        if available_sessions:

            target_date = available_sessions[0]

            print(
                f"嘗試點擊疑似有票場次："
                f"{target_date}"
            )

            try:

                locator = page.get_by_text(
                    re.compile(
                        re.escape(target_date)
                    )
                )

                if locator.count() > 0:

                    locator.first.click(
                        timeout=5000
                    )

                    page.wait_for_timeout(3000)

                    detail_text = page.inner_text(
                        "body"
                    )

                    detected_confirmation = [
                        keyword
                        for keyword in AVAILABLE_KEYWORDS
                        if keyword.lower()
                        in detail_text.lower()
                    ]

                    print(
                        "進一步確認關鍵字：",
                        detected_confirmation
                        if detected_confirmation
                        else "沒有，但場次已無暫無可售"
                    )

            except Exception as e:

                print(
                    f"點擊場次確認失敗：{e}"
                )

        # -------------------------------------------------------
        # 截圖
        # -------------------------------------------------------
        page.screenshot(
            path=SCREENSHOT_FILE,
            full_page=True,
        )

        # -------------------------------------------------------
        # 儲存 API Log
        # -------------------------------------------------------
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
    # 判定整體狀態
    # -----------------------------------------------------------

    if available_sessions:

        current_state = "available"

    elif sold_out_sessions:

        current_state = "sold_out"

    else:

        current_state = "unclear"

    last_state = load_last_state()

    now = (
        datetime.now(timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )

    print("")
    print("========== 最終結果 ==========")

    print(
        f"[{now}] "
        f"上次狀態：{last_state} "
        f"-> 這次狀態：{current_state}"
    )

    print(
        "暫無可售場次：",
        sold_out_sessions
        if sold_out_sessions
        else "無"
    )

    print(
        "疑似有票場次：",
        available_sessions
        if available_sessions
        else "無"
    )

    print(
        "確認關鍵字：",
        detected_confirmation
        if detected_confirmation
        else "無"
    )

    print("==============================")

    # -----------------------------------------------------------
    # 有票通知
    # -----------------------------------------------------------
    if (
        current_state == "available"
        and last_state != "available"
    ):

        session_text = "\n".join(
            f"• {session}"
            for session in available_sessions
        )

        confirmation_text = (
            "、".join(detected_confirmation)
            if detected_confirmation
            else "場次的「暫無可售」已消失"
        )

        send_telegram(
            (
                "🎫 快達票疑似釋票！\n\n"
                f"場次：\n{session_text}\n\n"
                f"偵測依據：{confirmation_text}\n\n"
                f"時間：{now}\n\n"
                f"{TICKET_URL}"
            ),
            photo_path=SCREENSHOT_FILE,
        )

    # -----------------------------------------------------------
    # 第一次完全無法判斷
    # -----------------------------------------------------------
    elif (
        current_state == "unclear"
        and last_state == "unknown"
    ):

        send_telegram(
            (
                "⚠️ 快達票目前無法判斷票務狀態。\n\n"
                "請查看 GitHub Actions 的 "
                "latest_screenshot.png。\n\n"
                f"時間：{now}\n\n"
                f"{TICKET_URL}"
            ),
            photo_path=SCREENSHOT_FILE,
        )

    save_state(current_state)


if __name__ == "__main__":
    main()
