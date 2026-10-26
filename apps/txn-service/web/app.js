// BankSim Corporate Banking - single-page app, no dependencies.
"use strict";

const state = { meta: null, user: null, accounts: [], healthTimer: null };
const $ = (sel, root = document) => root.querySelector(sel);

// ------------------------------------------------------------------ helpers

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function money(minor, ccy) {
  // currency codes, not symbols: "$" is ambiguous between SGD and USD in a bank
  return new Intl.NumberFormat("en-SG", { style: "currency", currency: ccy, currencyDisplay: "code" })
    .format(minor / 100).replace(/\u00a0/g, " ");
}
function when(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-SG", { day: "2-digit", month: "short" }) + " " +
    d.toLocaleTimeString("en-SG", { hour: "2-digit", minute: "2-digit" });
}
function newKey() { // idempotency key; getRandomValues also works on plain-HTTP origins
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
const RAILS = {
  PAYNOW: { label: "PayNow / FAST", note: "Instant, SGD, up to S$200k" },
  MEPS: { label: "MEPS+", note: "High-value SGD, same day" },
  SWIFT: { label: "SWIFT", note: "Cross-border, any currency" },
};
const status = (s) => `<span class="status status-${esc(s)}">${esc(s.replace("_", " "))}</span>`;

class ApiError extends Error {
  constructor(status, message, trace) { super(message); this.status = status; this.trace = trace; }
}

async function api(path, opts = {}) {
  const headers = { "Accept": "application/json" };
  if (state.user) headers["X-Demo-User"] = state.user.id;
  if (opts.body) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  } catch (e) {
    throw new ApiError(0, "Cannot reach the bank portal. Check your connection.");
  }
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
  if (!res.ok) throw new ApiError(res.status, data.error || `Request failed (HTTP ${res.status})`, data.trace);
  return data;
}

function toast(msg, kind = "") {
  if (kind === "bad" && state.user) refreshHealth();
  const el = document.createElement("div");
  el.className = `toast ${kind ? "toast-" + kind : ""}`;
  el.textContent = msg;
  const box = $("#toasts");
  box.append(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => el.remove(), 6000);
}

// ------------------------------------------------------------------ trace viewer

const traces = new Map(); // id -> trace, so table buttons can open them
function traceButton(id, trace) {
  if (!trace) return "";
  traces.set(id, trace);
  return `<button class="link-btn" data-trace="${esc(id)}">Trace</button>`;
}

function showTrace(trace, title = "Processing trace") {
  $("#trace-title").textContent = title;
  const path = trace.path && trace.path.length ? trace.path : [trace.service];
  const hops = path.map((h) => `<li><span class="hop-dot">✓</span><span class="hop-name">${esc(h)}</span></li>`);
  if (!trace.ok) {
    hops.push(`<li class="hop-bad"><span class="hop-dot">✕</span><span>${esc(trace.error || "failed")}</span></li>`);
  }
  $("#trace-body").innerHTML = `
    <div class="alert ${trace.ok ? "alert-ok" : "alert-bad"}">
      <div><b>${trace.ok ? "Processed successfully" : "Processing failed"}</b>
      <p>Entry service <span class="mono">${esc(trace.service)}</span> · HTTP ${esc(trace.status_code || "-")} · ${esc(trace.latency_ms)} ms</p></div>
    </div>
    <h3>Services that handled this request</h3>
    <ol class="hops">${hops.join("")}</ol>
    <dl class="kv"><dt>Time</dt><dd>${esc(new Date(trace.at).toLocaleString("en-SG"))}</dd>
      ${trace.error ? `<dt>Error</dt><dd class="mono">${esc(trace.error)}</dd>` : ""}</dl>
    <p class="fineprint mt">This is what the platform team uses to find which hop broke.
      Correlate it with <span class="mono">./sim.sh status</span> and the pod logs.</p>`;
  $("#trace-dialog").showModal();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-trace]");
  if (b && traces.has(b.dataset.trace)) showTrace(traces.get(b.dataset.trace), b.dataset.trace);
});

// ------------------------------------------------------------------ login / shell

function renderLogin() {
  $("#login-users").innerHTML = state.meta.users.map((u) => `
    <button class="persona" data-user="${esc(u.id)}">
      <div class="avatar">${esc(initials(u.name))}</div>
      <div><b>${esc(u.name)}</b><span>${u.role === "maker" ? "Maker: initiates payments and trade" : "Checker: approves high-value payments"}</span></div>
    </button>`).join("");
  $("#login").hidden = false;
  $("#app").hidden = true;
  $("#login-users").querySelector("button")?.focus();
}
const initials = (n) => n.split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();

