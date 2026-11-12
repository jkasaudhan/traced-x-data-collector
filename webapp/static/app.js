// Evidence Viewer — vanilla JS, no build step. Hash routes:
//   #/overview
//   #/compare?q=&start=&end=&media=&sort=
//   #/explore?q=&side=&kind=&media=&start=&end=&sort=&author=&category=
//   #/analytics              (only when the analytics_* tables exist)
//   #/post/<uuid>            (opens over the previous view)
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const PAGE = 20;
let META = null;
let lastView = "#/overview";

// ---------------------------------------------------------------- helpers

async function api(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) qs.set(k, v);
  const res = await fetch(`/api/${path}${qs.toString() ? "?" + qs : ""}`);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const fmt = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const num = n => (n == null ? "–" : fmt.format(n));
const full = n => (n == null ? "–" : Number(n).toLocaleString("en"));

function when(iso, withTime = true) {
  if (!iso) return "unknown date";
  const d = new Date(iso);
  const opts = { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toLocaleString("en-GB", opts) + (withTime ? " UTC" : "");
}

// Escape first, then turn URLs, @mentions and #hashtags into links.
function richText(text) {
  let h = esc(text);
  h = h.replace(/https?:\/\/[^\s<]+/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u.replace(/^https?:\/\//, "")}</a>`);
  h = h.replace(/(^|[^\w/])@(\w{1,15})/g, (m, p, u) => `${p}<a href="https://x.com/${u}" target="_blank" rel="noopener noreferrer">@${u}</a>`);
  h = h.replace(/(^|[^\w&/])#(\w+)/g, (m, p, t) => `${p}<a href="https://x.com/hashtag/${t}" target="_blank" rel="noopener noreferrer">#${t}</a>`);
  return h;
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "") || "overview";
  const [path, query = ""] = raw.split("?");
  return { parts: path.split("/"), params: Object.fromEntries(new URLSearchParams(query)) };
}

function go(view, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  location.hash = `#/${view}${qs.toString() ? "?" + qs : ""}`;
}

function formToParams(form) {
  return Object.fromEntries([...new FormData(form)].filter(([, v]) => v !== ""));
}

function fillForm(form, params) {
  for (const el of form.elements) if (el.name) el.value = params[el.name] ?? (el.tagName === "SELECT" ? el.options[0].value : "");
}

// ---------------------------------------------------------------- badges

const LABELS = {
  subject_own: ["badge-real", "✓ Subject's personal account"],
  subject_affiliated: ["badge-real", "✓ Subject's official account"],
  impersonation_suspect: ["badge-suspect", "⚠ Possible impersonation"],
  third_party: ["badge-other", "Other account"],
  unknown: ["badge-soft", "Unknown author"],
};

// Content-analytics categories. Fixed order = fixed colour slot (see styles.css).
const CATS = [
  ["disinformation", "Disinformation"],
  ["racism", "Racism"],
  ["hate_speech", "Hate speech"],
  ["harassment", "Harassment"],
];
const CAT_NAME = Object.fromEntries(CATS);
const pct = v => (v == null ? "–" : `${(v * 100).toFixed(v < 0.1 && v > 0 ? 1 : 0)}%`);
const sw = cat => `<i class="sw sw-${cat}" aria-hidden="true"></i>`;

function catBadge(cat) {
  if (!cat || !CAT_NAME[cat]) return "";
  return `<span class="badge badge-cat" title="Automated label: open the post for the score and reasoning">${sw(cat)}${CAT_NAME[cat]}</span>`;
}

function severityHtml(sev) {
  if (!sev) return "";
  return sev === "critical"
    ? `<span class="sev critical">▲ Critical</span>`
    : `<span class="sev warning">△ Warning</span>`;
}

function badge(attribution) {
  const [cls, text] = LABELS[attribution] || LABELS.unknown;
  return `<span class="badge ${cls}">${text}</span>`;
}

function cardClass(item) {
  if (item.is_real) return "real";
  if (item.attribution === "impersonation_suspect") return "suspect";
  return "other";
}

// ---------------------------------------------------------------- post card

function mediaHtml(media) {
  if (!media?.length) return "";
  const n = Math.min(media.length, 4);
  const cells = media.slice(0, 4).map(m => {
    if (m.type === "video" || m.type === "animated_gif") {
      const gif = m.type === "animated_gif";
      return `<video controls preload="none" playsinline ${gif ? "loop muted" : ""}
                ${m.poster ? `poster="${esc(m.poster)}"` : ""} referrerpolicy="no-referrer">
                <source src="${esc(m.url)}" type="video/mp4"></video>`;
    }
    return `<a href="${esc(m.url)}?name=orig" target="_blank" rel="noopener noreferrer">
              <img src="${esc(m.url)}" alt="Attached photo" loading="lazy" referrerpolicy="no-referrer"></a>`;
  }).join("");
  return `<div class="media n${n}">${cells}</div>`;
}

function avatarHtml(a) {
  const src = a?.avatar || "";
  return src
    ? `<img class="avatar" src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="avatar" aria-hidden="true"></span>`;
}

function card(item, { compact = false } = {}) {
  const a = item.author || {};
  const tags = [];
  if (item.is_reply) tags.push("Reply");
  if (item.is_quote) tags.push("Quote");
  if (item.mentions_subject === false && !item.is_real) tags.push("Name not in text");
  if (a.verified) tags.push("X-verified");
  const label = catBadge(item.category);
  return `
  <article class="card ${cardClass(item)}" data-id="${item.id}">
    <div class="card-head">
      ${avatarHtml(a)}
      <div class="who">
        <div class="name">${esc(a.name || a.handle || "Unknown")}</div>
        <div class="handle"><a href="https://x.com/${esc(a.handle)}" target="_blank" rel="noopener noreferrer">@${esc(a.handle || "?")}</a>
          · ${num(a.followers)} followers</div>
        <div class="card-meta">${badge(item.attribution)}<span class="when">${when(item.posted_at)}</span></div>
      </div>
    </div>
    <div class="text">${richText(item.text)}</div>
    ${tags.length || label ? `<div class="tags">${label}${tags.map(t => `<span class="badge badge-soft">${t}</span>`).join("")}</div>` : ""}
    ${compact ? "" : mediaHtml(item.media)}
    <div class="stats">
      <span title="Views">👁 <b>${num(item.view_count)}</b></span>
      <span title="Likes">♥ <b>${num(item.like_count)}</b></span>
      <span title="Reposts">⟲ <b>${num(item.repost_count)}</b></span>
      <span title="Replies">💬 <b>${num(item.reply_count)}</b></span>
      <span title="Quotes">❝ <b>${num(item.quote_count)}</b></span>
    </div>
    <div class="card-foot">
      <button type="button" data-open="${item.id}">Details &amp; reactions</button>
      <a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">View on X ↗</a>
    </div>
  </article>`;
}

// One delegated listener opens any card's details.
document.addEventListener("click", e => {
  const b = e.target.closest("[data-open]");
  if (b) { e.preventDefault(); location.hash = `#/post/${b.dataset.open}`; }
});

// ---------------------------------------------------------------- paged list

function pagedList({ el, countEl, moreBtn, params, label }) {
  let offset = 0, total = 0;
  async function load(reset) {
    if (reset) { offset = 0; el.innerHTML = `<div class="loading">Loading…</div>`; }
    moreBtn.disabled = true;
    try {
      const data = await api("items", { ...params, limit: PAGE, offset });
      total = data.total;
      if (reset) el.innerHTML = "";
      el.insertAdjacentHTML("beforeend", data.items.map(i => card(i)).join(""));
      offset += data.items.length;
      if (!total) el.innerHTML = `<div class="empty">No ${label} match these filters.</div>`;
      if (countEl) countEl.textContent = `${full(total)} ${label}`;
      moreBtn.hidden = offset >= total;
    } catch (err) {
      el.innerHTML = `<div class="empty">Could not load: ${esc(err.message)}</div>`;
    } finally { moreBtn.disabled = false; }
  }
  moreBtn.onclick = () => load(false);
  return load(true);
}

// ---------------------------------------------------------------- views

function mount(tplId) {
  const view = $("#view");
  view.innerHTML = "";
  view.appendChild($(tplId).content.cloneNode(true));
  return view;
}

async function renderOverview() {
  const v = mount("#tpl-overview");
  const m = META;
  $("#ov-title", v).textContent = m.case.subject_name;
  $("#ov-role", v).textContent = m.case.subject_role || "";
  $("#ov-desc", v).textContent = m.case.description || "";
  $("#ov-window", v).textContent =
    `Collection window ${when(m.case.window_start, false)} – ${when(m.case.window_end, false)} · X (Twitter)`;
  $("#ov-accounts", v).innerHTML = m.accounts.map(a => `
    <a class="account" href="#/explore?side=real&author=${esc(a.handle)}" title="Show this account's posts">
      ${a.avatar ? `<img src="${esc(a.avatar)}" alt="" referrerpolicy="no-referrer">` : ""}
      <div><b>@${esc(a.handle)}</b>
        <span>${esc(a.display_name || "")}</span>
        <span>✓ verified ${esc(a.role)} account${a.valid_from ? ` · since ${when(a.valid_from, false)}` : ""}</span></div>
    </a>`).join("");

  const t = m.totals;
  const video = m.media.filter(x => x.type !== "photo").reduce((s, x) => s + x.n, 0);
  const photo = m.media.filter(x => x.type === "photo").reduce((s, x) => s + x.n, 0);
  $("#ov-kpis", v).innerHTML = [
    ["real", full(t.real), "Real posts from the subject"],
    ["other", full(t.others), "Posts by other accounts"],
    ["", full(t.authors), "Distinct accounts"],
    ["", num(t.views), "Total views"],
    ["", `${full(photo)} / ${full(video)}`, "Photos / videos"],
  ].map(([c, val, l]) => `<div class="kpi ${c}"><div class="v">${val}</div><div class="l">${l}</div></div>`).join("");

  api("timeline").then(rows => drawChart($("#ov-chart", v), rows));
  api("items", { side: "others", sort: "views", limit: 3 })
    .then(d => { $("#ov-top-others", v).innerHTML = d.items.map(i => card(i)).join(""); });
  api("top-authors", { side: "others", limit: 12 }).then(rows => {
    $("#ov-authors", v).innerHTML = rows.map(r => `
      <li><a href="#/explore?side=others&author=${esc(r.handle)}&sort=views">
        ${r.avatar ? `<img src="${esc(r.avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="avatar"></span>`}
        <span class="who"><b>${esc(r.name || r.handle)}${r.verified ? " ✓" : ""}</b><span>@${esc(r.handle)} · ${num(r.followers)} followers</span></span>
        <span class="num"><b>${num(r.views)}</b>${r.posts} post${r.posts === 1 ? "" : "s"}</span>
      </a></li>`).join("");
  });
}

function drawChart(el, rows) {
  if (!rows.length) { el.innerHTML = `<div class="empty">No dated posts.</div>`; return; }
  // Fill gaps so the x-axis is continuous.
  const byDay = new Map(rows.map(r => [r.day, r]));
  const days = [];
  for (let d = new Date(rows[0].day + "T00:00:00Z"); d <= new Date(rows.at(-1).day + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + 1)) {
    const k = d.toISOString().slice(0, 10);
    days.push(byDay.get(k) || { day: k, real: 0, others: 0 });
  }
  const H = 220, pad = { l: 34, b: 24, t: 8 };
  const bw = Math.max(3, Math.floor((Math.max(el.clientWidth, 600) - pad.l) / days.length));
  const W = pad.l + bw * days.length;
  const max = Math.max(...days.map(d => d.real + d.others), 1);
  const y = v => (H - pad.b) - (v / max) * (H - pad.b - pad.t);
  const ticks = [0, Math.round(max / 2), max];
  let svg = `<svg width="${W}" height="${H}" role="img" aria-label="Posts per day, real versus other accounts">`;
  for (const tk of ticks) svg += `<line x1="${pad.l}" x2="${W}" y1="${y(tk)}" y2="${y(tk)}" stroke="#e6e4e6"/><text x="${pad.l - 6}" y="${y(tk) + 4}" text-anchor="end">${tk}</text>`;
  days.forEach((d, i) => {
    const x = pad.l + i * bw;
    const yo = y(d.others), yr = y(d.others + d.real);
    svg += `<g class="bar" data-day="${d.day}" data-real="${d.real}" data-others="${d.others}">
      <rect x="${x}" y="${pad.t}" width="${bw}" height="${H - pad.b - pad.t}" fill="transparent"/>
      <rect x="${x + 0.5}" y="${yo}" width="${bw - 1}" height="${(H - pad.b) - yo}" fill="#FBD884"/>
      <rect x="${x + 0.5}" y="${yr}" width="${bw - 1}" height="${yo - yr}" fill="#038095"/></g>`;
    if (d.day.endsWith("-01")) svg += `<text x="${x}" y="${H - 6}">${when(d.day, false).replace(/^\d+ /, "")}</text>`;
  });
  el.innerHTML = svg + "</svg>";

  const tip = document.createElement("div");
  tip.className = "tooltip"; tip.hidden = true; document.body.appendChild(tip);
  el.onmousemove = e => {
    const g = e.target.closest(".bar");
    if (!g) { tip.hidden = true; return; }
    tip.hidden = false;
    tip.innerHTML = `<b>${when(g.dataset.day, false)}</b><br>Real: ${g.dataset.real} · Others: ${g.dataset.others}`;
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 200) + "px";
    tip.style.top = (e.clientY - 44) + "px";
  };
  el.onmouseleave = () => { tip.hidden = true; };
  el.onclick = e => {
    const g = e.target.closest(".bar");
    if (g) { tip.remove(); go("compare", { start: g.dataset.day, end: g.dataset.day, sort: "views" }); }
  };
}

function renderCompare(params) {
  const v = mount("#tpl-compare");
  const form = $("#cmp-form", v);
  fillForm(form, params);
  form.onsubmit = e => { e.preventDefault(); go("compare", formToParams(form)); };
  $("[data-reset]", form).onclick = () => go("compare");
  const shared = { q: params.q, start: params.start, end: params.end, media: params.media, sort: params.sort || "recent" };
  pagedList({ el: $("#cmp-real", v), countEl: $("#cmp-real-count", v), moreBtn: $("#cmp-real-more", v),
              params: { ...shared, side: "real" }, label: "real posts" });
  pagedList({ el: $("#cmp-others", v), countEl: $("#cmp-others-count", v), moreBtn: $("#cmp-others-more", v),
              params: { ...shared, side: "others" }, label: "posts by others" });
}

function renderExplore(params) {
  const v = mount("#tpl-explore");
  const form = $("#exp-form", v);
  if (META.analytics) form.querySelectorAll("[data-analytics]").forEach(el => { el.hidden = false; });
  fillForm(form, params);
  form.onsubmit = e => { e.preventDefault(); go("explore", formToParams(form)); };
  $("[data-reset]", form).onclick = () => go("explore");
  if (params.author) {
    const af = $("#exp-author", v);
    af.hidden = false;
    af.innerHTML = `Showing posts by <b>@${esc(params.author)}</b>
      <a href="https://x.com/${esc(params.author)}" target="_blank" rel="noopener noreferrer">profile on X ↗</a>
      <button class="btn btn-ghost" type="button">Clear</button>`;
    $("button", af).onclick = () => { const p = { ...params }; delete p.author; go("explore", p); };
  }
  pagedList({ el: $("#exp-feed", v), countEl: $("#exp-count", v), moreBtn: $("#exp-more", v),
              params: { ...params, sort: params.sort || "recent" }, label: "posts" });
}

// ---------------------------------------------------------------- detail modal

function verdictHtml(item) {
  const a = item.author || {};
  if (item.is_real) return `
    <div class="verdict real"><h3>✓ Real — posted by the subject</h3>
      This post comes from one of ${esc(META.case.subject_name)}'s registered accounts. The match is on
      X's numeric account ID (<code>${esc(a.platform_user_id)}</code>), which stays the same if the handle changes.
      <div class="small muted">Rule: <code>${esc(item.attribution_reason)}</code></div></div>`;
  if (item.attribution === "impersonation_suspect") return `
    <div class="verdict suspect"><h3>⚠ Possible impersonation</h3>
      The handle looks like one of the subject's accounts, but the account ID does not match.
      <div class="small muted">Rule: <code>${esc(item.attribution_reason)}</code></div></div>`;
  return `
    <div class="verdict other"><h3>Posted by another account — not the subject</h3>
      @${esc(a.handle)} is not one of the subject's registered accounts. That alone does not make it
      false: compare any claim about what ${esc(META.case.subject_name)} said or did against the real posts
      (left column of Compare), and open the original on X to check context and Community Notes.
      <div class="small muted">Rule: <code>${esc(item.attribution_reason)}</code></div></div>`;
}

const VERDICTS = {
  contradicted: "Contradicted by the subject's own posts",
  supported: "Supported by the subject's own posts",
  unverified_claim: "Claim not verified against the subject's posts",
  unverified: "Claim not verified against the subject's posts",
  opinion: "Opinion, not a factual claim",
  no_claim: "No factual claim",
};

function analysisHtml(item, an) {
  if (!an) return "";
  if (an.status !== "classified") return `
    <section class="analysis"><h3>Automated content analysis</h3>
      <p class="muted small">Not scored (status: <code>${esc(an.status)}</code>).</p></section>`;
  const bars = CATS.map(([k, name]) => {
    const v = an[k] ?? 0, thr = an.thresholds?.[k] ?? 0.6, on = an[`is_${k}`];
    return `<div class="score-row">
      <span class="score-name">${sw(k)}${name}</span>
      <span class="score-track" role="img" aria-label="${name} score ${v.toFixed(2)}, threshold ${thr}">
        <span class="score-fill fill-${k}" style="width:${Math.min(v, 1) * 100}%"></span>
        <span class="score-thr" style="left:${thr * 100}%" title="Label threshold ${thr}"></span>
      </span>
      <span class="score-val"><b>${v.toFixed(2)}</b>${on ? `<span class="badge badge-cat">Labelled</span>` : ""}</span>
    </div>`;
  }).join("");
  const facts = [];
  if (an.disinfo_verdict && !item.is_real) facts.push(["Claim check", esc(VERDICTS[an.disinfo_verdict] || an.disinfo_verdict)]);
  if (an.claim_summary) facts.push(["Claim", esc(an.claim_summary)]);
  if (an.racism_target) facts.push(["Group targeted", esc(an.racism_target)]);
  if (an.hate_speech_basis?.length) facts.push(["Protected basis", an.hate_speech_basis.map(b => esc(b.replace(/_/g, " "))).join(", ")]);
  if (an.harassment_target) facts.push(["Aimed at", esc(an.harassment_target)]);
  facts.push(["Volume factor", `${an.harassment_volume.toFixed(2)} <span class="muted small">(harassment score is scaled by repeated posting at the same target)</span>`]);
  return `
    <section class="analysis">
      <div class="panel-head"><h3>Automated content analysis</h3>${catBadge(an.primary_category)}</div>
      <div class="scores">${bars}</div>
      <p class="muted small">The tick on each bar is the label threshold. Scores run from 0 to 1.</p>
      <dl class="facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${an.rationale ? `<blockquote class="rationale">${esc(an.rationale)}</blockquote>` : ""}
      ${an.references?.length ? `<details class="raw"><summary>Subject posts the claim was checked against (${an.references.length})</summary>
        <div class="stack refs">${an.references.map(r => card(r, { compact: true })).join("")}</div></details>` : ""}
      <p class="muted small">${esc(an.model || "")} · prompt ${esc(an.prompt_version || "")} · scored ${when(an.classified_at)}.
        Automated output. A person should review it before relying on it.</p>
    </section>`;
}

async function openPost(id) {
  const modal = $("#modal"), body = $("#modal-body");
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  body.innerHTML = `<div class="loading">Loading…</div>`;
  try {
    const { item, parent, reactions, analysis } = await api(`items/${id}`);
    const a = item.author || {};
    const realReactions = reactions.filter(r => r.is_real);
    const otherReactions = reactions.filter(r => !r.is_real);
    body.innerHTML = `
      <h2 id="modal-title">Post by @${esc(a.handle)}</h2>
      ${card(item)}
      ${verdictHtml(item)}
      ${analysisHtml(item, analysis)}
      <dl class="facts">
        <dt>Posted</dt><dd>${when(item.posted_at)}</dd>
        <dt>Original</dt><dd><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.url)}</a></dd>
        <dt>Author</dt><dd>${esc(a.name || "")} (@${esc(a.handle)}) · ${full(a.followers)} followers · ${full(a.following)} following${a.verified ? " · X-verified" : ""}</dd>
        ${a.description ? `<dt>Bio</dt><dd>${esc(a.description)}</dd>` : ""}
        <dt>Engagement</dt><dd>${full(item.view_count)} views · ${full(item.like_count)} likes · ${full(item.repost_count)} reposts · ${full(item.reply_count)} replies · ${full(item.quote_count)} quotes · ${full(item.bookmark_count)} bookmarks</dd>
        <dt>Language</dt><dd>${esc(item.lang || "–")}</dd>
        <dt>Post ID</dt><dd><code>${esc(item.platform_item_id)}</code></dd>
      </dl>
      ${parent ? `<h3 class="section-title">${item.is_quote ? "Quotes" : "Replies to"} this post</h3>${card(parent, { compact: true })}` : ""}
      ${item.is_real
        ? `<h3 class="section-title">How other accounts reacted (${otherReactions.length})</h3>
           <p class="muted small">Collected posts by others that reply to or quote this post, or sit in the same thread.</p>
           <div class="stack">${otherReactions.map(r => card(r, { compact: true })).join("") || `<div class="empty">No reactions in the dataset.</div>`}</div>
           ${realReactions.length ? `<h3 class="section-title">Same thread by the subject (${realReactions.length})</h3><div class="stack">${realReactions.map(r => card(r, { compact: true })).join("")}</div>` : ""}`
        : reactions.length
          ? `<h3 class="section-title">Same thread (${reactions.length})</h3><div class="stack">${reactions.map(r => card(r, { compact: true })).join("")}</div>`
          : ""}
      <details class="raw"><summary>Raw data as collected</summary><pre>${esc(JSON.stringify(item.raw, null, 2))}</pre></details>`;
    $(".modal-panel").scrollTop = 0;
  } catch (err) {
    body.innerHTML = `<div class="empty">Could not load post: ${esc(err.message)}</div>`;
  }
}

function closeModal() {
  $("#modal").hidden = true;
  document.body.style.overflow = "";
  $$pause();
  if (location.hash.startsWith("#/post/")) history.pushState(null, "", lastView), route(false);
}
function $$pause() { document.querySelectorAll("#modal video").forEach(v => v.pause()); }

$("#modal").addEventListener("click", e => { if (e.target.closest("[data-close]")) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#modal").hidden) closeModal(); });

// ---------------------------------------------------------------- analytics

async function renderAnalytics() {
  const v = mount("#tpl-analytics");
  const s = await api("analytics/summary");
  const run = s.last_run;
  $("#an-sub", v).textContent =
    `${full(s.classified)} of ${full(s.others)} posts by other accounts scored` +
    (run ? ` · latest run ${when(run.finished_at || run.started_at)} with ${run.model}, prompt ${run.prompt_version}` : "");

  const open = sev => s.alerts.filter(a => a.severity === sev).reduce((t, a) => t + a.n, 0);
  const share = n => (s.classified ? ` · ${pct(n / s.classified)}` : "");
  $("#an-kpis", v).innerHTML = [
    ["", "#/explore?side=others&category=any&sort=views", full(s.flagged), `Labelled posts${share(s.flagged)}`],
    ...CATS.map(([k, name]) => [`cat cat-${k}`, `#/explore?side=others&category=${k}&sort=views`,
      full(s[k]), `${name}${share(s[k])}`]),
    ["", "#/explore?side=others&category=any&sort=views", num(s.flagged_views), "Views of labelled posts"],
    ["", "", full(s.flagged_authors), "Accounts with a labelled post"],
    ["alert", "#an-alerts", `${full(open("critical"))} / ${full(open("warning"))}`, "Open alerts: critical / warning"],
  ].map(([c, href, val, l]) => href
    ? `<a class="kpi ${c}" href="${href}"><div class="v">${val}</div><div class="l">${l}</div></a>`
    : `<div class="kpi ${c}"><div class="v">${val}</div><div class="l">${l}</div></div>`).join("");
  $("#an-kpis a[href='#an-alerts']", v).onclick = e => { e.preventDefault(); $("#an-alerts").scrollIntoView({ behavior: "smooth" }); };

  $("#an-legend", v).innerHTML = CATS.map(([k, n]) => `<span>${sw(k)}${n}</span>`).join("");
  api("analytics/daily").then(d => {
    drawLabelChart($("#an-chart", v), d.days, d.alerts);
    $("#an-daily-table", v).innerHTML = dailyTable(d.days);
  });

  const af = $("#an-alert-form", v);
  af.onchange = () => loadAlerts(v, formToParams(af));
  loadAlerts(v, {});

  const pc = $("#an-profile-cat", v);
  pc.onchange = () => loadProfiles(v, pc.value);
  loadProfiles(v, "any");

  api("analytics/bursts").then(rows => { $("#an-bursts", v).innerHTML = burstsTable(rows); });
}

function drawLabelChart(el, rows, alerts) {
  if (!rows.length) { el.innerHTML = `<div class="empty">No daily analytics.</div>`; return; }
  const byDay = new Map(rows.map(r => [r.day, r]));
  const alertsByDay = new Map();
  for (const a of alerts) (alertsByDay.get(a.day) || alertsByDay.set(a.day, []).get(a.day)).push(a);
  const days = [];
  for (let d = new Date(rows[0].day + "T00:00:00Z"); d <= new Date(rows.at(-1).day + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + 1)) {
    const k = d.toISOString().slice(0, 10);
    days.push(byDay.get(k) || { day: k, posts: 0, disinformation: 0, racism: 0, hate_speech: 0, harassment: 0 });
  }
  const H = 240, pad = { l: 34, b: 24, t: 18 };
  const bw = Math.max(4, Math.floor((Math.max(el.clientWidth, 600) - pad.l) / days.length));
  const W = pad.l + bw * days.length;
  const total = d => CATS.reduce((t, [k]) => t + (d[k] || 0), 0);
  const max = Math.max(...days.map(total), 1);
  const y = v => (H - pad.b) - (v / max) * (H - pad.b - pad.t);
  const ticks = [...new Set([0, Math.round(max / 2), max])];
  let svg = `<svg width="${W}" height="${H}" role="img" aria-label="Labelled posts by other accounts per day, stacked by category">`;
  for (const tk of ticks) svg += `<line x1="${pad.l}" x2="${W}" y1="${y(tk)}" y2="${y(tk)}" stroke="#e6e4e6"/><text x="${pad.l - 6}" y="${y(tk) + 4}" text-anchor="end">${tk}</text>`;
  days.forEach((d, i) => {
    const x = pad.l + i * bw;
    let acc = 0, segs = "";
    for (const [k] of CATS) {
      const n = d[k] || 0;
      if (!n) continue;
      const y0 = y(acc), y1 = y(acc + n);
      // 1px surface gap between stacked segments.
      segs += `<rect class="seg-${k}" x="${x + 0.5}" y="${y1}" width="${bw - 1}" height="${Math.max(y0 - y1 - 1, 1)}"/>`;
      acc += n;
    }
    const al = alertsByDay.get(d.day) || [];
    const crit = al.some(a => a.severity === "critical");
    const mark = al.length
      ? `<path class="mark ${crit ? "critical" : "warning"}" d="M${x + bw / 2 - 4},${y(acc) - 3} l4,-7 l4,7 z"/>` : "";
    svg += `<g class="bar" data-day="${d.day}"><rect x="${x}" y="0" width="${bw}" height="${H - pad.b}" fill="transparent"/>${segs}${mark}</g>`;
    if (d.day.endsWith("-01")) svg += `<text x="${x}" y="${H - 6}">${when(d.day, false).replace(/^\d+ /, "")}</text>`;
  });
  el.innerHTML = svg + "</svg>";

  const tip = document.createElement("div");
  tip.className = "tooltip"; tip.hidden = true; document.body.appendChild(tip);
  el.onmousemove = e => {
    const g = e.target.closest(".bar");
    if (!g) { tip.hidden = true; return; }
    const d = byDay.get(g.dataset.day) || { posts: 0 };
    const al = alertsByDay.get(g.dataset.day) || [];
    tip.hidden = false;
    tip.innerHTML = `<b>${when(g.dataset.day, false)}</b> · ${full(d.posts)} posts scored<br>` +
      CATS.map(([k, n]) => `${sw(k)}${n}: ${d[k] || 0}${d.posts ? ` (${pct(d[`${k}_rate`])})` : ""}`).join("<br>") +
      (al.length ? `<br><span class="tip-alert">${al.length} alert${al.length > 1 ? "s" : ""}: ` +
        [...new Set(al.map(a => `${a.severity} ${CAT_NAME[a.category]}`))].join(", ") + "</span>" : "");
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 240) + "px";
    tip.style.top = Math.max(e.clientY - tip.offsetHeight - 10, 4) + "px";
  };
  el.onmouseleave = () => { tip.hidden = true; };
  el.onclick = e => {
    const g = e.target.closest(".bar");
    if (g) { tip.remove(); go("explore", { side: "others", category: "any", start: g.dataset.day, end: g.dataset.day, sort: "views" }); }
  };
}

