/* FunPay & Playerok Castor — mini app */
"use strict";
const tg = window.Telegram && Telegram.WebApp && Telegram.WebApp.initData !== undefined ? Telegram.WebApp : null;
const inTG = !!(tg && tg.initData);
if (inTG) { document.documentElement.classList.add("tg"); tg.ready(); tg.expand(); }

let TOKEN = sessionStorage.getItem("duo_t") || "";
let CFG = null, HAS = {}, STATUS = null, dirty = false, TAB = "home";
const stack = [];

const $ = s => document.querySelector(s);
const H = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = v => (Math.round((+v || 0) * 100) / 100).toLocaleString("ru-RU");
const when = t => new Date(t * 1000).toLocaleString("ru-RU", {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"});
const PN = {funpay: "FunPay", playerok: "Playerok"};

function toast(t) { const e = $("#toast"); e.textContent = t; e.classList.add("on"); clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove("on"), 2400); }
function haptic(type = "success") { try { tg && tg.HapticFeedback.notificationOccurred(type); } catch (e) {} }

async function api(path, body) {
  const r = await fetch("/api/" + path, {method: body ? "POST" : "GET",
    headers: {"Content-Type": "application/json", "X-Token": TOKEN}, body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { TOKEN = ""; sessionStorage.removeItem("duo_t"); showLogin(); throw new Error("auth"); }
  const j = await r.json().catch(() => ({error: "Сервер вернул не JSON"}));
  if (j.error) { toast(j.error); haptic("error"); }
  return j;
}

/* ───────── вход ───────── */
async function boot() {
  if (!TOKEN && inTG) {
    try {
      const j = await (await fetch("/api/auth", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({initData: tg.initData})})).json();
      if (j.token) { TOKEN = j.token; sessionStorage.setItem("duo_t", TOKEN); }
      else if (j.error) $("#lhint").textContent = j.error;
    } catch (e) {}
  }
  if (!TOKEN) return showLogin();
  $("#login").hidden = true; $("#app").hidden = false;
  ME = await api("me");
  if (can("settings")) await loadCfg(); else { CFG = {templates: ME.templates || []}; HAS = {}; }
  document.querySelectorAll(".tabs button").forEach(b => {
    const need = {chats: "chat", sales: "orders", goods: "products"}[b.dataset.tab];
    b.hidden = need ? !can(need) : false;
  });
  bindTabs(); go(TAB);
  refreshDuo();
}
function showLogin() { $("#app").hidden = true; $("#login").hidden = false; setTimeout(() => $("#pw").focus(), 50); }
async function pwLogin(ev) {
  ev.preventDefault();
  const j = await (await fetch("/api/auth", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: $("#pw").value})})).json();
  if (j.token) { TOKEN = j.token; sessionStorage.setItem("duo_t", TOKEN); boot(); }
  else $("#lerr").textContent = j.error || "Неверный пароль";
}

/* ───────── конфиг и сохранение ───────── */
async function loadCfg() { const j = await api("config"); CFG = j.config; HAS = j.has || {}; HAS.providers = j.withdraw_providers || []; setDirty(false); }
function getPath(p) { return p.split(".").reduce((o, k) => o == null ? o : o[k], CFG); }
function setPath(p, v) { const ks = p.split("."); let o = CFG; ks.slice(0, -1).forEach(k => o = o[k] = o[k] || {}); o[ks.at(-1)] = v; setDirty(true); }
function setDirty(v) {
  dirty = v;
  document.body.classList.toggle("dirty", !!v && !inTG);
  if (inTG) {
    const mb = tg.MainButton;
    if (v) { mb.setText("Сохранить изменения"); mb.show(); } else mb.hide();
  } else $("#savebar").hidden = !v;
}
if (inTG) tg.MainButton.onClick(() => saveCfg());
async function saveCfg() {
  if (inTG) tg.MainButton.showProgress();
  const j = await api("config", {config: CFG});
  if (inTG) tg.MainButton.hideProgress();
  if (j.ok) { toast("Сохранено"); haptic(); await loadCfg(); }
}

/* ───────── навигация ───────── */
function bindTabs() {
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => { stack.length = 0; go(b.dataset.tab); });
}
const TABS = {home, chats, sales, goods, more};
function go(tab) {
  TAB = tab;
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  backBtn(false);
  TABS[tab]();
  scrollTo(0, 0);
}
function push(fn, ...args) {
  stack.push([fn, args]); backBtn(true); fn(...args); scrollTo(0, 0);
}
function back() {
  stack.pop();
  if (!stack.length) return go(TAB);
  const [fn, args] = stack.at(-1); fn(...args);
}
function backBtn(show) {
  if (inTG) { show ? tg.BackButton.show() : tg.BackButton.hide(); }
}
if (inTG) tg.BackButton.onClick(back);
function view(html) { $("#view").innerHTML = html; }
function title(t) { return `<h2>${inTG || !stack.length ? "" : `<button class="btn ghost sm" onclick="back()" aria-label="Назад">‹</button> `}${H(t)}</h2>`; }

/* ───────── компоненты ───────── */
function sw(path, onchange) {
  const v = !!getPath(path);
  return `<label class="sw"><input type="checkbox" ${v ? "checked" : ""} aria-label="${H(path)}" onchange="${onchange || `setPath('${path}',this.checked)`}"><i></i></label>`;
}
function tog(path, label, sub = "", onchange) {
  return `<div class="item"><div class="tx"><b>${label}</b>${sub ? `<span>${sub}</span>` : ""}</div>${sw(path, onchange)}</div>`;
}
function fld(path, label, type = "text", note = "") {
  const v = getPath(path) ?? "";
  const id = "f_" + path.replace(/\W/g, "_");
  let ctl;
  if (type === "area") ctl = `<textarea id="${id}" onchange="setPath('${path}',this.value)">${H(v)}</textarea>`;
  else if (type === "number") ctl = `<input id="${id}" type="number" step="any" inputmode="decimal" value="${H(v)}" onchange="setPath('${path}',+this.value)">`;
  else if (type === "secret") ctl = `<input id="${id}" type="password" autocomplete="off" placeholder="${v === "***" ? "сохранено — введите новое, чтобы заменить" : ""}" value="" onchange="setPath('${path}',this.value||'***')">`;
  else ctl = `<input id="${id}" type="${type}" value="${H(v)}" onchange="setPath('${path}',this.value)">`;
  return `<div class="field"><label for="${id}">${label}</label>${ctl}${note ? `<div class="note">${note}</div>` : ""}</div>`;
}
function lines(path, label, note = "", num = false) {
  const v = (getPath(path) || []).join("\n");
  return `<div class="field"><label>${label}</label><textarea onchange="setPath('${path}',this.value.split('\\n').map(s=>s.trim()).filter(Boolean)${num ? ".map(Number)" : ""})">${H(v)}</textarea>${note ? `<div class="note">${note}</div>` : ""}</div>`;
}
function link(icon, label, sub, onclick, val = "") {
  return `<button class="item" onclick="${onclick}"><span class="ic">${icon}</span><span class="tx"><b>${label}</b>${sub ? `<span>${sub}</span>` : ""}</span>${val ? `<span class="val">${val}</span>` : ""}<span class="chev">›</span></button>`;
}
const VARS = `Переменные: $username $order_id $order_title $price $product $amount $left $date $time. Несколько вариантов через <b>|||</b> — бот выберет случайный.`;
function ask(titleText, value = "", placeholder = "") {
  return new Promise(res => {
    const d = $("#dlg");
    $("#dlgform").innerHTML = `<h4>${H(titleText)}</h4><textarea id="dlgv" placeholder="${H(placeholder)}">${H(value)}</textarea>
      <div class="row"><button class="btn ghost" value="cancel">Отмена</button><button class="btn" value="ok">Готово</button></div>`;
    d.onclose = () => res(d.returnValue === "ok" ? $("#dlgv").value : null);
    d.showModal(); setTimeout(() => $("#dlgv").focus(), 30);
  });
}
function confirmBox(text) {
  return new Promise(res => { if (inTG && tg.showConfirm) tg.showConfirm(text, ok => res(ok)); else res(confirm(text)); });
}

/* ───────── шапка: две площадки ───────── */
async function refreshDuo() {
  try { STATUS = await api("status"); } catch (e) { return; }
  const t = STATUS.stats.today, fin = can("finance");
  const half = (p, s) => {
    const dot = !s.enabled ? "" : s.ok ? "ok" : "bad";
    const big = fin ? money(t[p].sum) : t[p].count;
    const sub = fin ? `прибыль ${money(t[p].profit)} · ${t[p].count} ${plural(t[p].count, "заказ", "заказа", "заказов")}`
                    : `${plural(t[p].count, "заказ", "заказа", "заказов")} сегодня`;
    return `<button class="half ${p === "funpay" ? "fp" : "pl"} ${s.enabled ? "" : "off"}" onclick="${can("settings") ? `stack.length=0;TAB='more';go('more');push(accountsView,'${p}')` : ""}">
      <div class="name"><span class="dot ${dot}"></span>${PN[p]} <small>${H(s.username || (s.accounts ? "подключаюсь…" : "нет аккаунтов"))}</small></div>
      <div class="sum num">${big}</div><div class="sub num">${sub}</div></button>`;
  };
  $("#duo").innerHTML = half("funpay", STATUS.funpay) + half("playerok", STATUS.playerok);
}
function plural(n, a, b, c) { n = Math.abs(n) % 100; const n1 = n % 10; return n > 10 && n < 20 ? c : n1 > 1 && n1 < 5 ? b : n1 === 1 ? a : c; }

