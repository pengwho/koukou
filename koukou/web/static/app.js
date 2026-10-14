const $ = (sel) => document.querySelector(sel);
const money = (n) => {
  const v = Math.round(n ?? 0);
  return `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("zh-TW")}`;
};
const monthText = (m) => {
  const [y, mm] = m.split("-");
  return `${y} 年 ${Number(mm)} 月`;
};

// 線上展示（GitHub Pages）沒有伺服器：同一份畫面改從 data/ledger.json 讀資料。
const STATIC = document.querySelector('meta[name="koukou-mode"]')?.content === "static";

async function api(path, { method = "GET", body } = {}) {
  if (STATIC) return staticApi(path, method, body);
  const res = await fetch(path, {
    method,
    headers: { "X-KouKou": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "格式不對，請再檢查一次" : data.detail;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

// ---- 線上展示：阿財的小模型在瀏覽器裡跑（koukou-model.js 跟 Python 版逐筆一致）----
let ledger;
const live = { items: new Map(), userExamples: [], model: null };

async function loadLedger() {
  if (ledger) return;
  ledger = await fetch("data/ledger.json").then((r) => r.json());
  for (const it of ledger.items) live.items.set(it.key, it);
}

async function retrain() {
  const { NaiveBayes } = await import("./koukou-model.js");
  const cfg = ledger.model.config;
  const base = ledger.model.trainset.map(([text, store, category]) => ({ text, store, category, weight: 1 }));
  const user = live.userExamples.map(([text, category]) => ({ text, store: "", category, weight: cfg.user_weight }));
  live.model = new NaiveBayes([...base, ...user], cfg);
}

async function relabelLocalItems() {
  const { guess } = await import("./koukou-model.js");
  const cfg = ledger.model.config;
  for (const it of live.items.values()) {
    if (it.source !== "local") continue;
    const g = guess(live.model, { description: it.description, store: it.store, amount: it.max_amount }, cfg);
    it.category = g.category;
    it.confidence = g.confidence;
  }
  const { normalize } = await import("./koukou-model.js");
  for (const m of Object.values(ledger.months)) {
    for (const inv of m.invoices) {
      for (const item of inv.items) {
        const idx = live.items.get(normalize(item.description));
        if (!idx) continue;
        item.category = idx.category;
        item.category_source = idx.source;
        item.confidence = idx.confidence;
      }
    }
  }
}

function staticQueue(limit = 30) {
  const unsure = ledger.model.config.unsure;
  return [...live.items.values()]
    .filter((it) => ["local", "llm"].includes(it.source) && it.total > 0
      && (it.confidence < unsure || it.category === "未分類"))
    .sort((a, b) => b.total - a.total)
    .slice(0, limit);
}

function staticStats() {
  const stats = { rule: 0, local: 0, llm: 0, user: 0, demo: 0, pending: 0 };
  for (const it of live.items.values()) stats[it.source] = (stats[it.source] ?? 0) + 1;
  stats.low_confidence = staticQueue(10_000).length;
  return stats;
}

/** 跟 Store.breakdown 相同：折扣與差額依比例攤回，各分類加總等於發票總額。 */
function staticBreakdown(month) {
  const totals = new Map();
  const invoices = new Map();
  for (const inv of month.invoices) {
    let parts = inv.items
      .filter((it) => it.amount > 0 && it.category !== "折扣")
      .map((it) => [it.category ?? "未分類", it.amount]);
    if (!parts.length) parts = [["未分類", inv.amount]];
    const base = parts.reduce((a, [, v]) => a + v, 0) || 1;
    for (const [cat, amt] of parts) {
      totals.set(cat, (totals.get(cat) ?? 0) + amt * inv.amount / base);
      if (!invoices.has(cat)) invoices.set(cat, new Set());
      invoices.get(cat).add(inv.inv_num);
    }
  }
  const grand = [...totals.values()].reduce((a, b) => a + b, 0) || 1;
  return [...totals].map(([category, amt]) => ({
    category, amount: Math.round(amt), share: amt / grand, invoices: invoices.get(category).size,
  })).sort((a, b) => b.amount - a.amount);
}

async function staticFixCategory({ description, category }) {
  const { normalize } = await import("./koukou-model.js");
  const key = normalize(description);
  const it = live.items.get(key);
  const before = new Set(staticQueue(10_000).map((q) => q.key));
  if (it) Object.assign(it, { category, source: "user", confidence: 1 });
  live.userExamples.unshift([description, category]);  // 跟 Python 一樣，最新的修正排前面
  await retrain();
  await relabelLocalItems();
  const after = new Set(staticQueue(10_000).map((q) => q.key));
  let alsoResolved = 0;
  for (const k of before) if (!after.has(k) && k !== key) alsoResolved += 1;
  return { applied_to: it?.n ?? 0, also_resolved: alsoResolved };
}

async function staticApi(path, method, body) {
  await loadLedger();
  const url = new URL(path, location.href);
  const p = url.searchParams;
  const route = url.pathname.slice(url.pathname.indexOf("api/"));
  const month = ledger.months[p.get("month") || ledger.meta.default_month];
  if (method === "POST" && route === "api/items/category") return staticFixCategory(body);
  if (method !== "GET") throw new Error("這是線上展示，不會連線。");
  if (route === "api/meta") return ledger.meta;
  if (route === "api/monthly") return ledger.monthly;
  if (route === "api/summary") return month.summary;
  if (route === "api/merchants") return month.merchants.slice(0, Number(p.get("limit") || 8));
  if (route === "api/sync") return { running: false };
  if (route === "api/credentials") return { configured: false };
  if (route === "api/categories") return ledger.categories;
  if (route === "api/breakdown") return staticBreakdown(month);
  if (route === "api/ai") return { ...ledger.ai, stats: staticStats() };
  if (route === "api/ai/review") return staticQueue(Number(p.get("limit") || 30));
  if (route === "api/invoices") {
    const q = (p.get("q") || "").toLowerCase();
    const cat = p.get("category");
    return month.invoices.filter((inv) => {
      if (cat && !inv.items.some((it) => it.amount > 0 && (it.category ?? "未分類") === cat)) return false;
      if (!q) return true;
      return [inv.merchant, inv.seller_name, inv.inv_num, ...inv.items.map((it) => it.description)]
        .some((text) => (text || "").toLowerCase().includes(q));
    });
  }
  const items = route.match(/^api\/invoices\/([^/]+)\/items$/);
  if (items) {
    const num = decodeURIComponent(items[1]);
    for (const m of Object.values(ledger.months)) {
      const inv = m.invoices.find((i) => i.inv_num === num);
      if (inv) return inv.items;
    }
    return [];
  }
  throw new Error(`線上展示不支援 ${route}`);
}

const state = { months: [], month: null, query: "", category: "", demo: false, configured: false, categories: [] };

async function init() {
  const meta = await api("api/meta");
  state.months = meta.months;
  state.month = meta.default_month;
  state.demo = meta.demo;
  $("#version").textContent = `v${meta.version}`;
  if (meta.demo) {
    $("#demo-badge").hidden = false;
    $("#demo-badge").textContent = STATIC ? "線上展示" : "展示資料";
    $("#live-banner").hidden = !STATIC;
  }
  await refreshAccount();
  state.categories = await api("api/categories");
  await Promise.all([renderTrend(), render(), refreshAcai()]);
  $("#stamp").classList.add("press");
  const sync = await api("api/sync");
  if (sync.running) pollSync();
  else if (!meta.demo && !state.configured && meta.months.length === 0) openLogin();  // 第一次打開
}

async function render() {
  const { month, query } = state;
  const idx = state.months.indexOf(month);
  $("#month-label").textContent = monthText(month);
  $("#receipt-title").textContent = monthText(month);
  $("#prev").disabled = idx <= 0;
  $("#next").disabled = idx === -1 || idx >= state.months.length - 1;

  const cat = state.category ? `&category=${encodeURIComponent(state.category)}` : "";
  const [summary, invoices, shops, cats] = await Promise.all([
    api(`api/summary?month=${month}`),
    api(`api/invoices?month=${month}&q=${encodeURIComponent(query)}${cat}`),
    api(`api/merchants?month=${month}&limit=6`),
    api(`api/breakdown?month=${month}`),
  ]);
  $("#filter").hidden = !state.category;
  $("#filter-name").textContent = state.category;

  $("#total").textContent = money(summary.amount);
  $("#total-sub").textContent = summary.count
    ? `${summary.count} 張發票，${summary.merchants} 家店，平均每張 ${money(summary.average)}`
    : "這個月還沒有發票";
  $("#stamp").hidden = summary.count === 0;

  renderInvoices(invoices);
  renderShops(shops);
  renderCategories(cats);
  document.querySelectorAll(".trend-col").forEach((col) =>
    col.setAttribute("aria-pressed", String(col.dataset.month === month)),
  );
}

function renderInvoices(invoices) {
  const list = $("#invoice-list");
  const tpl = $("#invoice-row");
  list.replaceChildren();
  for (const inv of invoices) {
    const li = tpl.content.firstElementChild.cloneNode(true);
    const [, mm, dd] = inv.inv_date.split("-");
    li.querySelector(".inv-date").textContent = `${mm}/${dd}`;
    li.querySelector(".inv-shop").textContent = inv.merchant;
    li.querySelector(".inv-shop").title = inv.seller_name ?? "";
    li.querySelector(".inv-amount").textContent = money(inv.amount);
    const btn = li.querySelector(".inv-btn");
    const box = li.querySelector(".inv-items");
    btn.addEventListener("click", () => toggleItems(btn, box, inv));
    list.append(li);
  }
  const empty = $("#empty");
  empty.hidden = invoices.length > 0;
  if (state.query) empty.textContent = `這個月沒有符合「${state.query}」的發票。`;
  else if (state.category) empty.textContent = `這個月沒有「${state.category}」的發票。`;
  else empty.textContent = state.demo
    ? "這個月沒有發票。"
    : "這個月還沒有發票。按右上角「摳回最新發票」把它們抓回來。";
}

async function toggleItems(btn, box, inv) {
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", String(!open));
  box.hidden = open;
  if (open || box.childElementCount) return;
  const items = await api(`api/invoices/${encodeURIComponent(inv.inv_num)}/items`);
  for (const it of items) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "desc";
    const label = document.createElement("span");
    label.textContent = it.description;
    name.append(label);
    if (it.amount > 0) name.append(categoryTag(it));
    const qty = document.createElement("span");
    qty.className = "qty";
    qty.textContent = it.quantity && it.quantity !== 1 ? `×${it.quantity}` : "";
    const amt = document.createElement("span");
    amt.className = "amt" + (it.amount < 0 ? " neg" : "");
    amt.textContent = money(it.amount);
    li.append(name, qty, amt);
    box.append(li);
  }
  const meta = document.createElement("li");
  meta.className = "meta";
  meta.textContent = `${inv.inv_num}　${inv.inv_time ?? ""}　${inv.seller_name ?? ""}`;
  box.append(meta);
}

// ---- 分類 ----
function categoryTag(it) {
  const tag = document.createElement("button");
  tag.type = "button";
  tag.className = "tag";
  const paint = () => {
    tag.textContent = it.category ?? "未分類";
    tag.classList.toggle("mine", it.category_source === "user");
    tag.classList.toggle("unsure", ["llm", "local"].includes(it.category_source) && it.confidence < 0.6);
    const sure = `把握 ${Math.round((it.confidence ?? 0) * 100)}%`;
    const who = { user: "你改的", llm: `AI 複查（${sure}）`, local: `阿財猜的（${sure}）`,
                  rule: "規則判斷", demo: "展示資料" }[it.category_source] ?? "還沒分類";
    tag.title = `${who}，點一下修改`;
    tag.setAttribute("aria-label", `分類：${tag.textContent}，${who}，點一下修改`);
  };
  paint();
  tag.addEventListener("click", (e) => {
    e.stopPropagation();
    const select = document.createElement("select");
    select.className = "tag-select";
    select.setAttribute("aria-label", `把「${it.description}」改成`);
    for (const c of state.categories) {
      const opt = new Option(c.id, c.id, false, c.id === it.category);
      opt.title = c.hint;
      select.append(opt);
    }
    tag.replaceWith(select);
    select.focus();
    let finished = false;
    const done = async (save) => {
      if (finished) return;  // 換回標籤時會觸發 blur，避免重複處理
      finished = true;
      if (save && select.value !== it.category) {
        try {
          const r = await api("api/items/category", { method: "POST",
            body: { description: it.description, category: select.value } });
          it.category = select.value;
          it.category_source = "user";
          const more = r.also_resolved ? `，阿財也順便想通了另外 ${r.also_resolved} 種相似的品項` : "";
          $("#sync-status").textContent = `已把「${it.description}」改成${select.value}（${r.applied_to} 筆）${more}。`;
          render();
          refreshAcai();
        } catch (err) {
          $("#sync-status").textContent = err.message;
        }
      }
      paint();
      if (select.isConnected) select.replaceWith(tag);
    };
    select.addEventListener("change", () => done(true));
    select.addEventListener("blur", () => done(false));
    select.addEventListener("keydown", (ev) => { if (ev.key === "Escape") done(false); });
  });
  return tag;
}

function renderCategories(cats) {
  const list = $("#cats");
  list.replaceChildren();
  const max = Math.max(1, ...cats.map((c) => c.amount));
  for (const c of cats) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "shop";
    btn.setAttribute("aria-pressed", String(state.category === c.category));
    btn.innerHTML = `<span class="shop-name"></span><span class="shop-amt"><small></small><span></span></span>
      <span class="shop-track"><span class="shop-fill"></span></span>`;
    btn.querySelector(".shop-name").textContent = c.category;
    btn.querySelector(".shop-amt small").textContent = `${Math.round(c.share * 100)}%`;
    btn.querySelector(".shop-amt span").textContent = money(c.amount);
    btn.querySelector(".shop-fill").style.width = `${(c.amount / max) * 100}%`;
    btn.title = `只看有「${c.category}」的發票`;
    btn.addEventListener("click", () => {
      state.category = state.category === c.category ? "" : c.category;
      render();
    });
    li.append(btn);
    list.append(li);
  }
  if (!cats.length) list.innerHTML = '<li class="empty">還沒有資料</li>';
}

async function refreshAcai() {
  const ai = await api("api/ai");
  const s = ai.stats ?? {};
  const done = (s.rule ?? 0) + (s.local ?? 0) + (s.llm ?? 0) + (s.user ?? 0) + (s.demo ?? 0);
  const unsure = s.low_confidence ?? 0;
  const text = $("#acai-text");
  const meta = $("#acai-meta");
  const review = $("#acai-review");
  const run = $("#acai-run");
  review.hidden = true;
  run.hidden = true;
  if (ai.running) {
    text.textContent = `正在分類：${ai.step}`;
    meta.textContent = "";
    setTimeout(refreshAcai, 1000);
    return;
  }
  if (ai.error) {
    text.textContent = `分類沒完成：${ai.error}`;
  } else if (!done) {
    text.textContent = "摳回發票之後，我會幫每個品項分類。不用網路，也不用設定任何東西。";
  } else if (unsure) {
    text.textContent = `${done} 種品項我都分好了，其中 ${unsure} 種沒把握。幫我確認一下，相似的我會一起學起來。`
      + (STATIC ? "（線上展示也能試，學習只發生在你的瀏覽器裡）" : "");
    review.hidden = false;
    review.textContent = `幫我確認 ${unsure} 個`;
  } else {
    text.textContent = `${done} 種品項都分好了。分錯的話點品項旁的標籤改一下，我會記住。`;
  }
  const parts = [];
  if (s.rule) parts.push(`規則 ${s.rule}`);
  if (s.local) parts.push(`阿財自己判斷 ${s.local}`);
  if (s.llm) parts.push(`AI 複查 ${s.llm}`);
  if (s.user) parts.push(`你改過 ${s.user}`);
  if (s.demo) parts.push(`展示標註 ${s.demo}`);
  meta.textContent = (parts.length ? parts.join("、") + "。" : "") +
    (ai.configured ? `沒把握的會請 ${ai.model} 複查。` : "全部在這台電腦上完成。");
  run.hidden = !(ai.configured && unsure && !STATIC);
}

// ---- 幫阿財確認：一次一個，先問影響金額最大的 ----
const review = { queue: [], answered: 0, learned: 0 };

async function openReview() {
  review.queue = await api("api/ai/review?limit=50");
  review.answered = 0;
  review.learned = 0;
  $("#review-note").textContent = STATIC
    ? "線上展示：阿財在你的瀏覽器裡重新學習，重新整理頁面就會還原。"
    : "";
  if (!review.queue.length) return;
  showReviewItem();
  $("#review").showModal();
}

function showReviewItem() {
  const item = review.queue[0];
  if (!item) {
    $("#review").close();
    if (review.answered) {
      const more = review.learned ? `，阿財順便學會了另外 ${review.learned} 種相似的` : "";
      $("#sync-status").textContent = `謝謝！確認了 ${review.answered} 個${more}。`;
    }
    render();
    refreshAcai();
    return;
  }
  const guessed = item.category !== "未分類";
  $("#review-progress").textContent = `還有 ${review.queue.length} 個沒把握，先問影響金額最大的。`;
  $("#review-item").textContent = item.description;
  $("#review-meta").textContent = `${item.store}，共 ${item.n} 次，${money(item.total)}`;
  $("#review-guess").innerHTML = guessed
    ? `阿財猜是<strong></strong>（把握 ${Math.round(item.confidence * 100)}%）`
    : "阿財猜不出來";
  if (guessed) $("#review-guess strong").textContent = item.category;
  const box = $("#review-choices");
  box.replaceChildren();
  for (const c of state.categories) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "choice" + (c.id === item.category ? " guess" : "");
    b.textContent = c.id;
    b.title = c.hint;
    b.addEventListener("click", () => answerReview(item, c.id));
    box.append(b);
  }
  box.querySelector(".guess, .choice")?.focus();
}

async function answerReview(item, category) {
  $("#review-choices").querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const r = await api("api/items/category", { method: "POST", body: { description: item.description, category } });
    review.answered += 1;
    review.learned += r.also_resolved;
    $("#review-note").textContent = r.also_resolved
      ? `學到了！另外 ${r.also_resolved} 種相似的品項也一起有把握了。`
      : "記住了。";
    review.queue = await api("api/ai/review?limit=50");
  } catch (e) {
    $("#review-note").textContent = e.message;
    review.queue.shift();
  }
  showReviewItem();
}