function signIn(id) {
  state.user = state.meta.users.find((u) => u.id === id);
  if (!state.user) return renderLogin();
  try { sessionStorage.setItem("banksim-user", id); } catch (_) { /* private mode */ }
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#user-name").textContent = state.user.name;
  $("#user-role").textContent = state.user.role;
  $("#user-avatar").textContent = initials(state.user.name);
  $("#company-name").textContent = state.meta.company;
  $("#build-version").textContent = "v" + state.meta.version;
  if (!location.hash) location.hash = "#/dashboard";
  route();
  pollHealth();
}

$("#login-users").addEventListener("click", (e) => {
  const b = e.target.closest("[data-user]");
  if (b) signIn(b.dataset.user);
});
$("#switch-user").addEventListener("click", () => {
  try { sessionStorage.removeItem("banksim-user"); } catch (_) { }
  state.user = null;
  clearInterval(state.healthTimer);
  renderLogin();
});
$("#nav-toggle").addEventListener("click", () => {
  const open = $("#sidenav").classList.toggle("open");
  $("#nav-toggle").setAttribute("aria-expanded", String(open));
});

function updatePill(services) {
  const pill = $("#health-pill");
  if (!services) {
    pill.className = "pill pill-muted";
    $(".pill-text", pill).textContent = "Status unavailable";
    return;
  }
  const down = services.filter(isDisrupted);
  pill.className = "pill " + (down.length ? "pill-bad" : "pill-ok");
  $(".pill-text", pill).textContent = down.length ? `${down.length} service${down.length > 1 ? "s" : ""} disrupted` : "All services operational";
}
const isDisrupted = (s) => !s.reachable || (s.recent_calls > 0 && s.success_pct < 90);

async function refreshHealth() {
  try { updatePill((await api("/api/status")).services); } catch (_) { updatePill(null); }
}
function pollHealth() {
  clearInterval(state.healthTimer);
  refreshHealth();
  state.healthTimer = setInterval(refreshHealth, 20000);
}

// ------------------------------------------------------------------ router

const views = { dashboard, payments, approvals, trade, onboarding, status: serviceStatus };

function route() {
  if (!state.user) return;
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1] || "dashboard";
  const view = views[name] || dashboard;
  document.querySelectorAll(".sidenav a").forEach((a) => a.classList.toggle("active", a.dataset.route === name));
  $("#sidenav").classList.remove("open");
  const main = $("#view");
  main.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
  view(main).catch((e) => {
    main.innerHTML = `<div class="alert alert-bad"><div><b>Something went wrong</b><p>${esc(e.message)}</p></div></div>`;
  });
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

async function refreshApprovalsBadge(count) {
  const el = $("#approvals-count");
  if (count === undefined) {
    try { count = (await api("/api/payments")).payments.filter((p) => p.status === "PENDING_APPROVAL").length; }
    catch (_) { return; }
  }
  el.textContent = count;
  el.hidden = !count;
}

// ------------------------------------------------------------------ views