/* ───────── главная ───────── */
async function home() {
  view(`<div class="empty">Загружаю…</div>`);
  await refreshDuo();
  const s = STATUS, st = s.stats, fin = can("finance");
  const ev = can("chat") ? await api("events") : {events: []};
  const warn = [];
  const goMore = (fn, arg = "") => `TAB='more';go('more');push(${fn}${arg ? ",'" + arg + "'" : ""})`;
  if (can("settings") && !s.accounts.length) warn.push(link("⚠️", "Добавьте аккаунт площадки", "golden_key FunPay или token Playerok", goMore("accountsView", "funpay")));
  s.accounts.filter(a => a.running && !a.ok && a.error).forEach(a => warn.push(link(a.platform === "funpay" ? "🔵" : "🟢", H(a.name) + ": ошибка", H(a.error), can("settings") ? goMore("accountView", a.id) : "")));
  if (s.telegram.error && can("system")) warn.push(link("🤖", "Telegram: нет связи", H(s.telegram.error), goMore("botView")));
  if (s.supplier_pending && can("delivery")) warn.push(link("🏭", "Заказы ждут проверки у поставщика", "связь оборвалась во время покупки — проверьте вручную", goMore("suppliersView")));
  Object.entries(s.withdraw || {}).forEach(([p, w]) => { if (w.paused && can("withdraw")) warn.push(link("⏸", `Автовывод ${PN[p]} на паузе`, H(w.paused), goMore("withdrawView"))); });
  view(`
    ${warn.length ? `<div class="group">${warn.join("")}</div>` : ""}
    ${fin ? chart(st.daily) : ""}
    <div class="actions">
      ${can("settings") ? `<button class="btn ghost" onclick="act('raise')">⬆️ Поднять лоты</button>
      <button class="btn ghost" onclick="act('vacation').then(home)">🏖 Отпуск: ${s.vacation ? "вкл" : "выкл"}</button>` : ""}
      ${fin ? `<button class="btn ghost" onclick="TAB='more';go('more');push(financeView)">📈 Финансы</button><button class="btn ghost" onclick="act('report')">🌙 Отчёт в Telegram</button>` : ""}
    </div>
    ${fin ? `<h3>За период</h3><div class="group">
      ${[["week", "7 дней"], ["month", "30 дней"], ["all", "Всё время"]].map(([k, n]) => `<div class="item"><div class="tx"><b>${n}</b>
        <span>выручка ${money(st[k].total.sum)} · ${st[k].total.count} ${plural(st[k].total.count, "заказ", "заказа", "заказов")}${st[k].total.expenses ? " · расходы " + money(st[k].total.expenses) : ""}</span></div>
        <span class="val num">${money(st[k].total.profit)}</span></div>`).join("")}
      <div class="item"><div class="tx"><b>Автовыдач</b><span>поднятий лотов: ${st.raises} · возвратов: ${st.refunds}</span></div><span class="val num">${st.deliveries}</span></div></div>` : ""}
    ${can("chat") ? `<h3>Последние события</h3>
    <div class="group">${ev.events.slice(0, 20).map(evRow).join("") || `<div class="empty"><b>Пока тихо</b>Здесь появятся сообщения и заказы со всех аккаунтов.</div>`}</div>` : ""}`);
}
function evRow(e) {
  const p = `<span class="pf ${e.platform}">${e.platform === "funpay" ? "FP" : "PL"}</span>`;
  const open = e.chat_id && e.chat_id !== "None" && can("chat") ? `push(chatView,'${e.platform}','${H(e.chat_id)}','${H(e.author || e.buyer || e.chat_name || "")}','${H(e.account || "")}')` : "";
  const tag = accTag(e.account, e.platform);
  if (e.kind === "order")
    return `<button class="item" onclick="${open}"><span class="ic">💰</span><span class="tx"><b>${p} ${H(e.title)}</b><span>${H(e.buyer)}${tag} · ${when(e.time)}</span></span><span class="val num">${e.price == null ? "" : money(e.price)}</span></button>`;
  return `<button class="item" onclick="${open}"><span class="ic">${e.by_me ? "↗️" : e.system ? "ℹ️" : "💬"}</span><span class="tx"><b>${p} ${H(e.author || e.chat_name)}</b><span>${H(e.text)}${tag}</span></span><span class="val">${new Date(e.time * 1000).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"})}</span></button>`;
}
function chart(days) {
  const max = Math.max(1, ...days.map(d => d.funpay + d.playerok));
  const w = 100 / days.length, total = days.reduce((a, d) => a + d.funpay + d.playerok, 0), prof = days.reduce((a, d) => a + d.profit, 0);
  const bars = days.map((d, i) => {
    const hf = d.funpay / max * 112, hp = d.playerok / max * 112, x = i * w + w * .18, bw = w * .64;
    const py = 120 - Math.max(0, d.profit) / max * 112;
    return `<g><title>${d.date}: FunPay ${money(d.funpay)}, Playerok ${money(d.playerok)}, прибыль ${money(d.profit)}</title>
      <rect x="${x}%" y="${120 - hf - hp}" width="${bw}%" height="${hp}" rx="2" fill="var(--pl)"/>
      <rect x="${x}%" y="${120 - hf}" width="${bw}%" height="${hf}" rx="2" fill="var(--fp)"/>
      ${d.funpay + d.playerok ? `<rect x="${x - w * .06}%" y="${py - 1}" width="${bw + w * .12}%" height="2.4" rx="1" fill="var(--text)" opacity=".75"/>` : `<rect x="${x}%" y="118" width="${bw}%" height="2" rx="1" fill="var(--line)"/>`}</g>`;
  }).join("");
  return `<div class="chart"><div class="head"><b>14 дней · выручка <span class="num">${money(total)}</span> · прибыль <span class="num">${money(prof)}</span></b></div>
    <div class="legend" style="margin-bottom:6px"><span><i style="background:var(--fp)"></i>FunPay</span><span><i style="background:var(--pl)"></i>Playerok</span><span><i style="background:var(--text);opacity:.75"></i>прибыль</span></div>
    <svg viewBox="0 0 100 122" preserveAspectRatio="none" role="img" aria-label="Выручка и прибыль по дням">${bars}</svg>
    <div class="x"><span>${days[0].date}</span><span>${days[Math.floor(days.length / 2)].date}</span><span>сегодня</span></div></div>`;
}
async function act(a, extra = {}) { const j = await api("action", {action: a, ...extra}); if (j.message) toast(j.message); haptic(); return j; }

/* ───────── чаты ───────── */
let CHAT_P = "funpay", CHAT_ACC = "";
async function chats() {
  const accs = (ME.accounts || []).filter(a => a.platform === CHAT_P);
  const accSel = accs.length > 1 ? `<select style="margin-bottom:12px" onchange="CHAT_ACC=this.value;chats()">${accs.map(a => `<option value="${a.id}" ${a.id === CHAT_ACC ? "selected" : ""}>${H(a.name)}</option>`).join("")}</select>` : "";
  const top = `${seg([["funpay", "FunPay"], ["playerok", "Playerok"]], CHAT_P, "CHAT_P=v;CHAT_ACC='';chats()")}${accSel}`;
  view(top + `<div class="empty">Загружаю чаты…</div>`);
  const j = await api(`chats?platform=${CHAT_P}&account=${CHAT_ACC}`);
  if (j.account) CHAT_ACC = j.account;
  view(top + `<div class="group">${j.chats.map(c => `<button class="item" onclick="push(chatView,'${CHAT_P}','${H(c.id)}','${H(c.name)}','${H(j.account || "")}')">
      <span class="ic">${c.unread ? "🔴" : "💬"}</span><span class="tx"><b>${H(c.name || c.id)}</b><span>${H(c.last_text)}</span></span><span class="chev">›</span></button>`).join("")
      || `<div class="empty"><b>Чатов нет</b>${H(j.error || "Когда покупатели напишут, чаты появятся здесь.")}</div>`}</div>`);
}
function seg(opts, cur, onpick) {
  return `<div class="seg" role="tablist">${opts.map(([v, n]) => `<button role="tab" aria-selected="${v === cur}" class="${v === cur ? "on" : ""}" onclick="(v=>{${onpick}})('${v}')">${n}</button>`).join("")}</div>`;
}
async function chatView(p, id, name, acc = "") {
  view(`${title(name || "Чат")}<div class="empty">Загружаю переписку…</div>`);
  const j = await api(`chat?platform=${p}&id=${encodeURIComponent(id)}&account=${acc}`);
  acc = acc || j.account || "";
  const cust = can("customers") ? (await api("customers?q=" + encodeURIComponent(name || ""))).customers.find(c => c.name === name && c.platform === p) : null;
  const url = p === "funpay" ? `https://funpay.com/chat/?node=${encodeURIComponent(id)}` : `https://playerok.com/chats/${encodeURIComponent(id)}`;
  view(`${title(name || "Чат")}
    <div class="row" style="margin:-4px 4px 10px"><span class="pf ${p}">${PN[p]}${accTag(acc, p)}</span>
      ${cust ? `<span class="hint">⭐ ${cust.orders} ${plural(cust.orders, "заказ", "заказа", "заказов")}${cust.spent != null ? " · " + money(cust.spent) : ""}${cust.note ? " · 📝 " + H(cust.note) : ""}</span>` : ""}
      <a class="hint" href="${url}" target="_blank" rel="noopener">открыть на сайте</a></div>
    <div class="chatview" id="msgs">${(j.messages || []).map(m => `<div class="msg ${m.system ? "sys" : m.by_me ? "me" : "them"}">${m.by_me || m.system ? "" : `<small>${H(m.author)}</small>`}${H(m.text)}</div>`).join("")
      || `<div class="empty">Сообщений нет</div>`}</div>
    <div class="chips">${(CFG.templates || []).map((t, i) => `<button onclick="$('#reply').value=CFG.templates[${i}];$('#reply').focus()">${H(t.slice(0, 34))}</button>`).join("")}</div>
    <div class="composer"><textarea id="reply" placeholder="Сообщение…" aria-label="Сообщение"></textarea>
      <button class="btn" onclick="sendReply('${p}','${H(id)}','${H(name)}','${H(acc)}')">Отправить</button></div>`);
  const box = $("#msgs"); box.lastElementChild && box.lastElementChild.scrollIntoView({block: "end"});
}
async function sendReply(p, id, name, acc) {
  const t = $("#reply").value.trim(); if (!t) return;
  const j = await api("send", {platform: p, chat_id: id, text: t, account: acc});
  if (j.ok) { toast("Отправлено"); haptic(); chatView(p, id, name, acc); } else toast("Не отправилось — проверьте подключение аккаунта");
}
/* ───────── продажи и покупатели ───────── */
let SALES_MODE = "orders", SALES_Q = "", SALES_P = "";
async function sales() {
  const fin = can("finance");
  const modes = [["orders", "Заказы"]]; if (can("customers")) modes.push(["customers", "Покупатели"]);
  const head = `${modes.length > 1 ? seg(modes, SALES_MODE, "SALES_MODE=v;sales()") : ""}
    <div class="row" style="margin-bottom:12px"><input id="sq" type="search" placeholder="${SALES_MODE === "orders" ? "Товар, покупатель или номер" : "Ник или заметка"}" value="${H(SALES_Q)}"
      onkeydown="if(event.key==='Enter'){SALES_Q=this.value;sales()}" style="flex:1">
      ${SALES_MODE === "orders" ? `<select style="width:auto" onchange="SALES_P=this.value;sales()"><option value="">Все</option><option value="funpay" ${SALES_P === "funpay" ? "selected" : ""}>FunPay</option><option value="playerok" ${SALES_P === "playerok" ? "selected" : ""}>Playerok</option></select>` : ""}</div>`;
  if (SALES_MODE === "orders") {
    const j = await api(`orders?q=${encodeURIComponent(SALES_Q)}&platform=${SALES_P}`);
    window._orders = j.orders;
    const live = j.orders.filter(o => !o.refunded);
    const sum = live.reduce((a, o) => a + (o.price || 0), 0), prof = live.reduce((a, o) => a + (o.profit || 0), 0);
    view(head + (fin ? `<p class="hint" style="margin:0 6px 8px">Найдено ${j.orders.length} · выручка <span class="num">${money(sum)}</span> · прибыль <span class="num">${money(prof)}</span></p>` : "")
      + `<div class="group">${j.orders.map((o, i) => `<button class="item" onclick="orderSheet(${i})"><span class="ic">${o.refunded ? "↩️" : o.delivered ? "📦" : "🧾"}</span><div class="tx">
        <b><span class="pf ${o.p}">${o.p === "funpay" ? "FP" : "PL"}</span> ${H(o.title)}</b><span>${H(o.buyer)}${accTag(o.a, o.p)} · #${H(o.id)} · ${when(o.t)}${o.refunded ? " · возврат" : ""}${fin && o.cost == null && !o.refunded ? " · ⚠️ нет себестоимости" : ""}</span></div>
        ${fin ? `<span class="val num">${money(o.price)}<br><small style="color:${o.profit >= 0 ? "var(--pl)" : "var(--danger)"}">${o.profit >= 0 ? "+" : ""}${money(o.profit)}</small></span>` : ""}</button>`).join("")
        || `<div class="empty"><b>Заказов нет</b>Новые заказы со всех аккаунтов попадут сюда.</div>`}</div>`);
  } else {
    const j = await api("customers?q=" + encodeURIComponent(SALES_Q));
    view(head + `<div class="group">${j.customers.map(c => `<button class="item" onclick="editNote('${c.platform}','${H(c.name)}','${H(c.note || "")}')">
      <span class="ic">${c.orders > 1 ? "⭐" : "👤"}</span><span class="tx"><b><span class="pf ${c.platform}">${c.platform === "funpay" ? "FP" : "PL"}</span> ${H(c.name)}</b>
      <span>${c.orders} ${plural(c.orders, "заказ", "заказа", "заказов")} · последний ${when(c.last)}${c.note ? " · 📝 " + H(c.note) : ""}</span></span>${c.spent != null ? `<span class="val num">${money(c.spent)}</span>` : ""}</button>`).join("")
      || `<div class="empty"><b>Покупателей пока нет</b>База пополняется автоматически с каждым заказом.</div>`}</div>
      <p class="hint" style="margin:0 6px">Нажмите на покупателя, чтобы оставить заметку — она будет приходить вместе с его сообщениями в Telegram.</p>`);
  }
}
async function editNote(p, name, cur) {
  const v = await ask("Заметка о покупателе " + name, cur, "Например: просит выдачу вечером");
  if (v === null) return;
  await api("customers/note", {platform: p, name, note: v}); toast("Заметка сохранена"); sales();
}