$("#acai-review").addEventListener("click", openReview);
$("#review-skip").addEventListener("click", () => {
  review.queue.push(review.queue.shift());
  if (review.queue.length > 1) showReviewItem();
});
$("#review-done").addEventListener("click", () => {
  review.queue = [];
  showReviewItem();
});

$("#acai-run").addEventListener("click", async () => {
  try {
    await api("api/ai/categorize", { method: "POST" });
  } catch (e) {
    $("#acai-text").textContent = e.message;
    return;
  }
  await refreshAcai();
  const wait = async () => {
    const ai = await api("api/ai");
    if (ai.running) return setTimeout(wait, 1000);
    render();
  };
  wait();
});
$("#filter-clear").addEventListener("click", () => {
  state.category = "";
  render();
});

function renderShops(shops) {
  const list = $("#shops");
  list.replaceChildren();
  const max = Math.max(1, ...shops.map((s) => s.amount));
  for (const s of shops) {
    const li = document.createElement("li");
    li.className = "shop";
    li.innerHTML = `<span class="shop-name"></span><span class="shop-amt"></span>
      <span class="shop-track"><span class="shop-fill"></span></span>`;
    const name = li.querySelector(".shop-name");
    name.textContent = s.name;
    const small = document.createElement("small");
    small.textContent = `${s.count} 次`;
    name.append(small);
    li.querySelector(".shop-amt").textContent = money(s.amount);
    li.querySelector(".shop-fill").style.width = `${(s.amount / max) * 100}%`;
    list.append(li);
  }
  if (!shops.length) list.innerHTML = '<li class="empty">還沒有資料</li>';
}

