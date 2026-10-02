/* AI Pulse — static reader for the digest built by news_bot. No framework,
   no build step: data comes from data/*.json, reading state lives in
   localStorage on this device. */
"use strict";

// ---------------------------------------------------------------- storage
const LS = {
  get(key, fallback) {
    try { const v = localStorage.getItem("pulse." + key); return v === null ? fallback : JSON.parse(v); }
    catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("pulse." + key, JSON.stringify(value)); } catch (e) { /* private mode */ }
  },
};

const state = {
  index: null,
  days: new Map(),          // date -> Promise<stories[]>
  read: new Set(LS.get("read", [])),
  saved: new Map(LS.get("saved", [])),
  catchDone: new Set(LS.get("catchDone", [])),
  prevVisit: LS.get("lastVisit", 0),
  focus: -1,
  pageSize: 40,
};

// Keep the newest few thousand ids; stories age off the site after 60 days anyway.
function persistRead() { LS.set("read", [...state.read].slice(-6000)); }
function persistSaved() { LS.set("saved", [...state.saved]); updateSavedCount(); }

// ---------------------------------------------------------------- helpers
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
const HOUR = 3600e3, DAY = 24 * HOUR;
const ext = 'target="_blank" rel="noopener"';

function ago(iso) {
  if (!iso) return "";
  const t = Date.parse(iso), d = Date.now() - t;
  if (d < 60e3) return "just now";
  if (d < HOUR) return Math.floor(d / 60e3) + "m ago";
  if (d < DAY) return Math.floor(d / HOUR) + "h ago";
  if (d < 7 * DAY) return Math.floor(d / DAY) + "d ago";
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const clock = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
function dayLabel(iso) {
  const d = new Date(iso), today = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / DAY);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}
function parseDay(iso) {
  if (/^\d{4}-\d{2}$/.test(iso)) return new Date(iso + "-15T12:00:00");
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return new Date(iso + "T12:00:00");
  return new Date(iso);
}
function fmtDate(iso, opts = { month: "short", day: "numeric", year: "numeric" }) {
  if (!iso) return "";
  const d = parseDay(iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, opts);
}
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.remove("show"), 1800);
}
const catName = (c) => (state.index?.categories || {})[c] || c;
const isNew = (s) => state.prevVisit > 0 && Date.parse(s.seen || s.published) > state.prevVisit;
const plural = (n, word) => `${n.toLocaleString()} ${n === 1 ? word : word.endsWith("y") && !/[aeiou]y$/.test(word) ? word.slice(0, -1) + "ies" : word + "s"}`;

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(path + " → HTTP " + res.status);
  return res.json();
}

// ---------------------------------------------------------------- data
async function loadIndex() {
  state.index = await getJSON("data/index.json");
  const gen = state.index.generated;
  const ed = $("#edition");
  const stale = Date.now() - Date.parse(gen) > 8 * HOUR;
  ed.textContent = (stale ? "Last edition " : "Edition of ") + new Date(gen).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) + " · " + ago(gen);
  ed.classList.toggle("stale", stale);
  ed.title = stale ? "The collector hasn't run for a while — check the GitHub Actions tab." : "News is collected every two hours.";
  const run = state.index.run || {};
  const src = state.index.sources || [];
  $("#colophon-run").textContent = `${src.filter((s) => s.ok).length}/${src.length} sources live · ${run.new_items ?? 0} new on last run`;
}

function loadDay(date) {
  if (!state.days.has(date)) {
    state.days.set(date, getJSON(`data/days/${date}.json`).catch(() => []));
  }
  return state.days.get(date);
}

/** Stories first published in the last `days` days, newest first. */
async function loadRange(days) {
  const cutoff = Date.now() - days * DAY;
  const cutoffDay = new Date(cutoff - DAY).toISOString().slice(0, 10);
  const dates = (state.index.days || []).map((d) => d.date).filter((d) => d >= cutoffDay);
  const lists = await Promise.all(dates.map(loadDay));
  return lists.flat()
    .filter((s) => Date.parse(s.published) >= cutoff)
    .sort((a, b) => b.published.localeCompare(a.published));
}

// Stories rendered on the current page, so actions can find the full object.
const onPage = new Map();
function remember(stories) { for (const s of stories) onPage.set(s.id, s); }

// ---------------------------------------------------------------- story pieces
function meter(n) {
  return `<span class="meter" title="Importance ${n} of 5">${[1, 2, 3, 4, 5].map((i) => `<i class="${i <= n ? "on" : ""}"></i>`).join("")}</span>`;
}

function kicker(s, { time = true } = {}) {
  const parts = [];
  if (s.importance >= 5) parts.push('<span class="flag">Must read</span>');
  if (!state.read.has(s.id) && isNew(s)) parts.push('<span class="fresh">New</span>');
  parts.push(`<span class="cat cat-${esc(s.category)}">${esc(catName(s.category))}</span>`);
  parts.push(`<span class="src">${esc(s.source_name)}${s.also?.length ? ` +${s.also.length}` : ""}</span>`);
  if (time) parts.push(`<time datetime="${esc(s.published)}" title="${esc(new Date(s.published).toLocaleString())}">${ago(s.published)}</time>`);
  if (s.points) parts.push(`<span title="Hacker News points">▲${s.points}</span>`);
  if (s.upvotes) parts.push(`<span title="Hugging Face upvotes">▲${s.upvotes}</span>`);
  parts.push(meter(s.importance));
  return `<div class="kicker">${parts.join("")}</div>`;
}

function headline(s, tag = "h3") {
  return `<${tag} class="hl"><a href="${safeUrl(s.url)}" ${ext} data-open="${esc(s.id)}">${esc(s.title)}</a></${tag}>`;
}

function body(s, { why = true } = {}) {
  const byline = s.authors?.length
    ? `<div class="byline">${esc(s.authors.slice(0, 4).join(", "))}${s.authors.length > 4 ? " et al." : ""}${s.org ? " — " + esc(s.org) : ""}</div>`
    : "";
  const text = s.summary
    ? `<p class="dek">${esc(s.summary)}</p>${why && s.why ? `<p class="why"><b>Why it matters.</b> ${esc(s.why)}</p>` : ""}`
    : s.excerpt ? `<p class="dek excerpt">${esc(s.excerpt)}</p>` : "";
  return byline + text;
}