/* ───────── автовыдача и товары ───────── */
async function goods() {
  const pr = (await api("products")).products;
  const rules = can("settings") ? CFG.delivery : [];
  const modeTxt = r => r.mode === "text" ? "одинаковый текст" : r.mode === "supplier" ? "поставщик " + H(supName(r.supplier)) : H(r.file) + " · осталось " + (pr[r.file] ?? 0);
  view(`${title("Автовыдача")}
    ${can("settings") ? `<div class="group">${rules.map((r, i) => link(r.enabled === false ? "⏸" : r.mode === "supplier" ? "🏭" : "📦", H(r.keyword || "без ключевых слов"),
      `${modeTxt(r)}${r.lots && r.lots.length ? " · лотов: " + r.lots.length : ""}`, `push(ruleView,${i})`)).join("")
      || `<div class="empty"><b>Правил пока нет</b>Правило связывает название заказа с товаром: файлом, текстом или поставщиком. После оплаты бот сам отправит товар.</div>`}
      ${can("delivery") ? `<button class="item" onclick="addRule()"><span class="ic">＋</span><span class="tx"><b>Добавить правило</b></span></button>` : ""}</div>
    <h3>Проверка</h3>
    <div class="group"><div class="field"><label for="tt">Название заказа — какое правило сработает?</label>
      <div class="row"><input id="tt" placeholder="Например: Steam ключ GTA V" style="flex:1"><button class="btn sm" onclick="act('test_delivery',{title:$('#tt').value})">Проверить</button></div></div></div>` : ""}
    <h3>Файлы товаров</h3>
    <div class="group">${Object.entries(pr).map(([f, n]) => link(n ? "🗂" : "📭", H(f), "одна строка — один товар", `push(productsView,'${H(f)}')`, n + " шт")).join("")
      || `<div class="empty">Файлы создаются, когда вы добавляете товары.</div>`}
      <button class="item" onclick="newFile()"><span class="ic">＋</span><span class="tx"><b>Новый файл</b></span></button></div>`);
}
function addRule() {
  CFG.delivery.push({keyword: "", mode: "file", file: "products.txt", text: "Спасибо за покупку, $username!\n\nВаш товар:\n$product",
    per_amount: true, per_unit: 1, enabled: true, platforms: ["funpay", "playerok"], out_of_stock_text: "", cost: 0, lots: [], accounts: []});
  setDirty(true); push(ruleView, CFG.delivery.length - 1);
}
async function newFile() { const n = await ask("Имя файла", "products.txt"); if (n) push(productsView, n.endsWith(".txt") ? n : n + ".txt"); }
function ruleView(i) {
  const r = CFG.delivery[i]; if (!r) return back();
  const P = `delivery.${i}`;
  r.lots = r.lots || [];
  const sups = CFG.suppliers || [];
  view(`${title("Правило выдачи")}
    <div class="group">${tog(P + ".enabled", "Правило включено")}
      ${fld(P + ".keyword", "Ключевые слова в названии заказа", "text", "Через | — сработает, если в названии есть любое из слов. Регистр не важен.")}
      ${tog(P + ".match_all", "Нужны все слова сразу", "вместо любого из них")}
      <div class="field"><label>Что выдавать</label><select onchange="setPath('${P}.mode',this.value);ruleView(${i})">
        <option value="file" ${r.mode === "file" || !r.mode ? "selected" : ""}>Строки из файла (каждому — своя)</option>
        <option value="text" ${r.mode === "text" ? "selected" : ""}>Один и тот же текст всем</option>
        <option value="supplier" ${r.mode === "supplier" ? "selected" : ""}>Купить у поставщика по API</option></select></div>
      ${r.mode === "supplier" ? `<div class="field"><label>Поставщик</label><select onchange="setPath('${P}.supplier',this.value)"><option value="">— выберите —</option>
          ${sups.map(s => `<option value="${H(s.id)}" ${s.id === r.supplier ? "selected" : ""}>${H(s.name || s.id)}</option>`).join("")}</select>
          <div class="note">${sups.length ? "" : "Сначала добавьте поставщика: Ещё → Поставщики. "}Количество берётся из заказа, если включено ниже.</div></div>
        ${fld(P + ".product_id", "ID товара у поставщика", "text", "Подставляется в запрос как $product_id")}
        ${fld(P + ".fallback_file", "Запасной файл товаров", "text", "Если поставщик не ответил — выдать отсюда. Пусто — только уведомить вас.")}
        ${tog(P + ".per_amount", "Учитывать количество в заказе")}` : ""}
      ${r.mode === "file" || !r.mode ? fld(P + ".file", "Файл товаров") + tog(P + ".per_amount", "Учитывать количество в заказе", "купили 3 шт. — выдать 3 строки")
        + fld(P + ".per_unit", "Строк на одну единицу", "number", "Например, 2, если товар — логин и пароль на разных строках") : ""}
      ${fld(P + ".text", "Сообщение покупателю", "area", VARS)}
      ${fld(P + ".out_of_stock_text", "Сообщение, если товар закончился", "area", "Оставьте пустым, чтобы молча уведомить только вас.")}
      <div class="item"><div class="tx"><b>Площадки</b></div>${platPick("delivery", i)}</div>
      ${(ME.accounts || []).length > 1 ? `<div class="field"><label>Только для аккаунтов</label><div class="row">${ME.accounts.map(a => `<label class="row" style="gap:4px"><input type="checkbox" style="width:auto"
        ${(r.accounts || []).includes(a.id) ? "checked" : ""} onchange="togAcc(${i},'${a.id}',this.checked)"> ${H(a.name)}</label>`).join("")}</div><div class="note">Ничего не отмечено — для всех аккаунтов.</div></div>` : ""}</div>
    ${can("finance") ? `<h3>Деньги</h3><div class="group">${fld(P + ".cost", "Себестоимость одной единицы", "number", r.mode === "supplier" ? "Если поставщик сам возвращает цену (cost_path), используется она." : "Сколько вам стоит товар — для подсчёта прибыли.")}</div>` : ""}
    ${r.mode !== "supplier" && r.mode !== "text" ? `<h3>Лоты FunPay</h3><div class="group">
      ${r.lots.map((l, k) => `<div class="item"><span class="ic">🏷</span><div class="tx"><b>${H(l.title || l.id)}</b><span>${H(accName(l.account))} · #${H(l.id)}</span></div>
        <button class="btn danger sm" aria-label="Отвязать" onclick="CFG.delivery[${i}].lots.splice(${k},1);setDirty(true);ruleView(${i})">✕</button></div>`).join("")
        || `<div class="item"><div class="tx"><span>Привяжите лоты, которые продают этот товар: когда файл опустеет, бот предложит их скрыть, а после пополнения — включить.</span></div></div>`}
      <button class="item" onclick="push(lotsPicker,${i})"><span class="ic">＋</span><span class="tx"><b>Привязать лоты</b></span></button></div>` : ""}
    <div class="row">${r.mode === "file" || !r.mode ? `<button class="btn ghost" onclick="push(productsView,'${H(r.file)}')">🗂 Товары в файле</button>` : ""}
      <button class="btn danger" onclick="confirmBox('Удалить правило?').then(ok=>{if(ok){CFG.delivery.splice(${i},1);setDirty(true);back()}})">Удалить правило</button></div>`);
}
function platPick(list, i) {
  const has = p => (CFG[list][i].platforms || []).includes(p);
  return `<div class="row">${["funpay", "playerok"].map(p => `<label class="row" style="gap:4px"><input type="checkbox" style="width:auto" ${has(p) ? "checked" : ""}
    onchange="togPlat('${list}',${i},'${p}',this.checked)"> ${PN[p]}</label>`).join("")}</div>`;
}
function togPlat(list, i, p, on) { const a = new Set(CFG[list][i].platforms || []); on ? a.add(p) : a.delete(p); CFG[list][i].platforms = [...a]; setDirty(true); }
async function productsView(f) {
  const j = await api("products?file=" + encodeURIComponent(f));
  const full = can("delivery");
  view(`${title(f)}
    <div class="group"><div class="field"><label for="pt">${full ? `Товары · сейчас ${j.count} шт` : `Добавить товары · в файле сейчас ${j.count} шт`}</label>
      <textarea id="pt" class="code" placeholder="Каждая строка — один товар">${full ? H(j.content) : ""}</textarea>
      <div class="note">${full ? "Пустые строки не считаются. Выданные товары удаляются из файла и записываются в журнал выдач." : "Новые строки добавятся в конец файла. Существующие товары менеджеру не показываются."}</div></div></div>
    <div class="row"><button class="btn" onclick="saveProducts('${H(f)}')">${full ? "Сохранить товары" : "Добавить в файл"}</button>
      ${full ? `<button class="btn ghost" onclick="dedupe()">Убрать дубли</button>
      <button class="btn danger" onclick="confirmBox('Удалить файл ${H(f)}?').then(ok=>ok&&api('products/delete',{file:'${H(f)}'}).then(back))">Удалить файл</button>` : ""}</div>`);
}
function dedupe() { const t = $("#pt"); const a = [...new Set(t.value.split("\n").map(s => s.trim()).filter(Boolean))]; t.value = a.join("\n"); toast("Осталось " + a.length + " уникальных"); }
async function saveProducts(f) {
  const j = await api("products", {file: f, content: $("#pt").value, append: !can("delivery")});
  if (j.ok) { toast("Готово: в файле " + j.count + " шт"); haptic(); if (!can("delivery")) $("#pt").value = ""; }
}
/* ───────── ещё ───────── */
function more() {
  const pc = CFG.telegram ? CFG.telegram.proxy : {enabled: false};
  const g = (h, items) => { const x = items.filter(Boolean); return x.length ? `<h3>${h}</h3><div class="group">${x.join("")}</div>` : ""; };
  const accN = p => (CFG.accounts && CFG.accounts[p] || []).length;
  const wmOn = CFG.general && CFG.general.watermark_enabled !== false;
  view(`<p class="hint" style="margin:0 6px 4px">Вы вошли как <b>${H(ME.role_name)}</b>${ME.role === "manager" ? ". Вам доступны чаты, заказы без сумм, пополнение товаров и заметки о покупателях. Настройки меняют владелец и администратор." : ""}</p>` +
    g("Площадки и аккаунты", [
      can("settings") && link("🔵", "FunPay", `${accN("funpay")} ${plural(accN("funpay"), "аккаунт", "аккаунта", "аккаунтов")} · ${CFG.funpay.enabled ? "вкл" : "выкл"}`, "push(accountsView,'funpay')"),
      can("settings") && link("🟢", "Playerok", `${accN("playerok")} ${plural(accN("playerok"), "аккаунт", "аккаунта", "аккаунтов")} · ${CFG.playerok.enabled ? "вкл" : "выкл"}`, "push(accountsView,'playerok')")]) +
    g("Деньги", [
      can("finance") && link("📈", "Финансы и прибыль", "выручка, себестоимость, комиссии, расходы", "push(financeView)"),
      can("withdraw") && link("💸", "Автовывод средств", ["funpay", "playerok"].map(p => PN[p] + ": " + ({off: "выкл", ask: "спрашивать", auto: "авто"})[CFG.withdraw[p].mode]).join(" · "), "push(withdrawView)")]) +
    g("Общение с покупателями", [
      can("settings") && link("✍️", "Подпись в чатах", wmOn ? H(CFG.general.watermark || (ME.app && ME.app.default_watermark)) : "выключена", "push(watermarkView)"),
      can("settings") && link("🤖", "Автоответы на команды", `${CFG.commands.length} ${plural(CFG.commands.length, "команда", "команды", "команд")}`, "push(commandsView)"),
      can("settings") && link("📝", "Шаблоны быстрых ответов", `${CFG.templates.length} шт`, "push(templatesView)"),
      can("settings") && link("🕘", "Рабочее время и отпуск", CFG.features.vacation.enabled ? "сейчас в отпуске" : CFG.general.work_hours.enabled ? CFG.general.work_hours.from + "–" + CFG.general.work_hours.to : "круглосуточно", "push(scheduleView)"),
      can("settings") && link("🚫", "Чёрный список", `${CFG.blacklist.users.length}`, "push(blacklistView)")]) +
    g("Автоматизация", [
      can("delivery") && can("settings") && link("🏭", "Поставщики (API)", `${(CFG.suppliers || []).length} подключено`, "push(suppliersView)"),
      can("settings") && link("✨", "Функции", "лоты без товара, напоминания, отчёты, антифлуд, вебхуки", "push(featuresView)"),
      can("plugins") && link("🧩", "Плагины", "Cardinal, Castor, Playerok Universal", "push(pluginsView)")]) +
    g("Telegram и команда", [
      can("system") && link("🤖", "Бот и прокси", `${STATUS && STATUS.telegram.username ? "@" + H(STATUS.telegram.username) : "бот не подключён"} · ${pc.enabled ? "прокси вкл" : "без прокси"}`, "push(botView)"),
      can("settings") && link("🔔", "Уведомления", "что присылать в Telegram", "push(notifyView)"),
      can("users") && link("👨‍💼", "Команда и роли", "владельцы, администраторы, менеджеры", "push(teamView)"),
      can("system") && link("🔐", "Доступ и веб-панель", "пароли, адрес мини-аппа, туннель", "push(accessView)")]) +
    g("Система", [
      can("secrets") && link("💾", "Бекапы", "конфиг, товары, статистика, плагины", "push(backupsView)"),
      can("system") && link("📜", "Логи", "", "push(logsView)"),
      can("secrets") && link("🧾", "Весь конфиг (JSON)", "для опытных", "push(jsonView)"),
      can("system") && `<button class="item" onclick="confirmBox('Перезапустить бота?').then(ok=>ok&&act('restart').then(()=>setTimeout(()=>location.reload(),6000)))"><span class="ic">🔄</span><span class="tx"><b>Перезапустить бота</b><span>${STATUS ? "v" + STATUS.version + " · работает " + STATUS.uptime : ""}</span></span></button>`]) +
    g("Проект", [link(`<img src="logo.svg" alt="" width="30" height="30" style="border-radius:9px">`, "О боте", `${H((ME.app || {}).name || "")} v${H((ME.app || {}).version || "")}`, "push(aboutView)")]));
}
function platformView(p) {
  const fp = p === "funpay";
  view(`${title("Настройки " + PN[p])}<p class="hint" style="margin:0 6px 10px">Общие для всех аккаунтов ${PN[p]}.</p>
    <h3>Автоматизация</h3><div class="group">
      ${tog(p + ".auto_delivery", "Автовыдача товаров")}${tog(p + ".auto_reply", "Автоответы на команды")}
      ${fp ? tog("funpay.auto_raise.enabled", "Автоподнятие лотов", "бот сам ждёт, когда FunPay разрешит поднять снова") + fld("funpay.auto_raise.check_every_min", "Проверять поднятие каждые, мин", "number")
           + tog("funpay.auto_refund_no_stock", "Возврат денег, если товар закончился") + fld("funpay.poll_interval", "Проверять новые сообщения каждые, сек", "number")
           + fld("funpay.user_agent", "User-Agent по умолчанию")
           : tog("playerok.mark_sent", "Отмечать «товар отправлен» после выдачи") + fld("playerok.user_agent", "User-Agent по умолчанию")}</div>
    <h3>Сообщения</h3><div class="group">
      ${tog(p + ".greeting.enabled", "Приветствие в новых чатах")}${fld(p + ".greeting.text", "Текст приветствия", "area")}
      ${fld(p + ".greeting.cooldown_days", "Здороваться снова через, дней", "number")}
      ${tog(p + ".order_text.enabled", "Сообщение сразу после оплаты")}${fld(p + ".order_text.text", "Текст после оплаты", "area")}
      ${tog(p + ".confirm_reply.enabled", "Благодарность за подтверждение")}${fld(p + ".confirm_reply.text", "Текст благодарности", "area", VARS)}</div>
    ${fp ? `<h3>Ответы на отзывы</h3><div class="group">${tog("funpay.review_reply.enabled", "Отвечать на отзывы автоматически")}
      ${[5, 4, 3, 2, 1].map(n => fld("funpay.review_reply.texts." + n, "⭐".repeat(n), "area")).join("")}</div>` : ""}
    ${fp && STATUS && STATUS.funpay.raise.length ? `<h3>Поднятие лотов</h3><div class="group">${STATUS.funpay.raise.map(x => `<div class="item"><div class="tx"><span>${H(x)}</span></div></div>`).join("")}</div>` : ""}`);
}
function commandsView() {
  view(`${title("Автоответы")}<p class="hint" style="margin:0 6px 10px">Покупатель пишет команду — бот отвечает. Можно указать несколько триггеров через |.</p>
    <div class="group">${CFG.commands.map((c, i) => link(c.enabled === false ? "⏸" : c.notify ? "🔔" : "💬", H(c.trigger), H((c.text || "").slice(0, 70)), `push(commandView,${i})`)).join("")}
      <button class="item" onclick="CFG.commands.push({trigger:'!новая',text:'',match:'exact',platforms:['funpay','playerok'],notify:false,enabled:true});setDirty(true);push(commandView,CFG.commands.length-1)"><span class="ic">＋</span><span class="tx"><b>Добавить команду</b></span></button></div>`);
}
function commandView(i) {
  const c = CFG.commands[i]; if (!c) return back();
  const P = "commands." + i;
  if (c.enabled === undefined) c.enabled = true;
  view(`${title(c.trigger || "Команда")}<div class="group">${tog(P + ".enabled", "Включена")}
    ${fld(P + ".trigger", "Триггеры", "text", "Например: !цена|!прайс|сколько стоит")}
    <div class="field"><label>Как сравнивать</label><select onchange="setPath('${P}.match',this.value)">
      <option value="exact" ${c.match !== "contains" ? "selected" : ""}>Сообщение целиком совпадает</option>
      <option value="contains" ${c.match === "contains" ? "selected" : ""}>Сообщение содержит слово</option></select></div>
    ${fld(P + ".text", "Ответ", "area", VARS)}
    ${tog(P + ".notify", "Уведомить меня в Telegram", "удобно для «!продавец»")}
    <div class="item"><div class="tx"><b>Площадки</b></div>${platPick("commands", i)}</div></div>
    <button class="btn danger" onclick="confirmBox('Удалить команду?').then(ok=>{if(ok){CFG.commands.splice(${i},1);setDirty(true);back()}})">Удалить команду</button>`);
}
function templatesView() {
  view(`${title("Шаблоны ответов")}<div class="group">${lines("templates", "Каждая строка — отдельный шаблон", "Шаблоны появляются кнопками в чатах мини-аппа и в Telegram под каждым сообщением покупателя.")}</div>`);
}
function scheduleView() {
  view(`${title("Рабочее время и отпуск")}
    <div class="group">${tog("general.work_hours.enabled", "Учитывать рабочее время")}${fld("general.work_hours.from", "Начало", "time")}${fld("general.work_hours.to", "Конец", "time")}
      ${fld("general.work_hours.away_text", "Ответ в нерабочее время", "area", "Отправляется один раз в сутки на чат.")}</div>
    <h3>Отпуск</h3><div class="group">${tog("features.vacation.enabled", "Я в отпуске")}${fld("features.vacation.until", "До даты", "date", "В этот день режим выключится сам.")}
      ${tog("features.vacation.disable_delivery", "Остановить автовыдачу на время отпуска")}${fld("features.vacation.text", "Ответ покупателям", "area", "$vacation_until — дата окончания.")}</div>`);
}
function blacklistView() {
  view(`${title("Чёрный список")}<div class="group">${lines("blacklist.users", "Ники покупателей, каждый с новой строки")}
    ${tog("blacklist.block_reply", "Не отвечать им автоматически")}${tog("blacklist.block_delivery", "Не выдавать товар автоматически")}</div>`);
}
function featuresView() {
  const F = "features.";
  const sel = (path, opts) => `<select onchange="setPath('${path}',this.value)">${opts.map(([v, n]) => `<option value="${v}" ${getPath(path) === v ? "selected" : ""}>${n}</option>`).join("")}</select>`;
  view(`${title("Функции")}
    <h3>Лоты без товара</h3><div class="group">
      <div class="field"><label>Когда файл товаров опустел</label>${sel(F + "out_of_stock.mode", [["ask", "Спросить меня: скрыть привязанные лоты?"], ["auto", "Скрыть привязанные лоты сразу"], ["notify", "Только сообщить"], ["off", "Ничего не делать"]])}
        <div class="note">Работает для правил с привязанными лотами FunPay (Выдача → правило → Лоты FunPay). Playerok — только уведомление.</div></div>
      <div class="field"><label>Когда товар пополнили</label>${sel(F + "out_of_stock.restore", [["ask", "Спросить: включить скрытые лоты?"], ["auto", "Включить сразу"], ["off", "Не трогать"]])}</div></div>
    <div class="group">${tog(F + "returning_buyer.enabled", "Постоянные покупатели", "отдельное сообщение при повторном заказе")}
      ${fld(F + "returning_buyer.text", "Текст для постоянных", "area", "$orders_count — номер заказа покупателя.")}</div>
    <div class="group">${tog(F + "confirm_reminder.enabled", "Напоминать подтвердить заказ")}
      ${fld(F + "confirm_reminder.after_hours", "Через сколько часов после оплаты", "number")}${fld(F + "confirm_reminder.text", "Текст напоминания", "area")}</div>
    <div class="group">${tog(F + "daily_report.enabled", "Итоги дня в Telegram")}${fld(F + "daily_report.time", "Время отчёта", "time")}</div>
    <div class="group">${tog(F + "low_stock.enabled", "Предупреждать, когда товар заканчивается")}${fld(F + "low_stock.threshold", "Порог, шт", "number")}</div>
    <div class="group">${tog(F + "anti_flood.enabled", "Антифлуд", "защищает от зацикливания с другим ботом и от спама")}${fld(F + "anti_flood.max_replies_per_min", "Не больше автоответов в минуту на чат", "number")}</div>
    <div class="group">${tog(F + "auto_backup.enabled", "Автобекап раз в сутки")}${fld(F + "auto_backup.keep", "Сколько бекапов хранить", "number")}</div>
    <div class="group">${tog(F + "webhooks.enabled", "Вебхуки", "дублировать события в Discord или свой сервер")}
      ${lines(F + "webhooks.urls", "Адреса, каждый с новой строки", "Для Discord вставьте ссылку вебхука канала.")}
      <div class="item"><div class="tx"><b>События</b></div><div class="row">${["order", "message", "delivery"].map(e => `<label class="row" style="gap:4px"><input type="checkbox" style="width:auto"
        ${(CFG.features.webhooks.events || []).includes(e) ? "checked" : ""} onchange="togEv('${e}',this.checked)"> ${({order: "заказы", message: "сообщения", delivery: "выдачи"})[e]}</label>`).join("")}</div></div></div>`);
}
function togEv(e, on) { const a = new Set(CFG.features.webhooks.events || []); on ? a.add(e) : a.delete(e); CFG.features.webhooks.events = [...a]; setDirty(true); }