function dailyTable(rows) {
  return `<table class="data"><thead><tr><th>Day</th><th class="n">Posts scored</th>
    ${CATS.map(([, n]) => `<th class="n">${n}</th>`).join("")}<th class="n">Views of labelled</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><a href="#/explore?side=others&category=any&start=${r.day}&end=${r.day}&sort=views">${when(r.day, false)}</a></td>
      <td class="n">${full(r.posts)}</td>
      ${CATS.map(([k]) => `<td class="n">${r[k] ? `${r[k]} <span class="muted">(${pct(r[`${k}_rate`])})</span>` : `<span class="muted">0</span>`}</td>`).join("")}
      <td class="n">${num(r.views_flagged)}</td></tr>`).join("")}
    </tbody></table>`;
}

function accountCell(r, href) {
  return `<a class="acct" href="${href}">
    ${r.avatar ? `<img src="${esc(r.avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="avatar"></span>`}
    <span class="who"><b>${esc(r.name || r.handle || "Unknown")}${r.verified ? " ✓" : ""}</b>
      <span>@${esc(r.handle || "?")}${r.followers != null ? ` · ${num(r.followers)} followers` : ""}</span></span></a>`;
}

function postButtons(ids, max = 5) {
  if (!ids?.length) return `<span class="muted">–</span>`;
  return `<span class="post-btns">${ids.slice(0, max).map((id, i) =>
    `<button type="button" class="chip" data-open="${id}" title="Open example post">${i + 1}</button>`).join("")}${
    ids.length > max ? `<span class="muted small">+${ids.length - max}</span>` : ""}</span>`;
}

const ALERT_PAGE = 20;
async function loadAlerts(v, params) {
  const el = $("#an-alerts", v), more = $("#an-alerts-more", v);
  el.innerHTML = `<div class="loading">Loading…</div>`;
  const rows = await api("analytics/alerts", params);
  if (!rows.length) { el.innerHTML = `<div class="empty">No alerts match.</div>`; more.hidden = true; return; }
  let shown = 0;
  const fmtVal = (r, v) => (r.metric === "rate" ? pct(v) : full(v));
  const row = r => {
    const day = r.scope === "subject";
    const what = day
      ? `<a href="#/explore?side=others&category=${r.category}&start=${r.scope_key}&end=${r.scope_key}&sort=views"><b>${when(r.scope_key, false)}</b></a>
         <div class="muted small">posts about ${esc(r.subject_label || "the subject")}</div>`
      : accountCell(r, `#/explore?side=others&author=${esc(r.handle || r.scope_key)}&category=${r.category}&sort=views`);
    return `<tr>
      <td>${severityHtml(r.severity)}</td>
      <td>${what}</td>
      <td>${sw(r.category)}${CAT_NAME[r.category] || esc(r.category)}</td>
      <td class="n"><b>${fmtVal(r, r.value)}</b> <span class="muted">≥ ${fmtVal(r, r.threshold)}</span>
        <div class="muted small">${r.metric === "rate" ? "share of posts" : "labelled posts"}</div></td>
      <td>${r.window_start ? `${when(r.window_start, false)}${!day && r.window_end ? ` – ${when(r.window_end, false)}` : ""}` : "–"}</td>
      <td>${postButtons(r.sample_item_ids)}</td>
      ${r.status !== "open" ? `<td><span class="badge badge-soft">${esc(r.status)}</span></td>` : ""}
    </tr>`;
  };
  el.innerHTML = `<table class="data"><thead><tr><th>Severity</th><th>Day or account</th><th>Category</th>
    <th class="n">Value vs. threshold</th><th>Window</th><th>Examples</th></tr></thead><tbody></tbody></table>`;
  const body = $("tbody", el);
  const page = () => {
    body.insertAdjacentHTML("beforeend", rows.slice(shown, shown + ALERT_PAGE).map(row).join(""));
    shown = Math.min(shown + ALERT_PAGE, rows.length);
    more.hidden = shown >= rows.length;
    more.textContent = `Show more (${full(rows.length - shown)} left)`;
  };
  more.onclick = page;
  page();
}