function also(s) {
  if (!s.also?.length) return "";
  return `<details class="also"><summary>Also reported by ${esc(s.also.map((a) => a.source_name).slice(0, 3).join(", "))}${s.also.length > 3 ? ` and ${s.also.length - 3} more` : ""}</summary><ul>${
    s.also.map((a) => `<li><a href="${safeUrl(a.url)}" ${ext} data-open="${esc(s.id)}">${esc(a.source_name)}</a> — ${esc(a.title)}</li>`).join("")
  }</ul></details>`;
}

function tools(s) {
  const saved = state.saved.has(s.id), read = state.read.has(s.id);
  const tags = (s.tags || []).map((t) => `<button class="tag" data-tag="${esc(t)}">${esc(t)}</button>`).join("");
  const links = [];
  if (s.hn_url) links.push(`<a class="act" href="${safeUrl(s.hn_url)}" ${ext}>${plural(s.comments || 0, "comment")}</a>`);
  if (s.github) links.push(`<a class="act" href="${safeUrl(s.github)}" ${ext}>Code</a>`);
  return `<div class="tools"><div class="tags">${tags}</div><div class="acts">${links.join("")}
    <button class="act ${saved ? "on" : ""}" data-act="save" aria-pressed="${saved}" title="Save (s)">${saved ? "★ Saved" : "☆ Save"}</button>
    <button class="act" data-act="read" title="Toggle read (m)">${read ? "Unread" : "Mark read"}</button></div></div>`;
}

const cls = (s, extra = "") => `story imp-${Number(s.importance) || 2} ${state.read.has(s.id) ? "read" : ""} ${extra}`;

function leadStory(s) {
  return `<article class="${cls(s, "lead")}" data-id="${esc(s.id)}">${kicker(s)}${headline(s, "h2")}${body(s)}${also(s)}${tools(s)}</article>`;
}
function colStory(s, i) {
  return `<article class="${cls(s, i < 3 && s.importance >= 4 ? "big" : "")}" data-id="${esc(s.id)}" style="--i:${i}">${kicker(s)}${headline(s)}${body(s, { why: i < 3 })}${also(s)}${tools(s)}</article>`;
}
function rowStory(s, i) {
  return `<article class="${cls(s, "row")}" data-id="${esc(s.id)}" style="--i:${Math.min(i, 12)}">
    <div class="stamp"><b>${clock(s.published)}</b>${ago(s.published)}</div>
    <div>${kicker(s, { time: false })}${headline(s)}${body(s)}${also(s)}${tools(s)}</div></article>`;
}

function river(stories, { group = false, limit = Infinity } = {}) {
  if (!stories.length) return "";
  const shown = stories.slice(0, limit);
  if (!group) return `<div class="river reveal">${shown.map(rowStory).join("")}</div>`;
  const counts = {};
  for (const s of stories) { const l = dayLabel(s.published); counts[l] = (counts[l] || 0) + 1; }
  let html = "", last = "";
  shown.forEach((s, i) => {
    const label = dayLabel(s.published);
    if (label !== last) { html += `<div class="day-head"><h3>${esc(label)}</h3><span>${plural(counts[label], "story")}</span></div>`; last = label; }
    html += rowStory(s, i);
  });
  return `<div class="river reveal">${html}</div>`;
}

// ---------------------------------------------------------------- analytics
// Umami (index.html) is cookieless and may be blocked or still loading, so
// every call is best-effort and never allowed to break the page.
function track(name, data) {
  try { window.umami?.track(name, data); } catch (err) { /* analytics is optional */ }
}
/** One page view per section, keyed by route only, so filter tweaks don't count as visits. */
function trackView(route, tries = 0) {
  if (!window.umami) {
    if (tries < 20) setTimeout(() => trackView(route, tries + 1), 500);
    return;
  }
  try {
    window.umami.track((p) => ({ ...p, url: `${location.pathname}#/${route}`, title: `AI Pulse · ${route}` }));
  } catch (err) { /* analytics is optional */ }
}

// ---------------------------------------------------------------- routing
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  return { route: path || "today", params: Object.fromEntries(new URLSearchParams(query)) };
}
function setParams(params) {
  const { route } = parseHash();
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  const q = new URLSearchParams(clean).toString();
  history.replaceState(null, "", `#/${route}${q ? "?" + q : ""}`);
  render({ keepScroll: true });
}

const VIEWS = {};
let renderSeq = 0;
let lastRoute = null;

async function render({ keepScroll = false } = {}) {
  const { route, params } = parseHash();
  const seq = ++renderSeq;
  if (!keepScroll) togglePaletteMenu(false);
  $$("#nav .section-links a").forEach((a) => a.classList.toggle("active", a.dataset.route === route));
  const view = $("#view");
  const fn = VIEWS[route] || VIEWS.today;
  let html;
  try {
    html = await fn(params);
  } catch (err) {
    console.error(err);
    html = `<div class="empty">Couldn't load this page: ${esc(err.message)}</div>`;
  }
  if (seq !== renderSeq) return; // a newer navigation won
  const routeChanged = route !== lastRoute;
  lastRoute = route;
  if (routeChanged) trackView(VIEWS[route] ? route : "today");
  const swap = () => {
    view.innerHTML = html;
    state.focus = -1;
    if (routeChanged && !keepScroll) {
      const nav = $("#nav");
      const top = nav.offsetTop;
      if (scrollY > top) scrollTo({ top, behavior: "instant" });
    }
  };
  if (routeChanged && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.startViewTransition(swap);
  } else swap();
}

