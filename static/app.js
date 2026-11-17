// Dashboard client. All server-provided text is inserted with textContent, never innerHTML.
// Charts are hand-built SVG from existing API data only (no chart library, no invented metrics).
"use strict";

const REFRESH_MS = 60_000;
const TZ = "America/New_York";
const DAY = 86_400_000;
const fmt = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const fmtShort = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const fmtTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const fmtDayLbl = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", month: "numeric", day: "numeric" });
const fmtMonthDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });
const dayKeyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ });

const SEV = { critical: ["▲▲▲▲", "Critical"], high: ["▲▲▲", "High"], moderate: ["▲▲", "Moderate"], low: ["▲", "Low"], unknown: ["?", "Severity unknown"] };
const CONF = { high: ["●●●", "High confidence"], medium: ["●●○", "Medium confidence"], low: ["●○○", "Low confidence"] };
const EV = { confirmed: ["✔", "Confirmed"], attributed: ["◐", "Attributed report"], unverified: ["⚠", "Unverified signal"], dismissed: ["✕", "Dismissed"] };
const EV_ORDER = ["confirmed", "attributed", "unverified"];
const SCOPE = { national: "National", multi_state: "Multi-state", state: "State", local: "Local", unknown: "Unknown" };
const FRESH = { fresh: ["✔", "fresh", "good"], stale: ["◷", "stale", "warning"], error: ["✕", "error", "critical"],
                never_polled: ["–", "never polled", "unknown"], disabled: ["○", "disabled", "unknown"] };

let STATUS = null, ALL = [], ALERTS = [];
let operatorToken = ""; // kept in memory only; never stored
let COLORS = {};

// ------------------------------------------------------------------ small helpers
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  setAttrs(n, attrs);
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
function svg(tag, attrs = {}, ...children) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setAttrs(n, attrs);
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
function setAttrs(n, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.setAttribute("class", v);
    else if (k === "text") n.textContent = v;
    else if (k === "style") for (const [p, val] of Object.entries(v)) n.style.setProperty(p.startsWith("--") ? p : p.replace(/[A-Z]/g, m => `-${m.toLowerCase()}`), val);
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
}
const ms = iso => { const d = iso ? new Date(iso).getTime() : NaN; return Number.isNaN(d) ? null : d; };
function t(iso, approx) { const d = ms(iso); return d === null ? "unknown" : (approx ? "≈ " : "") + fmt.format(d); }
function tShort(iso, approx) { const d = ms(iso); return d === null ? "unknown" : (approx ? "≈ " : "") + fmtShort.format(d); }
function ago(iso) {
  const d = ms(iso); if (d === null) return "";
  const m = Math.round((Date.now() - d) / 60000);
  if (m < 1) return "just now"; if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60); return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}
function dur(a, b) {
  if (a === null || b === null || b < a) return null;
  const m = Math.round((b - a) / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  return [d ? `${d}d` : "", h ? `${h}h` : "", !d && mm ? `${mm}m` : ""].filter(Boolean).join(" ") || "0m";
}
const dayKey = msv => dayKeyFmt.format(msv);
function safeHref(url) { try { const u = new URL(url); return (u.protocol === "https:" || u.protocol === "http:") ? u.href : null; } catch { return null; } }
function link(url, label) {
  const href = safeHref(url);
  return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, label || href) : el("span", { text: label || "(invalid link)" });
}
function chip(cls, [glyph, label], title) { return el("span", { class: `chip ${cls}`, title }, el("span", { class: "cg", "aria-hidden": "true" }, glyph), label); }
const evChip = c => chip(`ev-${c}`, EV[c] || ["", c], "Evidence class");
const sevChip = s => chip(`sev-${s}`, SEV[s] || ["", s], "Severity");
const confChip = c => chip("conf", CONF[c.level], c.reasons.join(" "));
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json", ...(opts.headers || {}) }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `HTTP ${r.status}`);
  return body;
}
function readColors() {
  const cs = getComputedStyle(document.documentElement);
  for (const k of ["ev-confirmed", "ev-attributed", "ev-unverified", "ev-dismissed", "good", "warning", "critical", "unknown", "neutral", "surface-1", "text-2", "text-3"])
    COLORS[k] = cs.getPropertyValue(`--${k}`).trim();
}
function windowBounds() {
  return STATUS ? [ms(STATUS.window_start), ms(STATUS.window_end)] : [Date.now() - 7 * DAY, Date.now()];
}
const anchorOf = c => ms(c.event_start) ?? ms(c.first_reported);
function inWindow(c) { const [a, b] = windowBounds(), x = anchorOf(c); return x !== null && x >= a && x < b; }

// ------------------------------------------------------------------ tooltip
const tip = () => document.getElementById("tooltip");
function showTip(target, title, rows) {
  const tt = tip();
  tt.replaceChildren(el("div", { class: "tt-title", text: title }),
    ...rows.map(([k, v]) => el("div", { class: "tt-row" }, el("span", { text: k }), el("span", { text: v }))));
  tt.hidden = false;
  const r = target.getBoundingClientRect(), w = tt.offsetWidth, h = tt.offsetHeight;
  let x = r.left + r.width / 2 - w / 2, y = r.top - h - 10;
  if (y < 8) y = r.bottom + 10;
  x = Math.max(8, Math.min(x, window.innerWidth - w - 8));
  tt.style.left = `${x}px`; tt.style.top = `${y}px`;
}
function hideTip() { tip().hidden = true; }
function bindMark(node, chartEl, title, rows, onActivate) {
  const on = () => { chartEl.classList.add("dim"); showTip(node, title, rows); };
  const off = () => { chartEl.classList.remove("dim"); hideTip(); };
  node.addEventListener("mouseenter", on); node.addEventListener("mouseleave", off);
  node.addEventListener("focus", on); node.addEventListener("blur", off);
  if (onActivate) {
    node.addEventListener("click", onActivate);
    node.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onActivate(); } });
  }
}
function legend(id, items) {
  document.getElementById(id).replaceChildren(...items.map(([color, label, ring]) =>
    el("span", { class: "key" }, el("span", { class: `sw${ring ? " ring" : ""}`, style: { "--c": color } }), label)));
}
function simpleTable(head, rows, numericCols = []) {
  return el("div", { class: "table-wrap" }, el("table", { class: "grid" },
    el("thead", {}, el("tr", {}, ...head.map((h, i) => el("th", { scope: "col", class: numericCols.includes(i) ? "num" : null, text: h })))),
    el("tbody", {}, ...(rows.length ? rows.map(r => el("tr", {}, ...r.map((v, i) => el("td", { class: numericCols.includes(i) ? "num" : null }, v)))) :
      [el("tr", {}, el("td", { colspan: head.length, class: "hint", text: "No rows." }))]))));
}

