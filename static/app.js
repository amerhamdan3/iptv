/* IPTV browser - all state lives on the server, this is just the shell. */

const $ = (s) => document.querySelector(s);
const content = $("#content");
const sidebar = $("#sidebar");

const state = {
  tab: "home", category: "", query: "", cats: {},
  // Per-kind so switching Movies <-> Series keeps each one's filters.
  filters: {
    vod: { genre: "", year: "", rating: "", sort: "rating" },
    series: { genre: "", year: "", rating: "", sort: "rating" },
  },
  facets: {},
  seq: 0, // bumps on every grid render so late responses can be dropped
};

const PAGE = 120;

/* ------------------------------------------------------------ helpers */

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text();
    let msg = text || r.statusText;
    try { msg = JSON.parse(text).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const img = (u) => (u ? `/img?u=${encodeURIComponent(u)}` : "");

const epLabel = (s, e) =>
  `S${String(s).padStart(2, "0")}E${String(e).padStart(2, "0")}`;

function fmtTime(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return (n / 1024 ** i).toFixed(i ? 1 : 0) + " " + u[i];
}

function toast(msg, bad = false) {
  const d = document.createElement("div");
  d.textContent = msg;
  d.style.cssText =
    `position:fixed;bottom:44px;left:50%;transform:translateX(-50%);
     background:${bad ? "#8b2c2c" : "#1c232c"};color:#e6edf3;padding:10px 18px;
     border-radius:8px;z-index:99;border:1px solid #2a323d;font-size:13px`;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 3200);
}

/* ------------------------------------------------------------ actions */

async function play(kind, id, restart = false) {
  try {
    const r = await api("/api/play", { method: "POST", body: { kind, item_id: id, restart } });
    toast(r.offline ? `▶ Playing offline copy` : `▶ Playing`);
    setTimeout(poll, 700);
  } catch (e) {
    toast("Could not start playback: " + e.message, true);
  }
}

async function toggleFav(kind, id, btn) {
  const r = await api("/api/favorite", { method: "POST", body: { kind, item_id: id } });
  btn.classList.toggle("on", r.favorite);
  if (state.tab === "favorites") render();
}

async function download(kind, id) {
  try {
    await api("/api/download", { method: "POST", body: { kind, item_id: id } });
    toast("⬇ Added to offline queue");
  } catch (e) {
    toast("Download failed: " + e.message, true);
  }
}

async function markWatched(kind, id, completed) {
  await api("/api/mark", { method: "POST", body: { kind, item_id: id, completed } });
}

/* ------------------------------------------------------------ cards */

function cardHTML(it) {
  const kind = it.kind;
  const meta = [it.rating > 0 ? `★ ${(+it.rating).toFixed(1)}` : "", it.year || ""]
    .filter(Boolean).join(" · ");
  return `
  <div class="card ${kind === "live" ? "live" : ""}" data-kind="${kind}" data-id="${it.id}">
    <img class="thumb" loading="lazy" src="${img(it.icon)}" alt=""
         onerror="this.style.visibility='hidden'">
    ${meta ? `<span class="badge">${meta}</span>` : ""}
    <button class="star ${it.favorite ? "on" : ""}" data-fav="1">★</button>
    ${mineBadge(it.mine)}
    <div class="label" dir="auto">${esc(it.name)}</div>
  </div>`;
}

function bindCards(root) {
  root.querySelectorAll(".card").forEach((el) => {
    const kind = el.dataset.kind, id = +el.dataset.id;
    el.addEventListener("click", (ev) => {
      if (ev.target.dataset.fav) return toggleFav(kind, id, ev.target);
      if (kind === "series") openSeries(id);
      else if (kind === "vod") openMovie(id);
      else openItem(kind, id, el.querySelector(".label").textContent);
    });
  });
}

/* ------------------------------------------------------------ modals */

function showModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
$("#modal-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

function openItem(kind, id, name) {
  const dl = kind === "vod"
    ? `<button class="ghost" id="m-dl">⬇ Save offline</button>` : "";
  showModal(`
    <h2 dir="auto">${esc(name)}</h2>
    <div class="btns" style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
      <button class="primary" id="m-play">▶ Play</button>
      <button class="ghost" id="m-restart">↺ Start over</button>
      ${dl}
    </div>`);
  $("#m-play").onclick = () => { play(kind, id); $("#modal").classList.add("hidden"); };
  $("#m-restart").onclick = () => { play(kind, id, true); $("#modal").classList.add("hidden"); };
  if ($("#m-dl")) $("#m-dl").onclick = () => download(kind, id);
}

/* My list: the shared watchlog entry for a title (watched, rating, verdict).
   `mine` is null when the watchlog isn't set up, {} when the title isn't on
   the list yet. */
function mineBadge(m) {
  if (!m || !m.status) return "";
  const txt = m.status === "watched"
    ? `✓${m.rating ? " " + m.rating : ""}${m.liked === -1 ? " 👎" : ""}`
    : m.status === "suggested" ? "💡" : "📌";
  const tip = m.status === "watched"
    ? `Watched${m.watched_at ? " " + m.watched_at : ""}${m.rating ? " · rated " + m.rating + "/10" : ""}`
    : m.status === "suggested" ? "Suggested for you" : "On your watchlist";
  return `<span class="mine-badge ${m.status}" title="${esc(tip)}">${txt}</span>`;
}

function mineHTML(m) {
  if (m == null) return "";
  const on = (c) => (c ? "on" : "");
  return `
    <div class="mine" id="mine">
      <div class="mine-head">
        <b>My list</b>
        <span class="muted">${m.pending ? "⏳ will sync when the list is reachable"
          : m.status === "watched" ? `✓ Watched${m.watched_at ? " · " + esc(m.watched_at) : ""}`
          : m.status === "suggested" ? "💡 Suggested for you"
          : m.status === "watchlist" ? "📌 On your watchlist" : "Not on your list yet"}</span>
      </div>
      ${m.status === "suggested" && m.reason ? `<div class="muted mine-reason" dir="auto">${esc(m.reason)}</div>` : ""}
      <div class="mine-row">
        <button class="small ghost ${on(m.status === "watched")}" data-mine-status="watched">✓ Watched</button>
        <button class="small ghost ${on(m.status === "watchlist")}" data-mine-status="watchlist">📌 Watchlist</button>
        <span class="mine-sep"></span>
        <button class="small ghost up ${on(m.liked === 1)}" data-mine-liked="1" title="Liked it">👍</button>
        <button class="small ghost down ${on(m.liked === -1)}" data-mine-liked="-1" title="Didn't like it">👎</button>
      </div>
      <div class="mine-stars">${[1,2,3,4,5,6,7,8,9,10].map((n) =>
        `<button class="${m.rating && n <= m.rating ? "on" : ""}" data-mine-rate="${n}">${n}</button>`).join("")}</div>
    </div>`;
}

function bindMine(kind, id, m) {
  const box = $("#mine");
  if (!box) return;
  let cur = { ...(m || {}) };
  const send = async (changes) => {
    box.classList.add("busy");
    try {
      const r = await api("/api/mine", { method: "POST", body: { kind, item_id: id, changes } });
      cur = { ...cur, ...r };
      box.outerHTML = mineHTML(cur);
      bindMine(kind, id, cur);
      if (r.pending) toast("Saved here; it will sync to your list when it's reachable");
    } catch (e) {
      box.classList.remove("busy");
      toast("My list: " + e.message, true);
    }
  };
  box.querySelectorAll("[data-mine-status]").forEach((b) => b.onclick = () => {
    const v = b.dataset.mineStatus;
    if (cur.status !== v) send({ status: v });
  });
  box.querySelectorAll("[data-mine-liked]").forEach((b) => b.onclick = () => {
    const v = +b.dataset.mineLiked;
    send({ liked: cur.liked === v ? null : v });
  });
  box.querySelectorAll("[data-mine-rate]").forEach((b) => b.onclick = () => {
    const v = +b.dataset.mineRate;
    // Rating something means you've seen it; tap the same score to clear it.
    const changes = { rating: cur.rating === v ? null : v };
    if (changes.rating && cur.status !== "watched") changes.status = "watched";
    send(changes);
  });
}

/* IMDb panel: shared by movies and series. Cached data shows straight away;
   otherwise a button fetches it on demand. */
function imdbHTML(d) {
  if (!d) {
    return `<div class="imdb" id="imdb">
      <button class="ghost" id="imdb-fetch">🎬 Fetch from IMDb</button></div>`;
  }
  const votes = d.votes >= 1e6 ? (d.votes / 1e6).toFixed(1) + "M"
    : d.votes >= 1e3 ? Math.round(d.votes / 1e3) + "K" : d.votes;
  const years = d.end_year && d.end_year !== d.year ? `${d.year}–${d.end_year}` : d.year;
  const facts = [
    years, d.certificate, d.runtime_sec ? fmtTime(d.runtime_sec) : "",
    (d.genres || []).join(", "),
  ].filter(Boolean).map(esc).join(" · ");
  const credits = Object.entries(d.credits || {}).map(([role, names]) =>
    `<div><span class="muted">${esc(role)}:</span> ${esc(names.join(", "))}</div>`).join("");
  const more = [
    d.countries?.length ? `<div><span class="muted">Country:</span> ${esc(d.countries.join(", "))}</div>` : "",
    d.languages?.length ? `<div><span class="muted">Language:</span> ${esc(d.languages.join(", "))}</div>` : "",
  ].join("");
  return `
    <div class="imdb" id="imdb">
      <div class="imdb-head">
        <span class="imdb-logo">IMDb</span>
        ${d.rating ? `<span class="imdb-score">★ ${d.rating}<small>/10</small></span>` : `<span class="muted">No rating yet</span>`}
        ${d.votes ? `<span class="muted">${votes} votes</span>` : ""}
        ${d.metascore ? `<span class="imdb-meta" title="Metascore">${d.metascore}</span>` : ""}
      </div>
      <div class="imdb-title"><b dir="auto">${esc(d.title)}</b>${facts ? ` · ${facts}` : ""}</div>
      ${d.plot ? `<div class="plot" dir="auto">${esc(d.plot)}</div>` : ""}
      <div class="imdb-credits">${credits}${more}</div>
      <div class="imdb-actions">
        <a href="${esc(d.url)}" target="_blank" rel="noopener">Open on IMDb ↗</a>
        <button class="small ghost" id="imdb-fetch" title="Fetch again">↻ Refresh</button>
        <button class="small ghost" id="imdb-fix">Wrong title?</button>
      </div>
    </div>`;
}

function bindImdb(kind, id) {
  const run = async (imdb_id = "") => {
    const box = $("#imdb");
    box.innerHTML = `<span class="muted">Looking up on IMDb…</span>`;
    try {
      const d = await api("/api/imdb", { method: "POST", body: { kind, item_id: id, imdb_id } });
      box.outerHTML = imdbHTML(d);
    } catch (e) {
      box.outerHTML = imdbHTML(null);
      toast("IMDb: " + e.message, true);
    }
    bindImdb(kind, id);
  };
  const fetchBtn = $("#imdb-fetch"), fixBtn = $("#imdb-fix");
  if (fetchBtn) fetchBtn.onclick = () => run();
  if (fixBtn) fixBtn.onclick = () => {
    const v = prompt("Paste the IMDb link or ID (tt…) for this title:");
    if (v && /tt\d{5,}/.test(v)) run(v);
    else if (v) toast("That doesn't look like an IMDb link", true);
  };
}

async function openMovie(id) {
  showModal(`<div class="empty">Loading…</div>`);
  let m;
  try {
    m = await api(`/api/vod/${id}`);
  } catch (e) {
    return showModal(`<div class="empty">Could not load: ${esc(e.message)}</div>`);
  }
  const h = m.history;
  const resumeAt = h && !h.completed && h.position_sec > 15 ? h.position_sec : 0;
  const pct = resumeAt && h.duration_sec ? Math.min(100, (resumeAt / h.duration_sec) * 100) : 0;
  const facts = [
    m.genre, m.year || "", m.duration_secs ? fmtTime(m.duration_secs) : "",
    m.rating > 0 ? `★ ${(+m.rating).toFixed(1)}` : "",
  ].filter(Boolean).map(esc).join(" · ");

  showModal(`
    <div class="hero">
      <img src="${img(m.icon)}" alt="" onerror="this.style.visibility='hidden'">
      <div style="flex:1;min-width:0">
        <h2 dir="auto">${esc(m.name)}</h2>
        ${facts ? `<div class="muted" style="margin-bottom:6px">${facts}</div>` : ""}
        <div class="plot" dir="auto">${esc(m.plot) || '<span class="muted">No description from the provider.</span>'}</div>
        ${m.director ? `<div class="credit"><span class="muted">Director:</span> ${esc(m.director)}</div>` : ""}
        ${m.cast ? `<div class="credit"><span class="muted">Cast:</span> ${esc(m.cast)}</div>` : ""}
        ${resumeAt ? `<div class="muted" style="margin-top:8px">Watched to ${fmtTime(resumeAt)}</div>
          ${pct > 1 ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}` : ""}
        <div class="btns" style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="primary" id="m-play">▶ ${resumeAt ? "Resume" : "Play"}</button>
          ${resumeAt || h?.completed ? `<button class="ghost" id="m-restart">↺ Start over</button>` : ""}
          <button class="ghost" id="m-dl">${m.download === "done" ? "✓ Saved offline" : m.download ? "⬇ Queued" : "⬇ Save offline"}</button>
          <button class="ghost" id="m-fav">${m.favorite ? "★ In favorites" : "☆ Add to favorites"}</button>
        </div>
      </div>
    </div>
    ${mineHTML(m.mine)}
    ${imdbHTML(m.imdb)}`);

  const close = () => $("#modal").classList.add("hidden");
  $("#m-play").onclick = () => { play("vod", id); close(); };
  if ($("#m-restart")) $("#m-restart").onclick = () => { play("vod", id, true); close(); };
  $("#m-dl").onclick = (e) => { if (!m.download) { download("vod", id); e.target.textContent = "⬇ Queued"; } };
  $("#m-fav").onclick = async (e) => {
    const r = await api("/api/favorite", { method: "POST", body: { kind: "vod", item_id: id } });
    e.target.textContent = r.favorite ? "★ In favorites" : "☆ Add to favorites";
  };
  bindImdb("vod", id);
  bindMine("vod", id, m.mine);
}

async function openSeries(id) {
  showModal(`<div class="empty">Loading episodes…</div>`);
  let data;
  try {
    data = await api(`/api/series/${id}`);
  } catch (e) {
    return showModal(`<div class="empty">Could not load: ${esc(e.message)}</div>`);
  }

  const s = data.series, eps = data.episodes, prog = data.progress;

  // Group by season so the list reads like a show, not a flat dump.
  const seasons = {};
  eps.forEach((e) => (seasons[e.season] ||= []).push(e));

  let banner = "";
  if (prog && prog.action !== "done") {
    const c = prog.current, n = prog.next;
    const at = prog.action === "resume"
      ? `You're on <b>${epLabel(c.season, c.ep_num)}</b> — ${fmtTime(c.position_sec)} in`
      : `Finished <b>${epLabel(c.season, c.ep_num)}</b>`;
    const target = prog.action === "resume" ? c : n;
    const verb = prog.action === "resume"
      ? `▶ Resume ${epLabel(c.season, c.ep_num)}`
      : `▶ Play next — ${epLabel(n.season, n.ep_num)}`;
    banner = `
      <div class="cw" style="min-width:0;margin:14px 0">
        <div class="info">
          <div class="at">${at}</div>
          ${n && prog.action === "resume"
            ? `<div class="muted">Next up: ${epLabel(n.season, n.ep_num)}</div>` : ""}
          <div class="btns">
            <button class="primary" data-play-ep="${target.episode_id || target.item_id}">${verb}</button>
          </div>
        </div>
      </div>`;
  }

  const body = Object.keys(seasons)
    .sort((a, b) => a - b)
    .map((sn) => `
      <div class="season-h">Season ${sn} · ${seasons[sn].length} episodes</div>
      ${seasons[sn].map((e) => {
        const pct = e.duration_secs && e.position_sec
          ? Math.min(100, (e.position_sec / e.duration_secs) * 100) : 0;
        const dls = e.download_status;
        return `
        <div class="ep ${e.completed ? "done" : ""}">
          <span class="num">${e.completed ? '<span class="tick">✓</span> ' : ""}${epLabel(e.season, e.ep_num)}</span>
          <span class="t" dir="auto">${esc(e.title || "Episode " + e.ep_num)}
            <small>${e.duration_secs ? " · " + fmtTime(e.duration_secs) : ""}</small>
            ${pct > 1 && !e.completed ? `<div class="bar" style="margin-top:4px"><i style="width:${pct}%"></i></div>` : ""}
          </span>
          <span class="acts">
            <button class="small" data-play-ep="${e.episode_id}">▶</button>
            <button class="ghost" data-dl-ep="${e.episode_id}" title="Save offline">
              ${dls === "done" ? "✓⬇" : dls ? "…" : "⬇"}</button>
            <button class="ghost" data-mark-ep="${e.episode_id}" data-done="${e.completed ? 1 : 0}"
              title="${e.completed ? "Mark unwatched" : "Mark watched"}">${e.completed ? "↺" : "✓"}</button>
          </span>
        </div>`;
      }).join("")}
    `).join("");

  showModal(`
    <div class="hero">
      <img src="${img(s.cover)}" alt="" onerror="this.style.visibility='hidden'">
      <div style="flex:1;min-width:0">
        <h2 dir="auto">${esc(s.name)}</h2>
        <div class="muted" style="margin-bottom:6px">
          ${esc(s.genre || "")}${s.release_date ? " · " + esc(s.release_date) : ""}
          ${s.rating ? " · ★ " + s.rating : ""}
        </div>
        <div class="plot" dir="auto">${esc(s.plot) || '<span class="muted">No description from the provider.</span>'}</div>
        <div style="margin-top:10px">
          <button class="ghost" id="s-fav">${s.favorite ? "★ In favorites" : "☆ Add to favorites"}</button>
        </div>
      </div>
    </div>
    ${mineHTML(data.mine)}
    ${imdbHTML(data.imdb)}
    ${banner}
    ${body || '<div class="empty">No episodes listed.</div>'}
  `);

  $("#s-fav").onclick = async (e) => {
    const r = await api("/api/favorite", { method: "POST", body: { kind: "series", item_id: id } });
    e.target.textContent = r.favorite ? "★ In favorites" : "☆ Add to favorites";
  };
  bindImdb("series", id);
  bindMine("series", id, data.mine);
  $("#modal-body").querySelectorAll("[data-play-ep]").forEach((b) =>
    b.onclick = () => { play("episode", +b.dataset.playEp); $("#modal").classList.add("hidden"); });
  $("#modal-body").querySelectorAll("[data-dl-ep]").forEach((b) =>
    b.onclick = () => { download("episode", +b.dataset.dlEp); b.textContent = "…"; });
  $("#modal-body").querySelectorAll("[data-mark-ep]").forEach((b) =>
    b.onclick = async () => {
      await markWatched("episode", +b.dataset.markEp, b.dataset.done !== "1");
      openSeries(id);
    });
}

/* ------------------------------------------------------------ views */

async function renderHome() {
  sidebar.classList.add("hidden");
  const [cont, favs] = await Promise.all([
    api("/api/continue"), api("/api/favorites"),
  ]);

  let html = "";

  if (cont.length) {
    html += `<h2>Continue watching</h2><div class="continue-row">`;
    html += cont.map((c) => {
      const p = c.progress, cur = p.current, nxt = p.next;
      let line, btn, epId;
      if (c.kind === "series") {
        if (p.action === "resume") {
          line = `You're on <b>${epLabel(cur.season, cur.ep_num)}</b> — ${fmtTime(cur.position_sec)} in`;
          btn = `▶ Resume ${epLabel(cur.season, cur.ep_num)}`;
          epId = cur.item_id;
        } else {
          line = `Finished <b>${epLabel(cur.season, cur.ep_num)}</b>`;
          btn = `▶ Play ${epLabel(nxt.season, nxt.ep_num)}`;
          epId = nxt.episode_id;
        }
      } else {
        line = `${fmtTime(cur.position_sec)} in`;
        btn = `▶ Resume`;
        epId = cur.item_id;
      }
      const pct = cur.duration_sec || cur.duration_secs
        ? Math.min(100, (cur.position_sec / (cur.duration_sec || cur.duration_secs)) * 100) : 0;
      const kind = c.kind === "series" ? "episode" : "vod";
      const nextLine = c.kind === "series" && p.action === "resume" && nxt
        ? `<div class="muted" style="font-size:12px">Next up: ${epLabel(nxt.season, nxt.ep_num)}</div>` : "";
      return `
        <div class="cw">
          <img src="${img(c.icon)}" alt="" onerror="this.style.visibility='hidden'">
          <div class="info">
            <div class="name" dir="auto">${esc(c.name)}</div>
            <div class="at">${line}</div>
            ${nextLine}
            ${pct > 1 ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
            <div class="btns">
              <button class="primary" data-cw="${kind}:${epId}">${btn}</button>
              ${c.kind === "series"
                ? `<button class="ghost" data-open-series="${c.id}">Episodes</button>` : ""}
            </div>
          </div>
        </div>`;
    }).join("");
    html += `</div>`;
  }

  html += favSectionsHTML(favs);

  if (!html) {
    html = `<div class="empty">Nothing watched yet.<br><br>
      Pick something from <b>Live</b>, <b>Movies</b> or <b>Series</b> — once you
      start watching, this page remembers exactly where you left off.</div>`;
  }

  content.innerHTML = html;
  bindCards(content);
  content.querySelectorAll("[data-cw]").forEach((b) => {
    const [kind, id] = b.dataset.cw.split(":");
    b.onclick = () => play(kind, +id);
  });
  content.querySelectorAll("[data-open-series]").forEach((b) =>
    b.onclick = () => openSeries(+b.dataset.openSeries));
}

async function renderCategories(kind) {
  sidebar.classList.remove("hidden");
  if (!state.cats[kind]) state.cats[kind] = await api(`/api/categories?kind=${kind}`);
  const cats = state.cats[kind];
  // No "All" entry on purpose: thousands of posters at once is unusable.
  // Clicking the active category again deselects it.
  $("#cats").innerHTML = cats.map((c) => `<div class="cat ${state.category === c.id ? "active" : ""}"
        data-c="${esc(c.id)}"><span dir="auto">${esc(c.name)}</span>
        <span class="n">${c.count}</span></div>`).join("");
  $("#cats").querySelectorAll(".cat").forEach((el) =>
    el.onclick = () => {
      state.category = state.category === el.dataset.c ? "" : el.dataset.c;
      render();
    });
}

const hasFilter = (f) => !!(f && (f.genre || f.year || f.rating));

/* "2012" -> 2012..2012, "2010s" -> 2010..2019 */
function yearRange(v) {
  if (!v) return [0, 0];
  if (v.endsWith("s")) return [+v.slice(0, -1), +v.slice(0, -1) + 9];
  return [+v, +v];
}

function browseURL(kind, offset) {
  const p = new URLSearchParams({ kind, category: state.category, offset, limit: PAGE });
  const f = state.filters[kind];
  if (f) {
    const [yf, yt] = yearRange(f.year);
    if (f.genre) p.set("genre", f.genre);
    if (yf) { p.set("year_from", yf); p.set("year_to", yt); }
    if (f.rating) p.set("min_rating", f.rating);
    p.set("sort", f.sort);
  }
  return `/api/browse?${p}`;
}

async function filterBarHTML(kind) {
  // Movie genres trickle in while the details backfill runs; don't cache yet.
  if (!state.facets[kind] || (kind === "vod" && state.detailsRunning))
    state.facets[kind] = await api(`/api/facets?kind=${kind}`);
  const { genres, years } = state.facets[kind];
  const f = state.filters[kind];
  const opt = (v, label, cur) =>
    `<option value="${esc(v)}" ${String(cur) === String(v) ? "selected" : ""}>${esc(label)}</option>`;

  const decades = [...new Set(years.map((y) => Math.floor(y.year / 10) * 10))];
  return `
    <div class="filters">
      <select data-f="genre">
        ${opt("", "All genres", f.genre)}
        ${genres.map((g) => opt(g.name, `${g.name} (${g.count})`, f.genre)).join("")}
      </select>
      <select data-f="year">
        ${opt("", "Any year", f.year)}
        <optgroup label="Decade">${decades.map((d) => opt(`${d}s`, `${d}s`, f.year)).join("")}</optgroup>
        <optgroup label="Year">${years.map((y) => opt(y.year, `${y.year} (${y.count})`, f.year)).join("")}</optgroup>
      </select>
      <select data-f="rating">
        ${opt("", "Any rating", f.rating)}
        ${[9, 8, 7, 6, 5].map((r) => opt(r, `★ ${r}+`, f.rating)).join("")}
      </select>
      <select data-f="sort">
        ${opt("rating", "Top rated", f.sort)}
        ${opt("year", "Newest release", f.sort)}
        ${opt("added", "Recently added", f.sort)}
        ${opt("name", "A–Z", f.sort)}
      </select>
      ${hasFilter(f) || state.category ? `<button class="ghost" id="f-clear">Clear</button>` : ""}
    </div>`;
}

function bindFilterBar(kind) {
  content.querySelectorAll(".filters select").forEach((s) =>
    s.onchange = () => { state.filters[kind][s.dataset.f] = s.value; render(); });
  const clear = $("#f-clear");
  if (clear) clear.onclick = () => {
    Object.assign(state.filters[kind], { genre: "", year: "", rating: "" });
    state.category = "";
    render();
  };
}

async function renderGrid(kind) {
  const seq = ++state.seq;
  await renderCategories(kind);
  const filterable = kind !== "live";
  const bar = filterable ? await filterBarHTML(kind) : "";
  if (seq !== state.seq) return;

  const label = { live: "channels", vod: "movies", series: "shows" }[kind];
  if (!state.category && !hasFilter(state.filters[kind])) {
    content.innerHTML = bar + `<div class="empty">${filterable
      ? `Pick a category on the left, or choose a genre, year or rating above
         to search all ${label}.`
      : "Pick a category on the left."}</div>`;
    if (filterable) bindFilterBar(kind);
    return;
  }

  content.innerHTML = bar + `<div class="empty">Loading…</div>`;
  if (filterable) bindFilterBar(kind);
  const items = await api(browseURL(kind, 0));
  if (seq !== state.seq) return;

  const scope = state.category
    ? (state.cats[kind].find((c) => c.id === state.category)?.name || "")
    : `all ${label}`;
  content.innerHTML = bar + (items.length
    ? `<h2><span id="g-count">${items.length}${items.length === PAGE ? "+" : ""}</span> ${label}
         <span class="muted" dir="auto">· ${esc(scope)}</span></h2>
       <div class="grid" id="g-grid">${items.map(cardHTML).join("")}</div>
       ${items.length === PAGE
         ? `<div style="text-align:center;margin:20px 0"><button class="small" id="g-more">Load more</button></div>` : ""}`
    : `<div class="empty">Nothing matches these filters.</div>`);
  if (filterable) bindFilterBar(kind);
  bindCards(content);

  let offset = items.length;
  const more = $("#g-more");
  if (more) more.onclick = async () => {
    more.disabled = true;
    const next = await api(browseURL(kind, offset));
    if (seq !== state.seq) return;
    offset += next.length;
    // Bind in a scratch node so existing cards don't get a second listener.
    const tmp = document.createElement("div");
    tmp.innerHTML = next.map(cardHTML).join("");
    bindCards(tmp);
    $("#g-grid").append(...tmp.children);
    $("#g-count").textContent = offset + (next.length === PAGE ? "+" : "");
    if (next.length < PAGE) more.remove();
    else more.disabled = false;
  };
}

// Favorites split by kind, so channels, movies and shows never share a grid.
const FAV_SECTIONS = [["live", "Live TV"], ["vod", "Movies"], ["series", "Series"]];

function favSectionsHTML(favs) {
  return FAV_SECTIONS.map(([kind, title]) => {
    const items = favs.filter((f) => f.kind === kind);
    return items.length
      ? `<h2>★ ${title}</h2><div class="grid">${items.map(cardHTML).join("")}</div>`
      : "";
  }).join("");
}

async function renderFavorites() {
  sidebar.classList.add("hidden");
  const favs = await api("/api/favorites");
  content.innerHTML = favs.length
    ? favSectionsHTML(favs)
    : `<div class="empty">No favorites yet — tap the ★ on any poster.</div>`;
  bindCards(content);
}

async function renderMyList() {
  sidebar.classList.add("hidden");
  content.innerHTML = `<div class="empty">Loading your list…</div>`;
  const d = await api("/api/mylist");
  if (!d.enabled) {
    content.innerHTML = `<div class="empty">Your shared list isn't connected.<br><br>
      Add <code>WATCHLOG_URL</code> and <code>WATCHLOG_KEY</code> to <code>.env</code> and restart.</div>`;
    return;
  }
  const row = (it) => {
    const m = it.match;
    const sub = [it.year, it.type === "show" ? "Series" : "Movie",
      it.status === "watched" && it.watched_at ? "watched " + it.watched_at : ""].filter(Boolean).join(" · ");
    const art = it.poster || (m && img(m.icon)) || "";
    return `
      <div class="ml-item ${m ? "" : "missing"}" ${m ? `data-ml="${m.kind}:${m.id}"` : ""}
           title="${m ? "Open in your library" : "Not in your IPTV library"}">
        ${art ? `<img src="${esc(art)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">` : `<div class="ph"></div>`}
        <div class="t">
          <div class="name" dir="auto">${esc(it.title)}</div>
          <div class="muted sub">${esc(sub)}${m ? "" : " · not in library"}</div>
          ${it.reason && it.status !== "watched" ? `<div class="muted sub reason" dir="auto">${esc(it.reason)}</div>` : ""}
          ${it.note ? `<div class="muted sub" dir="auto">“${esc(it.note)}”</div>` : ""}
        </div>
        <div class="score">${it.rating ? it.rating : ""}${it.liked === 1 ? " 👍" : it.liked === -1 ? " 👎" : ""}</div>
      </div>`;
  };
  const section = (title, list, empty) => `
    <h2>${title} <span class="muted">${list.length || ""}</span></h2>
    ${list.length ? `<div class="ml-list">${list.map(row).join("")}</div>`
      : `<div class="muted" style="margin:6px 0 22px">${empty}</div>`}`;
  content.innerHTML = `
    ${d.error ? `<div class="muted" style="margin-bottom:10px">⚠ Showing the last copy: ${esc(d.error)}</div>` : ""}
    ${d.pending ? `<div class="muted" style="margin-bottom:10px">⏳ ${d.pending} change(s) waiting to sync</div>` : ""}
    ${section("💡 Suggestions", d.suggested, "Nothing suggested yet — ask Claude for ideas and they land here.")}
    ${section("📌 Watchlist", d.watchlist, "Tap 📌 Watchlist on any movie or show to save it for later.")}
    ${section("✓ Watched", d.watched, "Finished titles show up here automatically, with your rating.")}`;
  content.querySelectorAll("[data-ml]").forEach((el) => el.onclick = () => {
    const [kind, id] = el.dataset.ml.split(":");
    kind === "series" ? openSeries(+id) : openMovie(+id);
  });
}

async function renderDownloads() {
  sidebar.classList.add("hidden");
  const d = await api("/api/downloads");
  const note = d.paused_for_playback
    ? `<div class="muted" style="margin-bottom:12px">⏸ Downloads paused while the player is open
       (your subscription allows one connection at a time). They resume automatically.</div>` : "";
  content.innerHTML = `<h2>⬇ Offline <span class="muted">· ${fmtBytes(d.disk_bytes)} on disk</span></h2>${note}` +
    (d.items.length ? d.items.map((x) => {
      const pct = x.total_bytes ? (x.bytes_done / x.total_bytes) * 100 : 0;
      return `
      <div class="dl">
        <div class="info">
          <div class="name" dir="auto">${esc(x.title)}</div>
          <div class="bar"><i style="width:${pct}%"></i></div>
          <div class="muted" style="font-size:11.5px;margin-top:4px">
            ${fmtBytes(x.bytes_done)}${x.total_bytes ? " / " + fmtBytes(x.total_bytes) : ""}
            ${x.error ? " · " + esc(x.error) : ""}</div>
        </div>
        <span class="st ${x.status}">${x.status}</span>
        ${x.status === "done"
          ? `<button class="small" data-play-dl="${x.kind}:${x.item_id}">▶</button>` : ""}
        <button class="ghost" data-rm="${x.kind}:${x.item_id}">✕</button>
      </div>`;
    }).join("") : `<div class="empty">Nothing downloaded yet.<br><br>
      Open any movie or episode and choose <b>Save offline</b>.</div>`);

  content.querySelectorAll("[data-rm]").forEach((b) =>
    b.onclick = async () => {
      const [k, i] = b.dataset.rm.split(":");
      if (confirm("Remove this download and delete the file?")) {
        await api(`/api/download/${k}/${i}`, { method: "DELETE" });
        renderDownloads();
      }
    });
  content.querySelectorAll("[data-play-dl]").forEach((b) =>
    b.onclick = () => { const [k, i] = b.dataset.playDl.split(":"); play(k, +i); });
}

async function renderSearch() {
  sidebar.classList.add("hidden");
  const rows = await api(`/api/search?q=${encodeURIComponent(state.query)}`);
  const groups = { live: [], vod: [], series: [] };
  rows.forEach((r) => groups[r.kind]?.push(r));
  const titles = { series: "Series", vod: "Movies", live: "Live channels" };
  let html = "";
  for (const k of ["series", "vod", "live"]) {
    if (groups[k].length)
      html += `<h2>${titles[k]} <span class="muted">· ${groups[k].length}</span></h2>
               <div class="grid" style="margin-bottom:24px">${groups[k].map(cardHTML).join("")}</div>`;
  }
  content.innerHTML = html || `<div class="empty">No matches for “${esc(state.query)}”.</div>`;
  bindCards(content);
}

async function render() {
  try {
    if (state.query.length >= 2) return renderSearch();
    if (state.tab === "home") return renderHome();
    if (state.tab === "favorites") return renderFavorites();
    if (state.tab === "mylist") return renderMyList();
    if (state.tab === "downloads") return renderDownloads();
    return renderGrid(state.tab);
  } catch (e) {
    content.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------ chrome */

$("#tabs").querySelectorAll("button").forEach((b) =>
  b.onclick = () => {
    $("#tabs .active")?.classList.remove("active");
    b.classList.add("active");
    state.tab = b.dataset.tab;
    state.category = "";
    state.query = "";
    $("#search").value = "";
    render();
  });

let searchTimer;
$("#search").addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(render, 180);
});

$("#btn-stop").onclick = () => api("/api/stop", { method: "POST" });

$("#btn-sync").onclick = async () => {
  await api("/api/sync", { method: "POST" });
  toast("Refreshing catalog in the background…");
};

/* Poll for player + sync state so the chrome stays honest. */
async function poll() {
  try {
    const s = await api("/api/status");
    const p = s.player;
    $("#playerbar").classList.toggle("hidden", !p);
    if (p) {
      $("#playing-title").textContent = p.title;
      $("#playing-pos").textContent =
        p.duration ? `${fmtTime(p.position)} / ${fmtTime(p.duration)}` : "live";
    }

    const sy = s.sync;
    if (state.detailsRunning && !sy.details.running) delete state.facets.vod;
    state.detailsRunning = sy.details.running;
    $("#sync-status").textContent = sy.running
      ? `Syncing: ${sy.stage} ${sy.progress}%`
      : sy.last_error
        ? `Sync failed: ${sy.last_error}`
        : `${sy.counts.live} channels · ${sy.counts.vod} movies · ${sy.counts.series} shows`
          + (sy.last_sync ? ` · updated ${new Date(sy.last_sync * 1000).toLocaleString()}` : "")
          + (sy.details.running
            ? ` · fetching movie genres ${sy.details.done}/${sy.details.total}` : "");
    $("#disk").textContent = s.downloads.disk_bytes
      ? `${fmtBytes(s.downloads.disk_bytes)} offline` : "";

    if (!s.mpv) $("#sync-status").textContent = "⚠ mpv not found in bin/ — playback disabled";

    if (state.tab === "downloads" && !$("#modal").classList.contains("hidden") === false) {
      // keep the download list live while it's on screen
      if (document.querySelector(".dl")) renderDownloads();
    }
  } catch { /* server restarting; ignore */ }
}

setInterval(poll, 2500);
poll();
render();