// ---------------------------------------------------------------- front page
VIEWS.today = async () => {
  const idx = state.index;
  const week = await loadRange(7);
  remember(week);
  const now = Date.now();
  let day = week.filter((s) => now - Date.parse(s.published) < DAY);
  if (day.length < 10) day = week.filter((s) => now - Date.parse(s.published) < 2 * DAY);
  const ranked = [...day].sort((a, b) => b.score - a.score);
  const lead = ranked.find((s) => s.summary && s.kind !== "paper") || ranked[0];
  if (!lead) return '<div class="empty">The presses are warming up — no stories collected yet.</div>';
  const used = new Set([lead.id]);
  const pick = (n) => {
    const out = ranked.filter((s) => !used.has(s.id) && s.kind !== "paper").slice(0, n);
    out.forEach((s) => used.add(s.id));
    return out;
  };
  const subs = pick(2);
  const cols = pick(9);

  const papers = week.filter((s) => s.kind === "paper" && now - Date.parse(s.published) < 3 * DAY)
    .sort((a, b) => (b.upvotes || 0) - (a.upvotes || 0)).slice(0, 5);
  const unread = week.filter((s) => s.importance >= 4 && !state.read.has(s.id) && !used.has(s.id))
    .sort((a, b) => b.score - a.score).slice(0, 6);
  const fresh = week.filter((s) => isNew(s) && !state.read.has(s.id));

  const notice = fresh.length
    ? `<div class="notice"><span><b>${plural(fresh.length, "new story")}</b> since your last visit ${ago(new Date(state.prevVisit).toISOString())}.</span><a href="#/feed?new=1&sort=top&range=7">Read only those →</a></div>`
    : "";

  return `${notice}
  <section class="front reveal">
    <div class="lead-col">${leadStory(lead)}${subs.length ? `<div class="sublead">${subs.map((s, i) => colStory(s, i + 1)).join("")}</div>` : ""}</div>
    ${briefing(idx.brief, ranked)}
  </section>
  <section class="columns reveal">${cols.map(colStory).join("")}</section>
  <div class="desks reveal">
    <section class="desk" style="--i:1"><div class="sect-head"><h2>Research</h2><a href="#/research">Papers</a></div>${paperList(papers)}</section>
    <section class="desk" style="--i:2"><div class="sect-head"><h2>Open models</h2><a href="#/opensource">Trending</a></div>${modelList(5)}</section>
    <section class="desk" style="--i:3"><div class="sect-head"><h2>Calendar</h2><a href="#/conferences">Conferences</a></div>${calList(4)}</section>
  </div>
  ${unread.length ? `<div class="sect-head"><h2>Still unread <em>this week</em></h2><a href="#/feed?range=7&level=4&hideRead=1&sort=top">Everything notable</a></div>${river(unread)}` : ""}
  ${idx.weekly ? `<div class="sect-head"><h2>The week <em>in review</em></h2><a href="#/briefs?kind=weekly">Past weeks</a></div>${issue(idx.weekly)}` : ""}
  <div class="status-line"><span>Keys: <kbd>j</kbd> <kbd>k</kbd> move · <kbd>o</kbd> open · <kbd>m</kbd> read · <kbd>s</kbd> save · <kbd>/</kbd> search</span><a href="#/sources">Source status</a></div>`;
};

function briefRefs(ids, refs) {
  const seen = new Set();
  const links = [];
  for (const id of ids) {
    const story = onPage.get(id);
    const r = story ? { url: story.url, source: story.source_name, title: story.title } : refs?.[id];
    if (!r || seen.has(r.source) || links.length >= 3) continue;
    seen.add(r.source);
    links.push(`<a href="${safeUrl(r.url)}" ${ext} title="${esc(r.title)}" data-open="${esc(id)}">${esc(r.source)}↗</a>`);
  }
  return links.join(" ");
}

function briefing(b, ranked) {
  if (b?.bullets?.length) {
    return `<aside class="briefing" style="--i:1"><h2>The Briefing</h2><div class="when">Written ${ago(b.generated)}</div>
      ${b.headline ? `<p class="headline">${esc(b.headline)}</p>` : ""}
      <ol class="brief-list">${b.bullets.map((x) => `<li>${esc(x.text)}<span class="refs">${briefRefs(x.ids, b.refs)}</span></li>`).join("")}</ol></aside>`;
  }
  // No brief yet: fall back to the most-discussed stories.
  const talked = [...ranked].filter((s) => s.points).sort((a, b) => b.points - a.points).slice(0, 6);
  return `<aside class="briefing" style="--i:1"><h2>Most discussed</h2><div class="when">On Hacker News</div>
    <ol class="brief-list">${talked.map((s) => `<li><a href="${safeUrl(s.url)}" ${ext} data-open="${esc(s.id)}" style="text-decoration:none">${esc(s.title)}</a><span class="refs"><a href="${safeUrl(s.hn_url)}" ${ext}>▲${s.points}</a></span></li>`).join("") || "<li>Quiet day so far.</li>"}</ol></aside>`;
}

function paperList(papers) {
  if (!papers.length) return '<p class="empty">No papers in the last three days.</p>';
  return `<ol class="mini">${papers.map((s) => `<li><div><a href="${safeUrl(s.url)}" ${ext} data-open="${esc(s.id)}">${esc(s.title)}</a>
    <span class="sub">▲ ${s.upvotes || 0} upvotes${s.org ? " · " + esc(s.org) : ""}</span></div></li>`).join("")}</ol>`;
}

function modelList(n) {
  const rows = (state.index.trending_models || []).slice(0, n);
  if (!rows.length) return '<p class="empty">No trending data yet.</p>';
  const max = Math.max(...rows.map((m) => m.likes || 0), 1);
  return `<ol class="mini">${rows.map((m, i) => `<li><div><a href="${safeUrl(m.url)}" ${ext}>${esc(m.id)}</a>
    <span class="sub">${esc(m.task || "model")} · ♥ ${Number(m.likes).toLocaleString()}</span>
    <span class="bar"><i style="width:${Math.max(4, (100 * (m.likes || 0)) / max)}%;animation-delay:${i * 90}ms"></i></span></div></li>`).join("")}</ol>`;
}

function calList(n) {
  const rows = (state.index.conferences || []).slice(0, n);
  if (!rows.length) return '<p class="empty">No conference data yet.</p>';
  return `<ul class="cal">${rows.map((c) => {
    const d = c.start ? parseDay(c.start) : null;
    return `<li><div class="d"><small>${d ? d.toLocaleDateString(undefined, { month: "short" }) : "TBA"}</small>${d ? d.getDate() : "—"}</div>
      <div><a href="${safeUrl(c.url)}" ${ext}>${esc(c.name)}</a><span class="sub">${esc(c.location || c.dates || "")}</span></div></li>`;
  }).join("")}</ul>`;
}