/* бот и прокси — применяются сразу */
let PROXY_RES = {};
async function botView() {
  const px = await api("proxy");
  const t = px.telegram, pc = CFG.telegram.proxy;
  Object.assign(PROXY_RES, px.results || {});
  view(`${title("Бот и прокси")}
    <div class="group"><div class="item"><span class="ic">🤖</span><div class="tx"><b>${t.username ? "@" + H(t.username) : "Бот не подключён"}</b>
      <span>${t.ok ? "на связи" : H(t.error || "нет связи")} · ${H(px.label)}</span></div><span class="dot ${t.ok ? "ok" : "bad"}"></span></div>
${can("secrets") ? `      <div class="field"><label for="ntok">Токен бота от @BotFather</label><input id="ntok" type="password" autocomplete="off" placeholder="${HAS.tg_token ? "сохранён — вставьте новый, чтобы сменить бота" : "123456789:AA…"}">
        <div class="note">Смена бота происходит сразу, без перезапуска. Потом напишите новому боту /start — иначе он не сможет присылать вам уведомления.</div>
        <div class="row" style="margin-top:8px"><button class="btn ghost sm" onclick="testToken()">Проверить токен</button><button class="btn sm" onclick="applyToken()">Подключить бота</button></div></div>` : `<div class="field"><div class="note">Сменить бота может только владелец.</div></div>`}
      ${tog("telegram.enabled", "Telegram-бот включён", "", "setPath('telegram.enabled',this.checked);applyTG({enabled:this.checked})")}</div>
    <h3>Прокси для Telegram</h3>
    <div class="group">
      ${tog("telegram.proxy.enabled", "Использовать прокси", "если api.telegram.org недоступен", "pc().enabled=this.checked;applyTG()")}
      ${tog("telegram.proxy.failover", "Автопереключение", "если прокси умер — бот сам возьмёт следующий рабочий", "pc().failover=this.checked;applyTG()")}
      ${tog("telegram.proxy.allow_direct", "Пробовать напрямую, если все прокси упали", "", "pc().allow_direct=this.checked;applyTG()")}
      ${pc.list.map((p, i) => { const r = PROXY_RES[p]; const cur = p === px.current;
        return `<div class="item"><span class="ic">${r ? (r.ok ? "✅" : "❌") : "▫️"}</span><div class="tx"><b>${H(label(p))}${cur ? " · сейчас" : ""}</b>
          <span>${r ? (r.ok ? r.info + " мс" : H(r.info)) : "не проверялся"}</span></div>
          <div class="row">${i ? `<button class="btn ghost sm" aria-label="Сделать первым" onclick="moveProxy(${i})">↑</button>` : ""}
          <button class="btn ghost sm" onclick="useProxy(${i})">Использовать</button><button class="btn danger sm" aria-label="Удалить" onclick="delProxy(${i})">✕</button></div></div>`; }).join("")}
      <div class="field"><label for="npx">Добавить прокси</label><div class="row"><input id="npx" placeholder="socks5://user:pass@1.2.3.4:1080" style="flex:1"><button class="btn sm" onclick="addProxy()">Добавить</button></div>
        <div class="note">Форматы: socks5://user:pass@ip:port · http://ip:port · ip:port:user:pass. MTProto-прокси Bot API не поддерживает. Можно вставить сразу несколько, каждый с новой строки.</div></div></div>
    <div class="row"><button class="btn ghost" onclick="testAll()">Проверить все</button><button class="btn" onclick="applyTG()">Переподключиться</button></div>`);
}
const pc = () => CFG.telegram.proxy;
function label(p) { return (p.includes("://") ? p.split("://")[1] : p).split("@").pop(); }
async function testToken() {
  const tok = $("#ntok").value.trim();
  const j = await api("telegram/test_token", {token: tok}); if (j.ok) { toast("Токен рабочий: @" + j.username); haptic(); }
}
async function applyToken() {
  const tok = $("#ntok").value.trim(); if (!tok) return toast("Вставьте токен");
  const chk = await api("telegram/test_token", {token: tok}); if (!chk.ok) return;
  toast("Подключаю @" + chk.username + "…");
  await applyTG({token: tok});
  await loadCfg();
}
async function applyTG(extra = {}) {
  const j = await api("telegram/apply", {proxy: CFG.telegram.proxy, ...extra});
  if (j.message) toast((j.ok ? "Готово: " : "Не удалось: ") + j.message);
  if (j.ok) haptic(); else haptic("error");
  await loadCfg(); botView();
}
async function addProxy() {
  const v = $("#npx").value.split("\n").map(s => s.trim()).filter(Boolean); if (!v.length) return;
  const j = await api("proxy/test", {list: v, direct: false});
  j.results.forEach(r => PROXY_RES[r.proxy] = r);
  pc().list.push(...v); pc().enabled = true;
  await applyTG();
}
async function testAll() {
  toast("Проверяю…");
  const j = await api("proxy/test", {list: pc().list});
  j.results.forEach(r => PROXY_RES[r.proxy] = r);
  const d = j.results.find(r => !r.proxy);
  toast(`Рабочих: ${j.results.filter(r => r.ok && r.proxy).length} из ${pc().list.length}${d ? " · напрямую: " + (d.ok ? "есть связь" : "нет") : ""}`);
  botView();
}
function moveProxy(i) { const l = pc().list; l.unshift(l.splice(i, 1)[0]); applyTG(); }
function useProxy(i) { const l = pc().list; l.unshift(l.splice(i, 1)[0]); pc().enabled = true; applyTG(); }
function delProxy(i) { confirmBox("Удалить прокси " + label(pc().list[i]) + "?").then(ok => { if (ok) { pc().list.splice(i, 1); applyTG(); } }); }

