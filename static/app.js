/* wrss frontend */
(() => {
  "use strict";

  const state = { kind: "", cat: "", region: "", country: "", q: "", unread: false, offset: 0, total: 0 };
  let pollTimer = null;
  // Read by /api/status so the reader view can hide the per-article translate
  // button while the master switch is off. Without this the button would be
  // offered and then refused, which reads as a broken app rather than a
  // deliberate switch. (The background queue has its own server-side switch
  // and never spends unattended.)
  let translateEnabled = true;
  // Per-browser auto-translate of the visible page. Independent of the Admin
  // master switch (a visible page is deliberate spend, like one ⇄ click), but
  // still stops on empty budget or rate-limit cooldown. Default on: a foreign
  // section should read English without a click per headline.
  let autoTranslate = localStorage.getItem("wrss-auto-translate") !== "0";
  let autoTimer = null;
  let autoRun = 0;          // invalidates a scheduled run when a new load starts
  let autoBusy = false;     // one bulk run in flight at a time

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  const PFX = document.body.dataset.prefix || "";

  const api = async (url, opts = {}) => {
    const res = await fetch(PFX + url, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.status === 200 ? res.json() : null;
  };

  /* ---------- helpers ---------- */
  function timeAgo(ts) {
    if (!ts) return "";
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    const d = Math.floor(h / 24);
    if (d < 7) return d + "d ago";
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function hue(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
    return h;
  }

  function esc(s) {
    return (s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function toast(msg, cls = "") {
    const el = document.createElement("div");
    el.className = "toast " + cls;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3200);
    setTimeout(() => el.remove(), 3600);
  }

  function filterTitle() {
    if (state.country) return state.country;
    if (state.region) return state.region;
    if (state.cat) return state.cat;
    if (state.kind === "native") return "Native feeds";
    if (state.kind === "foreign") return "Foreign feeds (translated)";
    return "All news";
  }

  function qs(extra = {}) {
    const p = new URLSearchParams();
    if (state.kind) p.set("kind", state.kind);
    if (state.cat) p.set("cat", state.cat);
    if (state.region) p.set("region", state.region);
    if (state.country) p.set("country", state.country);
    if (state.q) p.set("q", state.q);
    if (state.unread) p.set("unread", "1");
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    return p.toString();
  }

  /* ---------- articles ---------- */
  async function loadArticles(append = false) {
    // A new load invalidates any scheduled auto-run: the cards it would have
    // translated are gone (filter change) or about to be joined by more.
    autoRun++;
    clearTimeout(autoTimer);
    const limit = 50;
    const offset = append ? state.offset : 0;
    const data = await api("/api/articles?" + qs({ limit, offset }));
    state.total = data.total;
    state.offset = offset + data.articles.length;

    const box = $("#articles");
    if (!append) box.innerHTML = "";
    const startIdx = append ? box.children.length : 0;
    for (const a of data.articles) box.appendChild(renderCard(a));

    const empty = data.articles.length === 0;
    $("#empty").classList.toggle("hidden", !empty);
    $("#btn-more").classList.toggle("hidden", empty || state.offset >= state.total);

    const unreadInView = data.articles.filter((a) => !a.read).length;
    $("#count-chip").textContent = `${state.total} articles`;
    $("#filter-title").textContent = filterTitle();
    document.title = (unreadInView ? `(${unreadInView}) ` : "") + "wrss";

    if (autoTranslate && translateEnabled && !document.hidden) {
      // Only the newly appended chunk after Load-more; the earlier cards were
      // already offered their run when they arrived.
      scheduleAutoTranslate(append ? [...box.children].slice(startIdx) : [...box.children]);
    }
  }

  function renderCard(a) {
    const el = document.createElement("article");
    el.className = "card" + (a.read ? " read" : "");
    el.dataset.id = a.id;
    // The auto-translate bulk path updates cards without the article object in
    // scope, but the ↳ original line needs the source headline -- so it rides
    // on the element.
    el._origTitle = a.title;

    const foreign = a.feed_kind === "foreign";
    const mainTitle = foreign && a.title_en ? a.title_en : a.title;
    const catTag = a.category ? `<span class="tag cat">${esc(a.category)}</span>` : "";
    const kindTag = `<span class="tag kind-${a.feed_kind}">${foreign ? "foreign" : "native"}</span>`;
    const regionTag = a.country || a.region
      ? `<span class="tag place">${esc(a.country || a.region)}</span>` : "";
    const orig = foreign && a.title_en && a.title_en !== a.title
      ? `<div class="orig">↳ original: ${esc(a.title)}</div>` : "";

    el.innerHTML = `
      <div class="avatar" style="--h:${hue(a.feed_name)}">${esc(a.feed_name[0] || "?")}</div>
      <div class="card-body">
        <div class="meta">
          <span class="src">${esc(a.feed_name)}</span>
          ${regionTag}${catTag}${kindTag}
          <span class="time">${timeAgo(a.published)}</span>
        </div>
        <a class="headline" href="${esc(a.link)}" target="_blank" rel="noopener">${esc(mainTitle)}</a>
        ${a.needs_translation && translateEnabled
          ? `<button class="htr-btn" title="Translate this headline into English">⇄</button>`
          : ""}
        ${orig}
        ${a.summary ? `<p class="summary">${esc(a.summary)}</p>` : ""}
      </div>
      <div class="card-actions">
        <button class="dot-btn${a.read ? " on" : ""}" title="${a.read ? "Mark unread" : "Mark read"}">●</button>
        ${foreign && translateEnabled ? `<button class="act-btn tr-btn" title="Translate this article into English">⇄</button>` : ""}
        ${foreign && a.translate_url ? `<a class="act-btn" href="${esc(a.translate_url)}" target="_blank" rel="noopener" title="Open via Google Translate">G</a>` : ""}
        <a class="act-btn" href="${esc(a.link)}" target="_blank" rel="noopener" title="Open article">↗</a>
      </div>`;

    const mark = (val) => { a.read = val; markRead(a.id, val, el); };
    el.querySelector(".dot-btn").addEventListener("click", () => mark(!a.read));
    el.querySelector(".headline").addEventListener("click", () => mark(true));
    for (const link of el.querySelectorAll(".card-actions a")) {
      link.addEventListener("click", () => mark(true));
    }
    const tr = el.querySelector(".tr-btn");
    if (tr) tr.addEventListener("click", () => { mark(true); openReader(a); });

    // Per-headline translate. Hidden entirely while the master switch is off
    // (needs_translation already arrives false; the translateEnabled check is
    // belt and braces for cards rendered before the status arrived).
    const htr = el.querySelector(".htr-btn");
    if (htr) htr.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      translateHeadline(a, el, htr);
    });
    return el;
  }

  async function markRead(id, val, el) {
    const card = el || $(`.card[data-id="${id}"]`);
    try {
      await api(`/api/articles/${id}/read`, { method: "POST", body: JSON.stringify({ read: val }) });
      if (card) {
        card.classList.toggle("read", val);
        const b = card.querySelector(".dot-btn");
        b.classList.toggle("on", val);
      }
      refreshBadges();
    } catch (e) { toast(e.message, "err"); }
  }

  /* ---------- per-headline translation ---------- */
  /* Translate a single headline without waiting for the queue. It costs one
     model call, so the button goes into a busy state and the result is dropped
     straight into the card in place of the original text -- no page reload. */
  function applyHeadlineTranslation(card, a, title) {
    // Shared by the single ⇄ button and the bulk auto-run: the card ends up
    // looking the same either way -- English headline, original underneath,
    // no translate button left.
    a.title_en = title;
    a.needs_translation = false;
    const link = card.querySelector(".headline");
    if (link) link.textContent = title;
    const orig = document.createElement("div");
    orig.className = "orig";
    orig.textContent = "↳ original: " + a.title;
    const existing = card.querySelector(".orig");
    if (existing) existing.replaceWith(orig); else if (link) link.after(orig);
    const btn = card.querySelector(".htr-btn");
    if (btn) btn.remove();
  }

  async function translateHeadline(a, card, btn) {
    btn.classList.add("busy");
    btn.disabled = true;
    try {
      const r = await api(`/api/articles/${a.id}/headline`, { method: "POST" });
      applyHeadlineTranslation(card, a, r.title);
      if (!r.cached) toast("Headline translated", "good");
    } catch (e) {
      btn.classList.remove("busy");
      btn.disabled = false;
      toast(e.message, "err");
    }
  }

  /* ---------- auto-translate visible page ---------- */
  /* One bulk call per page load instead of one call per headline: the ids of
     the visible cards that still need it go up in a single request and come
     back as id -> title, applied to the cards in place. Debounced so rapid
     filter changes schedule one run, invalidated so a stale run never paints
     over a newer page, and guarded so overlapping runs cannot stack. */
  function setAutoStatus(text) {
    const el = $("#auto-status");
    if (!text) { el.classList.add("hidden"); el.textContent = ""; return; }
    el.classList.remove("hidden");
    el.textContent = text;
  }

  function scheduleAutoTranslate(cards) {
    clearTimeout(autoTimer);
    const run = ++autoRun;
    autoTimer = setTimeout(() => autoTranslateCards(cards, run), 900);
  }

  async function autoTranslateCards(cards, run) {
    if (autoBusy) return;
    const targets = cards.filter((c) => c.querySelector(".htr-btn") && !c.querySelector(".htr-btn").disabled);
    if (!targets.length) return;
    // Mark busy up front so a manual ⇄ click on the same card does not double
    // spend while the bulk call is away.
    for (const c of targets) {
      const b = c.querySelector(".htr-btn");
      if (b) { b.classList.add("busy"); b.disabled = true; }
    }
    autoBusy = true;
    setAutoStatus(`Translating ${targets.length} headline${targets.length === 1 ? "" : "s"}…`);
    try {
      const byId = {};
      for (const c of targets) byId[c.dataset.id] = c;
      const r = await api("/api/articles/translate-batch", {
        method: "POST",
        body: JSON.stringify({ ids: Object.keys(byId).map(Number) }),
      });
      if (run !== autoRun) return;   // a newer page arrived; drop, don't paint
      let n = 0;
      for (const [id, title] of Object.entries(r.translated || {})) {
        const card = byId[id];
        if (!card || !card.isConnected) continue;
        applyHeadlineTranslation(card, { title: card._origTitle || "" }, title);
        // applyHeadlineTranslation needs the original for the ↳ line (kept on
        // the element at render); the English becomes the visible headline.
        n++;
      }
      // Restore buttons the server skipped (native/disabled-group/cached-miss):
      // anything still untranslated keeps its fallback button.
      for (const c of targets) {
        if (!c.isConnected) continue;
        const b = c.querySelector(".htr-btn");
        if (b && !Object.keys(r.translated || {}).includes(c.dataset.id)) {
          b.classList.remove("busy");
          b.disabled = false;
        }
      }
      if (n) toast(`Translated ${n} headline${n === 1 ? "" : "s"}`, "good");
      if (r.truncated) toast("Daily budget nearly spent — some headlines left untranslated", "err");
      if (r.cooldown) toast("Translator rate-limited — will resume shortly", "err");
    } catch (e) {
      // 409 (worker busy) is routine: leave the buttons for next time. 429 and
      // budget errors toast once and stop; no retry loop.
      if (run === autoRun && !/already in progress/i.test(e.message)) toast(e.message, "err");
      for (const c of targets) {
        if (!c.isConnected) continue;
        const b = c.querySelector(".htr-btn");
        if (b) { b.classList.remove("busy"); b.disabled = false; }
      }
    } finally {
      autoBusy = false;
      if (run === autoRun) setAutoStatus("");
    }
  }

  /* ---------- article reader (on-demand LLM translation) ---------- */
  /* Two paths to a foreign article, and the UI offers both because they fail
     in different places. Google Translate is free and instant but needs the
     publisher to serve the page to Google. The button below fetches the page
     here and sends the body to the model, which survives a script wall or a
     paywall but costs one call -- so it is opt-in, per article, and cached
     server-side once paid for. */
  function openReader(a) {
    $("#reader-title").textContent = (a.feed_kind === "foreign" && a.title_en) ? a.title_en : a.title;
    $("#reader-src").textContent = `${a.feed_name}${a.country ? " · " + a.country : ""}`;
    const gt = $("#reader-gt"), orig = $("#reader-orig");
    if (a.translate_url) { gt.href = a.translate_url; gt.hidden = false; } else { gt.hidden = true; }
    if (a.link) { orig.href = a.link; orig.hidden = false; } else { orig.hidden = true; }
    const body = $("#reader-body");
    if (!translateEnabled) {
      gt.hidden = true;
      body.innerHTML = '<div class="reader-status">Translation is currently disabled. Use Original ↗ to read the article.</div>';
      $("#reader").classList.remove("hidden");
      return;
    }
    body.innerHTML = '<div class="reader-status">Translating…</div>';
    $("#reader").classList.remove("hidden");
    api(`/api/articles/${a.id}/translate`, { method: "POST" })
      .then((r) => {
        if (r && r.text) {
          body.innerHTML = "";
          for (const para of r.text.split(/\n{2,}/)) {
            if (!para.trim()) continue;
            const p = document.createElement("p");
            p.textContent = para.trim();
            body.appendChild(p);
          }
          if (r.cached) {
            const n = document.createElement("div");
            n.className = "reader-status";
            n.textContent = "cached translation";
            body.prepend(n);
          }
        } else {
          body.innerHTML = `<div class="reader-status err">${esc((r && r.error) || "No translation available.")}</div>`;
        }
      })
      .catch((e) => {
        body.innerHTML = `<div class="reader-status err">${esc(e.message)}</div>`;
      });
  }

  /* ---------- sidebar / badges ---------- */
  async function loadBootstrap() {
    const data = await api("/api/bootstrap");
    renderSections(data.sections || []);
    const catList = $("#cat-list");
    catList.innerHTML = "";
    for (const c of data.categories) {
      if (!c.name) continue;
      const btn = document.createElement("button");
      btn.className = "nav-item" + (state.cat === c.name ? " active" : "");
      btn.dataset.cat = c.name;
      btn.innerHTML = `${esc(c.name)} <span class="badge ${c.unread ? "" : "zero"}">${c.unread || 0}</span>`;
      btn.addEventListener("click", () => {
        state.cat = state.cat === c.name ? "" : c.name;
        state.kind = ""; state.region = ""; state.country = "";
        syncNav(); loadArticles();
        closeSidebarMobile();
      });
      catList.appendChild(btn);
    }
    const dl = $("#cat-options");
    dl.dataset.cats = data.cat_names.join("|");
    dl.innerHTML = data.cat_names.map((n) => `<option value="${esc(n)}">`).join("");
    for (const kind of ["all", "native", "foreign"]) {
      const b = $(`[data-badge="${kind}"]`);
      const v = data.kinds[kind]?.unread || 0;
      b.textContent = v || "";
      b.classList.toggle("zero", !v);
    }
  }

  function renderSections(sections) {
    const box = $("#section-list");
    box.innerHTML = "";
    if (!sections.length) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const title = document.createElement("div");
    title.className = "side-title";
    title.textContent = "Sections";
    box.appendChild(title);

    for (const s of sections) {
      const btn = document.createElement("button");
      btn.className = "nav-item sec" + (state.region === s.name && !state.country ? " active" : "");
      btn.dataset.region = s.name;
      btn.innerHTML = `<span class="sec-label">${esc(s.name)}</span> <span class="badge ${s.unread ? "" : "zero"}">${s.unread || 0}</span>`;
      btn.addEventListener("click", () => {
        state.country = "";
        state.region = state.region === s.name && !state.country ? "" : s.name;
        state.cat = ""; state.kind = "";
        syncNav(); loadArticles(); closeSidebarMobile();
      });
      box.appendChild(btn);

      // Countries only show for the section that is open, so the sidebar stays
      // readable with a dozen sections in it.
      const open = state.region === s.name;
      for (const c of s.countries || []) {
        const cb = document.createElement("button");
        cb.className = "nav-item country" + (state.country === c.name ? " active" : "");
        cb.dataset.country = c.name;
        cb.innerHTML = `<span class="sec-label">${esc(c.name)}</span> <span class="badge ${c.unread ? "" : "zero"}">${c.unread || 0}</span>`;
        cb.addEventListener("click", (e) => {
          e.stopPropagation();
          state.region = s.name;
          state.country = state.country === c.name ? "" : c.name;
          state.cat = ""; state.kind = "";
          syncNav(); loadArticles(); closeSidebarMobile();
        });
        cb.hidden = !open;
        box.appendChild(cb);
      }
    }
  }

  function syncNav() {
    $$("#sidebar .nav-item").forEach((b) => {
      const on = (b.dataset.kind !== undefined && b.dataset.kind === state.kind)
        || (b.dataset.cat !== undefined && b.dataset.cat === state.cat)
        || (b.dataset.country !== undefined && b.dataset.country === state.country)
        || (b.dataset.region !== undefined && b.dataset.region === state.region && !state.country);
      b.classList.toggle("active", !!on);
    });
    const dl = $("#cat-options");
    if (dl) dl.innerHTML = (dl.dataset.cats || "").split("|").filter(Boolean)
      .map((n) => `<option value="${esc(n)}">`).join("");
  }

  async function refreshBadges() {
    try { await loadBootstrap(); } catch (_) {}
  }

  async function loadStatus() {
    try {
      const s = await api("/api/status");
      translateEnabled = s.translate_enabled !== false;
      const autoLabel = $("#auto-translate") && $("#auto-translate").closest("label");
      if (autoLabel) autoLabel.style.display = translateEnabled ? "" : "none";
      const ago = s.last_refresh ? timeAgo(s.last_refresh) : "never";
      let line = `last refresh: <b>${ago}</b><br>${s.articles} articles · ${s.unread} unread`;
      if (!translateEnabled) {
        line += `<br><span style="color:var(--muted)">translation paused`
          + (s.pending ? ` · ${s.pending} headlines waiting` : "")
          + `</span>`;
      }
      if (!s.translator_ok) line += `<br><span style="color:var(--warn)">translator: ${esc(s.translator_error || "unavailable")}</span>`;
      if (s.extractor_ok === false) line += `<br><span style="color:var(--warn)">article extraction unavailable — use Google Translate</span>`;
      $("#status-line").innerHTML = line;
      // Cards rendered while translation was on would still show a button the
      // service now refuses, so redraw when the switch changes.
      if (typeof state.cardsDrewTranslate === "boolean" && state.cardsDrewTranslate !== translateEnabled) {
        loadArticles();
      }
      state.cardsDrewTranslate = translateEnabled;
    } catch (_) {}
  }

  /* ---------- feed management ---------- */
  async function openModal() {
    $("#modal").classList.remove("hidden");
    await loadFeeds();
  }

  async function loadFeeds() {
    const data = await api("/api/feeds");
    const box = $("#feeds-list");
    box.innerHTML = "";
    for (const f of data.feeds) box.appendChild(feedRow(f));
    const cats = [...new Set(data.feeds.map((f) => f.category))].sort();
    $("#cat-options").dataset.cats = cats.join("|");
    $("#cat-options").innerHTML = cats.map((c) => `<option value="${esc(c)}">`).join("");
    const regions = [...new Set(data.feeds.map((f) => f.region).filter(Boolean))].sort();
    const ro = $("#region-options");
    ro.dataset.cats = regions.join("|");
    ro.innerHTML = regions.map((r) => `<option value="${esc(r)}">`).join("");
  }

  function feedRow(f) {
    const row = document.createElement("div");
    row.className = "feed-row";
    const stat = f.last_status || "";
    const cls = stat.startsWith("ERROR") ? "err" : "ok";
    row.innerHTML = `
      <input class="f-name" value="${esc(f.name)}" title="Name">
      <input class="f-url" value="${esc(f.url)}" title="URL">
      <select class="f-kind">
        <option value="native"${f.kind === "native" ? " selected" : ""}>native</option>
        <option value="foreign"${f.kind === "foreign" ? " selected" : ""}>foreign</option>
      </select>
      <input class="f-cat" value="${esc(f.category)}" list="cat-options" title="Category">
      <input class="f-region" value="${esc(f.region)}" list="region-options" title="Section">
      <input class="f-country" value="${esc(f.country)}" title="Country">
      <div class="act">
        <label class="active-toggle" title="Active"><input type="checkbox" class="f-active"${f.active ? " checked" : ""}>on</label>
        <button class="btn f-save" title="Save">💾</button>
        <button class="btn danger f-del" title="Delete">🗑</button>
      </div>
      <div class="fstats">
        <span class="${cls}">${esc(stat) || "never fetched"}</span>
        <span>${f.total || 0} articles · ${f.unread || 0} unread</span>
      </div>`;

    row.querySelector(".f-save").addEventListener("click", async () => {
      try {
        await api(`/api/feeds/${f.id}`, {
          method: "PUT",
          body: JSON.stringify({
            name: row.querySelector(".f-name").value,
            url: row.querySelector(".f-url").value,
            kind: row.querySelector(".f-kind").value,
            category: row.querySelector(".f-cat").value,
            region: row.querySelector(".f-region").value,
            country: row.querySelector(".f-country").value,
            active: row.querySelector(".f-active").checked,
          }),
        });
        toast(`Saved ${f.name}`, "good");
        loadFeeds();
      } catch (e) { toast(e.message, "err"); }
    });

    row.querySelector(".f-del").addEventListener("click", async () => {
      if (!confirm(`Remove feed "${f.name}" and its articles?`)) return;
      try {
        await api(`/api/feeds/${f.id}`, { method: "DELETE" });
        toast(`Removed ${f.name}`, "good");
        loadFeeds(); loadBootstrap();
      } catch (e) { toast(e.message, "err"); }
    });
    return row;
  }

  /* ---------- admin ---------- */
  /* Pull/translate switches per section and country, the master translation
     switch, and the daily budget. The backend decides what a group rule means;
     this just renders the switches that exist on the feeds table and posts the
     state back. Groups with no row are enabled, so "re-enable" is the absence
     of a checkbox being ticked -- only ticked rows are sent back. */
  let adminState = null;

  async function openAdmin() {
    $("#admin").classList.remove("hidden");
    await loadAdmin();
  }

  function fmtMins(secs) {
    const m = Math.max(1, Math.round(secs / 60));
    return m >= 60 ? `${Math.round(m / 60)}h ${m % 60}m` : `${m} min`;
  }

  async function loadAdmin() {
    let d;
    try { d = await api("/api/admin"); }
    catch (e) { $("#admin-status").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    adminState = d;

    const s = d.stats;
    const total = s.pending + s.translated;
    const parts = [
      `<b>${s.pending}</b> untranslated`,
      `<b>${s.translated}</b> translated`,
      `today <b>${s.calls_today}</b>/${s.budget} calls`,
      s.in_window ? `<span class="ok">in window</span>` : `<span class="faint">outside window</span>`,
    ];
    if (s.cooldown_seconds > 0) parts.push(`<span class="warn">ongoing paused ${fmtMins(s.cooldown_seconds)}</span>`);
    if (s.cooldown_batch_seconds > 0) parts.push(`<span class="warn">batch paused ${fmtMins(s.cooldown_batch_seconds)}</span>`);
    if (s.forced) parts.push(`<span class="ok">draining now</span>`);
    if (!s.translator_ok) parts.push(`<span class="err">${esc(s.translator_error || "translator error")}</span>`);
    if (s.translator_batch_ok === false) parts.push(`<span class="err">batch: ${esc(s.translator_batch_error || "error")}</span>`);
    $("#admin-status").innerHTML = parts.join(" · ");

    // Drain progress: visible while the forced drain runs, and whenever a
    // backlog exists so the size of the job is obvious before pressing it.
    const bar = $("#drain-bar"), fill = $("#drain-fill"), label = $("#drain-label");
    if (s.forced || s.pending > 0) {
      bar.classList.remove("hidden");
      const pct = total ? Math.round((s.translated / total) * 100) : 100;
      fill.style.width = pct + "%";
      label.textContent = s.forced
        ? `draining… ${s.translated}/${total} (${pct}%) · ${s.pending} left`
        : `${s.pending} headlines waiting · press ▶ to drain on the batch lane`;
    } else {
      bar.classList.add("hidden");
    }

    // Per-lane spend today, from each lane's own token counters and prices.
    for (const lane of ["ongoing", "batch"]) {
      const L = (s.lanes || {})[lane] || {};
      const el = lane === "ongoing" ? $("#ad-ongoing-spend") : $("#ad-batch-spend");
      if (!el) continue;
      const bits = [`model <b>${esc(L.model || "?")}</b>`];
      bits.push(L.has_key ? `<span class="ok">key set</span>` : `<span class="err">no key in .env</span>`);
      if (L.tracked) {
        bits.push(`today <b>${L.calls}</b> calls · ${(L.prompt_tokens / 1000).toFixed(1)}k in / ${(L.completion_tokens / 1000).toFixed(1)}k out · <b>$${Number(L.spend_usd).toFixed(4)}</b>`);
      } else {
        bits.push(`<span class="faint">no calls yet today</span>`);
      }
      el.innerHTML = bits.join(" · ");
    }

    $("#ad-enabled").checked = d.settings.translate_enabled === "1";
    $("#ad-budget").value = d.settings.translate_daily_budget;
    $("#ad-per-tick").value = d.settings.translate_per_tick;
    $("#ad-win-start").value = d.settings.translate_window_start;
    $("#ad-win-end").value = d.settings.translate_window_end;
    $("#ad-ongoing-model").value = d.settings.ongoing_model || "";
    $("#ad-ongoing-price-in").value = d.settings.ongoing_price_in || "";
    $("#ad-ongoing-price-out").value = d.settings.ongoing_price_out || "";
    $("#ad-batch-model").value = d.settings.batch_model || "";
    $("#ad-batch-price-in").value = d.settings.batch_price_in || "";
    $("#ad-batch-price-out").value = d.settings.batch_price_out || "";

    // index rules by "axis\0name" so a checkbox can look itself up
    const on = new Map();
    for (const r of d.rules) on.set(`${r.axis}\0${r.name}`, r);

    const box = $("#ad-groups");
    box.innerHTML = "";
    for (const [axis, label, groups] of [
      ["region", "Sections", d.regions],
      ["country", "Countries", d.countries]]) {
      const head = document.createElement("div");
      head.className = "group-label";
      head.textContent = label;
      box.appendChild(head);
      if (!groups.length) {
        const empty = document.createElement("div");
        empty.className = "group-row faint";
        empty.textContent = "none yet";
        box.appendChild(empty);
        continue;
      }
      for (const g of groups) {
        const key = `${axis}\0${g.name}`;
        const rule = on.get(key) || { pull: true, translate: true };
        const row = document.createElement("div");
        row.className = "group-row";
        row.dataset.axis = axis;
        row.dataset.name = g.name;
        row.dataset.n = g.n;
        row.innerHTML = `
          <span class="gname" title="${esc(g.name || "no section")}">${esc(g.name || "(no section)")}</span>
          <label class="active-toggle"><input type="checkbox" class="g-pull" ${rule.pull ? "checked" : ""}></label>
          <label class="active-toggle"><input type="checkbox" class="g-translate" ${rule.translate ? "checked" : ""}></label>
          <span class="gcount">${g.n} feed${g.n === 1 ? "" : "s"}</span>`;
        box.appendChild(row);
      }
    }
  }

  /* Only rows with something switched off are sent. A fully-enabled group
     therefore disappears from the table, which is exactly what re-enabling it
     means -- and means a group that was never touched is indistinguishable
     from one deliberately turned back on. */
  function collectRules() {
    return [...document.querySelectorAll("#ad-groups .group-row[data-axis]")]
      .filter((r) => !r.querySelector(".g-pull").checked || !r.querySelector(".g-translate").checked)
      .map((r) => ({
        axis: r.dataset.axis,
        name: r.dataset.name,
        pull: r.querySelector(".g-pull").checked,
        translate: r.querySelector(".g-translate").checked,
      }));
  }

  async function saveAdmin() {
    const msg = $("#admin-msg");
    try {
      await api("/api/admin", {
        method: "PUT",
        body: JSON.stringify({
          translate_enabled: $("#ad-enabled").checked ? "1" : "0",
          translate_daily_budget: $("#ad-budget").value,
          translate_per_tick: $("#ad-per-tick").value,
          translate_window_start: $("#ad-win-start").value,
          translate_window_end: $("#ad-win-end").value,
          ongoing_model: $("#ad-ongoing-model").value,
          ongoing_price_in: $("#ad-ongoing-price-in").value,
          ongoing_price_out: $("#ad-ongoing-price-out").value,
          batch_model: $("#ad-batch-model").value,
          batch_price_in: $("#ad-batch-price-in").value,
          batch_price_out: $("#ad-batch-price-out").value,
          rules: collectRules(),
        }),
      });
      msg.textContent = "Saved.";
      toast("Admin settings saved", "good");
      await loadAdmin();
    } catch (e) { msg.textContent = e.message; toast(e.message, "err"); }
  }

  async function translateNow() {
    const msg = $("#admin-msg");
    // Confirm: a forced drain ignores the budget and the pause switch, so the
    // click should state the size of the job up front.
    const pending = (adminState && adminState.stats && adminState.stats.pending) || 0;
    if (pending > 0 && !confirm(
      `Translate ${pending} queued headlines now on the batch lane?\n\n` +
      `This ignores the work window, the daily budget and the pause switch. ` +
      `The batch lane's own rate limit is the only stop.`)) return;
    try {
      await api("/api/admin/translate-now", { method: "POST" });
      msg.textContent = "Draining the queue…";
      toast("Translating the backlog now", "good");
      // Refresh fast while draining so the progress bar moves; loadAdmin is
      // cheap (counts + settings) and the modal is open anyway.
      setTimeout(loadAdmin, 4000);
      setTimeout(loadAdmin, 15000);
    } catch (e) { msg.textContent = e.message; toast(e.message, "err"); }
  }

  async function purgePending(btn) {
    const msg = $("#admin-msg");
    btn.disabled = true;
    msg.textContent = "counting…";
    try {
      // Dry run first: the confirm dialog states the exact number, and the
      // server re-reads it at delete time, so what is confirmed is what is
      // deleted even if the backlog grew in between.
      const d = await api("/api/admin/purge-pending?dry_run=1", { method: "POST" });
      if (!d.pending) { msg.textContent = "Nothing to delete."; return; }
      const ok = confirm(
        `Delete ${d.pending} untranslated article${d.pending === 1 ? "" : "s"}?\n\n` +
        `${d.unread} of them are unread, across ${d.feeds} foreign feeds.\n\n` +
        `Already-translated and native articles are kept. This cannot be undone.`);
      if (!ok) { msg.textContent = ""; return; }
      const r = await api("/api/admin/purge-pending", { method: "POST" });
      toast(`Deleted ${r.deleted} untranslated articles`, "good");
      msg.textContent = `Deleted ${r.deleted}.`;
      await loadAdmin();
      loadArticles(); refreshBadges();
    } catch (e) { msg.textContent = e.message; toast(e.message, "err"); }
    finally { btn.disabled = false; }
  }

  /* ---------- events ---------- */
  function bindNav() {
    $$("#sidebar .nav-item[data-kind]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.kind = btn.dataset.kind;
        state.cat = ""; state.region = ""; state.country = "";
        syncNav(); loadArticles();
        closeSidebarMobile();
      });
    });
  }

  let searchTimer = null;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = e.target.value.trim();
      loadArticles();
    }, 300);
  });

  $("#unread-only").addEventListener("change", (e) => {
    state.unread = e.target.checked;
    loadArticles();
  });

  const autoBox = $("#auto-translate");
  if (autoBox) {
    autoBox.checked = autoTranslate;
    autoBox.addEventListener("change", (e) => {
      autoTranslate = e.target.checked;
      localStorage.setItem("wrss-auto-translate", autoTranslate ? "1" : "0");
      if (autoTranslate) {
        // Switched back on: offer the current page its run immediately.
        scheduleAutoTranslate([...$("#articles").children]);
      } else {
        autoRun++;
        clearTimeout(autoTimer);
        setAutoStatus("");
      }
    });
  }

  $("#btn-readall").addEventListener("click", async () => {
    if (!confirm("Mark all articles in this view as read?")) return;
    try {
      const body = {};
      if (state.kind) body.kind = state.kind;
      if (state.cat) body.cat = state.cat;
      if (state.region) body.region = state.region;
      if (state.country) body.country = state.country;
      if (state.q) body.q = state.q;
      const r = await api("/api/articles/read_all", { method: "POST", body: JSON.stringify(body) });
      toast(`Marked ${r.marked} articles read`, "good");
      loadArticles(); refreshBadges();
    } catch (e) { toast(e.message, "err"); }
  });

  $("#btn-refresh").addEventListener("click", async () => {
    try {
      const r = await api("/api/refresh", { method: "POST" });
      toast(r.busy ? "Refresh already running…" : "Refreshing feeds…");
      setTimeout(() => { loadArticles(); refreshBadges(); loadStatus(); }, 6000);
    } catch (e) { toast(e.message, "err"); }
  });

  $("#btn-more").addEventListener("click", () => loadArticles(true));
  $("#btn-manage").addEventListener("click", openModal);
  $("#btn-admin").addEventListener("click", openAdmin);
  $("#btn-admin-close").addEventListener("click", () => $("#admin").classList.add("hidden"));
  $("#admin").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) $("#admin").classList.add("hidden");
  });
  $("#admin-save").addEventListener("click", saveAdmin);
  $("#ad-now").addEventListener("click", translateNow);
  $("#ad-purge").addEventListener("click", (e) => purgePending(e.currentTarget));
  $("#btn-modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) $("#modal").classList.add("hidden");
  });
  $("#btn-reader-close").addEventListener("click", () => $("#reader").classList.add("hidden"));
  $("#reader").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) $("#reader").classList.add("hidden");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    $("#reader").classList.add("hidden");
    $("#modal").classList.add("hidden");
    $("#admin").classList.add("hidden");
  });

  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#modal-msg");
    try {
      await api("/api/feeds", {
        method: "POST",
        body: JSON.stringify({
          name: $("#add-name").value,
          url: $("#add-url").value,
          kind: $("#add-kind").value,
          category: $("#add-cat").value,
          region: $("#add-region").value,
          country: $("#add-country").value,
        }),
      });
      $("#add-form").reset();
      msg.textContent = "Feed added — fetching…";
      toast("Feed added", "good");
      loadFeeds(); refreshBadges();
      setTimeout(() => { loadFeeds(); loadArticles(); msg.textContent = ""; }, 8000);
    } catch (err) { msg.textContent = err.message; }
  });

  /* theme */
  const themeBtn = $("#btn-theme");
  const applyTheme = (t) => {
    document.documentElement.dataset.theme = t;
    themeBtn.textContent = t === "dark" ? "☾" : "☀";
    localStorage.setItem("wrss-theme", t);
  };
  applyTheme(localStorage.getItem("wrss-theme") || "dark");
  themeBtn.addEventListener("click", () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

  /* mobile sidebar */
  const closeSidebarMobile = () => { $("#sidebar").classList.remove("open"); $("#scrim").classList.add("hidden"); };
  $("#btn-menu").addEventListener("click", () => {
    $("#sidebar").classList.add("open");
    $("#scrim").classList.remove("hidden");
  });
  $("#scrim").addEventListener("click", closeSidebarMobile);

  /* ---------- boot ---------- */
  /* The aggregated feed URL, for pasting into something that can hold a
     cookie. It is only reachable from a signed-in browser now -- there is no
     ?auth= token to hand out, so a reader app with no cookie support cannot
     subscribe to it. */
  $("#feed-url").textContent = `${location.origin}${PFX}/feed`;
  bindNav();
  // Status first: it carries translate_enabled, which decides whether the
  // translate button is rendered at all. Loading articles first would paint a
  // button the paused service would then refuse.
  loadStatus().then(loadArticles);
  loadBootstrap();

  /* gentle auto-refresh: badges + list only when sitting at the top */
  pollTimer = setInterval(() => {
    refreshBadges();
    loadStatus();
    if (window.scrollY < 40 && !state.q && !state.unread && state.offset <= 50) {
      loadArticles();
    }
  }, 45000);
})();