function issue(b, stampHtml = "") {
  const bullets = (list) => `<ol class="brief-list">${list.map((x) => `<li>${esc(x.text)}<span class="refs">${briefRefs(x.ids, b.refs)}</span></li>`).join("")}</ol>`;
  const content = b.bullets
    ? `${b.headline ? `<p class="headline">${esc(b.headline)}</p>` : ""}${bullets(b.bullets)}`
    : `${b.overview ? `<p class="overview">${esc(b.overview)}</p>` : ""}${(b.sections || []).map((s) => `<h4>${esc(s.title)}</h4>${bullets(s.bullets)}`).join("")}`;
  return stampHtml ? `<article class="issue">${stampHtml}<div>${content}</div></article>` : `<div class="reveal"><div>${content}</div></div>`;
}

// ---------------------------------------------------------------- list pages
const RANGES = [["1", "24h"], ["3", "3d"], ["7", "7d"], ["30", "30d"], ["60", "60d"]];
const LEVELS = [["2", "All"], ["3", "Solid"], ["4", "Notable"], ["5", "Must read"]];

function seg(name, label, options, current) {
  return `<div class="seg" role="group" aria-label="${esc(label)}"><span class="seg-label">${esc(label)}</span>${options.map(([v, text]) =>
    `<button data-param="${name}" data-value="${v}" aria-pressed="${String(v) === String(current)}">${text}</button>`).join("")}</div>`;
}

function matches(s, terms) {
  if (!terms.length) return true;
  const hay = [s.title, s.summary, s.why, s.excerpt, s.source_name, (s.tags || []).join(" "),
    (s.also || []).map((a) => a.source_name + " " + a.title).join(" "), (s.authors || []).join(" ")]
    .join(" ").toLowerCase();
  return terms.every((t) => hay.includes(t));
}

async function feedView(params, opts) {
  const p = { range: opts.range || "7", sort: opts.sort || "latest", level: "2", ...params };
  const q = (p.q || "").trim();
  const range = q ? 60 : Math.min(Number(p.range) || 7, 60);
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const all = await loadRange(range);

  let rows = all.filter((s) => (opts.filter ? opts.filter(s) : true));
  if (p.cat) rows = rows.filter((s) => s.category === p.cat);
  rows = rows.filter((s) => s.importance >= Number(p.level || 2));
  if (p.hideRead === "1") rows = rows.filter((s) => !state.read.has(s.id));
  if (p.new === "1") rows = rows.filter(isNew);
  rows = rows.filter((s) => matches(s, terms));
  if (p.sort === "top") rows.sort((a, b) => b.score - a.score);
  remember(rows);

  const limit = Number(p.limit) || state.pageSize;
  const cats = Object.entries(state.index.categories || {});
  const catRow = opts.categories === false ? "" : `<div class="cats">
      <button class="cat-btn" data-param="cat" data-value="" aria-pressed="${!p.cat}">Everything</button>
      ${cats.map(([k, v]) => `<button class="cat-btn cat-${k}" data-param="cat" data-value="${k}" aria-pressed="${p.cat === k}"><span class="dot"></span>${esc(v)}</button>`).join("")}
    </div>`;

  return `${q ? `<div class="notice"><span>Searching the last 60 days for <b>“${esc(q)}”</b></span><a href="#" data-clear-search>Clear search ✕</a></div>` : ""}
    <div class="filters">${catRow}
      ${q ? "" : seg("range", "When", RANGES, String(range))}
      ${seg("sort", "Order", [["latest", "Latest"], ["top", "Top"]], p.sort)}
      ${seg("level", "Show", LEVELS, p.level || "2")}
      <label class="chk"><input type="checkbox" data-param="hideRead" ${p.hideRead === "1" ? "checked" : ""}> Hide read</label>
      ${p.new === "1" ? `<button class="link-btn" data-param="new" data-value="">Only new ✕</button>` : ""}
      <button class="link-btn" data-mark-all>Mark these read</button>
      <span class="tally">${plural(rows.length, "story")}</span>
    </div>
    ${river(rows, { group: p.sort !== "top", limit }) || `<div class="empty">${opts.empty || "Nothing matches these filters."}</div>`}
    ${rows.length > limit ? `<div class="more"><button class="big-btn" data-param="limit" data-value="${limit + state.pageSize * 2}">More stories · ${rows.length - limit} left</button></div>` : ""}`;
}

const head = (title, sub, aside = "") =>
  `<header class="page-head"><h1>${title}</h1><span class="count">${aside}</span>${sub ? `<p>${sub}</p>` : ""}</header>`;

VIEWS.feed = async (params) =>
  head("All news", "Every story, de-duplicated across sources and ranked by how much it matters.") + await feedView(params, {});

VIEWS.research = async (params) =>
  head("Research", "Papers the community is upvoting on Hugging Face, and research from labs and analysts.") +
  await feedView(params, { categories: false, sort: "top", filter: (s) => s.kind === "paper" || s.category === "research", empty: "No research in this window yet." });

VIEWS.opensource = async (params) => {
  const rows = state.index.trending_models || [];
  const table = rows.length ? `<div class="ledger-wrap reveal"><table class="ledger"><thead><tr><th></th><th>Model</th><th>Task</th><th class="n">Likes</th><th class="n">Downloads</th><th class="n">Created</th></tr></thead><tbody>${
    rows.slice(0, 15).map((m, i) => `<tr style="--i:${i}"><td class="rank">${i + 1}</td><td><a href="${safeUrl(m.url)}" ${ext}>${esc(m.id)}</a></td><td class="mono">${esc(m.task || "—")}</td><td class="n">${Number(m.likes).toLocaleString()}</td><td class="n">${Number(m.downloads).toLocaleString()}</td><td class="n">${m.created ? ago(m.created) : "—"}</td></tr>`).join("")
  }</tbody></table></div>` : "";
  return head("Open source", "Open-weight models, libraries and releases.") +
    (table ? `<div class="sect-head" style="margin-top:8px"><h2>Trending <em>on Hugging Face</em></h2></div>${table}` : "") +
    `<div class="sect-head"><h2>Releases <em>&amp; news</em></h2></div>` +
    await feedView(params, { categories: false, filter: (s) => s.category === "opensource" || s.kind === "release" || s.kind === "model" });
};

