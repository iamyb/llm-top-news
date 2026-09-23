"""llm-top-news 静态站点生成。

用法:
    python pipeline/site.py     # 读取 data/raw/*.json → 生成 site/ 目录

输出:
    site/index.html             首页（只显示最新一天）
    site/archive.html           归档页（所有历史日期, 点击进入当天）
    site/daily/YYYY-MM-DD.html  每天一个独立页（可单独分享）
    site/style.css / app.js     共享样式与交互
    site/tailwind.js            Tailwind 本地副本（下载失败时回退 CDN）

设计:
    - Tailwind CSS（卡片/网格/暗色模式）, 零构建链, 可 GitHub Pages 部署
    - 卡片分层: Featured 大卡（当日跨源热点 Top1）→ 有图 Reddit 中卡（占 2 列）→ 普通小卡
    - 交互: 关键词搜索 / 源筛选 / 暗色模式（跟随系统 + 手动切换）
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
SITE_DIR = ROOT / "site"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import aggregate  # noqa: E402

TAILWIND_URL = "https://cdn.tailwindcss.com"


# ───────────────────────── 数据 ─────────────────────────

def load_all_snapshots() -> dict[str, dict]:
    """读取所有 raw 快照, 返回 {date: raw}。"""
    snaps: dict[str, dict] = {}
    for f in sorted(RAW_DIR.glob("*.json")):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ! 跳过 {f.name}: {e}", file=sys.stderr)
            continue
        if isinstance(raw, dict) and "date" in raw:
            snaps[raw["date"]] = raw
    return snaps


def build_cross(snap: dict) -> list[dict]:
    """跨源热点: 同题出现在 ≥2 个源。"""
    items = snap.get("github", []) + snap.get("hn", []) + snap.get("reddit", [])
    out = []
    for s in aggregate(items):
        if len(s["sources"]) < 2:
            continue
        out.append({
            "title": s["title"],
            "sources": s["sources"],
            "links": [
                {"source": i["source"],
                 "title": i.get("title") or i.get("full_name"),
                 "url": i.get("url") or i.get("reddit_url") or i.get("hn_url")}
                for i in s["items"]
            ],
        })
    return out


def snapshot_payload(snap: dict) -> dict:
    return {
        "github": snap.get("github", []),
        "hn": snap.get("hn", []),
        "reddit": snap.get("reddit", []),
        "cross": build_cross(snap),
    }


# ───────────────────────── Tailwind 本地化 ─────────────────────────

def ensure_tailwind() -> str:
    """优先用本地 tailwind.js; 缺失则尝试下载; 失败回退 CDN URL。"""
    local = SITE_DIR / "tailwind.js"
    if local.exists() and local.stat().st_size > 100_000:
        return "tailwind.js"
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(TAILWIND_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            local.write_bytes(resp.read())
        print(f"  ↓ tailwind.js → {local.relative_to(ROOT)}")
        return "tailwind.js"
    except Exception as e:  # noqa: BLE001
        print(f"  ! tailwind 下载失败（{e}）, 回退 CDN", file=sys.stderr)
        return TAILWIND_URL


# ───────────────────────── 模板 ─────────────────────────

STYLE_CSS = """
/* llm-top-news 自定义样式（Tailwind 之外的补充） */
html { scroll-behavior: smooth; }
::-webkit-scrollbar { width: 10px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(120,130,150,.35); border-radius: 6px; }
::-webkit-scrollbar-track { background: transparent; }
.card-hover { transition: transform .15s ease, box-shadow .15s ease; }
.card-hover:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,.10); }
.dark .card-hover:hover { box-shadow: 0 8px 24px rgba(0,0,0,.5); }
.filter-btn.active { border-color: #3b82f6; background: rgba(59,130,246,.08); color: #3b82f6; font-weight: 600; }
.dark .filter-btn.active { color: #60a5fa; }
.archive-row .row-arrow { transition: transform .15s ease, color .15s ease; }
.archive-row:hover .row-arrow { transform: translateX(4px); color: #3b82f6; }
"""

APP_JS = r"""
// llm-top-news 站点交互: 主题 / 搜索 / 源筛选 / 渲染
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ── 主题按钮（所有页面都有）──
  const themeBtn = $("#theme-btn");
  if (themeBtn) themeBtn.onclick = () => {
    const dark = document.documentElement.classList.toggle("dark");
    localStorage.setItem("theme", dark ? "dark" : "light");
  };

  const dataEl = document.getElementById("data");
  if (!dataEl) return;  // 归档页无数据, 只挂主题按钮

  const SNAP = JSON.parse(dataEl.textContent).snapshot;
  let curSrc = "all", curQ = "", curSub = "";
  const featuredUrls = new Set();  // Featured 大卡已展示的条目, 网格中排除

  const SRC_LABEL = { github: "GitHub", hn: "Hacker News", reddit: "Reddit" };

  function matchQ(it) {
    if (!curQ) return true;
    const hay = [it.title, it.full_name, it.description, it.selftext,
      (it.topics || []).join(" "), (it.matched_keywords || []).join(" "),
      it.subreddit, it.language].filter(Boolean).join(" ").toLowerCase();
    return curQ.split(/\s+/).every((w) => hay.includes(w));
  }

  // ── 卡片 ──
  function githubCard(it) {
    const owner = (it.full_name || "").split("/")[0];
    const avatar = owner ? `https://github.com/${owner}.png?size=68` : "";
    const lang = it.language ? `<span class="text-xs px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">${esc(it.language)}</span>` : "";
    const topics = (it.topics || []).slice(0, 3).map((t) =>
      `<span class="text-xs px-2 py-0.5 rounded-full bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-300">${esc(t)}</span>`).join("");
    return `<div class="card-hover h-[180px] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4 flex flex-col gap-2 overflow-hidden">
      <div class="flex items-center gap-2.5 min-w-0">
        ${avatar ? `<img src="${esc(avatar)}" class="w-9 h-9 rounded-full bg-slate-100 dark:bg-slate-800 shrink-0" onerror="this.remove()">` : ""}
        <a href="${esc(it.url)}" target="_blank" rel="noopener"
           class="font-semibold text-sm truncate hover:text-blue-500">${esc(it.full_name)}</a>
      </div>
      ${it.description ? `<p class="text-[13px] text-slate-500 dark:text-slate-400 line-clamp-2">${esc(it.description)}</p>` : ""}
      <div class="mt-auto flex items-center gap-2 flex-wrap text-xs text-slate-500 dark:text-slate-400">
        <span class="font-semibold text-slate-700 dark:text-slate-200">Stars ${it.stars ?? 0}</span>${lang}${topics}
      </div>
    </div>`;
  }

  function hnCard(it) {
    let domain = "";
    try { domain = new URL(it.url).hostname; } catch (e) {}
    const fav = domain ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32` : "";
    const kws = (it.matched_keywords || []).map((k) =>
      `<span class="text-xs px-2 py-0.5 rounded-full bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-300">${esc(k)}</span>`).join("");
    return `<div class="card-hover h-[180px] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4 flex flex-col gap-2 overflow-hidden">
      <div class="flex items-start gap-2.5 min-w-0">
        ${fav ? `<img src="${esc(fav)}" class="w-5 h-5 rounded mt-0.5 shrink-0" onerror="this.remove()">` : ""}
        <a href="${esc(it.url)}" target="_blank" rel="noopener"
           class="font-semibold text-sm leading-snug hover:text-blue-500 line-clamp-2">${esc(it.title)}</a>
      </div>
      ${domain ? `<div class="text-xs text-slate-400 dark:text-slate-500 truncate">${esc(domain)}</div>` : ""}
      <div class="mt-auto flex items-center gap-2 flex-wrap text-xs text-slate-500 dark:text-slate-400">
        <span class="font-semibold text-slate-700 dark:text-slate-200">Points ${it.points ?? 0}</span>
        <span>${it.num_comments ?? 0} comments</span>
        <a href="${esc(it.hn_url)}" target="_blank" rel="noopener" class="text-blue-500 hover:underline">HN</a>
        ${kws}
      </div>
    </div>`;
  }

  function redditCard(it) {
    const meta = `<div class="mt-auto flex items-center gap-2 flex-wrap text-xs text-slate-500 dark:text-slate-400">
        <span class="px-2 py-0.5 rounded-full bg-orange-50 dark:bg-orange-950 text-orange-600 dark:text-orange-300">r/${esc(it.subreddit)}</span>
        <a href="${esc(it.reddit_url)}" target="_blank" rel="noopener" class="text-blue-500 hover:underline">Post</a>
      </div>`;
    const excerpt = it.selftext ? `<p class="text-[13px] text-slate-500 dark:text-slate-400 line-clamp-2">${esc(it.selftext)}</p>` : "";
    if (it.thumbnail) {
      return `<div class="card-hover h-[180px] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden flex flex-col">
        <img src="${esc(it.thumbnail)}" class="w-full h-24 object-cover bg-slate-100 dark:bg-slate-800 shrink-0"
             onerror="this.remove()">
        <div class="p-4 flex flex-col gap-2 flex-1 min-h-0">
          <a href="${esc(it.reddit_url)}" target="_blank" rel="noopener"
             class="font-semibold text-sm leading-snug hover:text-blue-500 line-clamp-1">${esc(it.title)}</a>
          ${meta}
        </div>
      </div>`;
    }
    return `<div class="card-hover h-[180px] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4 flex flex-col gap-2 overflow-hidden">
      <a href="${esc(it.reddit_url)}" target="_blank" rel="noopener"
         class="font-semibold text-sm leading-snug hover:text-blue-500 line-clamp-2">${esc(it.title)}</a>
      ${excerpt}
      ${meta}
    </div>`;
  }

  const CARDS = { github: githubCard, hn: hnCard, reddit: redditCard };

  // ── Featured 大卡（当日跨源热点 Top1）──
  function featuredHTML() {
    const top = SNAP.cross[0];
    if (!top) return "";
    top.links.forEach((l) => featuredUrls.add(l.url));
    return `<div class="card-hover bg-gradient-to-br from-rose-50 to-white dark:from-rose-950/40 dark:to-slate-900 border border-rose-200 dark:border-rose-900 rounded-2xl p-5">
      <div class="flex items-center gap-2 text-xs font-semibold text-rose-500 mb-2">
        <span class="px-2 py-0.5 rounded-full bg-rose-500 text-white">Cross-source ×${top.sources.length}</span>
      </div>
      <div class="text-xl font-bold leading-snug mb-3">${esc(top.title)}</div>
      <div class="flex gap-2 flex-wrap">${top.links.map((l) =>
        `<a href="${esc(l.url)}" target="_blank" rel="noopener"
           class="text-xs px-3 py-1.5 rounded-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 hover:border-blue-400">${SRC_LABEL[l.source] || l.source}</a>`).join("")}</div>
    </div>`;
  }

  // ── 渲染 ──
  function render() {
    // 跨源热点
    const cross = (SNAP.cross || []).filter((s) =>
      !curQ || s.title.toLowerCase().includes(curQ));
    $("#cross").innerHTML = cross.length ?
      `<div class="space-y-3 mb-6">${cross.slice(0, 5).map((s) =>
        `<div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 border-l-4 border-l-rose-500 rounded-xl px-4 py-3">
          <div class="font-semibold text-sm mb-1.5"><span class="text-rose-500 font-bold mr-1.5">×${s.sources.length}</span>${esc(s.title)}</div>
          <div class="flex gap-3 flex-wrap text-xs">${s.links.map((l) =>
            `<a href="${esc(l.url)}" target="_blank" rel="noopener" class="text-blue-500 hover:underline">${SRC_LABEL[l.source] || l.source}</a>`).join("")}</div>
        </div>`).join("")}</div>` : "";

    // Featured（不参与搜索过滤, 是当日门面）
    $("#featured").innerHTML = featuredHTML();

    // sub 筛选条: 仅当 Reddit 在视图内时显示
    const subWrap = $("#sub-filters");
    if (subWrap) subWrap.style.display = (curSrc === "all" || curSrc === "reddit") ? "" : "none";

    // 分源网格
    const srcs = curSrc === "all" ? ["github", "hn", "reddit"] : [curSrc];
    let html = "";
    for (const src of srcs) {
      const items = (SNAP[src] || []).filter(matchQ)
        .filter((it) => src !== "reddit" || !curSub || it.subreddit === curSub)
        .filter((it) => !featuredUrls.has(it.url) && !featuredUrls.has(it.reddit_url));
      if (!items.length) continue;
      html += `<section class="mb-8">
        <h2 class="text-base font-bold mb-3">${SRC_LABEL[src]}
          <span class="text-slate-400 font-normal text-sm">(${items.length})</span></h2>
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 auto-rows-fr gap-4">${items.map(CARDS[src]).join("")}</div>
      </section>`;
    }
    $("#sections").innerHTML = html || `<div class="text-center text-slate-400 py-16">No matching items</div>`;
  }

  // ── 工具条 ──
  const search = $("#search");
  if (search) search.oninput = (e) => { curQ = e.target.value.trim().toLowerCase(); render(); };
  document.querySelectorAll(".filter-btn:not(.sub-btn)").forEach((b) => (b.onclick = () => {
    document.querySelectorAll(".filter-btn:not(.sub-btn)").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    curSrc = b.dataset.src;
    render();
  }));

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
"""

PAGE_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script>
(function(){var t=localStorage.getItem("theme");
if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
if(t==="dark")document.documentElement.classList.add("dark");})();
</script>
<script src="__TAILWIND__"></script>
<script>tailwind.config={darkMode:"class"}</script>
<link rel="stylesheet" href="__BASE__style.css">
</head>
<body class="bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100 antialiased min-h-screen">
<div class="max-w-6xl mx-auto px-4 py-6">
  <header class="flex items-center gap-3 flex-wrap mb-6">
    <a href="__BASE__index.html" class="text-xl font-extrabold tracking-tight">LLM Top News<span class="text-blue-500">.</span></a>
    <nav class="flex gap-1 text-sm">
      <a href="__BASE__index.html" class="px-3 py-1.5 rounded-lg hover:bg-slate-200/60 dark:hover:bg-slate-800 __HOME_ACTIVE__">Home</a>
      <a href="__BASE__archive.html" class="px-3 py-1.5 rounded-lg hover:bg-slate-200/60 dark:hover:bg-slate-800">Archive</a>
    </nav>
    <div class="flex-1"></div>
    __DAY_NAV__
    <button id="theme-btn" class="w-9 h-9 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 hover:bg-slate-100 dark:hover:bg-slate-800 flex items-center justify-center" title="Toggle theme">
      <svg class="w-4.5 h-4.5" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
  </header>

  <div class="flex gap-2.5 flex-wrap items-center mb-6">
    <input id="search" type="search" placeholder="Search titles / descriptions / keywords…"
           class="flex-1 min-w-[220px] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 dark:focus:ring-blue-950">
    <button class="filter-btn active px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-src="all">All</button>
    <button class="filter-btn px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-src="github">GitHub</button>
    <button class="filter-btn px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-src="hn">HN</button>
    <button class="filter-btn px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-src="reddit">Reddit</button>
  </div>
  <div id="sub-filters" class="flex gap-2 flex-wrap items-center mb-6"></div>

  <div id="cross"></div>
  <div id="featured" class="space-y-4 mb-8"></div>
  <div id="sections"></div>

  <footer class="text-center text-xs text-slate-400 mt-10">llm-top-news auto-generated · data as of __GENERATED__</footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script src="__BASE__app.js"></script>
</body>
</html>
"""

ARCHIVE_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archive · LLM Top News</title>
<script>
(function(){var t=localStorage.getItem("theme");
if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
if(t==="dark")document.documentElement.classList.add("dark");})();
</script>
<script src="__TAILWIND__"></script>
<script>tailwind.config={darkMode:"class"}</script>
<link rel="stylesheet" href="style.css">
</head>
<body class="bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100 antialiased min-h-screen">
<div class="max-w-3xl mx-auto px-4 py-6">
  <header class="flex items-center gap-3 mb-8">
    <a href="index.html" class="text-xl font-extrabold tracking-tight">LLM Top News<span class="text-blue-500">.</span></a>
    <nav class="flex gap-1 text-sm">
      <a href="index.html" class="px-3 py-1.5 rounded-lg hover:bg-slate-200/60 dark:hover:bg-slate-800">Home</a>
      <span class="px-3 py-1.5 rounded-lg bg-slate-200/60 dark:bg-slate-800 font-semibold">Archive</span>
    </nav>
    <div class="flex-1"></div>
    <button id="theme-btn" class="w-9 h-9 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 hover:bg-slate-100 dark:hover:bg-slate-800 flex items-center justify-center" title="Toggle theme">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
  </header>

  <div class="mb-8">
    <h1 class="text-3xl font-extrabold tracking-tight">Archive<span class="text-blue-500">.</span></h1>
    <p class="text-sm text-slate-500 dark:text-slate-400 mt-1.5">Every daily snapshot, newest first.</p>
  </div>
  <div class="grid grid-cols-3 gap-3 mb-8">
    <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl px-4 py-3">
      <div class="text-2xl font-extrabold">__NDAYS__</div>
      <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Days tracked</div>
    </div>
    <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl px-4 py-3">
      <div class="text-2xl font-extrabold">__NITEMS__</div>
      <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Items collected</div>
    </div>
    <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl px-4 py-3">
      <div class="text-2xl font-extrabold text-rose-500">__NCROSS__</div>
      <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Cross-source hits</div>
    </div>
  </div>
  <div class="space-y-3">
__ROWS__
  </div>
</div>
<script src="app.js"></script>
</body>
</html>
"""


def _day_nav(base: str, prev: str | None, next_: str | None) -> str:
    btn = ("px-3 py-1.5 rounded-lg text-sm border border-slate-200 dark:border-slate-700 "
           "bg-white dark:bg-slate-900 hover:bg-slate-100 dark:hover:bg-slate-800")
    parts = []
    if prev:
        parts.append(f'<a href="{base}daily/{prev}.html" class="{btn}">← Prev</a>')
    if next_:
        parts.append(f'<a href="{base}daily/{next_}.html" class="{btn}">Next →</a>')
    return " ".join(parts)


def render_day(date: str, snap: dict, base: str, dates: list[str],
               is_index: bool, tailwind: str) -> str:
    i = dates.index(date)
    prev = dates[i - 1] if i > 0 else None
    next_ = dates[i + 1] if i < len(dates) - 1 else None
    title = "LLM Top News" if is_index else f"{date} · LLM Top News"
    data_js = json.dumps(
        {"date": date, "snapshot": snapshot_payload(snap)},
        ensure_ascii=False).replace("</", "<\\/")
    return (PAGE_TMPL
            .replace("__TITLE__", title)
            .replace("__TAILWIND__", tailwind)
            .replace("__BASE__", base)
            .replace("__HOME_ACTIVE__",
                     "bg-slate-200/60 dark:bg-slate-800 font-semibold" if is_index else "")
            .replace("__DAY_NAV__", _day_nav(base, prev, next_))
            .replace("__GENERATED__", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            .replace("__DATA__", data_js))


def render_archive(dates: list[str], snaps: dict[str, dict], tailwind: str) -> str:
    payloads = {d: snapshot_payload(snaps[d]) for d in dates}
    n_items = sum(len(s["github"]) + len(s["hn"]) + len(s["reddit"]) for s in payloads.values())
    n_cross = sum(len(s["cross"]) for s in payloads.values())

    pill = {
        "github": ("bg-blue-50 text-blue-600 dark:bg-blue-950 dark:text-blue-300", "bg-blue-500"),
        "hn": ("bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300", "bg-amber-500"),
        "reddit": ("bg-orange-50 text-orange-600 dark:bg-orange-950 dark:text-orange-300", "bg-orange-500"),
    }
    label = {"github": "GitHub", "hn": "HN", "reddit": "Reddit"}

    rows = []
    for d in reversed(dates):
        s = payloads[d]
        dt = datetime.strptime(d, "%Y-%m-%d")
        counts = {k: len(s[k]) for k in ("github", "hn", "reddit")}
        total = sum(counts.values()) or 1
        pills = "".join(
            f'<span class="px-2 py-0.5 rounded-full font-semibold {pill[k][0]}">{label[k]} {counts[k]}</span>'
            for k in ("github", "hn", "reddit"))
        bar = "".join(
            f'<div class="{pill[k][1]}" style="width:{counts[k] / total * 100:.1f}%"></div>'
            for k in ("github", "hn", "reddit"))
        cross = (f'<span class="px-2 py-0.5 rounded-full font-semibold '
                 f'bg-rose-50 text-rose-600 dark:bg-rose-950 dark:text-rose-300">'
                 f'{len(s["cross"])} cross-source</span>') if s["cross"] else ""
        rows.append(f"""    <a href="daily/{d}.html"
       class="archive-row card-hover flex items-center gap-4 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4">
      <div class="w-14 shrink-0 text-center rounded-xl bg-gradient-to-b from-blue-500 to-indigo-600 text-white py-2 shadow-sm">
        <div class="text-xl font-extrabold leading-none">{dt.day:02d}</div>
        <div class="text-[10px] uppercase tracking-widest mt-1 opacity-90">{dt.strftime('%b')}</div>
      </div>
      <div class="min-w-0 flex-1">
        <div class="flex items-baseline gap-2">
          <span class="font-mono font-bold">{d}</span>
          <span class="text-xs text-slate-400 dark:text-slate-500">{dt.strftime('%A')}</span>
        </div>
        <div class="flex gap-1.5 mt-2 text-xs flex-wrap">{pills}{cross}</div>
        <div class="h-1.5 rounded-full overflow-hidden flex bg-slate-100 dark:bg-slate-800 mt-2.5">
          {bar}
        </div>
      </div>
      <span class="row-arrow text-slate-300 dark:text-slate-600 text-lg font-bold">→</span>
    </a>""")
    return (ARCHIVE_TMPL
            .replace("__TAILWIND__", tailwind)
            .replace("__NDAYS__", str(len(dates)))
            .replace("__NITEMS__", str(n_items))
            .replace("__NCROSS__", str(n_cross))
            .replace("__ROWS__", "\n".join(rows)))


# ───────────────────────── 主流程 ─────────────────────────

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not RAW_DIR.exists() or not list(RAW_DIR.glob("*.json")):
        sys.exit("✗ data/raw 下没有快照, 先跑 python pipeline/collect.py")

    snaps = load_all_snapshots()
    dates = sorted(snaps.keys())
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "daily").mkdir(exist_ok=True)
    (SITE_DIR / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    (SITE_DIR / "app.js").write_text(APP_JS, encoding="utf-8")
    tailwind = ensure_tailwind()

    # 首页 = 最新一天
    (SITE_DIR / "index.html").write_text(
        render_day(dates[-1], snaps[dates[-1]], "", dates, True, tailwind),
        encoding="utf-8")
    # 每天独立页
    for d in dates:
        (SITE_DIR / "daily" / f"{d}.html").write_text(
            render_day(d, snaps[d], "../", dates, False, tailwind),
            encoding="utf-8")
    # 归档页
    (SITE_DIR / "archive.html").write_text(
        render_archive(dates, snaps, tailwind), encoding="utf-8")

    total = sum(len(s.get("github", [])) + len(s.get("hn", [])) + len(s.get("reddit", []))
                for s in snaps.values())
    print(f"✓ {len(dates)} 天 / {total} 条 → {SITE_DIR.relative_to(ROOT)}/")
    print(f"  首页: file:///{(SITE_DIR / 'index.html').as_posix()}")
    print(f"  归档: file:///{(SITE_DIR / 'archive.html').as_posix()}")


if __name__ == "__main__":
    main()
