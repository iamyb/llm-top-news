
// llm-top-news 站点交互: 主题 / 搜索 / 源筛选 / 渲染
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const dataEl = document.getElementById("data");
  if (!dataEl) return;  // 归档页无数据

  const payload = JSON.parse(dataEl.textContent).snapshot;
  const SNAP = payload.sources || {};
  const SECTIONS = payload.sections || {};
  const SOURCE_META = payload.source_meta || [];
  const SECTION_META = payload.section_meta || [];
  const SOURCE_BY_KEY = Object.fromEntries(SOURCE_META.map((meta) => [meta.key, meta]));
  const SECTION_BY_KEY = Object.fromEntries(SECTION_META.map((meta) => [meta.key, meta]));
  let curSection = "all", curSrc = "all", curQ = "", curSub = "";

  function matchQ(it) {
    if (!curQ) return true;
    const hay = [it.title, it.full_name, it.description, it.summary, it.selftext,
      (it.tags || []).join(" "), (it.topics || []).join(" "), (it.matched_keywords || []).join(" "),
      it.subreddit, it.language].filter(Boolean).join(" ").toLowerCase();
    return curQ.split(/\s+/).every((w) => hay.includes(w));
  }

  // ── 卡片 ──
  function githubCard(it) {
    const owner = (it.full_name || "").split("/")[0];
    const avatar = owner ? `https://github.com/${owner}.png?size=68` : "";
    const lang = it.language ? ` · ${esc(it.language)}` : "";
    const topics = (it.topics || []).slice(0, 3).map((t) => `#${esc(t)}`).join(" ");
    return `<article class="news-item source-github">
      <div class="news-rail"></div><div class="news-body">
        <div class="flex items-start gap-3">
          ${avatar ? `<img src="${esc(avatar)}" class="w-8 h-8 rounded-full bg-slate-100 dark:bg-slate-800 shrink-0" onerror="this.remove()">` : ""}
          <div class="min-w-0 flex-1">
            <div class="source-label mb-1">GitHub · ${it.github_kind === "active" ? "Active" : "New"}</div>
            <a href="${esc(it.url)}" target="_blank" rel="noopener" class="news-title line-clamp-2">${esc(it.full_name)}</a>
            ${it.description ? `<p class="news-summary mt-1.5 line-clamp-2">${esc(it.description)}</p>` : ""}
            <div class="news-meta mt-2">★ ${it.stars ?? 0}${lang}${topics ? ` · ${topics}` : ""}</div>
          </div>
        </div>
      </div>
    </article>`;
  }

  function hnCard(it) {
    let domain = "";
    try { domain = new URL(it.url).hostname; } catch (e) {}
    const fav = domain ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32` : "";
    const kws = (it.matched_keywords || []).slice(0, 3).map((k) => `#${esc(k)}`).join(" ");
    return `<article class="news-item source-hn">
      <div class="news-rail"></div><div class="news-body">
        <div class="flex items-start gap-3">
          ${fav ? `<img src="${esc(fav)}" class="w-5 h-5 rounded mt-1 shrink-0" onerror="this.remove()">` : ""}
          <div class="min-w-0 flex-1">
            <div class="source-label mb-1">Hacker News</div>
            <a href="${esc(it.url)}" target="_blank" rel="noopener" class="news-title line-clamp-2">${esc(it.title)}</a>
            <div class="news-meta mt-2">${domain ? `${esc(domain)} · ` : ""}▲ ${it.points ?? 0} · ${it.num_comments ?? 0} comments${kws ? ` · ${kws}` : ""}</div>
          </div>
        </div>
      </div>
    </article>`;
  }

  function redditCard(it) {
    const excerpt = it.selftext ? `<p class="news-summary mt-1.5 line-clamp-2">${esc(it.selftext)}</p>` : "";
    const thumb = it.thumbnail ? `<img src="${esc(it.thumbnail)}" class="news-thumb shrink-0" onerror="this.remove()">` : "";
    return `<article class="news-item source-reddit">
      <div class="news-rail"></div><div class="news-body">
        <div class="flex items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="source-label mb-1">Reddit · r/${esc(it.subreddit)}</div>
            <a href="${esc(it.reddit_url)}" target="_blank" rel="noopener" class="news-title line-clamp-2">${esc(it.title)}</a>
            ${excerpt}
            <div class="news-meta mt-2">${it.score != null ? `▲ ${it.score} · ` : ""}${it.num_comments != null ? `${it.num_comments} comments · ` : ""}Open post ↗</div>
          </div>${thumb}
        </div>
      </div>
    </article>`;
  }

  function genericCard(it, meta) {
    const tags = (it.tags || []).slice(0, 3).map((tag) => `#${esc(tag)}`).join(" ");
    return `<article class="news-item" style="--source-color: ${esc(meta.color)}">
      <div class="news-rail"></div><div class="news-body">
        <div class="source-label mb-1">${esc(meta.label)}</div>
        <a href="${esc(it.url)}" target="_blank" rel="noopener" class="news-title line-clamp-2">${esc(it.title)}</a>
        ${it.summary ? `<p class="news-summary mt-1.5 line-clamp-2">${esc(it.summary)}</p>` : ""}
        ${tags ? `<div class="news-meta mt-2">${tags}</div>` : ""}
      </div>
    </article>`;
  }

  function hfPapersCard(it) {
    const authors = (it.authors || []).slice(0, 3).join(", ");
    const more = (it.authors || []).length > 3 ? " et al." : "";
    return `<article class="news-item source-hf_papers">
      <div class="news-rail"></div><div class="news-body">
        <div class="flex items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="source-label mb-1">HF Papers</div>
            <a href="${esc(it.url)}" target="_blank" rel="noopener" class="news-title line-clamp-2">${esc(it.title)}</a>
            ${it.summary ? `<p class="news-summary mt-1.5 line-clamp-2">${esc(it.summary)}</p>` : ""}
            <div class="news-meta mt-2">👍 ${it.upvotes ?? 0} · 💬 ${it.num_comments ?? 0}${authors ? ` · ${esc(authors)}${more}` : ""}</div>
          </div>
        </div>
      </div>
    </article>`;
  }

  const CARDS = { github_new: githubCard, github_active: githubCard, hn: hnCard, reddit: redditCard, hf_papers: hfPapersCard };

  // ── 渲染 ──
  function render() {
    // sub 筛选条: 仅当 Reddit 在视图内时显示
    const subWrap = $("#sub-filters");
    const sourceWrap = $("#source-filters");
    if (sourceWrap) sourceWrap.style.display = curSection === "all" ? "none" : "flex";
    if (subWrap) subWrap.style.display = curSection !== "all" && curSrc === "reddit" && SNAP.reddit ? "" : "none";

    const sectionKeys = curSection === "all" ? SECTION_META.map((meta) => meta.key) : [curSection];
    let html = "";
    for (const section of sectionKeys) {
      const sectionItems = (SECTIONS[section] || []).filter(matchQ)
        .filter((it) => curSrc === "all" || it.source === curSrc)
        .filter((it) => curSrc !== "reddit" || !curSub || it.subreddit === curSub);
      if (!sectionItems.length) continue;
      const sectionMeta = SECTION_BY_KEY[section] || { key: section, label: section };
      const configuredSources = sectionMeta.sources || [];
      const groupKeys = (configuredSources.length ? configuredSources : [...new Set(sectionItems.map((it) => it.source))])
        .filter((source) => sectionItems.some((it) => it.source === source));
      const groups = groupKeys.map((source) => {
        const items = sectionItems.filter((it) => it.source === source);
        const meta = SOURCE_BY_KEY[source] || { key: source, label: source, color: "#64748b" };
        const card = CARDS[source] || ((item) => genericCard(item, meta));
        const heading = groupKeys.length > 1 ? `<div class="source-heading"><h3>${esc(meta.label)} (${items.length})</h3></div>` : "";
        return `<div class="source-group">${heading}<div class="news-list">${items.map(card).join("")}</div></div>`;
      }).join("");
      html += `<section>
        <div class="section-heading"><h2>${esc(sectionMeta.label)} (${sectionItems.length})</h2></div>
        ${groups}
      </section>`;
    }
    $("#sections").innerHTML = html || `<div class="text-center text-slate-400 py-16">No matching items</div>`;
  }

  // ── 工具条 ──
  function renderSourceFilters() {
    const filterWrap = $("#source-filters");
    if (!filterWrap) return;
    const sectionMeta = SECTION_BY_KEY[curSection];
    const keys = sectionMeta?.sources?.length
      ? sectionMeta.sources
      : [...new Set((SECTIONS[curSection] || []).map((it) => it.source))];
    filterWrap.innerHTML = `<span class="text-xs text-slate-400 self-center">Source</span><button class="source-filter-btn filter-btn active px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-src="all">All sources</button>` + keys.map((key) => {
      const meta = SOURCE_BY_KEY[key] || { key, label: key, color: "#64748b" };
      return `<button class="source-filter-btn filter-btn px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-src="${esc(meta.key)}">${esc(meta.label)}</button>`;
    }).join("");
  }
  const sectionFilterWrap = $("#section-filters");
  if (sectionFilterWrap) {
    const totalItems = Object.values(SECTIONS).reduce((total, items) => total + items.length, 0);
    const allButton = sectionFilterWrap.querySelector('[data-section="all"]');
    if (allButton) allButton.textContent = `All sections (${totalItems})`;
    sectionFilterWrap.innerHTML += SECTION_META.filter((meta) => (SECTIONS[meta.key] || []).length > 0).map((meta) =>
      `<button class="section-filter-btn filter-btn px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-section="${esc(meta.key)}">${esc(meta.label)} (${(SECTIONS[meta.key] || []).length})</button>`).join("");
  }
  renderSourceFilters();
  const search = $("#search");
  if (search) search.oninput = (e) => { curQ = e.target.value.trim().toLowerCase(); render(); };
  document.querySelectorAll(".section-filter-btn").forEach((b) => (b.onclick = () => {
    document.querySelectorAll(".section-filter-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    curSection = b.dataset.section;
    curSrc = "all";
    renderSourceFilters();
    render();
  }));
  const filterWrap = $("#source-filters");
  if (filterWrap) filterWrap.onclick = (e) => {
    const button = e.target.closest(".source-filter-btn");
    if (!button) return;
    filterWrap.querySelectorAll(".source-filter-btn").forEach((x) => x.classList.remove("active"));
    button.classList.add("active");
    curSrc = button.dataset.src;
    render();
  };

  // Reddit sub 筛选（多 sub 时才显示）
  const subWrap = $("#sub-filters");
  if (subWrap) {
    const subs = [...new Set((SNAP.reddit || []).map((it) => it.subreddit).filter(Boolean))];
    if (subs.length > 1) {
      subWrap.innerHTML = subs.map((s) =>
        `<button class="filter-btn sub-btn px-3 py-1.5 rounded-full text-xs border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-sub="${esc(s)}">r/${esc(s)}</button>`).join("");
      subWrap.querySelectorAll(".sub-btn").forEach((b) => (b.onclick = () => {
        curSub = (curSub === b.dataset.sub) ? "" : b.dataset.sub;
        subWrap.querySelectorAll(".sub-btn").forEach((x) => x.classList.toggle("active", x.dataset.sub === curSub));
        render();
      }));
    } else subWrap.remove();
  }

  render();
})();