// ------------------------------------------------------------------ status, advisories, sources
function stateCell(freshness) {
  const [g, label, tone] = FRESH[freshness] || ["?", freshness, "unknown"];
  return el("span", { class: "state-cell" }, el("span", { class: "dot", style: { background: COLORS[tone] }, "aria-hidden": "true" }), `${g} ${label}`);
}
function renderStatus(s) {
  const [ws, we] = [ms(s.window_start), ms(s.window_end)];
  document.getElementById("window-label").textContent = `Reporting window: ${s.window_label_et}`;
  document.getElementById("window-short").textContent = `${fmtMonthDay.format(ws)} – ${fmtMonthDay.format(we - 1000)}`;
  document.getElementById("last-checked").textContent = s.last_checked ? fmtTime.format(ms(s.last_checked)) : "never";
  document.getElementById("last-checked-age").textContent = s.last_checked ? `· ${ago(s.last_checked)}` : "";
  const enabled = s.sources.filter(x => x.enabled);
  const bad = enabled.filter(x => x.freshness === "error" || x.freshness === "stale");
  const dot = document.getElementById("poller-dot");
  dot.className = `dot ${!s.poller_enabled ? "" : bad.length ? "warn" : s.last_checked ? "ok" : "warn"}`;
  document.getElementById("poller-state").textContent = s.poller_enabled ? (bad.length ? "Running · degraded" : "Running") : "Off";

  document.querySelector("#sources-table tbody").replaceChildren(...s.sources.map(src => el("tr", {},
    el("td", {}, link(src.url, src.name)), el("td", { class: "mono", text: src.tier }),
    el("td", {}, stateCell(src.freshness), src.consecutive_failures ? el("div", { class: "hint", text: `${src.consecutive_failures} consecutive failures` }) : null),
    el("td", { class: "mono", text: src.last_success ? `${tShort(src.last_success)} · ${ago(src.last_success)}` : "—" }),
    el("td", { text: src.last_error || "—" }), el("td", { class: "hint", text: src.terms_note || "" }))));
  document.getElementById("source-summary").textContent =
    `${enabled.length} enabled · ${bad.length} stale or failing · ${s.sources.length - enabled.length} disabled`;

  const adv = [];
  if (!s.poller_enabled) adv.push(["warn", "!", "Poller off.", "No live polling is running. Only stored data is shown."]);
  else if (!s.last_checked) adv.push(["warn", "…", "Waiting for first poll.", "No feed has been checked yet."]);
  if (bad.length) adv.push(["error", "✕", "Source problems.", bad.map(b => `${b.name} (${b.freshness})`).join("; ")]);
  if (s.research_seed_loaded) adv.push(["info", "i", "Research snapshot.", "Incidents labelled \"analyst research\" were compiled 2026-11-17 from cited sources. They are a dated snapshot, not live data."]);
  adv.push(["info", "i", "Coverage.", "Only the enabled feeds listed under Sources are monitored. Absence of an alert does not mean there is no outage."]);
  document.getElementById("banners").replaceChildren(...adv.map(([k, ico, h, m]) =>
    el("div", { class: `advisory ${k}`, role: k === "error" ? "alert" : null }, el("span", { class: "ico", "aria-hidden": "true", text: ico }), el("div", {}, el("b", { text: h }), m))));
}

// ------------------------------------------------------------------ overview: KPIs
function renderKpis() {
  const inw = ALL.filter(inWindow);
  const n = k => inw.filter(c => c.evidence_class === k).length;
  const open = ALERTS.filter(a => !a.acknowledged_at).length;
  const enabled = STATUS ? STATUS.sources.filter(s => s.enabled) : [];
  const fresh = enabled.filter(s => s.freshness === "fresh").length;
  const tiles = [
    ["Confirmed", n("confirmed"), "in reporting window", COLORS["ev-confirmed"], "✔"],
    ["Attributed", n("attributed"), "in window · not yet confirmed", COLORS["ev-attributed"], "◐"],
    ["Unverified", n("unverified"), "in window · awaiting review", COLORS["ev-unverified"], "⚠"],
    ["Open alerts", open, `of ${ALERTS.length} recorded`, open ? COLORS.warning : null, open ? "●" : "○"],
    ["Feeds fresh", [fresh, `/${enabled.length}`], enabled.length - fresh ? `${enabled.length - fresh} stale, failing or unpolled` : "all enabled feeds current", fresh === enabled.length && enabled.length ? COLORS.good : COLORS.warning, "◆"],
    ["Last poll", STATUS && STATUS.last_checked ? ago(STATUS.last_checked).replace(" ago", "") : "—", STATUS && STATUS.last_checked ? `at ${fmtTime.format(ms(STATUS.last_checked))}` : "no poll yet", null, "◷"],
  ];
  document.getElementById("kpis").replaceChildren(...tiles.map(([label, value, sub, color, glyph]) =>
    el("div", { class: "kpi", role: "listitem", style: color ? { "--kpi-color": color } : {} },
      el("div", { class: "k-label" }, el("span", { class: "glyph", "aria-hidden": "true", text: glyph }), label),
      el("div", { class: "k-value" }, Array.isArray(value) ? [String(value[0]), el("small", { text: value[1] })] : String(value)),
      el("div", { class: "k-sub", text: sub }))));
}

