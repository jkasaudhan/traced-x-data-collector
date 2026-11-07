// Evidence Viewer — vanilla JS, no build step. Hash routes:
//   #/overview
//   #/compare?q=&start=&end=&media=&sort=
//   #/explore?q=&side=&kind=&media=&start=&end=&sort=&author=
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
    ${tags.length ? `<div class="tags">${tags.map(t => `<span class="badge badge-soft">${t}</span>`).join("")}</div>` : ""}
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

async function openPost(id) {
  const modal = $("#modal"), body = $("#modal-body");
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  body.innerHTML = `<div class="loading">Loading…</div>`;
  try {
    const { item, parent, reactions } = await api(`items/${id}`);
    const a = item.author || {};
    const realReactions = reactions.filter(r => r.is_real);
    const otherReactions = reactions.filter(r => !r.is_real);
    body.innerHTML = `
      <h2 id="modal-title">Post by @${esc(a.handle)}</h2>
      ${card(item)}
      ${verdictHtml(item)}
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
  const view = ["overview", "compare", "explore"].includes(parts[0]) ? parts[0] : "overview";
  document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("active", a.dataset.view === view));
  document.querySelectorAll(".tooltip").forEach(t => t.remove());
  if (rendered === hash) return;
  rendered = hash;
  ({ overview: renderOverview, compare: renderCompare, explore: renderExplore })[view](params);
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
  document.title = `Evidence Viewer · ${META.case.subject_name}`;
  addEventListener("hashchange", () => route());
  route();
}

boot();