async function renderTrend() {
  const data = await api("api/monthly");
  const box = $("#trend");
  box.replaceChildren();
  const max = Math.max(1, ...data.map((d) => d.amount));
  for (const d of data.slice(-8)) {
    const col = document.createElement("button");
    col.className = "trend-col";
    col.dataset.month = d.month;
    col.setAttribute("aria-pressed", String(d.month === state.month));
    col.setAttribute("aria-label", `${monthText(d.month)}，${money(d.amount)}`);
    col.innerHTML = `<span class="trend-track"><span class="trend-bar"><span class="trend-val"></span></span></span><span class="trend-month"></span>`;
    col.querySelector(".trend-val").textContent = money(d.amount);
    col.querySelector(".trend-bar").style.height = `${Math.max(2, (d.amount / max) * 100)}%`;
    col.querySelector(".trend-month").textContent = `${Number(d.month.slice(5))}月`;
    col.addEventListener("click", () => go(d.month));
    box.append(col);
  }
}

function go(month) {
  if (!month || month === state.month) return;
  state.month = month;
  render();
}

// ---- 連結帳號 ----
async function refreshAccount() {
  const acc = await api("api/credentials");
  state.configured = acc.configured;
  $("#account").hidden = !acc.configured;
  $("#account-phone").textContent = acc.phone ?? "";
}