VIEWS.conferences = async (params) => {
  const rows = state.index.conferences || [];
  const now = Date.now();
  const list = rows.map((c, i) => {
    const start = c.start ? parseDay(c.start) : null;
    const end = c.end ? new Date(parseDay(c.end).getTime() + 12 * HOUR) : start;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const days = start ? Math.round((new Date(start).setHours(0, 0, 0, 0) - today) / DAY) : null;
    const live = start && start <= now && end >= now;
    const dl = c.deadline ? Math.round((parseDay(c.deadline).setHours(0, 0, 0, 0) - today) / DAY) : null;
    const countdown = live ? '<div class="countdown live"><b>Live</b>happening now</div>'
      : days != null && days >= 0 ? `<div class="countdown"><b>${days}</b>day${days === 1 ? "" : "s"} away</div>` : "";
    return `<li style="--i:${Math.min(i, 12)}"><div class="d">${start ? `<small>${start.toLocaleDateString(undefined, { month: "short" })}</small>${start.getDate()}<small>${start.getFullYear()}</small>` : "TBA"}</div>
      <div><h3><a href="${safeUrl(c.url)}" ${ext}>${esc(c.name)}</a></h3><p>${esc(c.dates || "")}${c.location ? " · " + esc(c.location) : ""}${c.estimated ? " · <i>dates estimated</i>" : ""}</p>
      ${c.note ? `<p class="note">${esc(c.note)}</p>` : ""}
      ${dl != null && dl >= 0 ? `<span class="deadline ${dl <= 21 ? "soon" : ""}">Paper deadline ${fmtDate(c.deadline, { month: "short", day: "numeric" })} — ${plural(dl, "day")} left</span>` : ""}</div>
      ${countdown}</li>`;
  }).join("");
  return head("Conferences", "The major AI and ML venues, where new research is presented. Best-paper awards and announcements appear in the news below.", `${rows.length} upcoming`) +
    (list ? `<ol class="confs reveal">${list}</ol>` : '<div class="empty">No conference data yet.</div>') +
    `<div class="sect-head"><h2>Conference <em>news</em></h2></div>` +
    await feedView(params, { categories: false, range: "60", filter: (s) => s.category === "events", empty: "No conference news in this window." });
};

let catchupData = null;
VIEWS.catchup = async () => {
  catchupData = catchupData || await getJSON("data/catchup.json").catch(() => null);
  const c = catchupData;
  if (!c) return '<div class="empty">The catch-up guide is not available.</div>';
  const doneIn = (s) => s.items.filter((_, i) => state.catchDone.has(`${s.id}:${i}`)).length;
  const total = c.sections.reduce((n, s) => n + s.items.length, 0);
  const done = c.sections.reduce((n, s) => n + doneIn(s), 0);
  const chapters = c.sections.map((s, n) => `<section class="chapter" id="catch-${esc(s.id)}">
      <div class="chapter-head"><div class="no">${String(n + 1).padStart(2, "0")}</div><div><h2>${esc(s.title)}</h2>${s.intro ? `<p>${esc(s.intro)}</p>` : ""}</div></div>
      <ol class="entries">${s.items.map((it, i) => {
        const key = `${s.id}:${i}`, isDone = state.catchDone.has(key);
        const dateOpts = /^\d{4}-\d{2}$/.test(it.date || "") ? { month: "short", year: "numeric" } : undefined;
        return `<li class="entry ${isDone ? "done" : ""}"><div class="when">${esc(fmtDate(it.date, dateOpts))}</div>
          <div><h3>${esc(it.title)}</h3><p class="what">${esc(it.what)}</p>${it.why_it_matters ? `<p class="why"><b>Why it matters.</b> ${esc(it.why_it_matters)}</p>` : ""}
          <div class="links">${(it.links || []).map((l) => `<a href="${safeUrl(l.url)}" ${ext}>${esc(l.label)} ↗</a>`).join("")}
          <button class="got" data-catch="${esc(key)}">${isDone ? "✓ Understood" : "Got it"}</button></div></div></li>`;
      }).join("")}</ol></section>`).join("");
  const glossary = c.glossary?.length ? `<section class="chapter" id="catch-glossary"><div class="chapter-head"><div class="no">Aa</div><div><h2>Glossary</h2><p>Terms you'll keep running into.</p></div></div>
      <div class="glossary">${c.glossary.map((g) => `<div><b>${esc(g.term)}</b><span>${esc(g.definition)}</span></div>`).join("")}</div></section>` : "";
  return head(`Catch up`, `What changed in AI from ${esc(c.period || "the past year")}: a guided tour for anyone who looked away. Progress is saved on this device.`) +
    `<section class="catch-hero reveal">
      <div><div class="sect-head" style="margin-top:0"><h2>The short <em>version</em></h2></div><ol>${(c.tldr || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ol></div>
      <aside class="catch-progress"><div class="big">${done}<small> / ${total} understood</small></div><div class="gauge"><i style="width:${total ? (100 * done) / total : 0}%"></i></div>
        <ul class="toc">${c.sections.map((s) => `<li><a href="#/catchup" data-jump="catch-${esc(s.id)}">${esc(s.title)}<span>${doneIn(s)}/${s.items.length}</span></a></li>`).join("")}
        ${glossary ? '<li><a href="#/catchup" data-jump="catch-glossary">Glossary<span>Aa</span></a></li>' : ""}</ul></aside>
    </section>${chapters}${glossary}
    <p class="status-line">Compiled ${esc(fmtDate(c.generated))}. From here on, the front page and the weekly review keep you current.</p>`;
};

let briefsData = null;
VIEWS.briefs = async (params) => {
  briefsData = briefsData || await getJSON("data/briefs.json").catch(() => ({ daily: [], weekly: [] }));
  const kind = params.kind === "weekly" ? "weekly" : "daily";
  const list = briefsData[kind] || [];
  const stamp = (b) => {
    const d = new Date(b.generated);
    return `<div class="stamp"><b>${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}</b>${d.toLocaleDateString(undefined, { weekday: "long" })}<br>${clock(b.generated)}</div>`;
  };
  return head("Briefs", "Every daily briefing and weekly review, newest first.", plural(list.length, kind === "weekly" ? "review" : "briefing")) +
    `<div class="filters">${seg("kind", "Edition", [["daily", "Daily"], ["weekly", "Weekly"]], kind)}</div>` +
    (list.length ? `<div class="reveal">${list.map((b) => issue(b, stamp(b))).join("")}</div>` : '<div class="empty">No briefs yet. The first one is written on the next collector run.</div>');
};