// ------------------------------------------------------------------ overview: incident spans
function renderSpans() {
  const host = document.getElementById("chart-spans");
  const [ws, we] = windowBounds(), now = Date.now();
  const live = ALL.filter(c => c.evidence_class !== "dismissed");
  const rows = live.filter(c => { const s = ms(c.event_start); if (s === null) return false; const e = ms(c.event_end) ?? Math.min(now, we); return s < we && e >= ws; })
    .sort((a, b) => ms(a.event_start) - ms(b.event_start));
  const signals = live.filter(c => ms(c.event_start) === null && inWindow(c));
  legend("spans-legend", [[COLORS["ev-confirmed"], "Confirmed"], [COLORS["ev-attributed"], "Attributed"], [COLORS["ev-unverified"], "Unverified"],
    [COLORS["text-3"], "Faded end = approximate time"], [COLORS["text-3"], "Ring = first report, no recorded start", true]]);

  const W = Math.max(300, host.clientWidth || 600), narrow = W < 620;
  const labelW = narrow ? 0 : Math.min(300, Math.round(W * 0.27));
  const rowH = narrow ? 46 : 34, top = 36, plotL = labelW + (narrow ? 4 : 12), plotR = W - 8;
  const sigRows = []; // pack signal dots into sub-rows to avoid overlap
  const x = v => plotL + ((Math.min(Math.max(v, ws), we) - ws) / (we - ws)) * (plotR - plotL);
  for (const c of signals) {
    const cx = x(anchorOf(c)); let r = sigRows.findIndex(row => row.every(px => Math.abs(px - cx) > 12));
    if (r === -1) { sigRows.push([]); r = sigRows.length - 1; } sigRows[r].push(cx); c._sigRow = r; c._cx = cx;
  }
  const sigH = signals.length ? 30 + Math.min(sigRows.length, 6) * 14 : 0;
  const H = top + rows.length * rowH + sigH + 8;
  if (!rows.length && !signals.length) {
    host.replaceChildren(el("div", { class: "chart-empty", text: "No incidents with a recorded start, and no first reports, inside the reporting window." }));
    document.getElementById("table-spans").replaceChildren();
    return;
  }
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Incident spans across the reporting window: ${rows.length} incidents with recorded starts and ${signals.length} first reports without a start.` });
  const defs = svg("defs");
  s.append(defs);
  // day grid + labels
  for (let i = 0; i <= 7; i++) {
    const d = ws + i * DAY, gx = x(d);
    s.append(svg("line", { class: i === 0 || i === 7 ? "axis" : "gridline", x1: gx, x2: gx, y1: top - 6, y2: H - 4 }));
    if (i < 7) {
      const cx = (gx + x(ws + (i + 1) * DAY)) / 2;
      const [wd, md] = fmtDayLbl.format(d + DAY / 2).split(", ");
      s.append(svg("text", { x: cx, y: 10, "text-anchor": "middle", class: "lbl-2" }, wd), svg("text", { x: cx, y: 22, "text-anchor": "middle" }, md));
    }
  }
  s.append(svg("text", { x: plotR, y: H - 2, "text-anchor": "end" }, "00:00 ET day boundaries"));
  if (now > ws && now < we) s.append(svg("line", { class: "nowline", x1: x(now), x2: x(now), y1: top - 6, y2: H - 4 }));

  let gid = 0;
  const grad = (color, fadeStart) => {
    const id = `g${gid++}`;
    defs.append(svg("linearGradient", { id, x1: "0", x2: "1", y1: "0", y2: "0" },
      svg("stop", { offset: "0", "stop-color": color, "stop-opacity": fadeStart ? 0.15 : 1 }),
      svg("stop", { offset: fadeStart ? "0.18" : "0.82", "stop-color": color, "stop-opacity": 1 }),
      svg("stop", { offset: "1", "stop-color": color, "stop-opacity": fadeStart ? 1 : 0.15 })));
    return `url(#${id})`;
  };
  rows.forEach((c, i) => {
    const y0 = top + i * rowH, barY = narrow ? y0 + 22 : y0 + (rowH - 12) / 2;
    const sMs = ms(c.event_start), eMs = ms(c.event_end);
    const open = eMs === null && c.status !== "resolved";
    const endMs = eMs ?? (open ? Math.min(now, we) : sMs);
    const x0 = x(sMs), x1 = Math.max(x(endMs), x0 + 6);
    const color = COLORS[`ev-${c.evidence_class}`] || COLORS.neutral;
    const fill = c.event_start_approx && (c.event_end_approx || open) ? null : c.event_start_approx ? grad(color, true) : (c.event_end_approx || open) ? grad(color, false) : color;
    const g = svg("g", { class: "mark", tabindex: 0, role: "button",
      "aria-label": `${c.title}. ${EV[c.evidence_class][1]}. Start ${t(c.event_start, c.event_start_approx)}. End ${eMs !== null ? t(c.event_end, c.event_end_approx) : open ? "ongoing" : "not recorded"}.` });
    if (labelW) {
      const maxChars = Math.floor((labelW - 8) / 6.6);
      const label = c.title.length > maxChars ? c.title.slice(0, maxChars - 1) + "…" : c.title;
      g.append(svg("text", { x: 0, y: y0 + rowH / 2 - 3, class: "lbl-1" }, label),
               svg("text", { x: 0, y: y0 + rowH / 2 + 10 }, c.organization.toUpperCase()));
    } else {
      g.append(svg("text", { x: plotL, y: y0 + 13, class: "lbl-1" }, `${c.organization} · ${c.title.slice(0, Math.floor((plotR - plotL) / 6.6) - c.organization.length - 4)}…`));
    }
    g.append(svg("rect", { class: "hit", x: x0 - 4, y: barY - 6, width: x1 - x0 + 8, height: 24, rx: 4, fill: "transparent" }));
    if (fill === null) {
      // both ends approximate: solid core with two faded ends
      const mid = svg("rect", { x: x0, y: barY, width: x1 - x0, height: 12, rx: 4, fill: color, "fill-opacity": 0.55 });
      g.append(mid, svg("rect", { x: x0 + (x1 - x0) * .15, y: barY, width: (x1 - x0) * .7, height: 12, fill: color }));
    } else {
      g.append(svg("rect", { x: x0, y: barY, width: x1 - x0, height: 12, rx: 4, fill }));
    }
    if (eMs === null && !open) g.append(svg("text", { x: x1 + 6, y: barY + 10 }, "end not recorded"));
    if (open) g.append(svg("text", { x: x1 + 6, y: barY + 10, class: "lbl-2" }, "ongoing"));
    const durTxt = dur(sMs, eMs);
    bindMark(g, host, c.title, [["Evidence", EV[c.evidence_class][1]], ["Severity", SEV[c.severity][1]],
      ["Start", tShort(c.event_start, c.event_start_approx)], ["End", eMs !== null ? tShort(c.event_end, c.event_end_approx) : open ? "ongoing" : "not recorded"],
      ["Duration", durTxt ? ((c.event_start_approx || c.event_end_approx) ? "≈ " : "") + durTxt : "—"], ["", "Click for detail"]], () => openDetail(c.id));
    s.append(g);
  });
  if (signals.length) {
    const y0 = top + rows.length * rowH + 6;
    s.append(svg("line", { class: "gridline", x1: 0, x2: plotR, y1: y0, y2: y0 }));
    s.append(svg("text", { x: labelW ? 0 : plotL, y: y0 + 16, class: "lbl-2" }, labelW ? "First reports" : "First reports (no recorded start)"));
    if (labelW) s.append(svg("text", { x: 0, y: y0 + 29 }, "NO RECORDED START"));
    const shown = Math.min(sigRows.length, 6);
    for (const c of signals) {
      if (c._sigRow >= shown) continue;
      const cy = y0 + (labelW ? 14 : 30) + c._sigRow * 14;
      const color = COLORS[`ev-${c.evidence_class}`];
      const g = svg("g", { class: "mark", tabindex: 0, role: "button", "aria-label": `${c.title}. ${EV[c.evidence_class][1]}. First reported ${t(c.first_reported)}.` },
        svg("circle", { class: "hit", cx: c._cx, cy, r: 9, fill: "transparent" }),
        svg("circle", { cx: c._cx, cy, r: 5, fill: COLORS["surface-1"], stroke: color, "stroke-width": 2.5 }));
      bindMark(g, host, c.title, [["Organization", c.organization], ["Evidence", EV[c.evidence_class][1]], ["First reported", tShort(c.first_reported)], ["", "Click for detail"]], () => openDetail(c.id));
      s.append(g);
    }
    if (sigRows.length > shown) s.append(svg("text", { x: plotR, y: y0 + 16, "text-anchor": "end" }, `+${signals.filter(c => c._sigRow >= shown).length} more in the Incidents list`));
  }
  host.replaceChildren(s);
  document.getElementById("table-spans").replaceChildren(simpleTable(["Incident", "Evidence", "Start (ET)", "End (ET)", "Duration"],
    rows.map(c => [c.title, EV[c.evidence_class][1], t(c.event_start, c.event_start_approx), c.event_end ? t(c.event_end, c.event_end_approx) : (c.status === "resolved" ? "not recorded" : "ongoing"),
      dur(ms(c.event_start), ms(c.event_end)) ? ((c.event_start_approx || c.event_end_approx) ? "≈ " : "") + dur(ms(c.event_start), ms(c.event_end)) : "—"])
      .concat(signals.map(c => [c.title, EV[c.evidence_class][1], `first reported ${t(c.first_reported)}`, "—", "—"]))));
}

