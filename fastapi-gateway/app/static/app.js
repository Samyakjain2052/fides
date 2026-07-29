/* ==========================================================================
   DSAR Console — behaviour
   Vanilla JS, no build step, no external requests. Talks only to this
   gateway's own API (same origin), so there is no CORS to configure.

   Endpoints used:
     GET  /health
     GET  /data/subject/{email}
     POST /data/subject
     POST /dsar
     GET  /dsar/{id}
   ========================================================================== */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* --------------------------------------------------------------------------
   The data-category map, mirroring fides-config/resources/*.yml.
   `erasable: true` means the erasure policy's targets cover that category, so
   the column is nulled by an erasure. Kept here (rather than derived) because
   Fides exposes the dataset over an authenticated admin API the browser has no
   business holding a token for — the gateway deliberately never proxies it.
   If you edit a dataset YAML, edit this too.
   -------------------------------------------------------------------------- */
const CATEGORIES = {
  users: {
    id: ["user.unique_id", false],
    email: ["user.contact.email", true],
    full_name: ["user.name", true],
    phone: ["user.contact.phone_number", true],
    created_at: ["system.operations", false],
  },
  orders: {
    id: ["system.operations", false],
    user_email: ["user.contact.email", true],
    amount: ["user.behavior.purchase_history", false],
    item: ["user.behavior.purchase_history", false],
    created_at: ["system.operations", false],
  },
  events: {
    _id: ["system.operations", false],
    email: ["user.contact.email", true],
    event_type: ["user.behavior", false],
    "metadata.ip_address": ["user.device.ip_address", true],
    "metadata.user_agent": ["user.device", true],
    "metadata.session_id": ["user.unique_id.pseudonymous", true],
    timestamp: ["system.operations", false],
  },
};

/* Fides privacy-request statuses -> pill class + icon-ish label. Status colour
   is never the only signal: the label spells the state out. */
const STATUS = {
  complete: ["good", "complete"],
  error: ["bad", "error"],
  canceled: ["serious", "canceled"],
  denied: ["serious", "denied"],
  paused: ["serious", "paused"],
  requires_input: ["serious", "requires input"],
  identity_unverified: ["serious", "identity unverified"],
  pending: ["warn busy", "pending"],
  in_processing: ["warn busy", "in processing"],
  approved: ["warn busy", "approved"],
};

const state = { email: "demo@example.com", poll: null, adminUrl: null, lastLog: null };

