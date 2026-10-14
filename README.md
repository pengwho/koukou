<div align="center">

<img src="koukou/web/static/stamp.svg" width="72" alt="">

# 摳摳 KouKou

**把散在各家銀行和發票載具裡的摳摳，一筆一筆摳回來。**<br>
*Dig your money data back out — and let AI tell you where it went.*

[![CI](https://github.com/pengwho/koukou/actions/workflows/ci.yml/badge.svg)](https://github.com/pengwho/koukou/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-25304A)
![Local-first](https://img.shields.io/badge/資料-只存在你的電腦-D23A2E)

**[👉 線上試玩 Live demo](https://pengwho.github.io/koukou/)**　·　[English](#english) · [中文](#中文)

<img src="docs/screenshot.webp" alt="摳摳的主畫面：左邊是當月的收據明細與「已摳回」印章，右邊是每月花費長條圖和常去的店" width="860">

</div>

## 中文

「摳摳」是台灣人講錢的口語。這個專案做三件事，剛好對上「摳」的三個意思：

| 摳 | 做什麼 |
| --- | --- |
| 摳摳＝錢 | 把手機條碼的電子發票（之後還有銀行帳戶）收進**你自己電腦上**的一本帳 |
| 摳＝挖出來 | 模擬官方 App 的協定把資料抓回來，不用一張一張手動匯出 |
| 摳＝省 | AI 助理**阿財**幫每個品項分類，看出錢花在什麼上；你改過的分類它會記住 |

### 先看看長什麼樣子

直接打開 **[線上展示](https://pengwho.github.io/koukou/)**，裡面是虛構的發票，什麼都不用裝。

### 用你自己的發票

```bash
pip install git+https://github.com/pengwho/koukou
koukou serve
```

瀏覽器會自動打開。第一次會請你輸入手機條碼的**手機號碼**和**驗證碼**（申請手機條碼時自己設定的那組，不是簡訊驗證碼），之後按「摳回最新發票」就會更新。

- 帳密存在系統鑰匙圈（macOS Keychain），不會寫進檔案；頁尾可以隨時「取消連結」
- 帳本在 `~/.koukou/koukou.sqlite3`；`koukou export` 匯出 CSV；`koukou fetch` 可從命令列同步
- `koukou demo` 用虛構資料在本機打開

### 阿財怎麼分類

```bash
koukou ai setup        # 貼上 Claude API 金鑰（存進鑰匙圈）
koukou ai categorize   # 或在網頁上按「請阿財分類」；每次摳回發票後也會自動分新品項
koukou ai eval         # 用人工標註的測試集量準確率
```

```mermaid
flowchart LR
  A[發票品項] --> B{規則<br>只放不會錯的}
  B -- 命中 --> Z[(分類快取<br>同品名只分一次)]
  B -- 沒把握 --> C{快取裡有？}
  C -- 有 --> Z
  C -- 沒有 --> D[LLM 批次<br>40 個一批<br>JSON schema 輸出]
  E[你親手改過的分類] -- few-shot 範例 --> D
  E -- 永遠優先 --> Z
  D --> Z
```

- **分層，便宜又快。** 高精準的關鍵字規則先處理（例如「無鉛汽油」→交通、負數→折扣），每個品名只問模型一次，結果快取起來；一份半年的帳本大約 300 種品名，一次就分完，之後每天只有新品名要問。
- **隱私。** 送給模型的只有「品名」和「店名」，不送金額、日期、發票號碼或載具。想完全不出電腦，可以接本機模型：`KOUKOU_AI_PROVIDER=openai KOUKOU_AI_MODEL=qwen2.5:7b`（Ollama 或任何 OpenAI 相容 API）。
- **越用越準。** 點品項旁的分類標籤就能改，同名品項一起套用；你改過的分類不會被模型覆蓋，還會被當成範例放進提示詞。
- **模型不能亂答。** 用 tool use 強制輸出 JSON schema，分類必須在清單內；漏掉的品項會補問一次，信心低的在畫面上用虛線標出來。
- **數字對得起來。** 折扣、點數折抵和發票差額依比例攤回同一張發票的其他品項，所以各分類加總等於當月發票總額。

**評估。** `koukou/ai/evalset.jsonl` 是 126 個人工標註的台灣發票品項（超商、量販、外送、加油、藥妝……），刻意包含「霜淇淋在 IKEA」「美式雞腿堡不是咖啡」這類容易錯的例子。

| 層 | 覆蓋率 | 準確率 |
| --- | ---: | ---: |
| 規則（測試集） | 19.8% | 100% |
| 規則（作者真實帳本，289 種品名，人工逐筆檢查） | 13.5% | 100% |
| LLM | 其餘全部 | 執行 `koukou ai eval` 量測 |

規則的兩個誤判（「美式雞腿堡」「多力多滋美式辣起士」被當成咖啡）是拿真實帳本跑出來才發現的；修正後同類例子已經加進測試集當回歸測試。

### 它怎麼運作

```mermaid
flowchart LR
  A[koukou fetch / 網頁按鈕] --> B[sync.py<br>逐月逐頁]
  B --> C[client.py<br>登入、401 自動重登]
  C --> D[protocol.py<br>AES-256-GCM 信封<br>HS256 JWT 簽章]
  C --> E[transport.py<br>curl_cffi 模仿 Chrome TLS<br>或 headless Chromium]
  E --> F[(財政部 App API)]
  B --> G[(SQLite 帳本<br>~/.koukou)]
  G --> H[FastAPI 本機網頁<br>127.0.0.1]
```

幾個值得一提的設計決定：

- **模擬手機 App，而不是爬網頁。** App 的 API 回傳結構化 JSON，有品項明細，也沒有圖形驗證碼。代價是要重現它的加密：登入資料用 AES-256-GCM 包成 `a|密文|b` 信封，查詢用登入拿到的密鑰簽 HS256 JWT。加密層有固定測試向量，跟參考實作逐位元比對。
- **兩種連線方式。** 財政部主機前面有 Cloudflare，一般的 HTTP client 會因為 TLS 指紋被擋。預設用 `curl_cffi` 模仿 Chrome 指紋（輕量，實測可通過）；被擋時可改用 `--transport browser` 在真的 Chromium 裡發請求，並在網路層拔掉查詢主機不接受的 `Origin` 標頭。
- **App 的 session 只有約 30 秒。** 抓半年資料一定會過期，所以遇到 `401` 就自動重新登入再重試。
- **增量同步。** 以發票號碼為主鍵覆寫；已經有品項的發票不會重抓，所以每天跑也很快。
- **本機服務也要防網頁攻擊。** 任何你打開的網站都能叫瀏覽器對 `127.0.0.1` 發請求。所以伺服器只接受 Host 是本機的請求（擋 DNS rebinding），會改變狀態的請求必須帶自訂標頭（跨站送不出來）。
- **線上展示不需要伺服器。** `koukou build-static` 把展示帳本匯出成一個 JSON，同一份前端在靜態模式下改從它讀資料、在瀏覽器裡搜尋；GitHub Actions 每次更新自動部署到 Pages。
- **展示模式走同一條路。** `koukou demo` 產生的假資料是 App API 的原始格式，經過跟真資料一樣的 `Store.save()`，所以它同時是寫入路徑的測試。

### 路線圖

- [x] 電子發票：表頭、品項、增量同步、CSV 匯出
- [x] 本機網頁介面與展示模式
- [x] 線上展示（GitHub Pages）、網頁上直接連結手機條碼、帳密存鑰匙圈
- [x] **阿財**：LLM 品項分類（規則＋快取＋LLM、使用者修正、評估）
- [ ] **阿財**：用自然語言問帳本（text-to-SQL，唯讀與白名單防護）
- [ ] MCP server：讓 Claude 等 AI 直接查你的帳本
- [ ] 第一家銀行（Playwright）
- [ ] 打包成 macOS App，帳密改存 Keychain

### 注意

這是非官方工具，模擬官方 App 的做法不符合其使用條款；協定可能隨 App 改版失效。請只用在自己的帳號，資料只會存在你的電腦。

協定細節參考 [TedLin1993/all-set-tw](https://github.com/TedLin1993/all-set-tw)（MIT）。

---

## English

**KouKou** (摳摳, Taiwanese slang for *money*; 摳 also means *to dig out* and *to be thrifty*) is a local-first personal finance hub for Taiwan.

It pulls your e-invoices — every receipt tied to your mobile barcode carrier, down to line items — by speaking the official mobile app's API directly, stores them in a SQLite ledger on your own machine, and shows them in a small local web app. An AI assistant, **A-Tsâi (阿財, "little fortune")**, categorizes every line item: precise keyword rules first, a per-name cache, then batched LLM calls (Claude, or any OpenAI-compatible local model) with schema-constrained output; your corrections override the model and are fed back as few-shot examples. A hand-labeled 126-item eval set (`koukou ai eval`) measures accuracy per layer. Bank accounts are next.

**[Try the live demo](https://pengwho.github.io/koukou/)** (generated data, nothing to install), or run it on your own invoices:

```bash
pip install git+https://github.com/pengwho/koukou
koukou serve           # opens http://127.0.0.1:8765 and asks for your carrier login on first run
```

Engineering notes: the app protocol wraps credentials in an AES-256-GCM envelope and signs queries as HS256 JWTs (covered by fixed test vectors); Cloudflare's TLS fingerprinting is handled with `curl_cffi` Chrome impersonation, with a headless-Chromium fallback that strips the `Origin` header at the network layer; the ~30-second app session is renewed transparently on `401`; syncs are incremental and idempotent; credentials live in the OS keychain; the local server rejects foreign Host headers and requires a custom header on state-changing requests to block DNS-rebinding and CSRF from other sites.

*Unofficial. Emulating the official app is against its terms of use and may break when the app updates. Use only with your own account.*

## License

MIT