async function loadProfiles(v, category) {
  const el = $("#an-profiles", v), more = $("#an-profiles-more", v);
  let offset = 0;
  el.innerHTML = `<div class="loading">Loading…</div>`;
  const cell = (r, k) => r[k]
    ? `<b>${r[k]}</b> <span class="muted">${pct(r[`${k}_rate`])}</span>`
    : `<span class="muted">–</span>`;
  const row = r => `<tr>
    <td>${accountCell(r, `#/explore?side=others&author=${esc(r.handle)}&category=${category}&sort=views`)}</td>
    <td class="n">${full(r.posts)}</td>
    ${CATS.map(([k]) => `<td class="n${k === category ? " hl" : ""}">${cell(r, k)}</td>`).join("")}
    <td class="n">${r.peak != null ? r.peak.toFixed(2) : "–"}</td>
    <td>${severityHtml(r.alert) || `<span class="muted">–</span>`}</td></tr>`;
  async function page(reset) {
    more.disabled = true;
    const d = await api("analytics/profiles", { category, limit: 25, offset });
    if (reset) {
      if (!d.total) { el.innerHTML = `<div class="empty">No account has a post with this label.</div>`; more.hidden = true; return; }
      el.innerHTML = `<p class="muted small">${full(d.total)} accounts</p>
        <table class="data"><thead><tr><th>Account</th><th class="n">Posts</th>
        ${CATS.map(([k, n]) => `<th class="n${k === category ? " hl" : ""}">${sw(k)}${n}</th>`).join("")}
        <th class="n" title="Highest score among this account's posts">Peak score</th><th>Alert</th></tr></thead><tbody></tbody></table>`;
    }
    $("tbody", el).insertAdjacentHTML("beforeend", d.items.map(row).join(""));
    offset += d.items.length;
    more.hidden = offset >= d.total;
    more.disabled = false;
  }
  more.onclick = () => page(false);
  page(true);
}

function burstsTable(rows) {
  if (!rows.length) return `<div class="empty">No posting bursts detected.</div>`;
  const minutes = r => Math.max(1, Math.round((new Date(r.window_end) - new Date(r.window_start)) / 60000));
  const dur = m => (m < 120 ? `${m} min` : `${(m / 60).toFixed(1)} h`);
  return `<table class="data"><thead><tr><th>Account</th><th>Target</th><th>Pattern</th><th>When</th>
    <th class="n">Posts</th><th class="n" title="Highest per-post abuse score in the run (0–1)">Max abuse</th><th>Posts</th></tr></thead><tbody>
    ${rows.map(r => `<tr>
      <td>${accountCell(r, `#/explore?side=others&author=${esc(r.handle)}&start=${r.window_start.slice(0, 10)}&end=${r.window_end.slice(0, 10)}&sort=oldest`)}</td>
      <td>${r.target === "subject" ? "the subject" : esc(r.target)}</td>
      <td>${r.pattern === "burst" ? "Burst" : "Continuous replies"}</td>
      <td>${when(r.window_start)}<div class="muted small">over ${dur(minutes(r))}</div></td>
      <td class="n"><b>${r.post_count}</b>${r.reply_count ? `<div class="muted small">${r.reply_count} replies</div>` : ""}</td>
      <td class="n">${r.max_abuse != null ? r.max_abuse.toFixed(2) : "–"}</td>
      <td>${postButtons(r.item_ids, 6)}</td></tr>`).join("")}
    </tbody></table>`;
}

// ---------------------------------------------------------------- router

let rendered = null;

function route(allowModal = true) {
  const { parts, params } = parseHash();
  const view = parts[0];
  if (view === "post" && parts[1]) {
    if (allowModal) openPost(parts[1]);
    if (!rendered) renderFor(lastView);   // deep link: draw something behind the modal
    return;
  }
  if (!$("#modal").hidden) { $("#modal").hidden = true; document.body.style.overflow = ""; $$pause(); }
  lastView = location.hash || "#/overview";
  renderFor(lastView);
}

function renderFor(hash) {
  const saved = location.hash;
  const { parts, params } = (() => {
    const raw = hash.replace(/^#\/?/, "") || "overview";
    const [path, query = ""] = raw.split("?");
    return { parts: path.split("/"), params: Object.fromEntries(new URLSearchParams(query)) };
  })();
  const views = ["overview", "compare", "explore", ...(META.analytics ? ["analytics"] : [])];
  const view = views.includes(parts[0]) ? parts[0] : "overview";
  document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("active", a.dataset.view === view));
  document.querySelectorAll(".tooltip").forEach(t => t.remove());
  if (rendered === hash) return;
  rendered = hash;
  const fn = { overview: renderOverview, compare: renderCompare, explore: renderExplore, analytics: renderAnalytics }[view];
  Promise.resolve(fn(params)).catch(err => {
    $("#view").innerHTML = `<div class="empty">Could not load: ${esc(err.message)}</div>`;
  });
  if (saved === hash) window.scrollTo(0, 0);
}

async function boot() {
  try {
    META = await api("meta");
  } catch (err) {
    $("#view").innerHTML = `<div class="empty">Cannot reach the database: ${esc(err.message)}</div>`;
    return;
  }
  $("#brand-sub").textContent = `${META.case.subject_name} · real vs. other accounts`;
  $("#db-pill").textContent = `DB: ${META.database}`;
  $("#tab-analytics").hidden = !META.analytics;
  document.title = `Evidence Viewer · ${META.case.subject_name}`;
  addEventListener("hashchange", () => route());
  route();
}

boot();