async function dashboard(main) {
  let accounts = null, err = null, pending = 0;
  try {
    const r = await api("/api/accounts");
    accounts = r.accounts; pending = r.pending_approvals; state.accounts = accounts;
  } catch (e) { err = e; refreshHealth(); }
  const { transactions } = await api("/api/transactions");
  refreshApprovalsBadge(err ? undefined : pending);

  const totalSGD = accounts ? accounts.reduce((s, a) => s + a.balance_minor * state.meta.fx_to_sgd[a.currency] / 10000, 0) : 0;
  main.innerHTML = `
    <div class="page-head">
      <div><h1>Good ${greeting()}, ${esc(state.user.name.split(" ")[0])}</h1>
      <p class="muted">${esc(state.meta.company)} · ${new Date().toLocaleDateString("en-SG", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</p></div>
      <div class="form-actions">
        <a class="btn btn-primary" href="#/payments">New payment</a>
        <a class="btn btn-ghost" href="#/trade">Apply for LC</a>
      </div>
    </div>
    ${err ? `
      <div class="alert alert-bad" role="alert">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
        <div><b>${esc(err.message)}</b><p>We can't retrieve your balances right now. Payments you submit are not affected.</p></div>
        <div class="actions">${err.trace ? traceButton("accounts-error", err.trace) : ""}<button class="btn btn-ghost btn-sm" id="retry">Retry</button></div>
      </div>` : `
      <div class="card summary-strip">
        <div><span>Total balance (SGD equivalent)</span><b>${money(Math.round(totalSGD), "SGD")}</b></div>
        <div><span>Accounts</span><b>${accounts.length}</b></div>
        <div><span>Awaiting approval</span><b>${pending ? `<a href="#/approvals">${pending}</a>` : 0}</b></div>
      </div>
      <div class="grid grid-accounts">${accounts.map((a) => `
        <article class="card acct">
          <div class="label">${esc(a.name)}</div>
          <div class="balance">${money(a.balance_minor, a.currency)}</div>
          <div class="number">${esc(a.number)} · ${esc(a.currency)}</div>
        </article>`).join("")}
      </div>`}
    <section class="card">
      <div class="card-head"><h2>Recent activity</h2><span class="muted">${transactions.length} transactions</span></div>
      ${txnTable(transactions)}
    </section>`;
  $("#retry")?.addEventListener("click", route);
}
const greeting = () => { const h = new Date().getHours(); return h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"; };

function txnTable(txns) {
  if (!txns.length) return `<div class="empty">No transactions yet.</div>`;
  const acct = (id) => state.accounts.find((a) => a.id === id);
  return `<div class="table-wrap"><table>
    <thead><tr><th class="hide-sm">Date</th><th>Description</th><th class="hide-sm">Reference</th><th class="num">Amount</th></tr></thead>
    <tbody>${txns.map((t) => {
      const a = acct(t.account_id);
      const ccy = t.currency;
      return `<tr><td class="hide-sm">${esc(when(t.at))}</td>
        <td>${esc(t.description)}<span class="sub">${esc(a ? a.name : t.account_name)}<span class="show-sm"> · ${esc(when(t.at))}</span></span></td>
        <td class="hide-sm mono">${esc(t.reference)}</td>
        <td class="num ${t.amount_minor > 0 ? "credit" : ""}">${t.amount_minor > 0 ? "+" : ""}${money(t.amount_minor, ccy)}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

// ---------- payments

async function payments(main) {
  if (!state.accounts.length) {
    try { state.accounts = (await api("/api/accounts")).accounts; } catch (_) { /* balances unavailable; fallback below */ }
  }
  const { payments: list } = await api("/api/payments");
  const maker = state.user.role === "maker";
  const benes = state.meta.beneficiaries;
  const accts = state.accounts.length ? state.accounts : [
    { id: "sgd-ops", name: "Operating Account", currency: "SGD", balance_minor: null },
    { id: "usd-trade", name: "USD Trade Account", currency: "USD", balance_minor: null },
  ];
  let key = newKey(); // reused if the same submission is retried

  main.innerHTML = `
    <div class="page-head"><div><h1>Payments</h1>
      <p class="muted">Pay suppliers in Singapore and overseas. Payments above ${money(state.meta.approval_threshold_sgd_minor, "SGD")} need a checker's approval.</p></div></div>
    <div class="stack">
      <section class="card form-card">
        <div class="card-head"><h2>New payment</h2></div>
        ${maker ? `
        <form class="form" id="pay-form" novalidate>
          <div class="field"><label for="from">Pay from</label>
            <select id="from" required>${accts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} (${esc(a.currency)})${a.balance_minor != null ? " · " + money(a.balance_minor, a.currency) : ""}</option>`).join("")}</select></div>
          <div class="field"><label for="bene">Beneficiary</label>
            <select id="bene" required>${benes.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("")}</select>
            <span class="hint" id="bene-hint"></span></div>
          <div class="field"><span class="label">Payment method</span><div class="rails" id="rails"></div></div>
          <div class="field"><label for="amount">Amount</label>
            <div class="row"><input id="amount" inputmode="decimal" autocomplete="off" placeholder="0.00" required>
              <select id="ccy" aria-label="Currency">${Object.keys(state.meta.fx_to_sgd).map((c) => `<option>${c}</option>`).join("")}</select></div></div>
          <div class="field"><label for="ref">Reference</label>
            <input id="ref" maxlength="100" placeholder="e.g. Invoice INV-2026-1043" required></div>
          <div class="preview" id="preview" hidden></div>
          <div class="form-actions"><button class="btn btn-primary" id="pay-submit" type="submit">Submit payment</button>
            <span class="muted" id="pay-msg"></span></div>
        </form>` : `
        <div class="card-pad"><div class="alert alert-info"><div><b>You are signed in as a checker.</b>
          <p>Checkers approve payments; they cannot initiate them. <a href="#/approvals">Go to approvals</a> or switch user.</p></div></div></div>`}
      </section>
      <section class="card">
        <div class="card-head"><h2>Payment history</h2></div>
        ${paymentTable(list)}
      </section>
    </div>`;
  if (!maker) return;

  const f = {
    from: $("#from"), bene: $("#bene"), amount: $("#amount"), ccy: $("#ccy"), ref: $("#ref"),
    rails: $("#rails"), preview: $("#preview"), submit: $("#pay-submit"), msg: $("#pay-msg"),
  };
  const selectedRail = () => (main.querySelector("input[name=rail]:checked") || {}).value;

  function renderRails() {
    const b = benes.find((x) => x.id === f.bene.value);
    $("#bene-hint").textContent = `${b.bank} · ${b.account} · ${b.country}`;
    const prev = selectedRail();
    f.rails.innerHTML = Object.entries(RAILS).map(([k, r]) => `
      <div class="rail"><input type="radio" name="rail" id="rail-${k}" value="${k}" ${b.rails.includes(k) ? "" : "disabled"}>
        <label for="rail-${k}"><b>${r.label}</b><span>${r.note}</span></label></div>`).join("");
    const pick = b.rails.includes(prev) ? prev : b.rails[0];
    $(`#rail-${pick}`).checked = true;
    f.ccy.value = b.currency;
    syncCurrency();
  }
  function syncCurrency() {
    const domestic = ["PAYNOW", "MEPS"].includes(selectedRail());
    if (domestic) f.ccy.value = "SGD";
    f.ccy.disabled = domestic;
    renderPreview();
  }
  function renderPreview() {
    const amt = Number(f.amount.value.replace(/,/g, ""));
    const a = accts.find((x) => x.id === f.from.value);
    if (!(amt > 0) || !a) { f.preview.hidden = true; return; }
    const rate = state.meta.fx_to_sgd;
    const minor = Math.round(amt * 100);
    const sgd = minor * rate[f.ccy.value] / 10000;
    const debit = Math.round(minor * rate[f.ccy.value] / rate[a.currency]);
    const lines = [`<div><span>Beneficiary receives</span><b>${money(minor, f.ccy.value)}</b></div>`];
    if (f.ccy.value !== a.currency) {
      lines.push(`<div><span>Debited from ${esc(a.name)}</span><b>${money(debit, a.currency)}</b></div>`,
        `<div><span>Indicative rate</span><span>1 ${f.ccy.value} = ${(rate[f.ccy.value] / rate[a.currency]).toFixed(4)} ${a.currency}</span></div>`);
    }
    if (sgd > state.meta.approval_threshold_sgd_minor) lines.push(`<div><span>Approval</span><b class="muted">Needs a checker</b></div>`);
    if (selectedRail() === "PAYNOW" && sgd > state.meta.fast_limit_sgd_minor) lines.push(`<div><span>Limit</span><b class="bad-text">Above the PayNow limit, use MEPS+</b></div>`);
    if (a.balance_minor != null && debit > a.balance_minor) lines.push(`<div><span>Balance</span><b class="bad-text">Insufficient funds</b></div>`);
    f.preview.innerHTML = lines.join("");
    f.preview.hidden = false;
  }

  f.bene.addEventListener("change", renderRails);
  f.rails.addEventListener("change", syncCurrency);
  [f.amount, f.ccy, f.from].forEach((el) => el.addEventListener("input", renderPreview));
  [f.from, f.bene, f.amount, f.ccy, f.ref].forEach((el) => el.addEventListener("change", () => { key = newKey(); }));
  renderRails();

  $("#pay-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    f.amount.setAttribute("aria-invalid", String(!/^\d[\d,]*(\.\d{1,2})?$/.test(f.amount.value.trim())));
    f.ref.setAttribute("aria-invalid", String(!f.ref.value.trim()));
    if (main.querySelector("[aria-invalid=true]")) { f.msg.textContent = "Please check the highlighted fields."; return; }
    f.submit.disabled = true;
    f.submit.innerHTML = `<span class="spinner"></span> Processing…`;
    f.msg.textContent = "";
    try {
      const { payment: p, duplicate } = await api("/api/payments", {
        method: "POST", body: {
          from_account: f.from.value, beneficiary_id: f.bene.value, rail: selectedRail(),
          amount: f.amount.value.trim(), currency: f.ccy.value, reference: f.ref.value.trim(), idempotency_key: key,
        },
      });
      if (duplicate) toast(`Already submitted as ${p.id}. Not sent twice.`, "warn");
      else if (p.status === "COMPLETED") toast(`${p.id}: ${money(p.amount_minor, p.currency)} sent to ${p.beneficiary}.`);
      else if (p.status === "PENDING_APPROVAL") toast(`${p.id} is waiting for a checker's approval.`, "warn");
      else toast(`${p.id}: ${p.error}`, "bad");
      key = newKey();
      state.accounts = [];
      route();
    } catch (err) {
      f.msg.textContent = err.message;
      toast(err.message, "bad");
    } finally {
      f.submit.disabled = false;
      f.submit.textContent = "Submit payment";
    }
  });
}