function notifyView() {
  const N = {fp_message: "Сообщения FunPay", fp_order: "Заказы FunPay", fp_confirm: "Подтверждения FunPay", fp_review: "Отзывы FunPay",
    pl_message: "Сообщения Playerok", pl_deal: "Сделки Playerok", pl_confirm: "Подтверждения Playerok", pl_review: "Отзывы Playerok",
    delivery: "Автовыдача", low_stock: "Товар заканчивается", stock_out: "Лоты без товара", supplier: "Ошибки поставщиков",
    withdraw: "Вывод средств", raise: "Поднятие лотов", reminder: "Отправленные напоминания",
    report: "Итоги дня", errors: "Ошибки", start: "Запуск бота"};
  view(`${title("Уведомления")}<p class="hint" style="margin:0 6px 10px">Каждый участник команды получает только то, что разрешено его ролью. Менеджеры видят заказы без сумм.</p>
    <div class="group">${Object.entries(N).map(([k, n]) => tog("telegram.notify." + k, n)).join("")}</div>
    ${can("system") ? `<div class="group">${lines("telegram.extra_chats", "Дополнительные чаты (ID групп или каналов)", "Туда уходят сообщения и заказы без сумм и без кнопок управления.", true)}</div>` : ""}`);
}
function accessView() {
  view(`${title("Доступ и веб-панель")}
    <div class="group">${can("secrets") ? fld("telegram.password", "Пароль для первого входа (/login)", "secret", "Работает, только пока в команде нет ни одного владельца. Дальше — приглашения.") : ""}
      ${can("secrets") ? fld("web.password", "Пароль веб-панели", "secret", "Вход из браузера даёт права владельца — придумайте сложный пароль.") : ""}
      ${fld("web.port", "Порт", "number", "После смены — перезапуск.")}
      ${fld("web.public_url", "Публичный HTTPS-адрес", "url", "Нужен, чтобы мини-апп открывался из Telegram. Можно не заполнять, если включён туннель.")}
      <div class="field"><label>Туннель для HTTPS</label><select onchange="setPath('web.tunnel',this.value)">
        <option value="none" ${CFG.web.tunnel !== "cloudflared" ? "selected" : ""}>Нет — свой домен или только локально</option>
        <option value="cloudflared" ${CFG.web.tunnel === "cloudflared" ? "selected" : ""}>Cloudflare Tunnel — бесплатный адрес *.trycloudflare.com</option></select></div></div>
    <h3>Прочее</h3><div class="group">${fld("general.send_delay_sec", "Пауза между сообщениями, сек", "number")}
      <div class="field"><label>Подробность логов</label><select onchange="setPath('general.log_level',this.value)">${["DEBUG", "INFO", "WARNING", "ERROR"].map(l => `<option ${CFG.general.log_level === l ? "selected" : ""}>${l}</option>`).join("")}</select></div></div>`);
}
async function pluginsView() {
  const j = await api("plugins");
  view(`${title("Плагины")}
    <div class="group">${j.plugins.map(p => `<div class="item"><span class="ic">🧩</span><div class="tx"><b>${H(p.name)} <span class="hint">v${H(p.version)}</span></b>
      <span>${H(p.description || p.file)} · ${H(p.kind)}${p.credits ? " · " + H(p.credits) : ""}</span></div>
      <label class="sw"><input type="checkbox" ${p.enabled ? "checked" : ""} aria-label="Включить ${H(p.name)}" onchange="api('plugins/toggle',{uuid:'${p.uuid}'}).then(()=>toast(this.checked?'Включён':'Выключен'))"><i></i></label></div>
      <div class="field row" style="justify-content:flex-end"><button class="btn ghost sm" onclick="api('plugins/reload',{uuid:'${p.uuid}'}).then(j=>j.ok&&toast('Перезагружен'))">Перезагрузить</button>
      <button class="btn danger sm" onclick="confirmBox('Удалить плагин ${H(p.name)}?').then(ok=>ok&&api('plugins/delete',{uuid:'${p.uuid}'}).then(pluginsView))">Удалить</button></div>`).join("")
      || `<div class="empty"><b>Плагинов не установлено</b>Загрузите .py файл ниже, отправьте его боту в Telegram или положите в папку plugins.</div>`}</div>
    ${j.failed.length ? `<h3>Не загрузились</h3><div class="group">${j.failed.map(f => `<div class="item"><span class="ic">⚠️</span><div class="tx"><b>${H(f.file)}</b><span>${H(f.error)}</span></div></div>`).join("")}</div>` : ""}
    <h3>Установить</h3><div class="group">
      <div class="field"><label for="pf">Файл плагина (.py)</label><input id="pf" type="file" accept=".py"></div>
      <div class="field"><label for="purl">или ссылка на файл (GitHub, raw)</label><input id="purl" type="url" placeholder="https://github.com/.../plugin.py"></div>
      <div class="field"><button class="btn" onclick="installPlugin()">Установить плагин</button>
        <div class="note">Поддерживаются плагины FunPay Cardinal, плагины Castor (сразу для двух площадок) и модули Playerok Universal (события). Кнопки плагинов в Telegram и хуки запуска работают полностью после перезапуска.</div></div></div>`);
}
async function installPlugin() {
  const f = $("#pf").files[0], url = $("#purl").value.trim();
  let j;
  if (f) {
    const b64 = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result.split(",")[1]); fr.readAsDataURL(f); });
    j = await api("plugins/upload", {name: f.name, content: b64});
  } else if (url) j = await api("plugins/url", {url});
  else return toast("Выберите файл или вставьте ссылку");
  if (j.ok) { toast("Установлен: " + j.name); haptic(); pluginsView(); }
}
async function backupsView() {
  const j = await api("backups");
  view(`${title("Бекапы")}<div class="group">${j.backups.map(b => `<a class="item" style="text-decoration:none;color:inherit" href="/api/backup/download?file=${encodeURIComponent(b.name)}&t=${TOKEN}" download>
      <span class="ic">🗜</span><span class="tx"><b>${H(b.name)}</b><span>${when(b.t)} · ${(b.size / 1024).toFixed(0)} КБ</span></span><span class="chev">⤓</span></a>`).join("")
      || `<div class="empty"><b>Бекапов пока нет</b>Создайте первый — он сохранится на сервере и придёт вам в Telegram.</div>`}</div>
    <button class="btn" onclick="act('backup').then(backupsView)">Создать бекап сейчас</button>`);
}
let LOG_LVL = "";
async function logsView() {
  const j = await api("logs?level=" + LOG_LVL);
  view(`${title("Логи")}${seg([["", "Все"], ["WARNING", "Предупреждения"], ["ERROR", "Ошибки"]], LOG_LVL, "LOG_LVL=v;logsView()")}
    <div class="group"><pre class="logs">${H(j.logs.slice(-250).join("\n")) || "Пусто"}</pre></div><button class="btn ghost" onclick="logsView()">Обновить</button>`);
}
function jsonView() {
  view(`${title("Весь конфиг")}<div class="group"><div class="field"><textarea id="raw" class="code" spellcheck="false">${H(JSON.stringify(CFG, null, 2))}</textarea>
    <div class="note">Секретные поля показаны как ***: если их не трогать, они сохранятся как были.</div></div></div>
    <button class="btn" onclick="try{CFG=JSON.parse($('#raw').value);setDirty(true);toast('Применено — сохраните изменения')}catch(e){toast('Ошибка в JSON: '+e.message)}">Применить JSON</button>`);
}