// ------------------------------------------------------------------ overview: first reports per day
function renderDaily() {
  const host = document.getElementById("chart-daily");
  const [ws] = windowBounds();
  const days = Array.from({ length: 7 }, (_, i) => ({ key: dayKey(ws + i * DAY + DAY / 2), mid: ws + i * DAY + DAY / 2, counts: { confirmed: 0, attributed: 0, unverified: 0 } }));
  const idx = Object.fromEntries(days.map((d, i) => [d.key, i]));
  for (const c of ALL) {
    if (!EV_ORDER.includes(c.evidence_class)) continue;
    const a = ms(c.first_reported) ?? ms(c.event_start); if (a === null) continue;
    const i = idx[dayKey(a)]; if (i !== undefined) days[i].counts[c.evidence_class]++;
  }
  legend("daily-legend", EV_ORDER.map(k => [COLORS[`ev-${k}`], EV[k][1]]));
  const totals = days.map(d => EV_ORDER.reduce((s, k) => s + d.counts[k], 0));
  const max = Math.max(1, ...totals);
  const step = max <= 5 ? 1 : max <= 10 ? 2 : max <= 25 ? 5 : 10, yMax = Math.ceil(max / step) * step;
  const W = Math.max(280, host.clientWidth || 400), H = 230, L = 28, R = 4, T = 14, B = 36;
  const ph = H - T - B, bw = Math.min(34, (W - L - R) / 7 * 0.56), y = v => T + ph - (v / yMax) * ph;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `First reports per ET day: ${days.map((d, i) => `${fmtDayLbl.format(d.mid)} ${totals[i]}`).join(", ")}.` });
  for (let v = 0; v <= yMax; v += step) {
    s.append(svg("line", { class: v === 0 ? "axis" : "gridline", x1: L, x2: W - R, y1: y(v), y2: y(v) }), svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, v));
  }
  days.forEach((d, i) => {
    const cx = L + ((W - L - R) / 7) * (i + 0.5);
    const [wd, md] = fmtDayLbl.format(d.mid).split(", ");
    s.append(svg("text", { x: cx, y: H - 14, "text-anchor": "middle", class: "lbl-2" }, wd), svg("text", { x: cx, y: H - 2, "text-anchor": "middle" }, md));
    let acc = 0;
    const segs = EV_ORDER.filter(k => d.counts[k] > 0);
    segs.forEach((k, j) => {
      const v = d.counts[k], yTop = y(acc + v), yBot = y(acc), last = j === segs.length - 1;
      const hgt = Math.max(1, yBot - yTop - (j > 0 ? 2 : 0)); // 2px surface gap between stacked segments
      const g = svg("g", { class: "mark", tabindex: 0, role: "img", "aria-label": `${fmtDayLbl.format(d.mid)}: ${v} ${EV[k][1]}` });
      g.append(svg("rect", { class: "hit", x: cx - bw / 2 - 4, y: yTop - 2, width: bw + 8, height: hgt + 4, fill: "transparent" }));
      // rounded data-end only on the top segment; base stays square on the baseline
      if (last && hgt > 4) {
        g.append(svg("path", { fill: COLORS[`ev-${k}`], d: `M${cx - bw / 2},${yTop + hgt} V${yTop + 4} Q${cx - bw / 2},${yTop} ${cx - bw / 2 + 4},${yTop} H${cx + bw / 2 - 4} Q${cx + bw / 2},${yTop} ${cx + bw / 2},${yTop + 4} V${yTop + hgt} Z` }));
      } else g.append(svg("rect", { x: cx - bw / 2, y: yTop, width: bw, height: hgt, fill: COLORS[`ev-${k}`] }));
      bindMark(g, host, fmtDayLbl.format(d.mid), [[EV[k][1], String(v)], ["Day total", String(totals[i])]]);
      s.append(g);
      acc += v;
    });
    if (totals[i]) s.append(svg("text", { x: cx, y: y(totals[i]) - 5, "text-anchor": "middle", class: "lbl-2" }, totals[i]));
  });
  host.replaceChildren(s);
  document.getElementById("table-daily").replaceChildren(simpleTable(["Day (ET)", ...EV_ORDER.map(k => EV[k][1]), "Total"],
    days.map((d, i) => [fmtDayLbl.format(d.mid), ...EV_ORDER.map(k => String(d.counts[k])), String(totals[i])]), [1, 2, 3, 4]));
}