/* ----------------------------------------------------------------- helpers */
async function api(path, options) {
  const resp = await fetch(path, options);
  let body = null;
  try {
    body = await resp.json();
  } catch (_) {
    /* empty or non-JSON body */
  }
  if (!resp.ok) {
    const detail = body && body.detail !== undefined ? body.detail : resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function setPill(node, cls, label) {
  node.className = "pill " + (cls || "");
  node.textContent = "";
  node.appendChild(el("span", "dot"));
  node.appendChild(document.createTextNode(label));
}

function banner(node, kind, icon, text) {
  node.className = "banner " + kind;
  node.textContent = "";
  node.appendChild(el("span", "icon", icon));
  node.appendChild(el("span", null, text));
  node.classList.remove("hidden");
}

function show(sel, visible) {
  $(sel).classList.toggle("hidden", !visible);
}

/* Values arrive already JSON-safe from the API (ObjectId and Decimal are
   stringified server-side). Only nulls need special rendering — and they matter:
   a null here is what an erasure looks like. */
function cellFor(value) {
  if (value === null || value === undefined) {
    const td = el("td", "null", "null");
    return td;
  }
  if (typeof value === "object") return el("td", null, JSON.stringify(value));
  return el("td", null, String(value));
}

function flatten(row, prefix) {
  const out = {};
  for (const [key, value] of Object.entries(row)) {
    const path = prefix ? prefix + "." + key : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      Object.assign(out, flatten(value, path));
    } else {
      out[path] = value;
    }
  }
  return out;
}

/* Column order: whatever the declared category map says, then anything else the
   row happens to carry (so an undeclared field is visible rather than dropped). */
function columnsFor(collection, rows) {
  const declared = Object.keys(CATEGORIES[collection] || {});
  const seen = new Set();
  rows.forEach((row) => Object.keys(flatten(row)).forEach((k) => seen.add(k)));
  const cols = declared.filter((c) => seen.has(c));
  [...seen].forEach((c) => {
    if (!cols.includes(c)) cols.push(c);
  });
  return cols;
}

function tableFor(collection, rows) {
  const cols = columnsFor(collection, rows);
  const table = el("table");
  const thead = el("thead");
  const tr = el("tr");
  cols.forEach((col) => {
    const th = el("th");
    th.appendChild(document.createTextNode(col));
    const meta = (CATEGORIES[collection] || {})[col];
    if (meta) {
      const cat = el("span", "col-cat" + (meta[1] ? " erasable" : ""), meta[0]);
      th.appendChild(cat);
    }
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  table.appendChild(thead);

  const tbody = el("tbody");
  rows.forEach((row) => {
    const flat = flatten(row);
    const rowEl = el("tr");
    cols.forEach((col) => rowEl.appendChild(cellFor(flat[col])));
    tbody.appendChild(rowEl);
  });
  table.appendChild(tbody);

  const scroll = el("div", "scroll");
  scroll.appendChild(table);
  return scroll;
}

/* ------------------------------------------------------------------ health */
async function loadHealth() {
  try {
    const h = await api("/health");
    setPill($("#health-gateway"), "good", "gateway");
    setPill($("#health-fides"), h.fides && h.fides.webserver === "healthy" ? "good" : "warn", "Fides");
    const dbs = h.app_databases || {};
    setPill($("#health-db1"), dbs.app_postgres === "ok" ? "good" : "bad", "db1 postgres");
    setPill($("#health-db2"), dbs.app_mongo === "ok" ? "good" : "bad", "db2 mongo");

    state.adminUrl = h.fides_admin_url || null;
    const links = $("#links");
    links.textContent = "";
    if (state.adminUrl) {
      const a = el("a", null, "Fides Admin UI");
      a.href = state.adminUrl;
      a.target = "_blank";
      a.rel = "noopener";
      links.appendChild(a);
      links.appendChild(document.createTextNode(" · "));
    }
    const docs = el("a", null, "OpenAPI / Swagger");
    docs.href = "/docs";
    links.appendChild(docs);
    $("#version").textContent =
      h.fides && h.fides.version ? "  ·  Fides " + h.fides.version : "";
  } catch (err) {
    setPill($("#health-gateway"), "bad", "gateway unreachable");
  }
}

/* ------------------------------------------------------------------ locate */
function renderDatabase(cardId, data, cls) {
  const card = $(cardId);
  card.textContent = "";

  const head = el("div", "db-head");
  head.appendChild(el("h3", null, data.label));
  head.appendChild(el("span", "count", data.total + " record" + (data.total === 1 ? "" : "s")));
  card.appendChild(head);

  const meta = el("div", "db-meta");
  meta.textContent = data.host + "  ·  database " + data.database + "  ·  dataset " + data.fides_dataset;
  card.appendChild(meta);

  Object.entries(data.collections).forEach(([name, coll]) => {
    const block = el("div", "collection");
    const ch = el("div", "collection-head");
    const label = el("span", "name");
    label.appendChild(el("span", "swatch " + cls));
    label.appendChild(document.createTextNode(" " + name));
    ch.appendChild(label);
    ch.appendChild(el("span", "count", String(coll.count)));
    block.appendChild(ch);

    if (coll.count === 0) {
      block.appendChild(el("div", "empty", "No rows match this identity."));
    } else if (coll.rows.length === 0) {
      block.appendChild(el("div", "empty", coll.count + " row(s) — rows hidden."));
    } else {
      block.appendChild(tableFor(name, coll.rows));
    }
    card.appendChild(block);
  });
}

async function locate(quiet) {
  const email = $("#email").value.trim();
  if (!email) return;
  state.email = email;
  $("#subject-error").classList.add("hidden");

  try {
    const data = await api("/data/subject/" + encodeURIComponent(email));

    $("#kpi-total").textContent = data.total_records;
    $("#kpi-total-foot").textContent = data.found
      ? "across " + data.found_in.length + " database" + (data.found_in.length === 1 ? "" : "s")
      : "nothing matches this identity";
    $("#kpi-db1").textContent = data.db1_app_postgres.total;
    $("#kpi-db2").textContent = data.db2_app_mongo.total;

    const masked = data.masked_rows_remaining || {};
    const maskedTotal = Object.values(masked).reduce((a, b) => a + b, 0);
    $("#kpi-masked").textContent = maskedTotal;
    $("#kpi-masked-foot").textContent = Object.entries(masked)
      .map(([k, v]) => k + " " + v)
      .join(" · ");

    renderDatabase("#card-db1", data.db1_app_postgres, "db1");
    renderDatabase("#card-db2", data.db2_app_mongo, "db2");

    const note = $("#locate-note");
    if (data.note) {
      banner(note, data.found ? "" : "good", data.found ? "i" : "✓", data.note);
    } else {
      note.classList.add("hidden");
    }

    $("#found-for").textContent = email;
    $("#map-for").textContent = email;
    show("#kpis-section", true);
    show("#map-section", true);
  } catch (err) {
    if (!quiet) banner($("#subject-error"), "bad", "✕", "Lookup failed: " + err.message);
  }
}

/* -------------------------------------------------------------------- DSAR */
/* Fides logs every state change, so a finished collection appears twice:
   `in_processing / starting` and then `complete / success - retrieved N records`.
   Both are real audit entries, but the superseded one is noise in a proof view —
   so the default collapses to the OUTCOME per collection+action, and the
   "show every entry" checkbox reveals the untouched trail. Nothing is dropped:
   `?include_raw_log=true` on the API returns Fides' own records verbatim. */
function collapseLog(rows) {
  const latest = new Map();
  rows.forEach((e) => {
    // Entries arrive chronologically, so last write per key is the outcome.
    latest.set(e.dataset + "|" + e.collection + "|" + e.action_type, e);
  });
  return [...latest.values()];
}

function renderLog(entries) {
  const tbody = $("#dsar-log tbody");
  tbody.textContent = "";
  let rows = entries.filter((e) => e.collection);
  if (!rows.length) {
    show("#dsar-log-wrap", false);
    return;
  }
  const showAll = $("#log-all").checked;
  const full = rows.length;
  if (!showAll) rows = collapseLog(rows);
  state.lastLog = entries;
  $("#log-count").textContent =
    showAll ? full + " entries" : rows.length + " of " + full + " entries";
  rows.forEach((e) => {
    const tr = el("tr");

    const dbCell = el("td");
    const isMongo = (e.dataset || "").includes("mongo");
    dbCell.appendChild(el("span", "swatch " + (isMongo ? "db2" : "db1")));
    dbCell.appendChild(document.createTextNode(isMongo ? " db2 mongo" : " db1 postgres"));
    tr.appendChild(dbCell);

    tr.appendChild(el("td", "mono", e.dataset + " : " + e.collection));
    tr.appendChild(el("td", null, e.action_type || "—"));

    const statusCell = el("td");
    const pill = el("span");
    const [cls, label] = STATUS[e.status] || ["", e.status || "—"];
    setPill(pill, cls.replace(" busy", ""), label);
    statusCell.appendChild(pill);
    tr.appendChild(statusCell);

    tr.appendChild(el("td", null, e.message || "—"));

    const fieldsCell = el("td");
    const chips = el("div", "chips");
    (e.fields_affected || []).forEach((f) => {
      // Trim the `dataset:collection:` prefix — the row already says which.
      const parts = f.split(":");
      chips.appendChild(el("span", "chip", parts[parts.length - 1]));
    });
    fieldsCell.appendChild(chips);
    tr.appendChild(fieldsCell);

    tbody.appendChild(tr);
  });
  show("#dsar-log-wrap", true);
}

function renderReturnedData(data) {
  const host = $("#dsar-data");
  host.textContent = "";
  const keys = Object.keys(data || {});
  if (!keys.length) {
    host.appendChild(
      el(
        "div",
        "empty",
        "The access package is empty — no record in either database matches this identity."
      )
    );
    show("#dsar-data-wrap", true);
    return;
  }
  keys.forEach((key) => {
    const collection = key.split(":")[1] || key;
    const rows = data[key] || [];
    const block = el("div", "collection");
    const ch = el("div", "collection-head");
    const isMongo = key.includes("mongo");
    const label = el("span", "name");
    label.appendChild(el("span", "swatch " + (isMongo ? "db2" : "db1")));
    label.appendChild(document.createTextNode(" " + key));
    ch.appendChild(label);
    ch.appendChild(el("span", "count", String(rows.length)));
    block.appendChild(ch);
    block.appendChild(rows.length ? tableFor(collection, rows) : el("div", "empty", "No rows."));
    host.appendChild(block);
  });
  show("#dsar-data-wrap", true);
}

/* A privacy request is a durable artifact — ?request=<id> reopens one, so a link
   is enough to hand someone the proof. */
function setRequestInUrl(requestId, email) {
  const url = new URL(window.location.href);
  url.searchParams.set("request", requestId);
  if (email) url.searchParams.set("subject", email);
  window.history.replaceState({}, "", url);
}

async function openRequestFromUrl() {
  const params = new URL(window.location.href).searchParams;
  const requestId = params.get("request");
  if (!requestId) return;
  // Point the data map at the same person the request was for, so a shared link
  // shows the request AND the databases as they now stand for that subject.
  const subject = params.get("subject");
  if (subject) {
    $("#email").value = subject;
    await locate(true);
  }
  show("#dsar-section", true);
  $("#dsar-title").textContent = "Privacy request";
  $("#dsar-id").textContent = requestId;
  setPill($("#dsar-status"), "warn busy", "loading");
  try {
    const d = await refreshDsar(requestId, null);
    $("#dsar-title").textContent =
      (d.action === "erasure" ? "Erasure" : "Access") + " request";
    // Reopened mid-flight: keep watching it.
    if (!["complete", "error", "canceled", "denied"].includes(d.status)) {
      state.poll = setInterval(() => refreshDsar(requestId, d.action).catch(() => {}), 2000);
    }
  } catch (err) {
    setPill($("#dsar-status"), "bad", "not found");
    banner($("#dsar-banner"), "bad", "✕", "Could not load that request: " + err.message);
  }
}

function stopPolling() {
  if (state.poll) {
    clearInterval(state.poll);
    state.poll = null;
  }
}

async function refreshDsar(requestId, action) {
  const d = await api("/dsar/" + encodeURIComponent(requestId));
  const [cls, label] = STATUS[d.status] || ["", d.status || "unknown"];
  setPill($("#dsar-status"), cls, label);

  const kv = $("#dsar-kv");
  kv.textContent = "";
  const pairs = [
    ["policy", d.policy_key || "—"],
    ["action", d.action || action],
    ["created", d.created_at || "—"],
    ["finished", d.finished_processing_at || "still running"],
    ["collections", (d.collections_touched || []).length],
  ];
  pairs.forEach(([k, v]) => {
    const span = el("span");
    span.appendChild(document.createTextNode(k + " "));
    span.appendChild(el("b", null, String(v)));
    kv.appendChild(span);
  });

  renderLog(d.execution_log || []);
  if (d.action === "access" || action === "access") {
    if (d.status === "complete") renderReturnedData(d.data || {});
  } else {
    show("#dsar-data-wrap", false);
  }

  const settled = ["complete", "error", "canceled", "denied"].includes(d.status);
  if (settled) {
    stopPolling();
    const bnr = $("#dsar-banner");
    if (d.status === "complete") {
      const touched = (d.collections_touched || []).length;
      banner(
        bnr,
        "good",
        "✓",
        (d.action === "erasure"
          ? "Erasure complete across " + touched + " collections in both databases. "
          : "Access request complete — " + touched + " collections returned. ") +
          "The execution log below is the per-collection record of what happened."
      );
    } else {
      banner(bnr, "bad", "✕", "Request ended with status: " + d.status + ".");
    }
    // The point of the demo: show the databases as they are NOW.
    locate(true);
  }
  return d;
}

async function runDsar(action) {
  const email = $("#email").value.trim();
  if (!email) return;
  if (action === "erasure") {
    const ok = window.confirm(
      "Erase " + email + " across BOTH databases?\n\n" +
        "Fides will null their contact details, name and device identifiers in " +
        "app-postgres and app-mongo. This cannot be undone."
    );
    if (!ok) return;
  }

  stopPolling();
  $("#subject-error").classList.add("hidden");
  $("#dsar-banner").classList.add("hidden");
  show("#dsar-section", true);
  show("#dsar-log-wrap", false);
  show("#dsar-data-wrap", false);
  $("#dsar-title").textContent =
    (action === "erasure" ? "Erasure" : "Access") + " request for " + email;
  $("#dsar-id").textContent = "creating…";
  setPill($("#dsar-status"), "warn busy", "creating");
  $("#dsar-section").scrollIntoView({ behavior: "smooth", block: "start" });

  let created;
  try {
    created = await api("/dsar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, action }),
    });
  } catch (err) {
    setPill($("#dsar-status"), "bad", "failed");
    banner($("#dsar-banner"), "bad", "✕", "Fides rejected the request: " + err.message);
    return;
  }

  $("#dsar-id").textContent = created.request_id;
  setPill($("#dsar-status"), "warn busy", created.status || "pending");
  // Make the request linkable: the URL now identifies this piece of evidence, so
  // it can be pasted into a ticket and reopened later.
  setRequestInUrl(created.request_id, email);

  await refreshDsar(created.request_id, action).catch(() => {});
  // Fides queues the request to its Celery worker, so the first read is almost
  // always still in_processing. Poll until it settles.
  let ticks = 0;
  state.poll = setInterval(async () => {
    ticks += 1;
    if (ticks > 60) {
      stopPolling();
      banner(
        $("#dsar-banner"),
        "bad",
        "!",
        "Still running after two minutes. Check `docker compose logs fides-worker`."
      );
      return;
    }
    try {
      await refreshDsar(created.request_id, action);
    } catch (err) {
      /* transient; keep polling */
    }
  }, 2000);
}

/* ---------------------------------------------------------------- add data */
function orderRow(amount, item) {
  const row = el("div", "row-item order");
  const a = el("input");
  a.type = "text";
  a.placeholder = "24.00";
  a.className = "f-amount";
  a.value = amount || "";
  a.setAttribute("aria-label", "Order amount");
  const i = el("input");
  i.type = "text";
  i.placeholder = "Mechanical keyboard";
  i.className = "f-item";
  i.value = item || "";
  i.setAttribute("aria-label", "Order item");
  const rm = el("button", "ghost", "✕");
  rm.type = "button";
  rm.title = "Remove this order";
  rm.addEventListener("click", () => row.remove());
  row.append(a, i, rm);
  return row;
}

function eventRow(type, ip, ua, session) {
  const row = el("div", "row-item event");
  const mk = (cls, ph, val, aria) => {
    const input = el("input");
    input.type = "text";
    input.className = cls;
    input.placeholder = ph;
    input.value = val || "";
    input.setAttribute("aria-label", aria);
    return input;
  };
  const rm = el("button", "ghost", "✕");
  rm.type = "button";
  rm.title = "Remove this event";
  rm.addEventListener("click", () => row.remove());
  row.append(
    mk("f-type", "login", type, "Event type"),
    mk("f-ip", "203.0.113.99", ip, "IP address"),
    mk("f-ua", "Mozilla/5.0", ua, "User agent"),
    mk("f-session", "sess_newp01", session, "Session id"),
    rm
  );
  return row;
}

function collectSubject() {
  const orders = [...document.querySelectorAll("#orders .row-item")]
    .map((row) => ({
      amount: row.querySelector(".f-amount").value.trim(),
      item: row.querySelector(".f-item").value.trim(),
    }))
    .filter((o) => o.amount && o.item);

  const events = [...document.querySelectorAll("#events .row-item")]
    .map((row) => ({
      event_type: row.querySelector(".f-type").value.trim(),
      ip_address: row.querySelector(".f-ip").value.trim() || null,
      user_agent: row.querySelector(".f-ua").value.trim() || null,
      session_id: row.querySelector(".f-session").value.trim() || null,
    }))
    .filter((e) => e.event_type);

  const body = { email: $("#f-email").value.trim(), orders, events };
  const name = $("#f-name").value.trim();
  const phone = $("#f-phone").value.trim();
  if (name) body.full_name = name;
  if (phone) body.phone = phone;
  return body;
}

async function submitSubject() {
  const body = collectSubject();
  const out = $("#form-result");
  if (!body.email) {
    banner(out, "bad", "✕", "An email is required — it is the identity everything hangs off.");
    return;
  }
  const button = $("#submit-subject");
  button.disabled = true;
  try {
    const created = await api("/data/subject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    banner(out, "good", "✓", "Written — " + created.written_to.join("  ·  "));
    // Look the new person up straight away, so the write is visible as data.
    $("#email").value = created.email;
    await locate();
  } catch (err) {
    banner(out, "bad", "✕", "Write failed: " + err.message);
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------- theme */
function initTheme() {
  // ?theme=dark|light forces a mode, so a link can carry the view it was seen in
  // (and so the page can be rendered head-lessly in either mode). Otherwise the
  // remembered choice wins, and failing that the OS setting does.
  const forced = new URL(window.location.href).searchParams.get("theme");
  const saved = forced === "dark" || forced === "light" ? forced : localStorage.getItem("dsar-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  $("#theme").addEventListener("click", () => {
    const current =
      document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("dsar-theme", next);
  });
}

/* -------------------------------------------------------------------- wire */
function init() {
  initTheme();

  $("#locate").addEventListener("click", () => locate());
  $("#access").addEventListener("click", () => runDsar("access"));
  $("#erasure").addEventListener("click", () => runDsar("erasure"));
  $("#email").addEventListener("keydown", (e) => {
    if (e.key === "Enter") locate();
  });
  document.querySelectorAll(".quick-pick").forEach((b) =>
    b.addEventListener("click", () => {
      $("#email").value = b.dataset.email;
      locate();
    })
  );

  $("#log-all").addEventListener("change", () => {
    if (state.lastLog) renderLog(state.lastLog);
  });

  $("#add-order").addEventListener("click", () => $("#orders").appendChild(orderRow()));
  $("#add-event").addEventListener("click", () => $("#events").appendChild(eventRow()));
  $("#submit-subject").addEventListener("click", submitSubject);
  $("#fill-sample").addEventListener("click", () => {
    $("#f-email").value = "newperson@example.com";
    $("#f-name").value = "New Person";
    $("#f-phone").value = "+1-555-0142";
    $("#orders").textContent = "";
    $("#events").textContent = "";
    $("#orders").appendChild(orderRow("24.00", "Mechanical keyboard"));
    $("#orders").appendChild(orderRow("9.99", "Mouse pad"));
    $("#events").appendChild(
      eventRow("login", "203.0.113.99", "Mozilla/5.0 (X11; Linux x86_64)", "sess_newp01")
    );
    $("#events").appendChild(
      eventRow("checkout", "203.0.113.99", "Mozilla/5.0 (X11; Linux x86_64)", "sess_newp01")
    );
  });

  $("#orders").appendChild(orderRow());
  $("#events").appendChild(eventRow());

  loadHealth();
  locate(true);
  openRequestFromUrl();
  setInterval(loadHealth, 30000);
}

document.addEventListener("DOMContentLoaded", init);