let ME = {perms: [], role: "", accounts: []};

const can = p => ME.perms.includes(p);

function accName(id) { const a = (ME.accounts || []).find(x => x.id === id); return a ? a.name : (id || ""); }

function accTag(id, platform) { return (ME.accounts || []).filter(a => a.platform === platform).length > 1 ? ` · ${H(accName(id))}` : ""; }

async function orderSheet(i) {
  const o = window._orders[i]; if (!o) return;
  const fin = can("finance");
  const d = $("#dlg");
  $("#dlgform").innerHTML = `<h4>${H(o.title)}</h4><p class="hint">#${H(o.id)} · ${PN[o.p]}${accTag(o.a, o.p)} · ${H(o.buyer)} · ${when(o.t)}</p>
    ${fin ? `<div class="group" style="background:var(--soft)">
      <div class="item"><div class="tx"><b>Сумма</b></div><span class="val num">${money(o.price)}</span></div>
      <div class="item"><div class="tx"><b>Комиссия площадки</b></div><span class="val num">−${money(o.commission)}</span></div>
      <div class="field"><label for="ocost">Себестоимость</label><input id="ocost" type="number" step="any" inputmode="decimal" value="${o.cost ?? ""}" placeholder="не указана"></div>
      <div class="item"><div class="tx"><b>Прибыль</b></div><span class="val num">${money(o.profit)}</span></div></div>` : ""}
    <div class="row">${o.p === "funpay" && can("refund") && !o.refunded ? `<button class="btn danger" value="refund">Вернуть деньги</button>` : ""}
      <button class="btn ghost" value="cancel">Закрыть</button>${fin ? `<button class="btn" value="ok">Сохранить</button>` : ""}</div>`;
  d.onclose = async () => {
    if (d.returnValue === "ok") { const j = await api("order/cost", {id: o.id, platform: o.p, cost: $("#ocost").value}); if (j.ok) { toast("Себестоимость сохранена"); sales(); } }
    if (d.returnValue === "refund") { if (await confirmBox(`Вернуть деньги по заказу #${o.id}?`)) { const j = await api("order/refund", {id: o.id, account: o.a}); if (j.ok) { toast("Возврат выполнен"); sales(); } } }
  };
  d.showModal();
}

function supName(id) { const s = (CFG.suppliers || []).find(x => x.id === id); return s ? (s.name || s.id) : (id || "не выбран"); }

function togAcc(i, id, on) { const a = new Set(CFG.delivery[i].accounts || []); on ? a.add(id) : a.delete(id); CFG.delivery[i].accounts = [...a]; setDirty(true); }

async function lotsPicker(i) {
  const r = CFG.delivery[i];
  view(`${title("Лоты FunPay")}<div class="empty">Загружаю ваши лоты со всех аккаунтов…</div>`);
  const j = await api("lots");
  const kws = String(r.keyword || "").toLowerCase().split("|").map(s => s.trim()).filter(Boolean);
  const linked = new Set((r.lots || []).map(l => l.account + ":" + l.id));
  const lots = j.lots.map(l => ({...l, hit: kws.some(k => (l.title || "").toLowerCase().includes(k))})).sort((a, b) => b.hit - a.hit);
  window._lots = lots;
  view(`${title("Лоты FunPay")}${j.errors.length ? `<p class="err">${H(j.errors.join("; "))}</p>` : ""}
    <p class="hint" style="margin:0 6px 10px">Отметьте лоты этого товара. Совпавшие с ключевыми словами правила — сверху.</p>
    <div class="group">${lots.map((l, k) => `<label class="item"><input type="checkbox" style="width:auto" data-k="${k}" ${linked.has(l.account + ":" + l.id) ? "checked" : ""}>
      <div class="tx"><b>${l.hit ? "✨ " : ""}${H(l.title || "#" + l.id)}</b><span>${H(l.account_name)} · ${H(l.node_name)} · ${H(l.price)}${l.active ? "" : " · скрыт"}</span></div></label>`).join("")
      || `<div class="empty"><b>Лотов не нашлось</b>Проверьте, что аккаунт FunPay подключён и у него есть лоты.</div>`}</div>
    <button class="btn wide" onclick="saveLots(${i})">Привязать отмеченные</button>`);
}

function saveLots(i) {
  const picked = [...document.querySelectorAll("input[data-k]")].filter(x => x.checked).map(x => window._lots[+x.dataset.k]);
  CFG.delivery[i].lots = picked.map(l => ({account: l.account, id: l.id, title: l.title, node: l.node}));
  setDirty(true); toast(`Привязано лотов: ${picked.length} — сохраните изменения`); back();
}

function accountsView(p) {
  const list = CFG.accounts[p] || [];
  const st = (STATUS && STATUS.accounts) || [];
  view(`${title(PN[p])}
    <div class="group">${tog(p + ".enabled", "Площадка включена", "выключение останавливает все её аккаунты")}</div>
    <h3>Аккаунты</h3><div class="group">${list.map((a, i) => { const s = st.find(x => x.id === a.id) || {};
      return link(a.enabled === false ? "⏸" : s.ok ? "🟢" : s.running ? "🔴" : "⚪️", H(a.name || s.username || a.id),
        `${H(a.id)}${s.username ? " · " + H(s.username) : ""}${s.error && !s.ok ? " · " + H(s.error) : ""}`, `push(accountView,'${a.id}')`,
        s.balance != null && can("finance") ? money(s.balance) + " " + H(s.currency || "") : ""); }).join("")
      || `<div class="empty"><b>Аккаунтов нет</b>Добавьте первый — бот подключится сразу, без перезапуска.</div>`}
      ${can("secrets") ? `<button class="item" onclick="addAccount('${p}')"><span class="ic">＋</span><span class="tx"><b>Добавить аккаунт</b></span></button>` : ""}</div>
    <div class="row"><button class="btn ghost" onclick="push(platformView,'${p}')">⚙️ Тексты и автоматизация ${PN[p]}</button></div>`);
}

function addAccount(p) {
  const list = CFG.accounts[p] = CFG.accounts[p] || [];
  const used = new Set(list.map(a => a.id)); const pre = p === "funpay" ? "fp" : "pl"; let n = 1; while (used.has(pre + n)) n++;
  const a = {id: pre + n, name: "", enabled: true, proxy: "", user_agent: "", auto_delivery: true};
  if (p === "funpay") Object.assign(a, {golden_key: "", auto_raise: true}); else Object.assign(a, {token: "", cookies: ""});
  list.push(a); setDirty(true); push(accountView, a.id);
}

function accIndex(id) { const p = id.startsWith("fp") ? "funpay" : "playerok"; return [p, (CFG.accounts[p] || []).findIndex(a => a.id === id)]; }

function accountView(id) {
  const [p, i] = accIndex(id); if (i < 0) return back();
  const P = `accounts.${p}.${i}`, a = CFG.accounts[p][i];
  const s = ((STATUS && STATUS.accounts) || []).find(x => x.id === id) || {};
  view(`${title(a.name || id)}
    <div class="group"><div class="item"><span class="ic">${s.ok ? "🟢" : s.running ? "🔴" : "⚪️"}</span><div class="tx"><b>${s.username ? H(s.username) : "не подключён"}</b>
      <span>${s.ok ? "работает" : H(s.error || (HAS.accounts && HAS.accounts[id] ? "подключится после сохранения" : "добавьте ключ и сохраните"))}</span></div>
      ${s.balance != null && can("finance") ? `<span class="val num">${money(s.balance)} ${H(s.currency || "")}</span>` : ""}</div></div>
    <div class="group">${tog(P + ".enabled", "Аккаунт включён")}${fld(P + ".name", "Название (видно только вам)")}
      ${can("secrets") ? (p === "funpay" ? fld(P + ".golden_key", "golden_key", "secret", "funpay.com → F12 → Application → Cookies → golden_key")
        : fld(P + ".token", "token", "secret", "playerok.com → F12 → Application → Cookies → token") + fld(P + ".cookies", "или все cookies одной строкой", "secret")) : ""}
      ${fld(P + ".proxy", "Прокси для аккаунта", "text", "Для нескольких аккаунтов лучше у каждого свой прокси — так площадка не свяжет их между собой.")}
      ${fld(P + ".user_agent", "User-Agent браузера", "text", "Пусто — общий из настроек площадки.")}</div>
    <div class="group">${tog(P + ".auto_delivery", "Автовыдача на этом аккаунте")}${p === "funpay" ? tog(P + ".auto_raise", "Автоподнятие лотов на этом аккаунте") : ""}</div>
    <p class="hint" style="margin:0 6px 10px">После сохранения аккаунт подключится или переподключится сам — перезапуск не нужен.</p>
    ${can("secrets") ? `<button class="btn danger" onclick="confirmBox('Удалить аккаунт ${H(a.name || id)} из бота? Статистика сохранится.').then(ok=>{if(ok){CFG.accounts['${p}'].splice(${i},1);setDirty(true);back()}})">Удалить аккаунт</button>` : ""}`);
}