function openLogin(message = "") {
  const err = $("#login-error");
  err.hidden = !message;
  err.textContent = message;
  $("#login").showModal();
  $("#login-phone").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const phone = $("#login-phone").value.trim();
  if (!/^09\d{8}$/.test(phone)) {
    openLogin("手機號碼要是 09 開頭的 10 位數字。");
    return;
  }
  try {
    await api("api/credentials", {
      method: "POST",
      body: { phone, password: $("#login-password").value, remember: $("#login-remember").checked },
    });
  } catch (err) {
    openLogin(err.message);
    return;
  }
  $("#login-password").value = "";
  $("#login").close();
  await refreshAccount();
  startSync();
});
$("#login-cancel").addEventListener("click", () => $("#login").close());
$("#logout").addEventListener("click", async () => {
  await api("api/credentials", { method: "DELETE" });
  await refreshAccount();
  $("#sync-status").textContent = "已取消連結，這台電腦不再記得你的帳密。";
});

// ---- 同步 ----
async function startSync() {
  const status = $("#sync-status");
  status.classList.remove("error");
  try {
    await api("api/sync", { method: "POST" });
    pollSync();
  } catch (e) {
    if (e.message.includes("還沒連結")) return openLogin();
    status.classList.add("error");
    status.textContent = e.message;
  }
}