// ------------------------------------------------------------------ overview: severity + freshness meter lists
function renderSeverity() {
  const host = document.getElementById("chart-severity");
  const inw = ALL.filter(c => inWindow(c) && c.evidence_class !== "dismissed");
  const keys = ["critical", "high", "moderate", "low", "unknown"];
  const counts = Object.fromEntries(keys.map(k => [k, inw.filter(c => c.severity === k).length]));
  const max = Math.max(1, ...Object.values(counts));
  if (!inw.length) { host.replaceChildren(el("div", { class: "chart-empty", text: "No candidates in the reporting window." })); return; }
  host.replaceChildren(...keys.map(k => el("div", { class: "meter", role: "group", "aria-label": `${SEV[k][1]}: ${counts[k]}` },
    el("span", { class: "m-name" }, el("span", { class: "g", "aria-hidden": "true", text: SEV[k][0] }), SEV[k][1]),
    el("span", { class: "m-track", "aria-hidden": "true" }, counts[k] ? el("span", { class: "m-bar", style: { width: `${(counts[k] / max) * 100}%`, "--c": COLORS.neutral } }) : null),
    el("span", { class: "m-val", text: String(counts[k]) }))),
    el("p", { class: "hint", text: `${inw.length} candidates · bar length relative to the largest group` }));
}
function renderFreshness() {
  const host = document.getElementById("chart-freshness");
  if (!STATUS) return;
  const now = Date.now();
  const rows = STATUS.sources.map(s => {
    const age = s.last_success ? (now - ms(s.last_success)) / 60000 : null;
    return { s, age, limit: 3 * (s.poll_minutes || 10) };
  });
  const scale = Math.max(...rows.filter(r => r.s.enabled).map(r => Math.max(r.limit * 1.25, r.age ?? 0)), 1);
  host.replaceChildren(...rows.map(({ s, age, limit }) => {
    const freshness = s.enabled ? s.freshness : "disabled";
    const [g, label, tone] = FRESH[freshness] || FRESH.never_polled;
    const row = el("div", { class: `meter${s.enabled ? "" : " disabled"}`, tabindex: 0,
      "aria-label": `${s.name}: ${label}${age !== null ? `, last success ${Math.round(age)} minutes ago, stale after ${limit} minutes` : ""}` },
      el("span", { class: "m-name", text: s.name.replace(/\s*\(.*\)$/, "") }),
      el("span", { class: "m-track", "aria-hidden": "true" },
        s.enabled && age !== null ? el("span", { class: "m-bar", style: { width: `${Math.min(age / scale, 1) * 100}%`, "--c": COLORS[tone] } }) : null,
        s.enabled ? el("span", { class: "m-tick", style: { left: `calc(${(limit / scale) * 100}% - 1px)` } }) : null),
      el("span", { class: "m-val" }, s.enabled && age !== null ? `${Math.round(age)}m` : "—", el("div", { class: "m-state", text: `${g} ${label}` })));
    const tipRows = [["State", label], ["Poll every", s.poll_minutes ? `${s.poll_minutes} min` : "—"], ["Stale after", `${limit} min`],
      ["Last success", s.last_success ? tShort(s.last_success) : "never"]];
    if (s.last_error) tipRows.push(["Last error", s.last_error.slice(0, 60)]);
    if (!s.enabled) tipRows.push(["Note", (s.terms_note || "").slice(0, 60) + "…"]);
    bindMark(row, host, s.name, tipRows);
    return row;
  }), el("p", { class: "hint", text: "Bar = minutes since last successful poll (m). Tick = stale threshold." }));
}
function renderOverview() { renderKpis(); renderSpans(); renderDaily(); renderSeverity(); renderFreshness(); }