function paymentTable(list, actions = false) {
  if (!list.length) return `<div class="empty">No payments yet.</div>`;
  return `<div class="table-wrap"><table>
    <thead><tr><th>Payment</th><th>Beneficiary</th><th class="num">Amount</th><th>Status</th>${actions ? "<th></th>" : ""}</tr></thead>
    <tbody>${list.map((p) => `<tr>
      <td><span class="mono">${esc(p.id)}</span><span class="sub">${esc(when(p.created_at))} · ${esc(RAILS[p.rail]?.label || p.rail)}</span></td>
      <td>${esc(p.beneficiary)}<span class="sub">${esc(p.reference)}</span></td>
      <td class="num">${money(p.amount_minor, p.currency)}${p.fx_rate ? `<span class="sub">${esc(p.fx_rate)}</span>` : ""}</td>
      <td>${status(p.status)}<span class="sub">${p.error ? esc(p.error) : esc(p.approved_by ? "by " + p.approved_by : "by " + p.created_by)}</span>
        ${traceButton(p.id, p.trace)}</td>
      ${actions ? `<td class="num">${state.user.role === "checker" ? `
        <button class="btn btn-ok btn-sm" data-approve="${esc(p.id)}">Approve</button>
        <button class="btn btn-bad btn-sm" data-reject="${esc(p.id)}">Reject</button>` : ""}</td>` : ""}
    </tr>`).join("")}</tbody></table></div>`;
}