VIEWS.saved = async () => {
  const rows = [...state.saved.values()].sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  remember(rows);
  return head("Saved", "Stories you kept for later, stored on this device.", plural(rows.length, "story")) +
    (river(rows) || '<div class="empty">Nothing saved yet. Press ☆ Save — or the <b>s</b> key — on any story.</div>');
};

VIEWS.sources = async () => {
  const src = [...(state.index.sources || [])].sort((a, b) => a.category.localeCompare(b.category) || b.weight - a.weight || a.name.localeCompare(b.name));
  const run = state.index.run || {};
  const ok = src.filter((s) => s.ok).length;
  return head("Sources", `Where the news comes from. Last run ${ago(run.at)}: ${run.seconds ?? "?"}s, ${plural(run.llm_calls ?? 0, "summarizer call")}. Weight is how much a source counts toward ranking.`, `${ok}/${src.length} healthy`) +
    `<div class="ledger-wrap reveal"><table class="ledger"><thead><tr><th>Source</th><th>Desk</th><th class="n">Weight</th><th>Status</th><th class="n">Newest</th><th class="n">30 days</th></tr></thead><tbody>${
      src.map((s, i) => `<tr style="--i:${Math.min(i, 14)}"><td><a href="${safeUrl(s.homepage)}" ${ext}>${esc(s.name)}</a></td><td class="mono">${esc(s.category)}</td><td class="n">${"●".repeat(s.weight)}<span style="opacity:.25">${"●".repeat(5 - s.weight)}</span></td>
        <td class="mono ${s.ok ? "ok" : "fail"}" title="${esc(s.error)}">${s.ok ? "● live" : "● failing — " + esc((s.error || "").slice(0, 36))}</td><td class="n">${s.newest ? ago(s.newest) : "—"}</td><td class="n">${s.items_30d}</td></tr>`).join("")
    }</tbody></table></div>`;
};

VIEWS.palettes = async () => {
  const week = await loadRange(3).catch(() => []);
  const s = [...week].sort((a, b) => b.score - a.score)[0] || {
    title: "Gemini 4 Argon: our next era of frontier intelligence", source_name: "Google DeepMind",
    summary: "A new flagship model with a million-token context window.", category: "models", importance: 5,
  };
  const current = root.dataset.palette;
  const sample = (p, theme) => `<div class="pal-sample" data-palette="${p.id}" data-theme="${theme}">
      <div class="ps-ticker"><span>Latest</span><em>${esc(s.title)}</em></div>
      <div class="ps-body">
        <div class="ps-mark">AI${heartbeat(`${p.id}-${theme}`, { delay: 0.3 })}Pulse</div>
        <div class="kicker"><span class="flag">Must read</span><span class="cat cat-${esc(s.category)}">${esc(catName(s.category))}</span><span class="src">${esc(s.source_name)}</span></div>
        <h3 class="hl"><a href="#/palettes" tabindex="-1">${esc(s.title)}</a></h3>
        <p class="dek">${esc((s.summary || s.excerpt || "").slice(0, 150))}</p>
        <div class="ps-cats">${Object.keys(state.index.categories || {}).map((c) => `<i class="cat-${c}" title="${esc(catName(c))}"></i>`).join("")}</div>
      </div></div>`;
  return head("Palettes", "Every colour palette with today's top story, in the day and night editions. Pick one to use it across the site — it's remembered on this device. Press P anywhere to cycle through them.") +
    `<div class="pal-grid reveal">${PALETTES.map((p, i) => `<section class="pal-card ${p.id === current ? "current" : ""}" style="--i:${i}">
        <div class="pal-pair">${sample(p, "light")}${sample(p, "dark")}</div>
        <div class="pal-meta"><div><h2>${esc(p.name)}</h2><p>${esc(p.desc)}</p></div>
          ${p.id === current ? '<span class="pal-current">In use</span>' : `<button class="big-btn" data-choose-palette="${p.id}">Use ${esc(p.name)}</button>`}</div>
      </section>`).join("")}</div>`;
};

// ---------------------------------------------------------------- ticker
async function fillTicker() {
  const stories = (await loadRange(2)).filter((s) => s.importance >= 3).slice(0, 18);
  const track = $("#ticker");
  if (!stories.length) { track.innerHTML = '<a>Collecting the latest headlines…</a>'; return; }
  const items = stories.map((s) => `<a href="${safeUrl(s.url)}" ${ext} data-open="${esc(s.id)}"><b>${esc(clock(s.published))}</b>${esc(s.title)}</a><span class="sep">◆</span>`).join("");
  track.innerHTML = items + items.replace(/<a /g, '<a tabindex="-1" aria-hidden="true" '); // second copy makes the loop seamless
  track.style.setProperty("--crawl", Math.max(40, stories.length * 7) + "s");
}

// ---------------------------------------------------------------- interactions
function cardFor(id) { return document.querySelector(`.story[data-id="${CSS.escape(id)}"]`); }

function setRead(id, card, value) {
  if (value) state.read.add(id); else state.read.delete(id);
  persistRead();
  if (card) {
    card.classList.toggle("read", value);
    const btn = card.querySelector('[data-act="read"]');
    if (btn) btn.textContent = value ? "Unread" : "Mark read";
    if (value) card.querySelector(".fresh")?.remove();
  }
}

function toggleSave(id, card) {
  const story = onPage.get(id) || state.saved.get(id);
  if (!story) return;
  const on = !state.saved.has(id);
  if (on) track("save-story", { source: story.source_name, category: story.category });
  if (on) state.saved.set(id, { ...story, savedAt: Date.now() });
  else state.saved.delete(id);
  persistSaved();
  const btn = card?.querySelector('[data-act="save"]');
  if (btn) { btn.classList.toggle("on", on); btn.textContent = on ? "★ Saved" : "☆ Save"; btn.setAttribute("aria-pressed", on); }
  toast(on ? "Saved for later" : "Removed from saved");
}