// ------------------------------------------------------------------ incident lists
function card(c) {
  const conf = c.confidence;
  return el("button", { type: "button", class: "card", style: { "--c": COLORS[`ev-${c.evidence_class}`] }, onclick: () => openDetail(c.id), "aria-label": `Open details: ${c.title}` },
    el("div", { class: "c-org" }, el("span", { text: `${c.organization} · ${c.sector}` }),
      c.open_alerts ? el("span", { class: "alerts-flag", text: `● ${c.open_alerts} open alert${c.open_alerts > 1 ? "s" : ""}` }) : null),
    el("p", { class: "title", text: c.title }),
    el("div", { class: "row" }, evChip(c.evidence_class), sevChip(c.severity), confChip(conf),
      c.origin === "analyst_research" ? chip("origin", ["◇", "analyst research"], "Seeded from the cited research snapshot") : null),
    el("dl", { class: "facts" },
      ...[["Start", tShort(c.event_start, c.event_start_approx)], ["Status", c.status], ["Scope", `${SCOPE[c.scope]} (${c.scope_basis})`],
        ["Latest update", tShort(c.last_source_update)], ["First reported", tShort(c.first_reported)], ["Sources", `${c.source_count} · ${c.source_tiers.join(", ") || "—"}`]]
        .map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v })))),
    el("p", { class: "why", text: `Why included: ${c.inclusion_reason}` }));
}
function emptyState(msg, loading) { return el("div", { class: `state${loading ? " loading" : ""}`, role: "status" }, msg); }

async function loadCandidates() {
  const form = new FormData(document.getElementById("filters"));
  const params = new URLSearchParams();
  for (const [k, v] of form.entries()) if (v) params.set(k, v);
  const active = [...params.keys()].length;
  document.getElementById("filter-count").textContent = active ? `(${active})` : "";
  const lists = { confirmed: [], attributed: [], unverified: [] };
  for (const id of Object.keys(lists)) document.getElementById(`list-${id}`).replaceChildren(emptyState("Loading…", true));
  let data;
  try { data = await api(`/api/candidates?${params}`); }
  catch (e) {
    for (const id of Object.keys(lists)) document.getElementById(`list-${id}`).replaceChildren(el("div", { class: "advisory error", role: "alert" }, el("span", { class: "ico", text: "✕" }), `Could not load incidents: ${e.message}`));
    return;
  }
  for (const c of data) (lists[c.evidence_class] || lists.unverified).push(c);
  for (const [k, arr] of Object.entries(lists)) {
    document.getElementById(`n-${k}`).textContent = String(arr.length);
    document.querySelector(`[data-n="${k}"]`).textContent = String(arr.length);
    const empty = k === "confirmed" ? (active ? "No confirmed incidents match these filters."
      : "No verified incidents. Nothing has been confirmed by an operator with primary or independent evidence.")
      : k === "attributed" ? "No attributed reports." : "No unverified signals. Enabled feeds have produced nothing that needs review.";
    document.getElementById(`list-${k}`).replaceChildren(...(arr.length ? arr.map(card) : [emptyState(empty)]));
  }
}

async function loadAlerts() {
  const tb = document.querySelector("#alerts-table tbody");
  try { ALERTS = await api("/api/alerts?limit=500"); }
  catch (e) { tb.replaceChildren(el("tr", {}, el("td", { colspan: 6, text: `Could not load alerts: ${e.message}` }))); return; }
  const open = ALERTS.filter(a => !a.acknowledged_at).length;
  document.getElementById("alerts-note").textContent = `${open} open · ${ALERTS.length} recorded${ALERTS.length > 100 ? " · showing latest 100" : ""}`;
  tb.replaceChildren(...(ALERTS.length ? ALERTS.slice(0, 100).map(a => el("tr", {},
    el("td", { class: "mono", text: tShort(a.last_seen_at) }), el("td", { class: "mono", text: a.kind.replaceAll("_", " ") }),
    el("td", {}, el("button", { type: "button", class: "ghost", onclick: () => openDetail(a.candidate_id) }, a.title)),
    el("td", { text: a.message }), el("td", { class: "num", text: `${a.occurrences}×` }),
    el("td", {}, a.acknowledged_at ? el("span", { class: "state-cell" }, el("span", { class: "dot", style: { background: COLORS.good } }), `✔ ${a.acknowledged_by}, ${tShort(a.acknowledged_at)}`)
      : el("span", { class: "state-cell" }, el("span", { class: "dot", style: { background: COLORS.warning } }), "● open"))))
    : [el("tr", {}, el("td", { colspan: 6, class: "hint", text: "No alerts yet." }))]));
}

