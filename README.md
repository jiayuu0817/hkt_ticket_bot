# 快達票(HK Ticketing)票務監控機器人

自動每 10 分鐘檢查一次指定活動的頁面,一旦偵測到「有票可買」,就透過 Telegram 傳訊息通知你,並附上當下的截圖。全程免費,跑在 GitHub Actions 上,不用開著自己的電腦。

---

## 這個工具能做什麼 / 不能做什麼

✅ 定期打開票務頁面、判斷是否有票、有變化就通知你
✅ 免費、自動排程執行
✅ 記錄每次的截圖跟後台 API 回應,方便你自行核對或調整判斷邏輯

❌ **不會**幫你自動搶票、自動下單、自動付款——這只是「通知」工具
❌ 因為我沒辦法連到 hkticketing.com 實際看過「有票」跟「售罄」時頁面長怎樣,判斷邏輯是用常見字眼猜的,**第一次執行後幾乎一定要照下面「Step 5」校正一次**
❌ 請不要把檢查頻率調得太密集(例如每幾秒一次),太頻繁可能被網站判定為異常流量而封鎖

---

## Step 1:建立 Telegram Bot,拿到 Token

1. 在 Telegram 搜尋並打開 **@BotFather**
2. 傳送 `/newbot`,依指示取一個名字跟 username
3. 完成後 BotFather 會給你一串類似 `123456789:ABCdefGhIJKlmNoPQRstuVWxyz` 的 **Bot Token**,先存起來

## Step 2:取得你的 Chat ID

1. 在 Telegram 搜尋並打開你剛建立的 bot,傳送任意訊息(例如 `hi`)給它
2. 用瀏覽器打開(把 `<TOKEN>` 換成你的 Token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. 在回傳的 JSON 裡找 `"chat":{"id":123456789,...}`,這個數字就是你的 **Chat ID**

## Step 3:建立 GitHub Repository 並上傳這些檔案

1. 到 [github.com](https://github.com) 新增一個 repository(建議設為 **Public**,Actions 分鐘數完全免費且不限量;若設 Private,免費額度是每月 2000 分鐘,通常也很夠用)
2. 把這個資料夾裡所有檔案(含 `.github` 資料夾)上傳/push 上去

## Step 4:設定 Secrets

到你的 repo → **Settings → Secrets and variables → Actions → New repository secret**,新增兩組:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Step 1 拿到的 Token |
| `TELEGRAM_CHAT_ID` | Step 2 拿到的 Chat ID |

## Step 5:手動跑一次、校正判斷邏輯

1. 到 repo 的 **Actions** 分頁,選擇「Check HK Ticketing Availability」→ **Run workflow** 手動觸發一次
2. 跑完後,點進該次紀錄下載 `debug-xxx` 這個 artifact,裡面有:
   - `latest_screenshot.png`:當下頁面截圖
   - `network_log.json`:頁面背後打的 API 回應(如果有攔到的話)
3. 打開截圖,看「目前狀態」實際上寫的是什麼字(例如「尚未開賣」「立即購買」「選擇座位」等)
4. 打開 `check_tickets.py`,找到最上面的 `SOLD_OUT_KEYWORDS` 和 `AVAILABLE_KEYWORDS` 兩個清單,把你在截圖上實際看到的關鍵字加進去,存檔後 push 回 repo
5. 如果 `network_log.json` 裡有攔到像是庫存數量、場次狀態的 JSON,把內容貼給我,我可以幫你改成直接打 API(更快、更省資源,不用開瀏覽器)

## Step 6:調整檢查頻率(選用)

打開 `.github/workflows/check-tickets.yml`,修改這一行的 cron 排程:

```yaml
- cron: "*/10 * * * *"   # 目前是每 10 分鐘
```

例如改成每 5 分鐘:`*/5 * * * *`。注意 GitHub 官方排程本身可能有數分鐘誤差,不保證準時。

---

## 之後就不用管了

設定完成後,GitHub Actions 會自動照排程執行,狀態沒變化就不會吵你,只有從「售罄/不明」變成「有票」時才會傳 Telegram 通知。想隨時手動檢查,也可以隨時到 Actions 分頁按 Run workflow。