let FIN_P = "month";

async function financeView() {
  const j = await api("finance");
  const r = j.periods[FIN_P];
  const nc = r.funpay.no_cost + r.playerok.no_cost;
  const row = (n, v, neg) => `<div class="item"><div class="tx"><b>${n}</b></div><span class="val num">${neg && v ? "−" : ""}${money(v)}</span></div>`;
  view(`${title("Финансы")}${seg([["day", "24 ч"], ["week", "7 дней"], ["month", "30 дней"], ["all", "Всё"]], FIN_P, "FIN_P=v;financeView()")}
    <div class="group">
      ${row("Выручка", r.total.sum)}${row("Комиссии площадок", r.funpay.commission + r.playerok.commission, true)}
      ${row("Себестоимость", r.funpay.cost + r.playerok.cost, true)}${row("Расходы", r.total.expenses, true)}
      <div class="item"><div class="tx"><b>Прибыль</b><span>${r.total.count} ${plural(r.total.count, "заказ", "заказа", "заказов")}${r.funpay.refunded + r.playerok.refunded ? " · возвратов " + (r.funpay.refunded + r.playerok.refunded) : ""}</span></div>
        <span class="val num" style="font-size:18px;font-weight:700;color:${r.total.profit >= 0 ? "var(--pl)" : "var(--danger)"}">${money(r.total.profit)}</span></div></div>
    ${nc ? `<p class="hint" style="margin:-4px 6px 12px">⚠️ У ${nc} ${plural(nc, "заказа", "заказов", "заказов")} не указана себестоимость — прибыль по ним считается без неё. Укажите себестоимость в правилах выдачи или в самом заказе (Продажи → заказ).</p>` : ""}
    <h3>По площадкам</h3><div class="group">${["funpay", "playerok"].map(p => `<div class="item"><span class="pf ${p}">${PN[p]}</span><div class="tx"><span>${r[p].count} зак. · выручка ${money(r[p].sum)} · комиссия ${money(r[p].commission)} · себест. ${money(r[p].cost)}</span></div><span class="val num">${money(r[p].profit)}</span></div>`).join("")}</div>
    ${Object.keys(j.by_account).length > 1 ? `<h3>По аккаунтам · 30 дней</h3><div class="group">${Object.entries(j.by_account).map(([a, v]) => `<div class="item"><div class="tx"><b>${H(j.names[a] || a)}</b><span>${v.count} зак. · выручка ${money(v.sum)}</span></div><span class="val num">${money(v.profit)}</span></div>`).join("")}</div>` : ""}
    <h3>Самые прибыльные товары</h3><div class="group">${j.top.map(t => `<div class="item"><div class="tx"><b>${H(t.title)}</b><span>${t.count} шт · выручка ${money(t.sum)}</span></div><span class="val num">${money(t.profit)}</span></div>`).join("") || `<div class="empty">Продаж пока нет</div>`}</div>
    <h3>Комиссии площадок</h3><div class="group">${fld("finance.commission.funpay", "FunPay, %", "number")}${fld("finance.commission.playerok", "Playerok, %", "number",
      "Укажите, если сумма заказа в боте больше, чем приходит вам на баланс. Применяется к новым заказам.")}</div>
    <h3>Расходы</h3><div class="group">${(CFG.finance.expenses || []).slice().reverse().map(e => `<div class="item"><span class="ic">🧾</span><div class="tx"><b>${H(e.note || "расход")}</b><span>${H(e.date)}</span></div>
        <span class="val num">−${money(e.amount)}</span><button class="btn danger sm" aria-label="Удалить" onclick="delExpense('${H(e.id)}')">✕</button></div>`).join("") || `<div class="item"><div class="tx"><span>Реклама, закупки оптом, сервисы — всё, что не привязано к конкретному заказу.</span></div></div>`}
      <div class="field"><div class="row"><input id="exd" type="date" value="${new Date().toISOString().slice(0, 10)}" style="flex:1"><input id="exa" type="number" inputmode="decimal" placeholder="Сумма" style="flex:1"></div>
        <div class="row" style="margin-top:8px"><input id="exn" placeholder="За что" style="flex:2"><button class="btn sm" onclick="addExpense()">Добавить</button></div></div></div>`);
}

function addExpense() {
  const amount = +$("#exa").value; if (!amount) return toast("Укажите сумму");
  CFG.finance.expenses = CFG.finance.expenses || [];
  CFG.finance.expenses.push({id: Date.now().toString(36), date: $("#exd").value, amount, note: $("#exn").value});
  setDirty(true); financeView();
}

function delExpense(id) { CFG.finance.expenses = CFG.finance.expenses.filter(e => e.id !== id); setDirty(true); financeView(); }

async function withdrawView() {
  const j = await api("withdraw");
  const blk = p => {
    const P = `withdraw.${p}`, w = CFG.withdraw[p], plan = j.plans[p];
    const accs = (CFG.accounts[p] || []);
    return `<h3>${PN[p]}</h3><div class="group">
      <div class="field"><label>Режим</label><select onchange="setPath('${P}.mode',this.value)">
        <option value="off" ${w.mode === "off" ? "selected" : ""}>Выключен</option>
        <option value="ask" ${w.mode === "ask" ? "selected" : ""}>Спрашивать меня в Telegram перед выводом</option>
        <option value="auto" ${w.mode === "auto" ? "selected" : ""}>Выводить автоматически</option></select>
        <div class="note">В авторежиме бот выводит сам только если точно знает доступный баланс, иначе всё равно спросит.</div></div>
      ${accs.length > 1 ? `<div class="field"><label>С какого аккаунта</label><select onchange="setPath('${P}.account',this.value)"><option value="">первый подключённый</option>${accs.map(a => `<option value="${a.id}" ${a.id === w.account ? "selected" : ""}>${H(a.name || a.id)}</option>`).join("")}</select></div>` : ""}
      ${p === "funpay" ? `<div class="field"><label>Валюта баланса</label><select onchange="setPath('${P}.currency',this.value)">${["rub", "usd", "eur"].map(c => `<option ${w.currency === c ? "selected" : ""}>${c}</option>`).join("")}</select></div>
        ${fld(P + ".method", "Способ вывода (код FunPay)", "text", "Например: card_rub — карта в рублях, card_usd, card_eur, usdt_trc, fps. Код должен совпадать с вариантом в форме вывода на сайте.")}`
        : `<div class="field"><label>Способ вывода</label>${(j.providers || []).length ? `<select onchange="setPath('${P}.provider',this.value)"><option value="">— выберите —</option>${j.providers.map(x => `<option ${w.provider === x ? "selected" : ""}>${H(x)}</option>`).join("")}</select>` : `<input value="${H(w.provider)}" onchange="setPath('${P}.provider',this.value)"><div class="note">Список способов появится, когда аккаунт Playerok подключится.</div>`}</div>
        ${fld(P + ".sbp_bank", "Банк для СБП (если нужен)")}`}
      ${fld(P + ".wallet", "Реквизиты: номер карты или кошелька", "secret", "Хранится скрыто. В уведомлениях видны только последние 4 цифры.")}
      ${fld(P + ".threshold", "Выводить, когда баланс от", "number")}${fld(P + ".keep", "Оставлять на балансе", "number")}
      ${fld(P + ".min_amount", "Минимальная сумма вывода", "number")}${fld(P + ".max_per_day", "Не больше в сутки (0 — без лимита)", "number")}
      ${fld(P + ".check_every_hours", "Проверять баланс каждые, ч", "number")}
      <div class="item"><div class="tx"><b>Сейчас</b><span>баланс ${plan.available == null ? "неизвестен" : money(plan.available) + (plan.exact ? "" : " (приблизительно)")} · ${plan.amount ? "можно вывести " + money(plan.amount) : H(plan.reason)}</span></div></div>
      ${j.paused[p] ? `<div class="item"><span class="ic">⏸</span><div class="tx"><b>На паузе после ошибки</b><span>${H(j.paused[p])}</span></div><button class="btn sm" onclick="api('withdraw/unpause',{platform:'${p}'}).then(withdrawView)">Снять паузу</button></div>` : ""}
      <div class="field"><div class="row"><input id="wa_${p}" type="number" inputmode="decimal" placeholder="Сумма" value="${plan.amount || ""}" style="flex:1"><button class="btn sm" onclick="runWithdraw('${p}')">Вывести сейчас</button></div></div></div>`;
  };
  view(`${title("Автовывод средств")}
    <p class="hint" style="margin:0 6px 10px">Бот делает одну попытку на заявку и никогда не повторяет вывод сам. После любой ошибки автовывод встаёт на паузу, пока вы не проверите баланс на сайте. Сначала проверьте на небольшой сумме в режиме «спрашивать».</p>
    ${blk("funpay")}${blk("playerok")}
    <h3>История</h3><div class="group">${j.history.map(h => `<div class="item"><span class="ic">${h.status === "done" ? "✅" : h.status === "error" ? "❌" : "⏳"}</span><div class="tx"><b>${PN[h.platform]} · ${money(h.amount)}</b><span>${when(h.t)} · ${H(h.info)}</span></div></div>`).join("") || `<div class="empty">Выводов ещё не было</div>`}</div>`);
}

async function runWithdraw(p) {
  if (dirty) return toast("Сначала сохраните изменения");
  const amount = +$("#wa_" + p).value; if (!amount) return toast("Укажите сумму");
  if (!await confirmBox(`Вывести ${money(amount)} с ${PN[p]}?`)) return;
  const j = await api("withdraw/run", {platform: p, amount}); if (j.message) toast(j.message); withdrawView();
}

function suppliersView() {
  const L = CFG.suppliers = CFG.suppliers || [];
  view(`${title("Поставщики")}<p class="hint" style="margin:0 6px 10px">Подключите API поставщика, и правило выдачи сможет покупать товар в момент заказа. Подходит любой сервис с HTTP API: адрес запроса, ключ и путь к товару в ответе.</p>
    <div class="group">${L.map((s, i) => link(s.enabled === false ? "⏸" : "🏭", H(s.name || s.id), H(s.url || "адрес не указан"), `push(supplierView,${i})`)).join("")}
      <button class="item" onclick="addSupplier()"><span class="ic">＋</span><span class="tx"><b>Добавить поставщика</b></span></button></div>
    <div id="pend"></div>`);
  api("suppliers/pending").then(j => { if (j.pending && j.pending.length) $("#pend").innerHTML = `<h3>Ждут проверки</h3><div class="group">${j.pending.map(x => `<div class="item"><span class="ic">⚠️</span><div class="tx"><b>${H(x.key)}</b>
    <span>${when(x.t)} · запрос ушёл, ответ не пришёл. Проверьте у поставщика: если товар списан — выдайте его вручную.</span></div><button class="btn sm" onclick="api('supplier/resolve',{key:'${H(x.key)}'}).then(suppliersView)">Разобрался</button></div>`).join("")}</div>`; });
}