// ------------------------------------------------------------------ detail view
function table(head, rows) {
  return el("div", { class: "table-wrap" }, el("table", { class: "grid" },
    el("thead", {}, el("tr", {}, ...head.map(h => el("th", { scope: "col", text: h })))),
    el("tbody", {}, ...(rows.length ? rows : [el("tr", {}, el("td", { colspan: head.length, class: "hint", text: "None." }))]))));
}

async function openDetail(id) {
  hideTip();
  const dlg = document.getElementById("detail");
  const body = document.getElementById("d-body");
  body.replaceChildren(emptyState("Loading…", true));
  if (!dlg.open) dlg.showModal();
  let d;
  try { d = await api(`/api/candidates/${id}`); }
  catch (e) { body.replaceChildren(el("div", { class: "advisory error" }, el("span", { class: "ico", text: "✕" }), `Could not load: ${e.message}`)); return; }
  const c = d.candidate;
  const srcById = Object.fromEntries(d.sources.map(s => [s.id, s]));
  const durTxt = dur(ms(c.event_start), ms(c.event_end));
  document.getElementById("d-title").textContent = c.title;
  document.getElementById("d-eyebrow").textContent = `${c.organization} · ${c.sector} · ${c.origin === "analyst_research" ? "analyst research" : "feed poller"}`;
  body.replaceChildren(
    el("div", { class: "row" }, evChip(c.evidence_class), sevChip(c.severity), confChip(c.confidence)),
    el("dl", { class: "facts-grid" }, ...[
      ["Event start", t(c.event_start, c.event_start_approx)], ["Event end", t(c.event_end, c.event_end_approx)],
      ["Duration", durTxt ? ((c.event_start_approx || c.event_end_approx) ? "≈ " : "") + durTxt : "unknown"], ["Status", c.status],
      ["First reported", t(c.first_reported)], ["Latest source update", t(c.last_source_update)],
      ["Scope", `${SCOPE[c.scope]} — ${c.scope_basis}`], ["Service", c.service]]
      .map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v })))),
    el("dl", { class: "prose-grid" }, ...[
      ["Impact", c.impact || "Not yet described."], ["Scope detail", c.scope_detail || "No detail recorded."],
      ["Why included", c.inclusion_reason], ["Confidence", c.confidence.reasons.join(" ")],
      ["Origin", c.origin === "analyst_research" ? "Analyst research snapshot (cited, dated)" : "Automated feed poller"]]
      .flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
    el("p", { class: "hint", text: "≈ marks approximate times. Event times describe what happened; report/update times describe when a source published. All times ET." }),
    el("h3", { text: "Timeline" }),
    table(["Event time (ET)", "Evidence", "What happened", "Source"], d.timeline.map(e => el("tr", {},
      el("td", { class: "mono", text: t(e.event_at, e.approximate) }), el("td", { class: "mono", text: e.evidence }), el("td", { text: e.text }),
      el("td", {}, srcById[e.source_item_id] ? link(srcById[e.source_item_id].url, srcById[e.source_item_id].publisher) : "—")))),
    el("h3", { text: "Sources" }),
    table(["Tier", "Publisher", "Title / link", "Published (ET)", "Fetched by monitor (ET)"], d.sources.map(s => el("tr", {},
      el("td", { class: "mono", text: s.tier }), el("td", { text: s.publisher }),
      el("td", {}, link(s.url, s.title), s.summary ? el("div", { class: "hint", text: s.summary }) : null),
      el("td", { class: "mono", text: t(s.published_at) }), el("td", { class: "mono", text: s.fetched_at ? t(s.fetched_at) : "not fetched (cited by analyst/operator)" })))),
    el("h3", { text: "Claim checks" }),
    table(["Claim", "Who / when", "Verdict", "Why"], d.claims.map(cl => el("tr", {},
      el("td", { text: cl.claim }), el("td", { text: `${cl.claimant}${cl.claim_date ? ", " + cl.claim_date : ""}` }),
      el("td", {}, el("span", { class: `verdict v-${cl.verdict}`, text: cl.verdict })), el("td", { text: cl.rationale })))),
    el("h3", { text: "Alert history" }),
    table(["Last seen (ET)", "Kind", "Message", "Seen", "Acknowledged"], d.alerts.map(a => el("tr", {},
      el("td", { class: "mono", text: t(a.last_seen_at) }), el("td", { class: "mono", text: a.kind }), el("td", { text: a.message }),
      el("td", { class: "num", text: `${a.occurrences}×` }),
      el("td", {}, a.acknowledged_at ? `${a.acknowledged_by}, ${t(a.acknowledged_at)}`
        : (STATUS && STATUS.operator_actions_enabled ? el("button", { type: "button", onclick: () => ackAlert(a.id, id) }, "Acknowledge") : "open"))))),
    el("h3", { text: "Audit log" }),
    table(["When (ET)", "Actor", "Action", "Change", "Note"], d.audit.map(a => el("tr", {},
      el("td", { class: "mono", text: t(a.at) }), el("td", { class: "mono", text: a.actor }), el("td", { class: "mono", text: a.action }),
      el("td", { class: "hint", text: `${a.before_json || "—"} → ${a.after_json || "—"}` }), el("td", { text: a.note })))),
    reviewForm(c));
}