async function pollSync() {
  const btn = $("#sync");
  const status = $("#sync-status");
  btn.disabled = true;
  btn.textContent = "摳摳中…";
  const s = await api("api/sync");
  if (s.running) {
    status.textContent = s.step;
    setTimeout(pollSync, 1000);
    return;
  }
  btn.disabled = false;
  btn.textContent = "摳回最新發票";
  if (s.error) {
    if (s.error_kind === "login") {
      status.textContent = "";
      openLogin("登入沒成功：手機號碼或驗證碼不對。請再確認一次。");
      return;
    }
    status.classList.add("error");
    status.textContent = `沒摳成功：${s.error}`;
    return;
  }
  if (s.result) {
    status.textContent = `已摳回 ${s.result.invoices} 張發票`;
    const meta = await api("api/meta");
    state.months = meta.months;
    await renderTrend();
    await render();
    refreshAcai();
    $("#stamp").classList.remove("press");
    void $("#stamp").offsetWidth;
    $("#stamp").classList.add("press");
  }
}

// ---- 事件 ----
$("#prev").addEventListener("click", () => go(state.months[state.months.indexOf(state.month) - 1]));
$("#next").addEventListener("click", () => go(state.months[state.months.indexOf(state.month) + 1]));
$("#sync").addEventListener("click", () => {
  if (state.demo) {
    $("#sync-status").textContent = STATIC
      ? "線上展示不會連線。在你的電腦上安裝摳摳，就能摳回自己的發票。"
      : "這是展示資料，不會連線。執行 koukou serve 就能看你自己的發票。";
    return;
  }
  if (!state.configured) openLogin();
  else startSync();
});
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = e.target.value.trim();
    render();
  }, 200);
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input")) return;
  if (e.key === "ArrowLeft") $("#prev").click();
  if (e.key === "ArrowRight") $("#next").click();
});

init().catch((e) => {
  $("#sync-status").classList.add("error");
  $("#sync-status").textContent = `載入失敗：${e.message}`;
});