document.addEventListener("click", (e) => {
  const t = e.target;
  const open = t.closest("[data-open]");
  if (open) {
    const s = onPage.get(open.dataset.open);
    const where = open.closest(".ticker") ? "ticker" : open.closest(".briefing, .brief-list") ? "briefing" : parseHash().route;
    track("open-story", { source: s?.source_name || "unknown", category: s?.category || "unknown", from: where });
    setRead(open.dataset.open, cardFor(open.dataset.open), true);
    return;
  }

  const act = t.closest("[data-act]");
  if (act) {
    const card = act.closest(".story");
    if (!card) return;
    const id = card.dataset.id;
    if (act.dataset.act === "save") toggleSave(id, card);
    if (act.dataset.act === "read") setRead(id, card, !state.read.has(id));
    return;
  }
  const tag = t.closest("[data-tag]");
  if (tag) { track("search", { query: tag.dataset.tag, via: "tag" }); $("#search").value = tag.dataset.tag; search(tag.dataset.tag); return; }

  const param = t.closest("button[data-param]");
  if (param) {
    const { params } = parseHash();
    params[param.dataset.param] = param.dataset.value;
    if (param.dataset.param !== "limit") delete params.limit;
    setParams(params);
    return;
  }
  if (t.closest("[data-mark-all]")) {
    $$(".story").forEach((card) => setRead(card.dataset.id, card, true));
    toast("Marked as read");
    return;
  }
  if (t.closest("[data-clear-search]")) {
    e.preventDefault();
    $("#search").value = "";
    const { params } = parseHash();
    delete params.q;
    setParams(params);
    return;
  }
  const jump = t.closest("[data-jump]");
  if (jump) { e.preventDefault(); document.getElementById(jump.dataset.jump)?.scrollIntoView({ behavior: "smooth" }); return; }

  const c = t.closest("[data-catch]");
  if (c) {
    const key = c.dataset.catch;
    const on = !state.catchDone.has(key);
    if (on) state.catchDone.add(key); else state.catchDone.delete(key);
    if (on) track("catchup-understood", { chapter: key.split(":")[0] });
    LS.set("catchDone", [...state.catchDone]);
    const entry = c.closest(".entry");
    entry.classList.toggle("done", on);
    c.textContent = on ? "✓ Understood" : "Got it";
    updateCatchProgress();
  }
});

function updateCatchProgress() {
  const c = catchupData;
  if (!c) return;
  const total = c.sections.reduce((n, s) => n + s.items.length, 0);
  const done = state.catchDone.size && c.sections.reduce((n, s) => n + s.items.filter((_, i) => state.catchDone.has(`${s.id}:${i}`)).length, 0);
  const big = $(".catch-progress .big");
  if (big) big.innerHTML = `${done}<small> / ${total} understood</small>`;
  const gauge = $(".catch-progress .gauge i");
  if (gauge) gauge.style.width = (total ? (100 * done) / total : 0) + "%";
  $$(".toc a[data-jump]").forEach((a, i) => {
    const s = c.sections[i];
    if (s) a.querySelector("span").textContent = `${s.items.filter((_, j) => state.catchDone.has(`${s.id}:${j}`)).length}/${s.items.length}`;
  });
}

document.addEventListener("change", (e) => {
  const box = e.target.closest('input[type="checkbox"][data-param]');
  if (!box) return;
  const { params } = parseHash();
  params[box.dataset.param] = box.checked ? "1" : "";
  setParams(params);
});

const SEARCHABLE = ["feed", "research", "opensource"];
function search(q) {
  const { route, params } = parseHash();
  if (!SEARCHABLE.includes(route)) {
    location.hash = "#/feed" + (q.trim() ? "?" + new URLSearchParams({ q: q.trim() }) : "");
    return;
  }
  const next = { ...params, q: q.trim() };
  delete next.limit;
  setParams(next);
}
let searchTimer;
$("#search").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => search(e.target.value), 280); });
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#search").value.trim();
  if (q) track("search", { query: q.toLowerCase().slice(0, 60) });
  search(q);
});

// ---------------------------------------------------------------- palettes
// Keep in sync with palettes.css and the boot script in index.html.
const PALETTES = [
  { id: "broadsheet", name: "Broadsheet", desc: "Warm newsprint, ink and vermilion" },
  { id: "salmon", name: "Salmon", desc: "Financial-paper pink with claret" },
  { id: "riso", name: "Riso", desc: "Federal blue and fluorescent pink" },
  { id: "cobalt", name: "Cobalt", desc: "Swiss white, black and electric blue" },
  { id: "phosphor", name: "Phosphor", desc: "Terminal green on black" },
  { id: "nocturne", name: "Nocturne", desc: "Midnight navy with amber" },
];
const root = document.documentElement;
const paletteName = (id) => (PALETTES.find((p) => p.id === id) || PALETTES[0]).name;

function storeLook(key, value) {
  try { localStorage.setItem("pulse." + key, value); } catch (err) { /* private mode */ }
}

/** Switch palette and/or edition, cross-fading where the browser supports it. */
function applyLook({ palette = root.dataset.palette, theme = root.dataset.theme } = {}, animate = true) {
  const apply = () => {
    root.dataset.palette = palette;
    root.dataset.theme = theme;
    $("#theme-color").content = getComputedStyle(root).getPropertyValue("--paper").trim();
    renderPaletteMenu();
  };
  if (animate && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.startViewTransition(apply);
  } else apply();
}

function choosePalette(id) {
  storeLook("palette", id);
  track("palette", { palette: id });
  applyLook({ palette: id });
  toast("Palette · " + paletteName(id));
  if (parseHash().route === "palettes") render({ keepScroll: true });
}