function reviewForm(c) {
  if (!STATUS || !STATUS.operator_actions_enabled)
    return el("p", { class: "hint review", text: "Operator review is disabled on this server (set OPERATOR_TOKEN to enable)." });
  const msg = el("div", { class: "msg", role: "status" });
  const f = el("form", { class: "review-form", "aria-label": "Operator review" },
    el("label", {}, "Operator token", el("input", { type: "password", name: "token", autocomplete: "off", required: true, value: operatorToken })),
    el("label", {}, "Your name", el("input", { name: "operator", required: true, minlength: 2, maxlength: 60 })),
    el("label", {}, "Action", el("select", { name: "action" }, ...["update", "confirm", "attribute", "dismiss", "reopen"].map(a => el("option", { text: a })))),
    el("label", {}, "Severity", el("select", { name: "severity" }, el("option", { value: "", text: "(no change)" }), ...Object.keys(SEV).map(a => el("option", { text: a })))),
    el("label", {}, "Scope", el("select", { name: "scope" }, el("option", { value: "", text: "(no change)" }), ...Object.keys(SCOPE).map(a => el("option", { text: a })))),
    el("label", {}, "Scope basis", el("select", { name: "scope_basis" }, el("option", { value: "", text: "(no change)" }), ...["unknown", "reported", "confirmed"].map(a => el("option", { text: a })))),
    el("label", {}, "Status", el("select", { name: "status" }, el("option", { value: "", text: "(no change)" }), ...["unknown", "ongoing", "monitoring", "resolved"].map(a => el("option", { text: a })))),
    el("textarea", { name: "note", required: true, minlength: 10, maxlength: 2000, placeholder: "Required: what evidence did you check? (min 10 characters)", "aria-label": "Review note" }),
    el("div", { class: "actions" }, el("button", { type: "submit", class: "primary" }, "Record decision")), msg);
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(f);
    operatorToken = fd.get("token");
    const payload = { operator: fd.get("operator"), action: fd.get("action"), note: fd.get("note") };
    for (const k of ["severity", "scope", "scope_basis", "status"]) if (fd.get(k)) payload[k] = fd.get(k);
    try {
      await api(`/api/candidates/${c.id}/review`, { method: "POST", body: JSON.stringify(payload), headers: { "X-Operator-Token": operatorToken } });
      await Promise.all([openDetail(c.id), loadCandidates(), refreshOverview()]);
    } catch (e) { msg.textContent = `Not saved: ${e.message}`; }
  });
  return el("section", { class: "review" }, el("h3", { text: "Operator review (audited)" }),
    el("p", { class: "hint", text: "Confirming requires a primary source or two independent publishers. Every decision is written to the audit log with your name and note." }), f);
}

async function ackAlert(alertId, candidateId) {
  const name = window.prompt("Your name for the audit log:");
  if (!name) return;
  if (!operatorToken) operatorToken = window.prompt("Operator token:") || "";
  try {
    await api(`/api/alerts/${alertId}/ack`, { method: "POST", body: JSON.stringify({ operator: name }), headers: { "X-Operator-Token": operatorToken } });
    await Promise.all([openDetail(candidateId), loadCandidates(), refreshOverview()]);
  } catch (e) { window.alert(`Not acknowledged: ${e.message}`); }
}

// ------------------------------------------------------------------ boot
async function refreshOverview() {
  try { STATUS = await api("/api/status"); renderStatus(STATUS); }
  catch (e) {
    document.getElementById("banners").replaceChildren(el("div", { class: "advisory error", role: "alert" }, el("span", { class: "ico", text: "✕" }), el("div", {}, el("b", { text: "Server unreachable." }), e.message)));
  }
  try { ALL = await api("/api/candidates"); } catch { ALL = []; }
  await loadAlerts();
  renderOverview();
}
async function refresh() { await Promise.all([refreshOverview(), loadCandidates()]); }

function setupNav() {
  const bar = document.querySelector(".cmdbar");
  const setH = () => document.documentElement.style.setProperty("--cmdbar-h", `${bar.offsetHeight}px`);
  setH(); new ResizeObserver(setH).observe(bar);
  const links = [...document.querySelectorAll(".secnav a")];
  const obs = new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) links.forEach(a => a.setAttribute("aria-current", String(a.getAttribute("href") === `#${e.target.id}`)));
  }, { rootMargin: "-30% 0px -60% 0px" });
  links.forEach(a => { const sec = document.querySelector(a.getAttribute("href")); if (sec) obs.observe(sec); });
}
function setupLanes() {
  const tabs = [...document.querySelectorAll(".lane-switch [role=tab]")];
  const select = tab => {
    tabs.forEach(b => { b.setAttribute("aria-selected", String(b === tab)); b.tabIndex = b === tab ? 0 : -1; });
    document.getElementById("lanes").dataset.active = tab.dataset.lane;
  };
  tabs.forEach((b, i) => {
    b.tabIndex = i === 0 ? 0 : -1;
    b.addEventListener("click", () => select(b));
    b.addEventListener("keydown", e => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (d) { const n = tabs[(i + d + tabs.length) % tabs.length]; select(n); n.focus(); }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  readColors();
  setupNav();
  setupLanes();
  const filters = document.getElementById("filters");
  filters.addEventListener("submit", e => { e.preventDefault(); loadCandidates(); });
  filters.addEventListener("reset", () => setTimeout(loadCandidates, 0));
  const toggle = document.getElementById("filter-toggle");
  toggle.addEventListener("click", () => { const open = filters.classList.toggle("open"); toggle.setAttribute("aria-expanded", String(open)); });
  document.getElementById("window-btn").addEventListener("click", () => {
    if (!STATUS) return;
    filters.date_from.value = dayKey(ms(STATUS.window_start));
    filters.date_to.value = dayKey(ms(STATUS.window_end) - 1000);
    loadCandidates();
  });
  document.getElementById("close-detail").addEventListener("click", () => document.getElementById("detail").close());
  let rt;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (STATUS) { renderSpans(); renderDaily(); } }, 150); });
  window.addEventListener("scroll", hideTip, { passive: true });
  refresh();
  setInterval(refresh, REFRESH_MS);
});
