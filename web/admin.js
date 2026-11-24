const API = window.SHOPBOT_API.replace(/\/+$/, "");
const $ = (id) => document.getElementById(id);
const money = (c) => "$" + (c / 100).toFixed(2);
let token = sessionStorage.getItem("shopbot_admin") || "";

async function api(path, opts = {}) {
  const r = await fetch(API + path, { ...opts, headers: { "Content-Type": "application/json", "X-Admin-Token": token, ...(opts.headers || {}) } });
  if (r.status === 401) { logout(); throw new Error("Unauthorized"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const el = (tag, props = {}, kids = []) => {
  const e = Object.assign(document.createElement(tag), props);
  [].concat(kids).forEach((k) => e.appendChild(typeof k === "string" ? document.createTextNode(k) : k));
  return e;
};
function table(heads, rows) {
  const t = el("table"); t.appendChild(el("tr", {}, heads.map((h) => el("th", {}, h))));
  rows.forEach((r) => t.appendChild(el("tr", {}, r.map((c) => el("td", {}, c instanceof Node ? c : String(c ?? ""))))));
  const w = el("div", { className: "scroll-x" }, t);
  if (!rows.length) w.appendChild(el("div", { className: "empty" }, "Nothing here yet."));
  return w;
}

async function login() {
  token = $("tok").value.trim();
  try { await api("/admin/stats"); sessionStorage.setItem("shopbot_admin", token); start(); }
  catch (e) { $("loginErr").textContent = e.message === "Unauthorized" ? "Wrong token" : "Cannot reach API: " + e.message; }
}
function logout() { sessionStorage.removeItem("shopbot_admin"); token = ""; $("app").style.display = "none"; $("login").style.display = "flex"; }

const TABS = { Overview, Products, Orders, Conversations, Memories };
function start() {
  $("login").style.display = "none"; $("app").style.display = "block";
  $("tabs").innerHTML = "";
  Object.keys(TABS).forEach((n) => $("tabs").appendChild(el("button", { className: "ghost", onclick: () => show(n) }, n)));
  $("tabs").appendChild(el("button", { className: "ghost", onclick: logout }, "Sign out"));
  show("Overview");
}
async function show(name) {
  $("view").innerHTML = "Loading…";
  try { const node = await TABS[name](); $("view").innerHTML = ""; $("view").appendChild(node); }
  catch (e) { $("view").textContent = "Error: " + e.message; }
}

async function Overview() {
  const s = await api("/admin/stats");
  const card = (v, l) => el("div", { className: "stat" }, [el("b", {}, String(v)), el("span", {}, l)]);
  return el("div", { className: "stats" }, [
    card(s.users, "Customers who chatted"), card(s.agent_replies, "Agent replies"), card(s.blocked, "Blocked (off-topic / injection)"),
    card(s.orders, "Orders"), card(money(s.revenue_cents), "Revenue"), card(s.low_stock, "Low-stock products (≤3)"),
    card(s.memories, "Long-term memories stored"), card((s.tokens_in + s.tokens_out).toLocaleString(), `Tokens (${s.tokens_in.toLocaleString()} in / ${s.tokens_out.toLocaleString()} out)`),
    card("$" + s.est_cost_usd.toFixed(4), "Estimated LLM spend"),
  ]);
}

async function Products() {
  const list = await api("/admin/products");
  const wrap = el("div");
  const f = { name: el("input", { placeholder: "Name" }), category: el("select", {}, ["coffee", "tea", "equipment", "mugs"].map((c) => el("option", { value: c }, c))),
    price: el("input", { type: "number", step: "0.01", placeholder: "Price $" }), stock: el("input", { type: "number", placeholder: "Stock" }),
    image: el("input", { placeholder: "Image URL" }), desc: el("input", { placeholder: "Description" }) };
  const add = el("button", { onclick: async () => {
    await api("/admin/products", { method: "POST", body: JSON.stringify({ name: f.name.value, category: f.category.value, price_cents: Math.round(f.price.value * 100), stock: +f.stock.value || 0, image_url: f.image.value, description: f.desc.value }) });
    show("Products");
  } }, "Add product");
  wrap.appendChild(el("div", { className: "form-grid" }, [f.name, f.category, f.price, f.stock, f.image, f.desc, add]));
  const rows = list.map((p) => {
    const price = el("input", { type: "number", step: "0.01", value: (p.price_cents / 100).toFixed(2), style: "width:90px" });
    const stock = el("input", { type: "number", value: p.stock, style: "width:80px" });
    const save = el("button", { onclick: async () => {
      await api("/admin/products/" + p.id, { method: "PUT", body: JSON.stringify({ ...p, price_cents: Math.round(price.value * 100), stock: +stock.value }) }); show("Products");
    } }, "Save");
    const del = el("button", { className: "danger", onclick: async () => { if (confirm("Hide this product?")) { await api("/admin/products/" + p.id, { method: "DELETE" }); show("Products"); } } }, "Hide");
    return [p.id, p.name + (p.active ? "" : " (hidden)"), p.category, price, stock, el("div", { className: "row", style: "margin:0" }, p.active ? [save, del] : [save])];
  });
  wrap.appendChild(table(["ID", "Name", "Category", "Price", "Stock", ""], rows));
  return wrap;
}

async function Orders() {
  const list = await api("/admin/orders");
  const rows = list.map((o) => {
    const sel = el("select", { onchange: async () => { await api("/admin/orders/" + o.id, { method: "PATCH", body: JSON.stringify({ status: sel.value }) }); show("Orders"); } },
      ["placed", "shipped", "delivered", "cancelled"].map((s) => el("option", { value: s, selected: s === o.status }, s)));
    return [o.id, el("code", { className: "small" }, o.user_id), o.items.map((i) => `${i.qty}× ${i.name}`).join(", "), money(o.total_cents), sel, new Date(o.created_at + "Z").toLocaleString()];
  });
  return table(["#", "Customer", "Items", "Total", "Status", "Placed"], rows);
}

async function Conversations() {
  const list = await api("/admin/conversations");
  const wrap = el("div"); const detail = el("div");
  const rows = list.map((c) => [el("code", { className: "small" }, c.user_id), el("code", { className: "small" }, c.thread_id), c.messages, c.blocked,
    (c.tokens_in + c.tokens_out).toLocaleString(), new Date(c.last + "Z").toLocaleString(),
    el("button", { className: "ghost", onclick: () => openThread(c) }, "View")]);
  wrap.appendChild(table(["Customer", "Thread", "Msgs", "Blocked", "Tokens", "Last activity", ""], rows)); wrap.appendChild(detail);
  async function openThread(c) {
    const msgs = await api(`/admin/conversations/${encodeURIComponent(c.user_id)}/${encodeURIComponent(c.thread_id)}`);
    detail.innerHTML = ""; const box = el("div", { className: "thread" }, el("h3", {}, `${c.user_id} / ${c.thread_id}`));
    msgs.forEach((m) => {
      box.appendChild(el("div", { className: "m " + (m.role === "user" ? "user" : "bot"), style: m.role === "user" ? "margin-left:auto" : "" }, (m.blocked ? `[blocked: ${m.blocked}] ` : "") + m.content));
      if (m.tool_calls.length) box.appendChild(el("div", { className: "m tools" }, "🔧 " + m.tool_calls.map((t) => `${t.name}(${JSON.stringify(t.args)})`).join(", ")));
    });
    detail.appendChild(box); box.scrollIntoView({ behavior: "smooth" });
  }
  return wrap;
}

async function Memories() {
  const list = await api("/admin/memories");
  return table(["Customer", "Remembered fact", "Saved"], list.map((m) => [el("code", { className: "small" }, m.user_id), m.fact, new Date(m.created_at + "Z").toLocaleString()]));
}

if (token) api("/admin/stats").then(start).catch(() => {});