// ---------- approvals

async function approvals(main) {
  const { payments: all } = await api("/api/payments");
  const pending = all.filter((p) => p.status === "PENDING_APPROVAL");
  refreshApprovalsBadge(pending.length);
  const checker = state.user.role === "checker";
  main.innerHTML = `
    <div class="page-head"><div><h1>Approvals</h1>
      <p class="muted">Maker-checker control: payments above ${money(state.meta.approval_threshold_sgd_minor, "SGD")} need a second person. You can never approve your own payment.</p></div></div>
    ${checker ? "" : `<div class="alert alert-info"><div><b>Signed in as a maker</b><p>Switch to a checker (Priya Nair) to approve or reject.</p></div></div>`}
    <section class="card"><div class="card-head"><h2>Waiting for approval</h2><span class="muted">${pending.length}</span></div>
      ${paymentTable(pending, true)}</section>`;
}

// One delegated listener for approve / reject buttons (views re-render often).
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-approve],[data-reject]");
  if (!b || b.disabled) return;
  const id = b.dataset.approve || b.dataset.reject;
  const action = b.dataset.approve ? "approve" : "reject";
  b.closest("tr")?.querySelectorAll("button").forEach((x) => { x.disabled = true; });
  try {
    const { payment: p } = await api(`/api/payments/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    toast(p.status === "COMPLETED" ? `${p.id} approved and sent.` : p.status === "REJECTED" ? `${p.id} rejected.` : `${p.id} approved but failed: ${p.error}`,
      p.status === "FAILED" ? "bad" : "");
    route();
  } catch (err) {
    toast(err.message, "bad");
    b.closest("tr")?.querySelectorAll("button").forEach((x) => { x.disabled = false; });
  }
});

// ---------- trade finance

async function trade(main) {
  const { lcs } = await api("/api/lcs");
  const maker = state.user.role === "maker";
  const minExp = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const defExp = new Date(Date.now() + 90 * 86400000).toISOString().slice(0, 10);
  main.innerHTML = `
    <div class="page-head"><div><h1>Trade finance</h1><p class="muted">Apply for import letters of credit (LCs). Issuance runs sanctions screening and an account check.</p></div></div>
    <div class="grid grid-2">
      <section class="card"><div class="card-head"><h2>Apply for a letter of credit</h2></div>
        ${maker ? `<form class="form" id="lc-form" novalidate>
          <div class="field"><label for="lc-acct">Applicant account</label>
            <select id="lc-acct"><option value="usd-trade">USD Trade Account</option><option value="sgd-ops">Operating Account (SGD)</option><option value="eur-coll">EUR Collections</option></select></div>
          <div class="row-2">
            <div class="field"><label for="lc-bene">Beneficiary (seller)</label><input id="lc-bene" maxlength="100" value="Shenzhen Components Co Ltd"></div>
            <div class="field"><label for="lc-ctry">Country</label><input id="lc-ctry" maxlength="2" value="CN" class="upper"></div>
          </div>
          <div class="field"><label for="lc-amt">Amount</label>
            <div class="row"><input id="lc-amt" inputmode="decimal" placeholder="0.00" value="250000">
              <select id="lc-ccy"><option>USD</option><option>EUR</option><option>SGD</option></select></div></div>
          <div class="row-2">
            <div class="field"><label for="lc-exp">Expiry date</label><input id="lc-exp" type="date" min="${minExp}" value="${defExp}"></div>
            <div class="field"><label for="lc-goods">Goods</label><input id="lc-goods" maxlength="100" value="Electronic components, 40ft container x2"></div>
          </div>
          <div class="form-actions"><button class="btn btn-primary" type="submit" id="lc-submit">Submit application</button><span class="muted" id="lc-msg"></span></div>
        </form>` : `<div class="card-pad muted">Only makers can apply for letters of credit.</div>`}
      </section>
      <section class="card"><div class="card-head"><h2>Your letters of credit</h2></div>
        ${lcs.length ? `<div class="table-wrap"><table><thead><tr><th>LC</th><th>Beneficiary</th><th class="num">Amount</th><th>Status</th></tr></thead><tbody>
          ${lcs.map((l) => `<tr><td><span class="mono">${esc(l.id)}</span><span class="sub">expires ${esc(l.expiry)}</span></td>
            <td>${esc(l.beneficiary)}<span class="sub">${esc(l.country)} · ${esc(l.goods)}</span></td>
            <td class="num">${money(l.amount_minor, l.currency)}</td>
            <td>${status(l.status)}${l.error ? `<span class="sub">${esc(l.error)}</span>` : ""} ${traceButton(l.id, l.trace)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">No letters of credit yet.</div>`}
      </section>
    </div>`;
  $("#lc-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#lc-submit");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Submitting…`;
    try {
      const { lc } = await api("/api/lcs", { method: "POST", body: {
        applicant_account: $("#lc-acct").value, beneficiary: $("#lc-bene").value.trim(), country: $("#lc-ctry").value.trim().toUpperCase(),
        amount: $("#lc-amt").value.trim(), currency: $("#lc-ccy").value, expiry: $("#lc-exp").value, goods: $("#lc-goods").value.trim(),
      } });
      toast(lc.status === "ISSUED" ? `${lc.id} issued.` : `${lc.id} failed: ${lc.error}`, lc.status === "ISSUED" ? "" : "bad");
      route();
    } catch (err) { $("#lc-msg").textContent = err.message; btn.disabled = false; btn.textContent = "Submit application"; }
  });
}

// ---------- onboarding / KYC

async function onboarding(main) {
  const { cases } = await api("/api/kyc");
  const kinds = { "new-beneficiary": "Add a new beneficiary", "new-subsidiary": "Onboard a subsidiary", "periodic-review": "Periodic KYC review" };
  main.innerHTML = `
    <div class="page-head"><div><h1>Onboarding &amp; KYC</h1><p class="muted">Know-your-customer requests run sanctions screening, then notify you by email.</p></div></div>
    <div class="grid grid-2">
      <section class="card"><div class="card-head"><h2>New request</h2></div>
        <form class="form" id="kyc-form" novalidate>
          <div class="field"><label for="kyc-kind">Request type</label>
            <select id="kyc-kind">${Object.entries(kinds).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
          <div class="row-2">
            <div class="field"><label for="kyc-entity">Entity name</label><input id="kyc-entity" maxlength="100" value="Mekong Agri Trading Co"></div>
            <div class="field"><label for="kyc-ctry">Country</label><input id="kyc-ctry" maxlength="2" value="VN" class="upper"></div>
          </div>
          <div class="form-actions"><button class="btn btn-primary" type="submit" id="kyc-submit">Submit request</button><span class="muted" id="kyc-msg"></span></div>
        </form></section>
      <section class="card"><div class="card-head"><h2>Requests</h2></div>
        ${cases.length ? `<div class="table-wrap"><table><thead><tr><th>Case</th><th>Entity</th><th>Status</th></tr></thead><tbody>
          ${cases.map((c) => `<tr><td><span class="mono">${esc(c.id)}</span><span class="sub">${esc(kinds[c.kind] || c.kind)}</span></td>
            <td>${esc(c.entity)}<span class="sub">${esc(c.country)} · ${esc(when(c.created_at))}</span></td>
            <td>${status(c.status)}${c.error ? `<span class="sub">${esc(c.error)}</span>` : ""} ${traceButton(c.id, c.trace)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">No requests yet.</div>`}
      </section>
    </div>`;
  $("#kyc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#kyc-submit");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Submitting…`;
    try {
      const { case: c } = await api("/api/kyc", { method: "POST", body: {
        kind: $("#kyc-kind").value, entity: $("#kyc-entity").value.trim(), country: $("#kyc-ctry").value.trim().toUpperCase(),
      } });
      toast(c.status === "SUBMITTED" ? `${c.id} submitted. Screening passed.` : `${c.id} failed: ${c.error}`, c.status === "SUBMITTED" ? "" : "bad");
      route();
    } catch (err) { $("#kyc-msg").textContent = err.message; btn.disabled = false; btn.textContent = "Submit request"; }
  });
}