function renderPaletteMenu() {
  const current = root.dataset.palette, theme = root.dataset.theme;
  // Each option carries its own data-palette, so it previews itself in place.
  $("#palette-menu").innerHTML = `<div class="pal-title">Colour palette</div>${PALETTES.map((p) => `
    <button class="pal-opt" role="menuitemradio" aria-checked="${p.id === current}" data-choose-palette="${p.id}" data-palette="${p.id}" data-theme="${theme}">
      <span class="pal-chip" aria-hidden="true"><b>Aa</b><i></i></span>
      <span class="pal-text"><span class="pal-name">${esc(p.name)}</span><span class="pal-desc">${esc(p.desc)}</span></span>
      <span class="pal-check" aria-hidden="true">${p.id === current ? "●" : ""}</span>
    </button>`).join("")}
    <div class="pal-foot"><a href="#/palettes" data-close-palette>Compare all side by side →</a><span><kbd>P</kbd> cycles</span></div>`;
}

function togglePaletteMenu(open) {
  const menu = $("#palette-menu"), btn = $("#palette-btn");
  const show = open ?? menu.hidden;
  menu.hidden = !show;
  btn.setAttribute("aria-expanded", String(show));
  if (show) menu.querySelector('[aria-checked="true"]')?.focus();
}

$("#palette-btn").addEventListener("click", (e) => { e.stopPropagation(); togglePaletteMenu(); });
document.addEventListener("click", (e) => {
  const opt = e.target.closest("[data-choose-palette]");
  if (opt) { choosePalette(opt.dataset.choosePalette); return; }
  if (e.target.closest("[data-close-palette]") || !e.target.closest(".palette-picker")) togglePaletteMenu(false);
});

$("#theme-toggle").addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  storeLook("theme", next);
  track("theme", { theme: next });
  applyLook({ theme: next });
});
// Follow the system's light/dark setting until the reader picks one.
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  let chosen = null;
  try { chosen = localStorage.getItem("pulse.theme"); } catch (err) { /* private mode */ }
  if (!chosen) applyLook({ theme: e.matches ? "dark" : "light" });
});

/** The ECG mark: a faint trace that draws in once, then a bright pen with a
    glowing tip sweeps along it each beat. Pen and tip are SMIL animations on
    one clock so they never drift apart. `uid` keeps ids unique per page. */
function heartbeat(uid, { delay = 1.2 } = {}) {
  const d = "M2 22h30l7-15 10 30 9-24 6 9h54";
  const t = `dur="1.35s" begin="${delay}s" repeatCount="indefinite"`;
  return `<svg class="pulse-line" viewBox="0 0 120 40" aria-hidden="true">
    <path id="ecg-${uid}" class="trace-base" pathLength="100" d="${d}" opacity=".38">
      <animate attributeName="opacity" ${t} values=".38;.38;.75;.38;.38" keyTimes="0;0.1;0.2;0.36;1"/>
    </path>
    <path class="trace-pen" pathLength="100" d="${d}" stroke-dasharray="20 220" stroke-dashoffset="16">
      <animate attributeName="stroke-dashoffset" ${t} values="20;-80;-100;-100" keyTimes="0;0.6;0.68;1"/>
    </path>
    <g opacity="0">
      <circle class="trace-tip" r="3.6"/><circle class="trace-tip-core" r="1.3"/>
      <animateMotion ${t} keyPoints="0;1;1" keyTimes="0;0.6;1" calcMode="linear"><mpath href="#ecg-${uid}"/></animateMotion>
      <animate attributeName="opacity" ${t} values="0;1;1;0;0" keyTimes="0;0.04;0.58;0.64;1"/>
    </g>
  </svg>`;
}

// Keyboard navigation over stories.
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); $("#search").focus(); return; }
  if (e.key === "Escape" && typing) { document.activeElement.blur(); return; }
  if (typing) return;
  if (e.key === "Escape") { togglePaletteMenu(false); return; }
  if (e.key === "p" || e.key === "P") {
    const i = PALETTES.findIndex((p) => p.id === root.dataset.palette);
    choosePalette(PALETTES[(i + 1) % PALETTES.length].id);
    return;
  }
  const cards = $$("#view .story");
  if (!cards.length) return;
  const move = (d) => {
    state.focus = Math.max(0, Math.min(cards.length - 1, state.focus + d));
    cards.forEach((c, i) => c.classList.toggle("focused", i === state.focus));
    cards[state.focus].scrollIntoView({ block: "center", behavior: "smooth" });
  };
  const card = cards[state.focus];
  switch (e.key) {
    case "j": move(1); break;
    case "k": move(-1); break;
    case "o":
      if (card) { const a = card.querySelector(".hl a"); setRead(card.dataset.id, card, true); window.open(a.href, "_blank", "noopener"); }
      break;
    case "m": if (card) setRead(card.dataset.id, card, !state.read.has(card.dataset.id)); break;
    case "s": if (card) toggleSave(card.dataset.id, card); break;
    default: return;
  }
});

function updateSavedCount() { $("#saved-count").textContent = state.saved.size || ""; }

// ---------------------------------------------------------------- boot
async function boot() {
  $("#mast-pulse").outerHTML = heartbeat("mast");
  applyLook({}, false);
  $("#dateline").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  updateSavedCount();
  // Sticky day headers sit just under the section bar, whatever its height.
  const syncNav = () => document.documentElement.style.setProperty("--nav-h", $("#nav").offsetHeight + "px");
  syncNav();
  addEventListener("resize", syncNav);
  // Show the compact wordmark in the section bar once the masthead scrolls away.
  new IntersectionObserver(([entry]) => document.body.classList.toggle("stuck", !entry.isIntersecting))
    .observe($(".masthead"));
  try {
    await loadIndex();
  } catch (err) {
    $("#view").innerHTML = `<div class="empty">Couldn't load today's edition (${esc(err.message)}). On a fresh setup the first collector run may still be in progress.</div>`;
    return;
  }
  const q = parseHash().params.q;
  if (q) $("#search").value = q;
  window.addEventListener("hashchange", () => render());
  await render();
  fillTicker();
  // Count this as a visit once the reader has had a moment on the page, so
  // "new since last visit" survives an accidental reload.
  setTimeout(() => LS.set("lastVisit", Date.now()), 15000);
  // Pick up a fresh collector run if the tab stays open.
  setInterval(async () => {
    const before = state.index?.generated;
    try { await loadIndex(); } catch (err) { return; }
    if (state.index.generated !== before) { state.days.clear(); briefsData = null; fillTicker(); }
  }, 10 * 60e3);
}
boot();
