/* AudioFP web UI. Plain JS, no build step.
 *
 * Sections, in file order: prefs, api, ui helpers, theme, router, system status,
 * search, library, activity, settings, boot.
 */
(() => {
  "use strict";

  const BASE = "/api/v1";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ------------------------------------------------------------------ prefs */
  const prefs = {
    get(key, fallback = null) {
      try { const v = localStorage.getItem("audiofp:" + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem("audiofp:" + key, JSON.stringify(value)); } catch { /* private mode */ } },
    del(key) { try { localStorage.removeItem("audiofp:" + key); } catch { /* ignore */ } },
  };

  /* ------------------------------------------------------------------ api */
  class ApiError extends Error {
    constructor(message, { status = 0, code = "error", details = null, requestId = null } = {}) {
      super(message); this.status = status; this.code = code; this.details = details; this.requestId = requestId;
    }
  }

  const api = {
    key() { return prefs.get("apiKey", "") || ""; },
    headers(extra = {}) { const h = { ...extra }; const k = this.key(); if (k) h["X-API-Key"] = k; return h; },
    async request(path, { method = "GET", body = null, json = null, headers = {}, signal } = {}) {
      const opts = { method, headers: this.headers(headers), signal };
      if (json !== null) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
      else if (body) opts.body = body;
      let res;
      try { res = await fetch(BASE + path, opts); }
      catch (e) { throw new ApiError("Cannot reach the server. Is AudioFP running?", { code: "network" }); }
      let data = null;
      const text = await res.text();
      try { data = text ? JSON.parse(text) : null; } catch { data = null; }
      if (!res.ok) {
        const message = (data && data.error) || `HTTP ${res.status}`;
        const err = new ApiError(message, { status: res.status, code: data && data.code, details: data && data.details, requestId: data && data.request_id });
        if (res.status === 401) auth.prompt(err);
        throw err;
      }
      return data;
    },
    get(path, opts) { return this.request(path, { ...opts, method: "GET" }); },
    post(path, opts) { return this.request(path, { ...opts, method: "POST" }); },
    put(path, json) { return this.request(path, { method: "PUT", json }); },
    patch(path, json) { return this.request(path, { method: "PATCH", json }); },
    del(path) { return this.request(path, { method: "DELETE" }); },
    // an <audio> element can't send headers, so with a key set we fetch a short-lived token scoped to this track
    async streamUrl(trackId) {
      const plain = `${BASE}/tracks/${encodeURIComponent(trackId)}/audio`;
      if (!this.key()) return plain;
      const res = await this.get(`/tracks/${encodeURIComponent(trackId)}/stream-token`);
      return res && res.url ? res.url : plain;
    },
    upload(path, formData, onProgress) {
      // XHR, because fetch has no upload progress events
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", BASE + path);
        const k = this.key(); if (k) xhr.setRequestHeader("X-API-Key", k);
        xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
        xhr.onload = () => {
          let data = null; try { data = JSON.parse(xhr.responseText); } catch { /* not json */ }
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else {
            const err = new ApiError((data && data.error) || `HTTP ${xhr.status}`, { status: xhr.status, code: data && data.code, details: data && data.details, requestId: data && data.request_id });
            if (xhr.status === 401) auth.prompt(err);
            reject(err);
          }
        };
        xhr.onerror = () => reject(new ApiError("Upload failed: network error", { code: "network" }));
        xhr.send(formData);
      });
    },
  };

  /* ------------------------------------------------------------------ ui helpers */
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const fmt = {
    size(b) { if (b == null) return "-"; if (b < 1024) return b + " B"; if (b < 1048576) return (b / 1024).toFixed(1) + " KB"; if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB"; return (b / 1073741824).toFixed(2) + " GB"; },
    dur(s) { if (s == null || isNaN(s)) return "-"; s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60; return h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`; },
    sec(s) { if (s == null) return "-"; return s < 10 ? s.toFixed(2) + "s" : this.dur(s); },
    num(n) { if (n == null) return "-"; if (n >= 1e6) return (n / 1e6).toFixed(1) + "M"; if (n >= 1e3) return (n / 1e3).toFixed(1) + "K"; return String(n); },
    pct(v) { return Math.round((v || 0) * 100) + "%"; },
    ago(ts) { if (!ts) return "-"; const d = Date.now() / 1000 - ts; if (d < 60) return "just now"; if (d < 3600) return Math.floor(d / 60) + " min ago"; if (d < 86400) return Math.floor(d / 3600) + " h ago"; return new Date(ts * 1000).toLocaleString(); },
    date(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "-"; },
    ext(name) { const m = /\.([a-z0-9]+)$/i.exec(name || ""); return m ? m[1].toUpperCase() : ""; },
  };
  const ICON = {
    play: '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
    stop: '<svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>',
    trash: '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
    sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
    moon: '<svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  };

  async function copyText(text, label = "Copied") {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); toast(label, "ok"); return; }
      const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove();
      toast(ok ? label : "Copy failed, select the text by hand", ok ? "ok" : "warn");
    } catch { toast("Copy failed, select the text by hand", "warn"); }
  }

  function toast(msg, type = "info", { detail = null, duration = 5000 } = {}) {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.innerHTML = `<div>${esc(msg)}${detail ? `<small>${esc(detail)}</small>` : ""}</div><button aria-label="Dismiss">×</button>`;
    el.querySelector("button").onclick = () => el.remove();
    $("#toasts").appendChild(el);
    if (duration) setTimeout(() => el.remove(), duration);
  }
  const showError = (prefix, err) => toast(`${prefix}: ${err.message}`, "err", { detail: err.requestId ? `request ${err.requestId}` : null, duration: 8000 });

  const modal = {
    open({ title, body = "", actions = [], input = null }) {
      return new Promise((resolve) => {
        const root = $("#modal"), box = root.querySelector(".modal");
        $("#modal-title").textContent = title;
        $("#modal-body").innerHTML = (typeof body === "string" ? `<p>${body}</p>` : "") + (input ? `<input class="text-input" id="modal-input" type="${input.type || "text"}" placeholder="${esc(input.placeholder || "")}" value="${esc(input.value || "")}" />` : "");
        if (typeof body !== "string") { $("#modal-body").innerHTML = ""; $("#modal-body").appendChild(body); }
        const act = $("#modal-actions"); act.innerHTML = "";
        const previouslyFocused = document.activeElement;
        const finish = (value) => {
          root.classList.add("hidden"); document.removeEventListener("keydown", onKey, true); resolve(value);
          if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
        };
        const onKey = (e) => {
          if (e.key === "Escape") { e.stopPropagation(); finish(null); }
          else if (e.key === "Enter" && input && e.target && e.target.id === "modal-input") { e.stopPropagation(); finish($("#modal-input").value); }
          else if (e.key === "Tab") { // keep focus inside the dialog
            const focusable = $$("button, input, [tabindex]:not([tabindex='-1'])", box).filter((el) => !el.disabled);
            if (!focusable.length) return;
            const first = focusable[0], last = focusable[focusable.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        };
        actions.forEach((a) => {
          const b = document.createElement("button");
          b.className = `btn ${a.kind || "btn-ghost"}`; b.textContent = a.label;
          b.onclick = () => finish(a.value !== undefined ? a.value : (input ? $("#modal-input").value : true));
          act.appendChild(b);
        });
        root.classList.remove("hidden");
        document.addEventListener("keydown", onKey, true);
        setTimeout(() => (input ? $("#modal-input") : box).focus(), 20);
        root.onclick = (e) => { if (e.target === root) finish(null); };
      });
    },
    confirm(title, body, { danger = false, ok = "Confirm" } = {}) {
      return this.open({ title, body, actions: [{ label: "Cancel", value: false }, { label: ok, value: true, kind: danger ? "btn-danger" : "btn-primary" }] });
    },
    prompt(title, body, input) {
      return this.open({ title, body, input, actions: [{ label: "Cancel", value: null }, { label: "Save", kind: "btn-primary" }] });
    },
  };

  /* ------------------------------------------------------------------ auth */
  const auth = {
    prompting: false,
    async prompt(err) {
      if (this.prompting) return;
      this.prompting = true;
      const value = await modal.prompt("API key required", (err && err.message) || "This server requires an API key.", { type: "password", placeholder: "Paste the value of AUDIOFP_API_KEY" });
      this.prompting = false;
      if (value) { prefs.set("apiKey", value.trim()); toast("API key saved, retrying", "ok"); location.reload(); }
    },
  };

  /* ------------------------------------------------------------------ theme */
  const theme = {
    apply() {
      const pref = prefs.get("theme", "dark");
      const dark = pref === "system" ? window.matchMedia("(prefers-color-scheme: dark)").matches : pref !== "light";
      document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
      $$("[data-action='toggle-theme']").forEach((b) => (b.innerHTML = dark ? ICON.sun : ICON.moon));
      $$("[data-theme-set]").forEach((b) => { const on = b.dataset.themeSet === pref; b.classList.toggle("btn-primary", on); b.classList.toggle("btn-ghost", !on); b.setAttribute("aria-pressed", String(on)); });
    },
    set(pref) { prefs.set("theme", pref); this.apply(); },
    toggle() { this.set(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark"); },
  };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => theme.apply());

  /* ------------------------------------------------------------------ router */
  const VIEWS = ["search", "library", "activity", "settings"];
  const router = {
    current: null,
    go(view) { if (!VIEWS.includes(view)) view = "search"; if (location.hash !== "#" + view) location.hash = view; else this.render(); },
    render() {
      const view = (location.hash || "#search").slice(1).split("?")[0];
      if (!VIEWS.includes(view)) { if (this.current) return; }  // e.g. "#main" from the skip link, stay on the current view
      const target = VIEWS.includes(view) ? view : "search";
      $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + target));
      $$("[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === target));
      const changed = this.current !== target;
      this.current = target;
      if (target === "library") library.load({ silent: !changed });
      if (target === "activity") activity.load();
      if (target === "settings") settings.load();
      if (changed) $("#main").focus({ preventScroll: true });
    },
  };
  window.addEventListener("hashchange", () => router.render());

  /* ------------------------------------------------------------------ system status */
  const system = {
    info: null, health: null,
    async refresh({ silent = true } = {}) {
      try {
        this.health = await api.get("/health");
        if (!this.info) this.info = await api.get("/info");
        const stats = await api.get("/stats");
        this.renderStats(stats);
        const ok = this.health.status === "ok";
        this.pill(ok ? "" : "warn", `${fmt.num(stats.total_tracks)} track${stats.total_tracks === 1 ? "" : "s"}${this.health.ffmpeg && !this.health.ffmpeg.available ? ", no ffmpeg" : ""}`);
        const active = (this.health.jobs && this.health.jobs.active) || 0;
        $("#nav-active-jobs").textContent = active; $("#nav-active-jobs").classList.toggle("hidden", !active);
        this.applyFeatures();
      } catch (e) {
        this.pill("error", e.status === 401 ? "API key needed" : "Offline");
        if (!silent) showError("Server", e);
      }
    },
    pill(cls, text) { ["#status-pill", "#status-pill-mobile"].forEach((s) => { const el = $(s); el.className = "status-pill " + cls; el.querySelector(".status-text").textContent = text; }); },
    renderStats(s) {
      $("#st-tracks").textContent = fmt.num(s.total_tracks);
      $("#st-hashes").textContent = fmt.num(s.total_hashes);
      $("#st-duration").textContent = fmt.dur(s.total_duration_sec);
      $("#st-storage").textContent = (s.storage_type || "-") + (s.db_size_bytes ? `, ${fmt.size(s.db_size_bytes)}` : "");
    },
    applyFeatures() {
      const info = this.info; if (!info) return;
      const fmts = info.formats;
      const popular = ["mp3", "wav", "flac", "ogg", "m4a", "mp4", "mkv", "mov", "webm"];
      const supported = new Set([...fmts.native_audio, ...fmts.ffmpeg_audio, ...fmts.video]);
      const needsFfmpeg = new Set([...fmts.ffmpeg_audio, ...fmts.video]);
      $("#search-formats").innerHTML = popular.filter((f) => supported.has(f)).map((f) => `<span class="badge ${needsFfmpeg.has(f) && !info.ffmpeg.available ? "warn" : ""}" title="${needsFfmpeg.has(f) ? "decoded with ffmpeg" : "decoded natively"}">${esc(f.toUpperCase())}</span>`).join("")
        + `<span class="badge" title="${esc([...supported].sort().join(", "))}">+${supported.size - popular.length} more</span>`
        + (info.ffmpeg.available ? "" : `<span class="badge warn" title="Install ffmpeg on the server to decode video, M4A/AAC and WMA">ffmpeg not installed</span>`);
      $("#upload-limit").textContent = `up to ${info.limits.max_upload_mb} MB per file`;
      const folder = $("#folder-panel");
      const allowed = info.features.directory_indexing;
      $("#dir-btn").disabled = !allowed; $("#dir-input").disabled = !allowed;
      $("#folder-help").textContent = allowed
        ? (info.features.index_roots.length ? `Allowed roots: ${info.features.index_roots.join(", ")}` : "Index every supported file below a directory on the server.")
        : "Directory indexing is disabled on this server (set AUDIOFP_INDEX_ROOTS or AUDIOFP_ALLOW_DIRECTORY_INDEXING). Use uploads instead.";
      folder.classList.toggle("disabled", !allowed);
      if (!this.defaultsApplied) { search.applyDefaults(info.defaults); this.defaultsApplied = true; }
    },
  };

  /* ------------------------------------------------------------------ search */
  const search = {
    file: null, lastResult: null, recent: [], recorder: null, recChunks: [], recTimer: null, player: null,
    init() {
      dropZone("#search-zone", "#search-input", (files) => this.setFile(files[0]));
      $("#search-clear").onclick = () => this.setFile(null);
      $("#search-btn").onclick = () => this.run();
      $("#adv-toggle").onclick = () => { const p = $("#adv-panel"); const open = p.classList.toggle("hidden"); $("#adv-toggle").setAttribute("aria-expanded", String(!open)); };
      $("#adv-reset").onclick = () => { this.applyDefaults(system.info && system.info.defaults, true); toast("Thresholds reset to server defaults", "ok"); };
      $$("input[name='mode']").forEach((r) => (r.onchange = () => this.modeChanged()));
      $("#record-btn").onclick = (e) => { e.stopPropagation(); if (this.recorder) this.stopRecording(true); else this.startRecording(); };
      $("#rec-stop").onclick = () => this.stopRecording(true);
      $("#rec-cancel").onclick = () => this.stopRecording(false);
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || !this.file || router.current !== "search") return;
        const el = e.target instanceof Element ? e.target : null;
        if (el && el.closest("input, select, textarea, button, a, [role='button'], .modal, .drawer")) return;
        this.run();
      });
      ["#opt-topk", "#opt-minconf", "#opt-minaligned", "#opt-minratio"].forEach((s) => { $(s).onchange = () => this.saveOptions(); $(s).oninput = () => this.saveOptions(); });
      const saved = prefs.get("searchOptions");
      if (saved) { if (saved.mode) $(`input[name='mode'][value='${saved.mode}']`).checked = true; if (saved.top_k) $("#opt-topk").value = saved.top_k; }
      this.modeChanged();
    },
    modeChanged() {
      const mode = $("input[name='mode']:checked").value;
      $("#mode-hint").textContent = mode === "identify"
        ? "The best match per track, for when you want to know what a clip is. Works with 3-second snippets, even noisy ones."
        : "Every place the query and a track overlap. Index short patterns (a jingle, a disclaimer) and search with a long recording, or the other way round.";
      this.saveOptions();
    },
    saveOptions() {
      prefs.set("searchOptions", { mode: $("input[name='mode']:checked").value, top_k: $("#opt-topk").value, min_confidence: $("#opt-minconf").value, min_aligned_hashes: $("#opt-minaligned").value, min_peak_ratio: $("#opt-minratio").value });
    },
    applyDefaults(defaults, force = false) {
      if (!defaults) return;
      const saved = prefs.get("searchOptions") || {};
      const set = (sel, key) => { const el = $(sel); if (force || !saved[key]) el.value = defaults[key]; else el.value = saved[key]; };
      set("#opt-minconf", "min_confidence"); set("#opt-minaligned", "min_aligned_hashes"); set("#opt-minratio", "min_peak_ratio");
      if (force) { $("#opt-topk").value = String(defaults.top_k); $(`input[name='mode'][value='${defaults.mode}']`).checked = true; this.modeChanged(); }
    },
    setFile(file) {
      this.file = file;
      $("#search-bar").classList.toggle("hidden", !file);
      $("#search-results").innerHTML = "";
      if (file) {
        $("#search-fname").textContent = file.name;
        $("#search-fmeta").textContent = `${fmt.size(file.size)}${file.type ? ", " + file.type : ""}`;
        $("#search-btn").focus();
      }
    },
    async run() {
      const file = this.file;
      if (!file || this.searching) return;
      this.searching = true;
      if (this.player) { this.player.pause(); this.player = null; }
      const btn = $("#search-btn"); const original = btn.innerHTML;
      btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Searching';
      $("#search-results").innerHTML = '<div class="skeleton" style="height:120px"></div>';
      const fd = new FormData();
      fd.append("audio", file, file.name);
      fd.append("mode", $("input[name='mode']:checked").value);
      fd.append("top_k", $("#opt-topk").value);
      const optional = { min_confidence: "#opt-minconf", min_aligned_hashes: "#opt-minaligned", min_peak_ratio: "#opt-minratio" };
      for (const [k, sel] of Object.entries(optional)) { const v = $(sel).value; if (v !== "") fd.append(k, v); }
      try {
        const data = await api.post("/search", { body: fd });
        this.lastResult = data;
        if (this.file === file) { this.render(data); this.remember(data, file); }
      } catch (e) {
        $("#search-results").innerHTML = "";
        this.renderError(e);
      } finally { btn.disabled = false; btn.innerHTML = original; this.searching = false; }
    },
    renderError(e) {
      let hint = "";
      if (e.code === "ffmpeg_not_found") hint = "Install ffmpeg on the server to search with video or M4A/AAC files, or convert the clip to WAV/MP3/FLAC first.";
      else if (e.code === "audio_decode_error") hint = "The file could not be decoded. Try exporting it again, and check that it isn't empty.";
      else if (e.code === "payload_too_large") hint = "Trim the clip. A few seconds is enough to identify it.";
      $("#search-results").innerHTML = `<div class="notice err"><b>${esc(e.message)}</b>${hint ? `<br>${esc(hint)}` : ""}${e.requestId ? `<br><small class="mono">request ${esc(e.requestId)}</small>` : ""}</div>`;
    },
    render(data) {
      const el = $("#search-results");
      const q = data.query;
      const meta = `<div class="results-meta">
          <span class="badge">${esc(fmt.sec(q.duration_sec))} clip</span>
          <span class="badge">${fmt.num(q.num_hashes)} hashes</span>
          <span class="badge">${Math.round(data.processing_time_ms)} ms</span>
          <span class="badge">${esc(data.mode)}</span>
          <button class="btn btn-ghost btn-sm" id="copy-json" type="button">Copy JSON</button>
        </div>`;
      let html = `<div class="results-header"><h2>${data.found ? `${data.matches.length} match${data.matches.length === 1 ? "" : "es"}` : "No match"}</h2>${meta}</div>`;
      if (q.truncated) html += `<div class="notice warn">Only the first ${fmt.dur(q.duration_sec)} of the file were analysed (server limit AUDIOFP_MAX_QUERY_SECONDS).</div>`;
      if (!data.found) {
        html += `<div class="no-results"><div class="big">${ICON.search}</div><h3>Nothing above the thresholds</h3>
          <p>Nothing in the library lined up with this clip well enough (min confidence ${data.thresholds.min_confidence}, min aligned ${data.thresholds.min_aligned_hashes}, min peak ratio ${data.thresholds.min_peak_ratio}).</p>
          <ul><li>Try a longer or cleaner clip. Something like 5 to 10 seconds helps with noisy audio.</li><li>Check the track is actually in the library.</li><li>Lower the thresholds (the Thresholds button above) if you expect weak matches.</li></ul></div>`;
      } else {
        data.matches.forEach((m, i) => (html += this.matchCard(m, i === 0, data)));
      }
      el.innerHTML = html;
      $("#copy-json").onclick = () => copyText(JSON.stringify(data, null, 2), "Result JSON copied");
      $$(".tl-seg, .occ-chip", el).forEach((seg) => {
        const go = () => this.playAt(seg.dataset.track, parseFloat(seg.dataset.seek), seg.closest(".match-card"));
        seg.onclick = go;
        seg.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); go(); } };
      });
      $$("[data-play]", el).forEach((b) => (b.onclick = () => this.playAt(b.dataset.play, parseFloat(b.dataset.seek || "0"), b.closest(".match-card"))));
      $$("[data-open-track]", el).forEach((b) => (b.onclick = () => library.openDrawer(b.dataset.openTrack)));
    },
    matchCard(m, best, data) {
      const name = m.display_name || m.title || m.filename || "Untitled";
      const quality = m.quality || "weak";
      const qualityHelp = { strong: "High confidence and a very sharp alignment spike.", likely: "Clear alignment. Check it by listening if it matters.", weak: "Above the thresholds, but not by much. Could be a partial or noisy match." }[quality];
      const negative = m.offset_sec < 0;
      const qdur = data.query.duration_sec;
      // up to two timelines: where the query audio sits in the track (positive offsets), and where track audio sits in the query
      const trackSegs = m.occurrences.map((o) => ({ start: o.track_start_sec, end: o.track_end_sec, q: o.quality, seek: o.track_start_sec, label: `${fmt.sec(o.track_start_sec)} to ${fmt.sec(o.track_end_sec)}` }));
      const querySegs = m.occurrences.map((o) => ({ start: o.query_start_sec, end: o.query_end_sec, q: o.quality, seek: o.track_start_sec, label: `${fmt.sec(o.query_start_sec)} to ${fmt.sec(o.query_end_sec)}` }));
      const tl = (title, segs, total, subtitle) => `<div class="timeline"><div class="tl-label"><b>${esc(title)}</b><span>${esc(subtitle)}</span></div>
        <div class="tl-bar"><div class="tl-ticks"></div>${segs.map((s) => `<div class="tl-seg ${s.q}" data-track="${esc(m.track_id)}" data-seek="${s.seek}" style="left:${(100 * s.start / Math.max(total, 0.001)).toFixed(2)}%;width:${Math.max(0.6, 100 * (s.end - s.start) / Math.max(total, 0.001)).toFixed(2)}%" title="${esc(s.label)}, click to play from here" role="button" tabindex="0"></div>`).join("")}</div></div>`;
      let timelines = "";
      if (m.duration) timelines += tl("In the track", trackSegs, m.duration, `${name}, ${fmt.dur(m.duration)}`);
      if (negative || data.mode === "occurrences") timelines += tl("In your query", querySegs, qdur, `${data.query.filename || "query"}, ${fmt.dur(qdur)}`);
      const occChips = m.occurrences.length > 1 ? `<div class="occ-list">${m.occurrences.map((o) => `<span class="occ-chip" role="button" tabindex="0" data-track="${esc(m.track_id)}" data-seek="${o.track_start_sec}" title="click to play from here"><span class="badge ${o.quality}">${esc(o.quality)}</span>${o.offset_sec < 0 ? `in query at ${fmt.sec(o.query_offset_sec)}` : `in track at ${fmt.sec(o.track_start_sec)}`}, ${o.aligned_hashes} aligned</span>`).join("")}</div>` : "";
      const where = negative
        ? `Track audio found in your query at <b>${fmt.sec(m.query_offset_sec)}</b>`
        : `Clip starts at <b>${fmt.sec(m.track_offset_sec)}</b> in the track`;
      return `<article class="match-card ${best ? "best" : ""}">
        <div class="match-main">
          <div class="match-title">${best ? `<span class="badge tag">★ best</span>` : ""}${esc(name)}<span class="badge ${quality}" title="${esc(qualityHelp)}">${esc(quality)}</span></div>
          ${m.artist ? `<div class="match-artist">${esc(m.artist)}</div>` : ""}
          <div class="match-explain">${where}, <b>${m.aligned_hashes}</b> aligned hashes, peak ratio <b>${m.peak_ratio}</b>${m.occurrences.length > 1 ? `, <b>${m.occurrences.length}</b> occurrences` : ""}</div>
          ${m.tags && m.tags.length ? `<div class="match-tags">${m.tags.map((t) => `<span class="badge tag">${esc(t)}</span>`).join("")}</div>` : ""}
        </div>
        <div class="match-score"><div class="score-big">${fmt.pct(m.confidence)}</div><div class="score-label">confidence</div><div class="match-explain">${esc(fmt.ext(m.filename))}${m.duration ? ", " + fmt.dur(m.duration) : ""}</div></div>
        ${timelines}${occChips}
        <div class="match-footer">
          <button class="play-btn" data-play="${esc(m.track_id)}" data-seek="${negative ? m.track_start_sec : m.track_offset_sec}" title="Play from the matched position">${ICON.play}</button>
          <span class="muted">Play from match</span>
          <span class="spacer"></span>
          <button class="btn btn-ghost btn-sm" data-open-track="${esc(m.track_id)}" type="button">Details</button>
        </div>
      </article>`;
    },
    async playAt(trackId, seek, card) {
      if (!card) return;
      let holder = card.querySelector(".inline-player");
      if (!holder) { holder = document.createElement("div"); holder.className = "inline-player"; card.appendChild(holder); }
      let audio = holder.querySelector("audio");
      if (!audio) {
        audio = document.createElement("audio"); audio.controls = true; audio.preload = "metadata";
        try { audio.src = await api.streamUrl(trackId); } catch (e) { return showError("Cannot play", e); }
        audio.onerror = () => toast("Could not play this track. The original file may have been removed from disk.", "err");
        holder.appendChild(audio);
      }
      if (this.player && this.player !== audio) this.player.pause();
      this.player = audio;
      const start = prefs.get("autoplayAtMatch", true) ? Math.max(0, seek || 0) : 0;
      const go = () => { try { audio.currentTime = start; } catch { /* not seekable yet */ } audio.play().catch(() => {}); };
      if (audio.readyState >= 1) go(); else audio.addEventListener("loadedmetadata", go, { once: true });
    },
    remember(data, file) {
      this.recent.unshift({ name: file.name, at: Date.now(), found: data.found, top: data.found ? (data.matches[0].display_name || data.matches[0].filename) : null, conf: data.found ? data.matches[0].confidence : null, mode: data.mode });
      this.recent = this.recent.slice(0, 8);
      $("#recent-searches").classList.remove("hidden");
      $("#recent-list").innerHTML = this.recent.map((r) => `<div class="recent-item"><span class="name" title="${esc(r.name)}">${esc(r.name)}</span><span class="badge">${esc(r.mode)}</span>${r.found ? `<span class="badge ok">${esc(r.top)}, ${fmt.pct(r.conf)}</span>` : `<span class="badge">no match</span>`}<span class="muted">${new Date(r.at).toLocaleTimeString()}</span></div>`).join("");
    },
    /* --- microphone recording --- */
    async startRecording() {
      if (this.recorder) return;
      if (!window.isSecureContext) return toast("Microphone recording needs a secure context. Open the app over https:// or at http://localhost.", "warn", { duration: 8000 });
      if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Recording is not supported in this browser.", "warn");
      const candidates = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
      const mime = candidates.find((c) => MediaRecorder.isTypeSupported(c));
      if (!mime) return toast("No supported recording format in this browser.", "warn");
      const needsFfmpeg = !mime.startsWith("audio/ogg");
      if (needsFfmpeg && system.health && system.health.ffmpeg && !system.health.ffmpeg.available) {
        return toast("This browser records WebM or MP4. The server needs ffmpeg to decode those and it isn't installed. Use Firefox, which records OGG, or upload a file instead.", "warn", { duration: 9000 });
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this.recorder = new MediaRecorder(stream, { mimeType: mime });
        this.recChunks = [];
        this.recorder.ondataavailable = (e) => e.data.size && this.recChunks.push(e.data);
        this.recorder.onstop = () => stream.getTracks().forEach((t) => t.stop());
        this.recorder.start(250);
        const started = Date.now();
        $("#recorder").classList.remove("hidden");
        $("#record-btn").querySelector("span").textContent = "Stop";
        this.recTimer = setInterval(() => { const s = Math.floor((Date.now() - started) / 1000); $("#rec-time").textContent = fmt.dur(s); if (s >= 30) this.stopRecording(true); }, 250);
      } catch (e) { toast("Microphone access was denied or unavailable.", "err"); }
    },
    stopRecording(use) {
      clearInterval(this.recTimer);
      $("#recorder").classList.add("hidden");
      $("#record-btn").querySelector("span").textContent = "Record";
      const rec = this.recorder; if (!rec) return;
      rec.onstop = () => {
        rec.stream.getTracks().forEach((t) => t.stop());
        if (!use) return;
        const type = rec.mimeType || "audio/webm";
        const ext = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "m4a" : "webm";
        const blob = new Blob(this.recChunks, { type });
        if (blob.size < 2000) return toast("Recording was too short.", "warn");
        this.setFile(new File([blob], `recording-${new Date().toISOString().replace(/[:.]/g, "-")}.${ext}`, { type }));
        toast("Recording ready, press Search", "ok");
      };
      if (rec.state !== "inactive") rec.stop();
      this.recorder = null;
    },
  };

  function dropZone(zoneSel, inputSel, onFiles) {
    const zone = $(zoneSel), input = $(inputSel);
    const open = () => input.click();
    zone.addEventListener("click", (e) => { if (!e.target.closest("button")) open(); });
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("hover"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("hover"));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("hover"); const files = [...e.dataTransfer.files]; if (files.length) onFiles(files); });
    input.addEventListener("change", () => { if (input.files.length) { onFiles([...input.files]); input.value = ""; } });
  }

  /* ------------------------------------------------------------------ library */
  const library = {
    page: 1, total: 0, pages: 1, items: [], selecting: false, selected: new Set(), queue: [], active: 0, player: null, searchTimer: null,
    init() {
      dropZone("#lib-zone", "#lib-input", (files) => this.enqueue(files));
      $("#dir-form").onsubmit = (e) => { e.preventDefault(); this.indexDirectory(); };
      $("#lib-search").oninput = () => { clearTimeout(this.searchTimer); this.searchTimer = setTimeout(() => { this.page = 1; this.load(); }, 300); };
      ["#lib-sort", "#lib-type", "#lib-perpage"].forEach((s) => ($(s).onchange = () => { this.page = 1; this.savePrefs(); this.load(); }));
      $("#lib-refresh").onclick = () => this.load();
      $("#lib-select").onclick = () => this.toggleSelect();
      $("#bulk-cancel").onclick = () => this.toggleSelect(false);
      $("#bulk-all").onclick = () => { this.items.forEach((t) => this.selected.add(t.track_id)); this.renderGrid(); };
      $("#bulk-delete").onclick = () => this.bulkDelete();
      $("#drawer-close").onclick = () => this.closeDrawer();
      $("#drawer-backdrop").onclick = () => this.closeDrawer();
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && $("#modal").classList.contains("hidden")) this.closeDrawer();
        const el = e.target instanceof Element ? e.target : null;
        if (e.key === "/" && router.current === "library" && !(el && el.closest("input, textarea, select"))) { e.preventDefault(); $("#lib-search").focus(); }
      });
      const p = prefs.get("library") || {};
      if (p.sort) $("#lib-sort").value = p.sort; if (p.perPage) $("#lib-perpage").value = p.perPage;
    },
    savePrefs() { prefs.set("library", { sort: $("#lib-sort").value, perPage: $("#lib-perpage").value }); },
    async load({ silent = false } = {}) {
      const [sort, order] = $("#lib-sort").value.split(":");
      const params = new URLSearchParams({ page: this.page, per_page: $("#lib-perpage").value, sort, order });
      const q = $("#lib-search").value.trim(); if (q) params.set("q", q);
      const type = $("#lib-type").value; if (type) params.set("source_type", type);
      if (!silent && !this.items.length) $("#track-grid").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
      try {
        const data = await api.get("/tracks?" + params.toString());
        if (data.page > data.pages && data.total > 0) { this.page = data.pages; return this.load({ silent }); }  // happens after deleting the last item on the last page
        this.items = data.items; this.total = data.total; this.pages = data.pages; this.page = data.page;
        $("#lib-summary").textContent = data.total ? `${data.total} track${data.total === 1 ? "" : "s"}${q ? ` matching "${q}"` : ""}, page ${data.page} of ${data.pages}` : (q ? `Nothing matches "${q}"` : "");
        this.renderGrid(); this.renderPager();
      } catch (e) {
        showError("Could not load the library", e);
        $("#track-grid").innerHTML = `<div class="empty-state"><h3>Could not load the library</h3><p>${esc(e.message)}</p><p><button class="btn btn-ghost btn-sm" id="lib-retry" type="button">Retry</button></p></div>`;
        $("#lib-retry").onclick = () => this.load();
      }
    },
    art(seed) {
      let h = 5381; for (const ch of seed || "x") h = ((h << 5) + h) ^ ch.charCodeAt(0); h >>>= 0;
      const hue = h % 360, bars = Array.from({ length: 16 }, (_, i) => 20 + ((h * (i + 7) * 2654435761) >>> 0) % 70);
      return { style: `background:linear-gradient(135deg,hsl(${hue},55%,32%),hsl(${(hue + 40) % 360},70%,48%))`, bars: bars.map((b) => `<i style="height:${b}%"></i>`).join("") };
    },
    renderGrid() {
      const grid = $("#track-grid");
      if (!this.items.length) {
        grid.innerHTML = `<div class="empty-state"><h3>${$("#lib-search").value ? "No tracks match your search" : "Your library is empty"}</h3><p>${$("#lib-search").value ? "Try another term or clear the filter." : "Upload files above, index a folder on the server, or run <code>audiofp index &lt;folder&gt;</code> from a terminal."}</p></div>`;
        return;
      }
      grid.innerHTML = this.items.map((t) => {
        const name = t.title || t.filename || t.track_id, a = this.art(name);
        return `<article class="track-card ${this.selected.has(t.track_id) ? "selected" : ""}" data-id="${esc(t.track_id)}" tabindex="0" aria-label="${esc(name)}">
          ${this.selecting ? `<input type="checkbox" class="track-check" ${this.selected.has(t.track_id) ? "checked" : ""} aria-label="Select ${esc(name)}" />` : ""}
          <div class="track-art" style="${a.style}">${a.bars}</div>
          <div class="track-body"><div class="track-title" title="${esc(name)}">${esc(name)}</div>${t.artist ? `<div class="track-artist">${esc(t.artist)}</div>` : ""}
            <div class="track-meta">${t.source_type === "video" ? '<span class="badge video">video</span>' : ""}<span class="badge">${fmt.dur(t.duration)}</span><span class="badge">${fmt.num(t.num_hashes)} fp</span>${t.filename ? `<span class="badge">${esc(fmt.ext(t.filename))}</span>` : ""}${(t.tags || []).slice(0, 2).map((x) => `<span class="badge tag">${esc(x)}</span>`).join("")}</div></div>
          <div class="track-footer"><button class="play-btn" data-play title="Play">${ICON.play}</button><span class="spacer" style="flex:1"></span><button class="icon-btn" data-del title="Delete">${ICON.trash}</button></div>
        </article>`;
      }).join("");
      $$(".track-card", grid).forEach((card) => {
        const id = card.dataset.id;
        card.onclick = (e) => {
          if (e.target.closest("[data-play]")) return this.togglePlay(id, e.target.closest("[data-play]"), card);
          if (e.target.closest("[data-del]")) return this.deleteTrack(id);
          if (this.selecting || e.target.classList.contains("track-check")) { this.toggleSelected(id); return; }
          this.openDrawer(id);
        };
        card.onkeydown = (e) => { if (e.key === "Enter" && e.target === card) this.openDrawer(id); };
      });
    },
    renderPager() {
      const el = $("#lib-pager"); el.innerHTML = "";
      if (this.pages <= 1) return;
      const btn = (label, page, cls = "") => { const b = document.createElement("button"); b.className = `btn btn-ghost btn-sm ${cls}`; b.textContent = label; b.disabled = page < 1 || page > this.pages; b.onclick = () => { this.page = page; this.load(); window.scrollTo({ top: 0, behavior: "smooth" }); }; el.appendChild(b); };
      btn("‹", this.page - 1);
      const pages = new Set([1, this.pages, this.page - 1, this.page, this.page + 1].filter((p) => p >= 1 && p <= this.pages));
      let last = 0;
      [...pages].sort((a, b) => a - b).forEach((p) => { if (p - last > 1) { const s = document.createElement("span"); s.textContent = "..."; s.className = "muted"; el.appendChild(s); } btn(String(p), p, p === this.page ? "current" : ""); last = p; });
      btn("›", this.page + 1);
    },
    toggleSelect(on = !this.selecting) {
      this.selecting = on; this.selected.clear();
      $("#bulk-bar").classList.toggle("hidden", !on); $("#lib-select").textContent = on ? "Cancel" : "Select";
      this.renderGrid(); this.updateBulk();
    },
    toggleSelected(id) { this.selected.has(id) ? this.selected.delete(id) : this.selected.add(id); this.renderGrid(); this.updateBulk(); },
    updateBulk() { $("#bulk-count").textContent = `${this.selected.size} selected`; $("#bulk-delete").disabled = !this.selected.size; },
    async bulkDelete() {
      const ids = [...this.selected]; if (!ids.length) return;
      if (!(await modal.confirm(`Delete ${ids.length} track${ids.length === 1 ? "" : "s"}?`, "Their fingerprints are removed permanently. Uploaded files stay on disk unless you delete them from the track details.", { danger: true, ok: "Delete" }))) return;
      try { const r = await api.post("/tracks/bulk-delete", { json: { track_ids: ids } }); toast(`Deleted ${r.deleted} track${r.deleted === 1 ? "" : "s"}`, "ok"); this.toggleSelect(false); this.load(); system.refresh(); }
      catch (e) { showError("Bulk delete failed", e); }
    },
    async deleteTrack(id, { deleteFile = false } = {}) {
      const t = this.items.find((x) => x.track_id === id) || {};
      if (!(await modal.confirm("Delete this track?", `"${esc(t.title || t.filename || id)}" and its fingerprints will be removed.${deleteFile ? " The uploaded file will be deleted too." : ""}`, { danger: true, ok: "Delete" }))) return;
      try { await api.del(`/tracks/${encodeURIComponent(id)}${deleteFile ? "?delete_file=true" : ""}`); toast("Track deleted", "ok"); this.closeDrawer(); this.load(); system.refresh(); }
      catch (e) { showError("Delete failed", e); }
    },
    async togglePlay(id, btn, card) {
      if (this.player && this.player.btn === btn) { this.player.audio.pause(); this.player.audio.remove(); btn.innerHTML = ICON.play; btn.classList.remove("playing"); this.player = null; return; }
      if (this.player) { this.player.audio.pause(); this.player.audio.remove(); this.player.btn.innerHTML = ICON.play; this.player.btn.classList.remove("playing"); }
      let src;
      try { src = await api.streamUrl(id); } catch (e) { return showError("Cannot play", e); }
      const audio = document.createElement("audio"); audio.controls = true; audio.src = src; audio.style.width = "100%"; audio.style.marginTop = "6px";
      audio.onerror = () => { toast("Cannot play: the original file is no longer on disk.", "err"); btn.innerHTML = ICON.play; btn.classList.remove("playing"); };
      audio.onended = () => { btn.innerHTML = ICON.play; btn.classList.remove("playing"); };
      card.querySelector(".track-body").appendChild(audio);
      audio.play().catch(() => {});
      btn.innerHTML = ICON.stop; btn.classList.add("playing");
      this.player = { audio, btn };
    },
    /* --- drawer --- */
    async openDrawer(id) {
      this.lastOpened = id;
      const drawer = $("#drawer"), body = $("#drawer-body");
      drawer.classList.remove("hidden"); $("#drawer-backdrop").classList.remove("hidden");
      body.innerHTML = '<div class="skeleton" style="height:90px"></div>';
      try {
        const t = await api.get(`/tracks/${encodeURIComponent(id)}`);
        const name = t.title || t.filename || t.track_id, a = this.art(name);
        const streamSrc = t.file_exists ? await api.streamUrl(t.track_id) : "";
        $("#drawer-title").textContent = name;
        body.innerHTML = `
          <div class="drawer-art" style="${a.style}">${a.bars}</div>
          <audio controls preload="none" src="${esc(streamSrc)}" style="width:100%"></audio>
          ${t.file_exists ? "" : '<div class="notice warn">The original file is no longer on disk. Search still works, but playback will not.</div>'}
          <form id="track-edit" class="form-grid">
            <label class="field">Title<input name="title" value="${esc(t.title)}" /></label>
            <label class="field">Artist / speaker<input name="artist" value="${esc(t.artist)}" /></label>
            <label class="field" style="grid-column:1/-1">Tags<input name="tags" value="${esc((t.tags || []).join(", "))}" placeholder="comma, separated" /></label>
            <div class="row gap-2"><button class="btn btn-primary btn-sm" type="submit">Save</button><span class="muted" id="edit-status"></span></div>
          </form>
          <dl class="kv">
            <dt>Duration</dt><dd>${fmt.dur(t.duration)}</dd>
            <dt>Fingerprints</dt><dd>${(t.num_hashes || 0).toLocaleString()} hashes, ${(t.num_peaks || 0).toLocaleString()} peaks</dd>
            <dt>File</dt><dd>${esc(t.filename)}, ${fmt.size(t.file_size)}, ${esc(t.source_type)}</dd>
            <dt>Path</dt><dd>${esc(t.filepath || "-")}</dd>
            <dt>Indexed</dt><dd>${fmt.date(t.indexed_at)} (${esc(t.metadata && t.metadata.source || "?")})</dd>
            <dt>Content hash</dt><dd>${esc(t.content_hash || "-")}</dd>
            <dt>Track id</dt><dd>${esc(t.track_id)} <button class="btn btn-ghost btn-sm" id="copy-id" type="button">copy</button></dd>
            ${Object.keys(t.metadata || {}).filter((k) => k !== "source").map((k) => `<dt>${esc(k)}</dt><dd>${esc(JSON.stringify(t.metadata[k]))}</dd>`).join("")}
          </dl>
          <div class="row gap-2 wrap">
            <button class="btn btn-danger btn-sm" id="drawer-delete" type="button">Delete track</button>
            ${t.file_exists && t.metadata && t.metadata.source === "upload" ? '<button class="btn btn-danger btn-sm" id="drawer-delete-file" type="button">Delete track + file</button>' : ""}
          </div>`;
        $("#copy-id").onclick = () => copyText(t.track_id, "Track id copied");
        $("#drawer-delete").onclick = () => this.deleteTrack(t.track_id);
        const df = $("#drawer-delete-file"); if (df) df.onclick = () => this.deleteTrack(t.track_id, { deleteFile: true });
        $("#track-edit").onsubmit = async (e) => {
          e.preventDefault();
          const f = e.target;
          try {
            await api.patch(`/tracks/${encodeURIComponent(t.track_id)}`, { title: f.title.value, artist: f.artist.value, tags: f.tags.value });
            $("#edit-status").textContent = "Saved"; toast("Track updated", "ok"); this.load({ silent: true });
          } catch (err) { showError("Save failed", err); }
        };
        drawer.focus();
      } catch (e) { body.innerHTML = `<div class="notice err">${esc(e.message)}</div>`; }
    },
    closeDrawer() {
      if ($("#drawer").classList.contains("hidden")) return;
      $("#drawer").classList.add("hidden"); $("#drawer-backdrop").classList.add("hidden");
      const card = this.lastOpened && $(`.track-card[data-id="${CSS.escape(this.lastOpened)}"]`);
      if (card) card.focus();
    },
    /* --- uploads --- */
    enqueue(files) {
      const info = system.info; const limit = info ? info.limits.max_upload_mb * 1024 * 1024 : Infinity;
      files.forEach((file) => {
        const item = { file, status: "queued", el: null };
        if (file.size > limit) { item.status = "error"; item.message = `larger than the ${info.limits.max_upload_mb} MB limit`; }
        this.queue.push(item); this.renderQueueItem(item);
      });
      this.pump();
    },
    renderQueueItem(item) {
      if (!item.el) { item.el = document.createElement("li"); $("#upload-queue").prepend(item.el); }
      const cls = { error: "err", indexed: "ok", duplicate: "dup" }[item.status] || "";
      item.el.className = `uq-item ${cls}`;
      const label = { queued: "Queued", uploading: `Uploading ${Math.round((item.progress || 0) * 100)}%`, indexing: "Indexing", indexed: "Indexed", duplicate: "Already in the library (duplicate)", error: "Failed" }[item.status] || item.status;
      item.el.innerHTML = `<span class="uq-name" title="${esc(item.file.name)}">${esc(item.file.name)}</span><span class="muted">${fmt.size(item.file.size)}</span>
        ${item.status === "uploading" ? `<div class="uq-bar"><i style="width:${Math.round((item.progress || 0) * 100)}%"></i></div>` : ""}
        <span class="uq-status">${esc(label)}${item.message ? `: ${esc(item.message)}` : ""}</span>`;
    },
    async pump() {
      while (this.active < 2) {
        const next = this.queue.find((i) => i.status === "queued"); if (!next) break;
        this.active++;
        this.processUpload(next).finally(() => { this.active--; this.pump(); });
      }
    },
    async processUpload(item) {
      item.status = "uploading"; this.renderQueueItem(item);
      const fd = new FormData(); fd.append("audio", item.file, item.file.name);
      const tags = $("#upload-tags").value.trim(); if (tags) fd.append("tags", tags);
      try {
        const res = await api.upload("/tracks", fd, (p) => { item.progress = p; this.renderQueueItem(item); });
        item.status = "indexing"; this.renderQueueItem(item);
        const job = await activity.waitFor(res.job_id);
        if (job.status !== "completed") { item.status = "error"; item.message = job.error || job.status; }
        else if (job.result && job.result.status === "duplicate") { item.status = "duplicate"; }
        else if (job.result && job.result.status === "failed") { item.status = "error"; item.message = job.result.error; }
        else { item.status = "indexed"; }
        this.renderQueueItem(item);
        if (item.status === "indexed") { toast(`Indexed ${item.file.name}`, "ok"); this.load({ silent: true }); system.refresh(); }
        else if (item.status === "duplicate") toast(`${item.file.name} is already in the library`, "warn");
        else toast(`${item.file.name} failed: ${item.message}`, "err", { duration: 9000 });
      } catch (e) { item.status = "error"; item.message = e.message; this.renderQueueItem(item); toast(`${item.file.name} failed: ${e.message}`, "err", { duration: 9000 }); }
    },
    async indexDirectory() {
      const path = $("#dir-input").value.trim(); if (!path) return toast("Enter a directory path on the server", "warn");
      const btn = $("#dir-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
      try {
        const res = await api.post("/tracks/index-directory", { json: { directory_path: path, recursive: $("#dir-recursive").checked, tags: $("#upload-tags").value.trim() } });
        toast(`Indexing ${res.total_files} file${res.total_files === 1 ? "" : "s"} in the background`, "ok");
        $("#dir-input").value = "";
        router.go("activity");
      } catch (e) { showError("Could not start indexing", e); }
      finally { btn.disabled = false; btn.textContent = "Index"; }
    },
  };

  /* ------------------------------------------------------------------ activity */
  const activity = {
    timer: null,
    init() {
      $("#jobs-filter").onchange = () => this.load();
      $("#jobs-refresh").onclick = () => this.load();
    },
    async load() {
      clearTimeout(this.timer);
      const status = $("#jobs-filter").value;
      try {
        const data = await api.get(`/jobs?limit=100${status ? "&status=" + encodeURIComponent(status) : ""}`);
        this.render(data.items);
        $("#nav-active-jobs").textContent = data.active; $("#nav-active-jobs").classList.toggle("hidden", !data.active);
        if (router.current === "activity") this.timer = setTimeout(() => this.load(), data.active ? 1500 : 10000);
      } catch (e) {
        showError("Could not load jobs", e);
        $("#jobs-list").innerHTML = `<div class="empty-state"><h3>Could not load jobs</h3><p>${esc(e.message)}</p><p><button class="btn btn-ghost btn-sm" id="jobs-retry" type="button">Retry</button></p></div>`;
        $("#jobs-retry").onclick = () => this.load();
      }
    },
    render(jobs) {
      const el = $("#jobs-list");
      if (!jobs.length) { el.innerHTML = '<div class="empty-state"><h3>No jobs yet</h3><p>Upload files or index a folder from the Library view.</p></div>'; return; }
      const open = new Set($$(".job-errors[open]", el).map((d) => d.dataset.job));
      const focused = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute("data-cancel") || document.activeElement.getAttribute("data-remove") : null;
      const focusedKind = focused ? (document.activeElement.hasAttribute("data-cancel") ? "data-cancel" : "data-remove") : null;
      this.known = this.known || new Map();
      jobs.forEach((j) => {
        const prev = this.known.get(j.job_id);
        if (prev && prev !== j.status && ["completed", "failed", "cancelled", "interrupted"].includes(j.status)) {
          const label = j.label.length > 60 ? "..." + j.label.slice(-57) : j.label;
          toast(`${j.type === "directory" ? "Folder" : "Upload"} job ${j.status}: ${label}`, j.status === "completed" ? "ok" : "warn");
        }
        this.known.set(j.job_id, j.status);
      });
      el.innerHTML = jobs.map((j) => {
        const pct = Math.max(0, Math.min(100, j.percent || 0));
        const running = j.status === "running" || j.status === "pending";
        const fill = j.status === "completed" ? "done" : (j.status === "failed" || j.status === "interrupted") ? "error" : (running && !j.total ? "indeterminate" : "");
        const parts = [];
        if (j.total) parts.push(`${j.completed}/${j.total} files`);
        if (j.succeeded) parts.push(`${j.succeeded} indexed`);
        if (j.skipped) parts.push(`${j.skipped} duplicate${j.skipped === 1 ? "" : "s"}`);
        if (j.failed) parts.push(`${j.failed} failed`);
        if (running && j.eta_sec != null) parts.push(`~${fmt.dur(j.eta_sec)} left`);
        if (j.rate_per_sec && running) parts.push(`${j.rate_per_sec.toFixed(2)} files/s`);
        if (j.current_item && running) parts.push(`now: ${j.current_item}`);
        parts.push(j.finished_at ? `finished ${fmt.ago(j.finished_at)}` : `started ${fmt.ago(j.started_at || j.created_at)}`);
        return `<article class="job-card" data-job="${esc(j.job_id)}">
          <div class="job-top"><span class="badge">${esc(j.type)}</span><span class="job-name" title="${esc(j.label)}">${esc(j.label)}</span><span class="job-status ${esc(j.status)}">${esc(j.status)}</span>
            ${running ? `<button class="btn btn-ghost btn-sm" data-cancel="${esc(j.job_id)}" type="button" ${j.cancel_requested ? "disabled" : ""}>${j.cancel_requested ? "Cancelling" : "Cancel"}</button>` : `<button class="icon-btn" data-remove="${esc(j.job_id)}" title="Remove from history" aria-label="Remove from history">×</button>`}
          </div>
          <div class="progress-track"><div class="progress-fill ${fill}" style="width:${pct}%"></div></div>
          <div class="job-sub">${parts.map((p) => `<span>${esc(p)}</span>`).join("")}</div>
          ${j.error ? `<div class="notice err">${esc(j.error)}</div>` : ""}
          ${j.error_count ? `<details class="job-errors" data-job="${esc(j.job_id)}" ${open.has(j.job_id) ? "open" : ""}><summary>${j.error_count} file${j.error_count === 1 ? "" : "s"} failed, show details</summary><ul data-errors="${esc(j.job_id)}"><li class="muted">Loading</li></ul></details>` : ""}
        </article>`;
      }).join("");
      if (focused) { const again = $(`[${focusedKind}="${CSS.escape(focused)}"]`, el); if (again) again.focus(); }
      $$("[data-cancel]", el).forEach((b) => (b.onclick = async () => { try { await api.post(`/jobs/${b.dataset.cancel}/cancel`); toast("Cancellation requested", "ok"); this.load(); } catch (e) { showError("Cancel failed", e); } }));
      $$("[data-remove]", el).forEach((b) => (b.onclick = async () => { try { await api.del(`/jobs/${b.dataset.remove}`); this.load(); } catch (e) { showError("Remove failed", e); } }));
      $$(".job-errors", el).forEach((d) => {
        const fill = async () => {
          const list = d.querySelector("ul"); if (list.dataset.loaded) return;
          try { const job = await api.get(`/jobs/${d.dataset.job}`); list.innerHTML = job.errors.map((e) => `<li><b>${esc(e.file.split(/[\\/]/).pop())}</b>${esc(e.error)}${e.error_code ? ` <span class="badge err">${esc(e.error_code)}</span>` : ""}</li>`).join("") || "<li>No details recorded.</li>"; list.dataset.loaded = "1"; }
          catch (e) { list.innerHTML = `<li>${esc(e.message)}</li>`; }
        };
        d.addEventListener("toggle", () => d.open && fill());
        if (d.open) fill();
      });
    },
    waitFor(jobId) {
      return new Promise((resolve, reject) => {
        const tick = async () => {
          try {
            const job = await api.get(`/jobs/${jobId}`);
            if (["completed", "failed", "cancelled", "interrupted"].includes(job.status)) return resolve(job);
            setTimeout(tick, 1000);
          } catch (e) { reject(e); }
        };
        tick();
      });
    },
  };

  /* ------------------------------------------------------------------ settings */
  const settings = {
    init() {
      $("#apikey-save").onclick = () => { const v = $("#apikey-input").value.trim(); if (!v) return; prefs.set("apiKey", v); $("#apikey-input").value = ""; toast("API key saved", "ok"); this.renderKey(); system.info = null; system.refresh(); };
      $("#apikey-clear").onclick = () => { prefs.del("apiKey"); toast("API key forgotten", "ok"); this.renderKey(); };
      $$("[data-theme-set]").forEach((b) => (b.onclick = () => theme.set(b.dataset.themeSet)));
      $("#pref-autoplay").checked = prefs.get("autoplayAtMatch", true);
      $("#pref-autoplay").onchange = (e) => prefs.set("autoplayAtMatch", e.target.checked);
      $("#defaults-form").onsubmit = async (e) => {
        e.preventDefault();
        const f = e.target;
        const payload = { mode: f.mode.value, top_k: Number(f.top_k.value), min_confidence: Number(f.min_confidence.value), min_aligned_hashes: Number(f.min_aligned_hashes.value), min_peak_ratio: Number(f.min_peak_ratio.value) };
        try { await api.put("/settings", payload); toast("Server defaults saved", "ok"); system.info = null; await system.refresh(); search.applyDefaults(system.info.defaults, true); }
        catch (err) { showError("Could not save defaults", err); }
      };
      $("#sys-refresh").onclick = () => { system.info = null; this.load(); };
      this.renderKey();
    },
    renderKey() { const has = !!api.key(); $("#apikey-status").textContent = has ? "An API key is stored in this browser." : (system.info && system.info.features.auth_required ? "This server requires an API key." : "No API key stored (the server does not require one)."); },
    async load() {
      try {
        if (!system.info) system.info = await api.get("/info");
        const info = system.info, d = await api.get("/settings");
        const f = $("#defaults-form"); f.mode.value = d.mode; f.top_k.value = d.top_k; f.min_confidence.value = d.min_confidence; f.min_aligned_hashes.value = d.min_aligned_hashes; f.min_peak_ratio.value = d.min_peak_ratio;
        f.top_k.max = info.limits.max_top_k;
        const fp = info.fingerprint;
        const rows = [
          ["Version", info.version], ["Profile", info.profile], ["Storage", info.storage_type],
          ["ffmpeg", info.ffmpeg.available ? `${info.ffmpeg.version || "available"} (${info.ffmpeg.path})` : "not installed, so no video, M4A or WMA"],
          ["Native formats", info.formats.native_audio.join(", ")], ["Via ffmpeg", [...info.formats.ffmpeg_audio, ...info.formats.video].join(", ")],
          ["Upload limit", `${info.limits.max_upload_mb} MB`], ["Max query length", fmt.dur(info.limits.max_query_seconds)],
          ["Directory indexing", info.features.directory_indexing ? (info.features.index_roots.length ? info.features.index_roots.join(", ") : "any path (development)") : "disabled"],
          ["Duplicate detection", info.features.dedupe], ["Auth", info.features.auth_required ? "API key required" : "open"],
          ["Fingerprint", `sr ${fp.sample_rate} Hz, n_fft ${fp.n_fft}, hop ${fp.hop_length}, fan ${fp.fan_value}, neighbourhood ${fp.peak_neighborhood_size}, min amp ${fp.min_amplitude}, time delta ${fp.min_hash_time_delta} to ${fp.max_hash_time_delta}`],
          ["Signature", `${fp.signature} (algorithm v${fp.algorithm_version})`],
        ];
        $("#sys-info").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
        this.renderKey();
      } catch (e) { showError("Could not load settings", e); }
    },
  };

  /* ------------------------------------------------------------------ boot */
  theme.apply();
  $$("[data-action='toggle-theme']").forEach((b) => (b.onclick = () => theme.toggle()));
  search.init(); library.init(); activity.init(); settings.init();
  system.refresh({ silent: false }).then(() => router.render());
  setInterval(() => system.refresh(), 20000);
  window.addEventListener("online", () => system.refresh());
  window.addEventListener("offline", () => system.pill("error", "Offline"));
  window.AudioFP = { api, search, library, activity, settings, system, prefs };
})();