// ---------- service status

async function serviceStatus(main) {
  const { services, checked_at } = await api("/api/status");
  updatePill(services);
  main.innerHTML = `
    <div class="page-head"><div><h1>Service status</h1>
      <p class="muted">What the portal depends on, as seen from the portal. Checked ${esc(when(checked_at))}.</p></div>
      <button class="btn btn-ghost" id="recheck">Check again</button></div>
    <div class="grid svc-grid">${services.map((s) => {
      const bad = isDisrupted(s);
      const warn = !bad && s.recent_calls > 0 && s.success_pct < 99;
      return `<article class="card svc">
        <div class="svc-top"><h3>${esc(s.label)}</h3>
          <span class="pill ${bad ? "pill-bad" : warn ? "pill-warn" : "pill-ok"}"><span class="dot"></span>${bad ? "Disrupted" : warn ? "Degraded" : "Operational"}</span></div>
        <div class="muted small">${esc(s.used_for)}</div>
        <dl><dt>Health check</dt><dd>${s.reachable ? "reachable" : esc(s.probe_error || "unreachable")}</dd>
          <dt>Recent requests</dt><dd>${s.recent_calls ? `${s.success_pct}% ok of ${s.recent_calls}` : "none yet"}</dd>
          <dt>Average latency</dt><dd>${s.recent_calls ? s.avg_latency_ms + " ms" : "-"}</dd></dl>
        ${s.last_error ? `<div class="err">${esc(s.last_error)}<br><span class="muted">${esc(when(s.last_error_at))}</span></div>` : ""}
      </article>`;
    }).join("")}</div>
    <p class="fineprint mt">Health check = the service answers <span class="mono">/healthz</span>.
      Recent requests = real customer actions from this portal. A service can pass its health check and still fail requests, because its own dependencies may be down.</p>`;
  $("#recheck").addEventListener("click", route);
}

// ------------------------------------------------------------------ boot

(async function boot() {
  try {
    state.meta = await (await fetch("/api/meta")).json();
  } catch (e) {
    document.body.innerHTML = `<div class="login"><div class="login-card"><h1>Portal unavailable</h1><p class="muted">The bank portal backend cannot be reached.</p></div></div>`;
    return;
  }
  let saved = null;
  try { saved = sessionStorage.getItem("banksim-user"); } catch (_) { }
  if (saved && state.meta.users.some((u) => u.id === saved)) signIn(saved); else renderLogin();
})();