function addSupplier() {
  const used = new Set(CFG.suppliers.map(s => s.id)); let n = 1; while (used.has("s" + n)) n++;
  CFG.suppliers.push({id: "s" + n, name: "", enabled: true, url: "", method: "POST", headers: "", body: '{"product": "$product_id", "quantity": $quantity, "order": "$order_id"}',
    body_type: "json", items_path: "", cost_path: "", cost_mult: 1, success_path: "", success_value: "true", error_path: "", split_lines: false, timeout: 30, balance_url: "", balance_method: "GET", balance_path: "", proxy: ""});
  setDirty(true); push(supplierView, CFG.suppliers.length - 1);
}

function supplierView(i) {
  const s = CFG.suppliers[i]; if (!s) return back();
  const P = "suppliers." + i;
  view(`${title(s.name || s.id)}
    <div class="group">${tog(P + ".enabled", "Включён")}${fld(P + ".name", "Название")}
      ${fld(P + ".url", "Адрес запроса покупки", "url", "Можно подставлять $product_id, $quantity, $order_id")}
      <div class="field"><label>Метод</label><select onchange="setPath('${P}.method',this.value)">${["POST", "GET"].map(m => `<option ${s.method === m ? "selected" : ""}>${m}</option>`).join("")}</select></div>
      ${can("secrets") ? fld(P + ".headers", "Заголовки (каждый с новой строки)", "secret", "Например: Authorization: Bearer ВАШ_КЛЮЧ. Хранятся скрыто.") : `<div class="field"><label>Заголовки</label><div class="note">Ключи поставщика может менять только владелец.</div></div>`}
      <div class="field"><label>Формат тела</label><select onchange="setPath('${P}.body_type',this.value)"><option value="json" ${s.body_type !== "form" ? "selected" : ""}>JSON</option><option value="form" ${s.body_type === "form" ? "selected" : ""}>форма (a=1&b=2)</option></select></div>
      ${fld(P + ".body", "Тело запроса", "area", "Переменные: $product_id $quantity $order_id $buyer $platform $title $account")}</div>
    <h3>Ответ поставщика</h3><div class="group">
      ${fld(P + ".items_path", "Где товар в ответе", "text", "Путь через точку: data.codes · result.0.key · items[*].code. Список — каждому по элементу.")}
      ${tog(P + ".split_lines", "Разбивать текст ответа по строкам")}
      ${fld(P + ".success_path", "Поле успеха (необязательно)", "text", "Например: success. Если значение не совпадёт — заказ считается неудачным.")}${fld(P + ".success_value", "Значение успеха")}
      ${fld(P + ".error_path", "Где текст ошибки (необязательно)")}
      ${fld(P + ".cost_path", "Где цена закупки (необязательно)", "text", "Тогда прибыль считается по реальной цене.")}${fld(P + ".cost_mult", "Множитель цены", "number", "0.01, если поставщик отдаёт цену в копейках.")}
      ${fld(P + ".timeout", "Ждать ответа, сек", "number")}${fld(P + ".proxy", "Прокси (необязательно)")}</div>
    <h3>Баланс поставщика</h3><div class="group">${fld(P + ".balance_url", "Адрес проверки баланса", "url")}${fld(P + ".balance_path", "Где баланс в ответе")}</div>
    <h3>Проверка</h3><div class="group"><div class="field"><div class="row"><input id="tpid" placeholder="ID товара" style="flex:1"><input id="tq" type="number" value="1" style="width:80px"></div>
      <div class="row" style="margin-top:8px"><button class="btn ghost sm" onclick="supPreview('${s.id}')">Показать запрос</button><button class="btn ghost sm" onclick="supBalance('${s.id}')">Баланс</button>
      <button class="btn danger sm" onclick="supTest('${s.id}')">Тестовая покупка</button></div>
      <div class="note">«Показать запрос» ничего не отправляет. «Тестовая покупка» покупает по-настоящему. Перед проверкой сохраните изменения.</div><pre class="logs" id="supout" style="padding:8px 0 0"></pre></div></div>
    <button class="btn danger" onclick="confirmBox('Удалить поставщика?').then(ok=>{if(ok){CFG.suppliers.splice(${i},1);setDirty(true);back()}})">Удалить поставщика</button>`);
}

async function supPreview(id) { if (dirty) return toast("Сначала сохраните изменения"); const j = await api("supplier/preview", {id, product_id: $("#tpid").value || "TEST", quantity: +$("#tq").value || 1}); if (j.request) $("#supout").textContent = JSON.stringify(j.request, null, 2); }

async function supBalance(id) { if (dirty) return toast("Сначала сохраните изменения"); const j = await api("supplier/balance", {id}); if (j.message) $("#supout").textContent = j.message; }

async function supTest(id) {
  if (dirty) return toast("Сначала сохраните изменения");
  if (!await confirmBox("Это настоящая покупка у поставщика. Продолжить?")) return;
  const j = await api("supplier/test", {id, product_id: $("#tpid").value, quantity: +$("#tq").value || 1});
  if (j.items) $("#supout").textContent = "Получено:\n" + j.items.join("\n") + (j.cost != null ? "\n\nЦена закупки: " + j.cost : "");
}

async function teamView() {
  const j = await api("team");
  view(`${title("Команда и роли")}
    <div class="group">${j.users.map(u => `<div class="item"><span class="ic">${u.role === "owner" ? "👑" : u.role === "admin" ? "🛠" : "💬"}</span><div class="tx"><b>${H(u.name || u.id)}</b><span>ID ${u.id}</span></div>
      <select style="width:auto" aria-label="Роль" onchange="api('team/role',{id:${u.id},role:this.value}).then(r=>{if(r.ok)toast('Роль изменена');teamView()})">${j.roles.map(r => `<option value="${r.id}" ${r.id === u.role ? "selected" : ""}>${r.name}</option>`).join("")}</select>
      <button class="btn danger sm" aria-label="Убрать" onclick="confirmBox('Убрать ${H(u.name || u.id)} из команды?').then(ok=>ok&&api('team/remove',{id:${u.id}}).then(teamView))">✕</button></div>`).join("")}</div>
    <h3>Что может каждая роль</h3><div class="group">${j.roles.map(r => `<div class="item"><div class="tx"><b>${r.name}</b><span>${H(r.hint)}</span></div></div>`).join("")}</div>
    <h3>Пригласить</h3><div class="group"><div class="field"><div class="row">${j.roles.map(r => `<button class="btn ${r.id === "manager" ? "" : "ghost"} sm" onclick="invite('${r.id}')">${r.name}</button>`).join("")}</div>
      <div class="note">Бот создаст одноразовую ссылку на 24 часа. Помощник открывает её — и сразу получает роль.</div><div id="invout"></div></div></div>`);
}

async function invite(role) {
  const j = await api("team/invite", {role});
  if (!j.link) return toast("Telegram-бот не подключён");
  $("#invout").innerHTML = `<div class="row" style="margin-top:10px"><input readonly value="${H(j.link)}" style="flex:1" onclick="this.select()"><button class="btn sm" onclick="navigator.clipboard.writeText('${H(j.link)}').then(()=>toast('Ссылка скопирована'))">Копировать</button></div>`;
}

function previewWM(text) {
  const g = CFG.general || {};
  if (g.watermark_enabled === false) return text;
  const wm = (g.watermark || (ME.app && ME.app.default_watermark) || "").trim();
  if (!wm) return text;
  return wm + (g.watermark_newline === false ? " " : "\n") + text;
}

function watermarkView() {
  const g = CFG.general;
  const presets = (ME.app && ME.app.watermarks) || [];
  view(`${title("Подпись в чатах")}
    <p class="hint" style="margin:0 6px 10px">Бот ставит подпись перед каждым своим сообщением в чате покупателя на FunPay и Playerok — как Кардинал. По ней бот узнаёт свои сообщения и не отвечает сам себе.</p>
    <div class="group">${tog("general.watermark_enabled", "Подпись включена", "", "setPath('general.watermark_enabled',this.checked);watermarkView()")}
      ${tog("general.watermark_newline", "Отдельной строкой", "иначе — в начале той же строки", "setPath('general.watermark_newline',this.checked);watermarkView()")}
      ${tog("general.watermark_manual", "И в ответах из Telegram и мини-аппа", "выключите, чтобы ваши ручные ответы шли без подписи")}</div>
    <h3>Так увидит покупатель</h3>
    <div class="chatview" style="padding:0 4px 12px"><div class="msg them" style="max-width:92%"><small>вы</small>${H(previewWM("Спасибо за покупку! Ваш ключ: XXXX-XXXX"))}</div></div>
    <h3>Выберите подпись</h3>
    <div class="group">${presets.map(p => `<button class="item" onclick="setPath('general.watermark','${H(p)}');setPath('general.watermark_enabled',true);watermarkView()">
      <span class="tx"><b>${H(p)}</b></span>${(g.watermark || ME.app.default_watermark) === p ? `<span class="val">✓</span>` : ""}</button>`).join("")}
      <div class="field"><label for="wmc">Или своя</label><div class="row"><input id="wmc" maxlength="60" value="${presets.includes(g.watermark) ? "" : H(g.watermark)}" placeholder="Например: 🤖 Магазин Васи" style="flex:1">
        <button class="btn sm" onclick="setPath('general.watermark',$('#wmc').value.trim());setPath('general.watermark_enabled',true);watermarkView()">Применить</button></div></div></div>`);
}

function aboutView() {
  const a = ME.app || {};
  const L = CFG.project ? CFG.project.links : null;
  view(`${title("О боте")}
    <div class="group"><div class="item"><img src="logo.svg" alt="" width="48" height="48" style="border-radius:14px">
      <div class="tx"><b>${H(a.name)}</b><span>версия ${H(a.version)} · бот-помощник продавца для FunPay и Playerok</span></div></div>
      ${(a.links || []).map(l => `<a class="item" href="${H(l.url)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit" onclick="if(window.Telegram&&Telegram.WebApp&&Telegram.WebApp.openTelegramLink&&'${H(l.url)}'.startsWith('https://t.me/')){Telegram.WebApp.openTelegramLink('${H(l.url)}');return false}">
        <span class="ic">${l.emoji}</span><span class="tx"><b>${H(l.name)}</b><span>${H(l.text)}</span></span><span class="chev">›</span></a>`).join("")}</div>
    ${can("settings") && L ? `<h3>Ссылки проекта</h3><div class="group">
      ${fld("project.links.updates", "🔄 Канал обновлений", "text", "@username или ссылка. Пусто — не показывать.")}
      ${fld("project.links.plugins", "🧩 Канал плагинов")}${fld("project.links.chat", "💬 Чат")}${fld("project.links.donate", "💰 Донат")}
      ${fld("project.links.github", "🛠 GitHub", "text", "Например: github.com/ваш_ник/FPCastor")}
      ${tog("project.set_bot_description", "Заполнять описание Telegram-бота", "«Что умеет этот бот?» и «О себе» — с этими ссылками, обновляется само")}</div>` : ""}`);
}

setInterval(() => { if (TOKEN && !document.hidden && !$("#app").hidden) { refreshDuo(); if (TAB === "home" && !stack.length) home(); } }, 30000);
window.addEventListener("beforeunload", e => { if (dirty && !inTG) { e.preventDefault(); e.returnValue = ""; } });
boot();
