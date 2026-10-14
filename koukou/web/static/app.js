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
  if (STATIC) return staticApi(path, method);
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

let ledger;
async function staticApi(path, method) {
  ledger ??= await fetch("data/ledger.json").then((r) => r.json());
  const url = new URL(path, location.href);
  const p = url.searchParams;
  const route = url.pathname.slice(url.pathname.indexOf("api/"));
  const month = ledger.months[p.get("month") || ledger.meta.default_month];
  if (method !== "GET") throw new Error("這是線上展示，不會連線。");
  if (route === "api/meta") return ledger.meta;
  if (route === "api/monthly") return ledger.monthly;
  if (route === "api/summary") return month.summary;
  if (route === "api/merchants") return month.merchants.slice(0, Number(p.get("limit") || 8));
  if (route === "api/sync") return { running: false };
  if (route === "api/credentials") return { configured: false };
  if (route === "api/invoices") {
    const q = (p.get("q") || "").toLowerCase();
    if (!q) return month.invoices;
    return month.invoices.filter((inv) =>
      [inv.merchant, inv.seller_name, inv.inv_num, ...inv.items.map((it) => it.description)]
        .some((text) => (text || "").toLowerCase().includes(q)));
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

const state = { months: [], month: null, query: "", demo: false, configured: false };

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
  await Promise.all([renderTrend(), render()]);
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

  const [summary, invoices, shops] = await Promise.all([
    api(`api/summary?month=${month}`),
    api(`api/invoices?month=${month}&q=${encodeURIComponent(query)}`),
    api(`api/merchants?month=${month}&limit=6`),
  ]);

  $("#total").textContent = money(summary.amount);
  $("#total-sub").textContent = summary.count
    ? `${summary.count} 張發票，${summary.merchants} 家店，平均每張 ${money(summary.average)}`
    : "這個月還沒有發票";
  $("#stamp").hidden = summary.count === 0;

  renderInvoices(invoices);
  renderShops(shops);
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
    name.textContent = it.description;
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
