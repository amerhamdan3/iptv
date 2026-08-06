/* IPTV browser - all state lives on the server, this is just the shell. */

const $ = (s) => document.querySelector(s);
const content = $("#content");
const sidebar = $("#sidebar");

const state = { tab: "home", category: "", query: "", cats: {} };

/* ------------------------------------------------------------ helpers */

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
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
  return `
  <div class="card ${kind === "live" ? "live" : ""}" data-kind="${kind}" data-id="${it.id}">
    <img class="thumb" loading="lazy" src="${img(it.icon)}" alt=""
         onerror="this.style.visibility='hidden'">
    <button class="star ${it.favorite ? "on" : ""}" data-fav="1">★</button>
    <div class="label" dir="auto">${esc(it.name)}</div>
  </div>`;
}

function bindCards(root) {
  root.querySelectorAll(".card").forEach((el) => {
    const kind = el.dataset.kind, id = +el.dataset.id;
    el.addEventListener("click", (ev) => {
      if (ev.target.dataset.fav) return toggleFav(kind, id, ev.target);
      if (kind === "series") openSeries(id);
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
        <div class="plot" dir="auto">${esc(s.plot || "")}</div>
        <div style="margin-top:10px">
          <button class="ghost" id="s-fav">${s.favorite ? "★ In favorites" : "☆ Add to favorites"}</button>
        </div>
      </div>
    </div>
    ${banner}
    ${body || '<div class="empty">No episodes listed.</div>'}
  `);

  $("#s-fav").onclick = async (e) => {
    const r = await api("/api/favorite", { method: "POST", body: { kind: "series", item_id: id } });
    e.target.textContent = r.favorite ? "★ In favorites" : "☆ Add to favorites";
  };
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

  if (favs.length) {
    html += `<h2>★ Favorites</h2><div class="grid">${favs.map(cardHTML).join("")}</div>`;
  }

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
  $("#cats").innerHTML =
    `<div class="cat ${state.category === "" ? "active" : ""}" data-c="">All
       <span class="n">${cats.reduce((a, c) => a + c.count, 0)}</span></div>` +
    cats.map((c) => `<div class="cat ${state.category === c.id ? "active" : ""}"
        data-c="${esc(c.id)}"><span dir="auto">${esc(c.name)}</span>
        <span class="n">${c.count}</span></div>`).join("");
  $("#cats").querySelectorAll(".cat").forEach((el) =>
    el.onclick = () => { state.category = el.dataset.c; render(); });
}

async function renderGrid(kind) {
  await renderCategories(kind);
  content.innerHTML = `<div class="empty">Loading…</div>`;
  const items = await api(
    `/api/browse?kind=${kind}&category=${encodeURIComponent(state.category)}&limit=300`);
  const label = { live: "channels", vod: "movies", series: "shows" }[kind];
  content.innerHTML = items.length
    ? `<h2>${items.length >= 300 ? "First 300" : items.length} ${label}
         <span class="muted">${state.category ? "" : "· pick a category on the left to narrow down"}</span></h2>
       <div class="grid">${items.map(cardHTML).join("")}</div>`
    : `<div class="empty">Nothing here.</div>`;
  bindCards(content);
}

async function renderFavorites() {
  sidebar.classList.add("hidden");
  const favs = await api("/api/favorites");
  content.innerHTML = favs.length
    ? `<h2>★ Favorites</h2><div class="grid">${favs.map(cardHTML).join("")}</div>`
    : `<div class="empty">No favorites yet — tap the ★ on any poster.</div>`;
  bindCards(content);
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
    $("#sync-status").textContent = sy.running
      ? `Syncing: ${sy.stage} ${sy.progress}%`
      : sy.last_error
        ? `Sync failed: ${sy.last_error}`
        : `${sy.counts.live} channels · ${sy.counts.vod} movies · ${sy.counts.series} shows`
          + (sy.last_sync ? ` · updated ${new Date(sy.last_sync * 1000).toLocaleString()}` : "");
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
