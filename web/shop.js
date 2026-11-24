const API = window.SHOPBOT_API.replace(/\/+$/, "");
const $ = (id) => document.getElementById(id);
const money = (c) => "$" + (c / 100).toFixed(2);
const rid = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));

// ---- identity: user_id (long-term memory + data ownership) and thread_id (one conversation)
let userId = localStorage.getItem("shopbot_user_id");
if (!userId) { userId = "user_" + rid(); localStorage.setItem("shopbot_user_id", userId); }
let threadId = localStorage.getItem("shopbot_thread_id");
if (!threadId) { threadId = "thread_" + rid(); localStorage.setItem("shopbot_thread_id", threadId); }

async function api(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// ---- catalogue
const EMOJI = { coffee: "☕", tea: "🍵", equipment: "🫖", mugs: "🥛" };
let category = "";
async function loadProducts() {
  const items = await api("/products" + (category ? "?category=" + encodeURIComponent(category) : ""));
  const cats = ["", "coffee", "tea", "equipment", "mugs"];
  $("chips").innerHTML = "";
  cats.forEach((c) => {
    const b = document.createElement("button");
    b.className = "chip" + (c === category ? " on" : "");
    b.textContent = c || "All";
    b.onclick = () => { category = c; loadProducts(); };
    $("chips").appendChild(b);
  });
  $("grid").innerHTML = "";
  items.forEach((p) => {
    const d = document.createElement("div");
    d.className = "card";
    const tag = p.stock === 0 ? '<span class="tag out">Sold out</span>' : p.stock <= 3 ? `<span class="tag low">Only ${p.stock} left</span>` : '<span class="tag ok">In stock</span>';
    d.innerHTML = `<img loading="lazy" alt=""><div class="b"><h3></h3><p></p><div>${tag}</div><div class="price">${money(p.price_cents)}</div><button class="ghost">Ask ShopBot about this</button></div>`;
    const img = d.querySelector("img");
    const ph = () => {
      const f = document.createElement("div");
      f.className = "ph"; f.textContent = EMOJI[p.category] || "🛍️";
      img.replaceWith(f);
    };
    img.onerror = ph;
    if (p.image_url) img.src = p.image_url; else ph();
    d.querySelector("h3").textContent = p.name;
    d.querySelector("p").textContent = p.description;
    d.querySelector("button").onclick = () => { openChat(); send(`Tell me about "${p.name}" (id ${p.id}). Is it a good fit for me?`); };
    $("grid").appendChild(d);
  });
}

// ---- orders + memory (both are scoped to this user_id on the server)
async function loadOrders() {
  const o = await api("/orders?user_id=" + encodeURIComponent(userId));
  if (!o.length) { $("ordersBox").innerHTML = '<div class="empty">No orders yet. Ask ShopBot to help you order something!</div>'; return; }
  const t = document.createElement("table");
  t.innerHTML = "<tr><th>#</th><th>Items</th><th>Total</th><th>Status</th><th>Placed</th></tr>";
  o.forEach((x) => {
    const tr = document.createElement("tr");
    [x.id, x.items.map((i) => `${i.qty}× ${i.name}`).join(", "), money(x.total_cents), x.status, new Date(x.created_at + "Z").toLocaleString()]
      .forEach((v) => { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); });
    t.appendChild(tr);
  });
  $("ordersBox").innerHTML = ""; $("ordersBox").appendChild(t);
}
async function loadMemory() {
  const m = await api("/memory?user_id=" + encodeURIComponent(userId));
  $("memBox").innerHTML = m.facts.length ? "" : '<div class="empty">Nothing yet. Tell ShopBot your tastes, e.g. "I love light roast and I\'m allergic to nuts".</div>';
  m.facts.forEach((f) => { const s = document.createElement("span"); s.textContent = f; $("memBox").appendChild(s); });
}
async function forget() { if (confirm("Delete everything ShopBot remembers about you?")) { await api("/memory?user_id=" + encodeURIComponent(userId), { method: "DELETE" }); loadMemory(); } }
function newUser() { localStorage.setItem("shopbot_user_id", "user_" + rid()); newThreadId(); location.reload(); }
function newThreadId() { localStorage.setItem("shopbot_thread_id", "thread_" + rid()); }

// ---- chat widget
function bubble(text, cls) {
  const d = document.createElement("div");
  d.className = "m " + cls; d.textContent = text;
  $("msgs").appendChild(d); $("msgs").scrollTop = $("msgs").scrollHeight;
  return d;
}
const SUGGESTIONS = ["Recommend a light roast coffee", "What's under $20?", "Show my orders", "I'm allergic to caffeine"];
function renderSuggest() {
  $("suggest").innerHTML = "";
  SUGGESTIONS.forEach((s) => { const b = document.createElement("button"); b.type = "button"; b.textContent = s; b.onclick = () => send(s); $("suggest").appendChild(b); });
}
async function restoreHistory() {
  $("msgs").innerHTML = "";
  try {
    const h = await api(`/history?user_id=${encodeURIComponent(userId)}&thread_id=${encodeURIComponent(threadId)}`);
    if (!h.length) { bubble("Hi! I'm ShopBot 👋 Tell me what you're looking for and I'll help you find it.", "bot"); return; }
    h.forEach((m) => bubble(m.content, m.role === "user" ? "user" : "bot"));
  } catch { bubble("Hi! I'm ShopBot 👋 Tell me what you're looking for.", "bot"); }
}
function openChat() { $("chat").classList.add("open"); $("msgIn").focus(); }
function toggleChat() { $("chat").classList.toggle("open"); }
function newThread() { newThreadId(); threadId = localStorage.getItem("shopbot_thread_id"); restoreHistory(); }

let busy = false;
async function send(text) {
  text = (text || "").trim();
  if (!text || busy) return;
  busy = true; $("sendBtn").disabled = true;
  bubble(text, "user");
  const typing = bubble("ShopBot is typing…", "bot typing");
  try {
    const r = await api("/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, thread_id: threadId, message: text }) });
    typing.remove();
    if (r.tool_calls.length) bubble("🔧 " + r.tool_calls.map((t) => t.name).join(", "), "tools");
    bubble(r.reply, "bot");
    if (r.tool_calls.some((t) => /confirm|prepare/.test(t.name))) { loadOrders(); loadProducts(); }
    loadMemory();
  } catch (e) { typing.remove(); bubble("Sorry, something went wrong: " + e.message, "bot"); }
  busy = false; $("sendBtn").disabled = false; $("msgIn").focus();
}
$("chatform").addEventListener("submit", (e) => { e.preventDefault(); const v = $("msgIn").value; $("msgIn").value = ""; send(v); });

renderSuggest(); restoreHistory(); loadProducts(); loadOrders(); loadMemory();
