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

import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
SITE_DIR = ROOT / "site"
CONFIG_FILE = ROOT / "config.yaml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import aggregate  # noqa: E402

TAILWIND_URL = "https://cdn.tailwindcss.com"


# ───────────────────────── 数据 ─────────────────────────

DEFAULT_SOURCE_CONFIG = {
  "github": {"label": "GitHub", "color": "#2563eb", "order": 1},
  "hf_papers": {"label": "HF Papers", "color": "#8b5cf6", "order": 2},
  "hn": {"label": "Hacker News", "color": "#d97706", "order": 3},
  "reddit": {"label": "Reddit", "color": "#ea580c", "order": 4},
}
DEFAULT_SECTION_CONFIG = {
  "harness": {"label": "Harness & Agent Tooling", "order": 1},
  "papers": {"label": "Papers", "order": 2},
  "applications": {"label": "LLM Applications & Infrastructure", "order": 3},
  "community": {"label": "Community Pulse", "order": 4},
}


def load_ui_config() -> dict:
  if not CONFIG_FILE.exists():
    return {}
  with CONFIG_FILE.open(encoding="utf-8") as handle:
    return yaml.safe_load(handle) or {}


def source_registry() -> dict:
  config = load_ui_config().get("sources", {})
  return {key: {**DEFAULT_SOURCE_CONFIG.get(key, {}), **value}
      for key, value in config.items()}


def section_registry() -> dict:
  config = load_ui_config().get("sections", {})
  return {key: {**DEFAULT_SECTION_CONFIG.get(key, {}), **value}
      for key, value in config.items()}


def source_keys(snap: dict) -> list[str]:
  """Return source keys from a raw snapshot, in configured order."""
  keys = [key for key, value in snap.items()
      if key != "date" and isinstance(value, list)]
  registry = source_registry()
  return sorted(keys, key=lambda key: (
    registry.get(key, {}).get("order", 999), key))


def source_meta(keys: list[str]) -> list[dict]:
  """Return UI metadata, with a readable fallback for an unregistered source."""
  registry = source_registry()
  return [{
    "key": key,
    "label": registry.get(key, {}).get("label", key.replace("_", " ").title()),
    "color": registry.get(key, {}).get("color", "#64748b"),
  } for key in keys]


def classify_section(source: str, item: dict) -> str:
  sections = section_registry()
  explicit = item.get("section")
  if explicit in sections:
    return explicit
  configured = source_registry().get(source, {}).get("section")
  if configured in sections:
    return configured
  return source


def section_meta(keys: list[str]) -> list[dict]:
  registry = section_registry()
  return [{
    "key": key,
    "label": registry.get(key, {}).get("label", key.replace("_", " ").title()),
    "sources": registry.get(key, {}).get("sources", []),
  } for key in keys]


def normalize_item(source: str, item: dict) -> dict:
  """Add a small common presentation model while preserving source fields."""
  normalized = dict(item)
  normalized["source"] = source
  normalized["section"] = classify_section(source, item)
  normalized["title"] = item.get("title") or item.get("full_name") or "Untitled"
  normalized["url"] = item.get("url") or item.get("reddit_url") or item.get("hn_url") or ""
  normalized["summary"] = item.get("summary") or item.get("description") or item.get("selftext") or ""
  normalized["published_at"] = item.get("published_at") or item.get("created_at")
  normalized["tags"] = item.get("tags") or item.get("topics") or item.get("matched_keywords") or []
  return normalized

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
    items = [normalize_item(source, item)
             for source in source_keys(snap)
             for item in snap.get(source, [])]
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
    keys = source_keys(snap)
    items_by_source = {
        source: [normalize_item(source, item) for item in snap.get(source, [])]
        for source in keys
    }
    configured_sections = section_registry()
    populated_sections = {
      item["section"] for items in items_by_source.values() for item in items
    }
    section_keys = sorted(
      set(configured_sections) | populated_sections,
      key=lambda key: (configured_sections.get(key, {}).get("order", 999), key))
    return {
        "sources": items_by_source,
        "source_meta": source_meta(keys),
        "sections": {
            section: [item for items in items_by_source.values()
                      for item in items if item["section"] == section]
            for section in section_keys
        },
        "section_meta": section_meta(section_keys),
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
/* llm-top-news 阅读器样式 */
:root {
  --page: #f6f7f9;
  --surface: #ffffff;
  --ink: #18202a;
  --muted: #748091;
  --line: #e4e8ee;
  --accent: #2563eb;
}
.dark {
  --page: #11151b;
  --surface: #191f27;
  --ink: #eef2f6;
  --muted: #9aa6b5;
  --line: #2b3440;
  --accent: #75a7ff;
}
html { scroll-behavior: smooth; scrollbar-gutter: stable; }
body { background: var(--page); color: var(--ink); }
::-webkit-scrollbar { width: 10px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(120,130,150,.35); border-radius: 6px; }
::-webkit-scrollbar-track { background: transparent; }
.reader-shell { max-width: 900px; }
.masthead { border-bottom: 1px solid var(--line); padding-bottom: 18px; }
.brand-title { color: var(--ink); font-size: 1.2rem; font-weight: 800; letter-spacing: -.02em; text-decoration: none; }
.brand-subtitle { color: var(--muted); font-size: .75rem; margin-top: 3px; }
.header-status { color: var(--muted); font-size: .75rem; text-align: right; }
.header-status strong { color: var(--ink); font-weight: 700; }
.top-nav { border-bottom: 1px solid var(--line); margin-bottom: 24px; }
.top-nav a { color: var(--muted); display: inline-block; font-size: .8rem; padding: 10px 2px 9px; text-decoration: none; }
.top-nav a + a { margin-left: 20px; }
.top-nav a:hover, .top-nav a.active { color: var(--accent); }
.top-nav a.active { border-bottom: 2px solid var(--accent); font-weight: 700; }
.news-list { overflow: hidden; border-top: 1px solid var(--line); }
.news-item { position: relative; display: grid; grid-template-columns: 4px minmax(0, 1fr); background: var(--surface); border-bottom: 1px solid var(--line); transition: background .15s ease; }
.news-item:hover { background: color-mix(in srgb, var(--surface) 92%, var(--accent)); }
.news-item:focus-within { outline: 2px solid var(--accent); outline-offset: -2px; }
.news-rail { background: var(--source-color, var(--accent)); }
.news-body { min-width: 0; padding: 17px 20px 16px; }
.news-title { color: var(--ink); font-size: 1rem; line-height: 1.45; font-weight: 700; text-decoration: none; }
.news-title:hover { color: var(--accent); }
.news-summary { color: var(--muted); font-size: .875rem; line-height: 1.6; }
.news-meta { color: var(--muted); font-size: .75rem; line-height: 1.5; }
.source-github { --source-color: #2563eb; }
.source-hf_papers { --source-color: #8b5cf6; }
.source-hn { --source-color: #d97706; }
.source-reddit { --source-color: #ea580c; }
.source-label { color: var(--source-color, var(--accent)); font-size: .7rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.news-thumb { width: 92px; height: 64px; object-fit: cover; border-radius: 6px; background: #e9edf2; }
.section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 30px 0 10px; }
.section-heading h2 { margin: 0; font-size: .78rem; letter-spacing: .12em; text-transform: uppercase; }
.section-heading span { color: var(--muted); font-size: .75rem; }
.source-group + .source-group { margin-top: 26px; }
.source-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 0 0 9px; }
.source-heading h3 { margin: 0; color: var(--muted); font-size: .7rem; letter-spacing: .1em; text-transform: uppercase; }
.source-heading span { color: var(--muted); font-size: .72rem; }
.filter-btn.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent); font-weight: 700; }
.filter-bar { position: sticky; top: 0; z-index: 50; background: var(--page); width: 100vw; margin-left: calc(50% - 50vw); padding: 10px max(16px, calc(50vw - 560px)) 12px; border-bottom: 1px solid var(--line); }
.archive-row { transition: background .15s ease, border-color .15s ease; }
.archive-row:hover { border-color: var(--accent); }
@media (max-width: 640px) {
  .header-status { margin-top: 12px; text-align: left; width: 100%; }
  .top-nav a + a { margin-left: 16px; }
  .news-body { padding: 15px 14px 14px; }
  .news-thumb { width: 76px; height: 56px; }
  .news-title { font-size: .95rem; }
}
"""

APP_JS = r"""
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
            <div class="source-label mb-1">GitHub</div>
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

  const CARDS = { github: githubCard, hn: hnCard, reddit: redditCard, hf_papers: hfPapersCard };

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
"""

PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
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
  <header class="masthead flex items-start gap-4 flex-wrap">
    <div class="flex-1 min-w-[220px]">
      <a href="__BASE__index.html" class="brand-title">LLM Top News<span class="text-blue-500">.</span></a>
      <div class="brand-subtitle">Daily intelligence for the LLM ecosystem</div>
    </div>
    <div class="header-status">
      <strong>__DATE__</strong> · __TOTAL__ items · __SOURCES__ sources
    </div>
  </header>

  <nav class="top-nav" aria-label="Primary navigation">
    <a href="__BASE__index.html" class="__HOME_ACTIVE__">Today</a>
    <a href="__BASE__archive.html">Archive</a>
    <span class="float-right mt-1">__DAY_NAV__</span>
  </nav>

  <div class="filter-bar">
    <div class="flex gap-2.5 flex-wrap items-center">
        <input id="search" type="search" placeholder="Search titles, descriptions, or keywords…"
          class="flex-1 min-w-[220px] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-2.5 text-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 dark:focus:ring-blue-950">
    <div id="section-filters" class="contents">
      <button class="section-filter-btn filter-btn active px-4 py-2 rounded-full text-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900" data-section="all">All sections</button>
    </div>
    </div>
    <div id="source-filters" class="flex gap-2.5 flex-wrap items-center mt-3"></div>
    <div id="sub-filters" class="flex gap-2 flex-wrap items-center mt-3"></div>
  </div>

  <div id="sections"></div>

    <footer class="text-center text-xs text-slate-400 mt-10">llm-top-news auto-generated · updated __GENERATED__</footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script src="__BASE__app.js"></script>
</body>
</html>
"""

ARCHIVE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
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
<div class="max-w-6xl mx-auto px-4 py-6">
  <header class="masthead flex items-start gap-4 flex-wrap">
    <div class="flex-1 min-w-[220px]">
      <a href="index.html" class="brand-title">LLM Top News<span class="text-blue-500">.</span></a>
      <div class="brand-subtitle">Daily intelligence for the LLM ecosystem</div>
    </div>
    <div class="header-status">
      <strong>__NDAYS__ days</strong> · __NITEMS__ items · __NCROSS__ cross-source hits
    </div>
  </header>

  <nav class="top-nav" aria-label="Primary navigation">
    <a href="index.html">Today</a>
    <a href="archive.html" class="active">Archive</a>
  </nav>

  <div class="news-list">
__ROWS__
  </div>

  <footer class="text-center text-xs text-slate-400 mt-10">llm-top-news auto-generated</footer>
</div>
<script src="app.js"></script>
</body>
</html>
"""


def _day_nav(base: str, prev: str | None, next_: str | None) -> str:
    btn = ("text-xs text-slate-400 dark:text-slate-500 hover:text-slate-900 dark:hover:text-slate-100 "
           "transition-colors duration-150")
    parts = []
    if prev:
        parts.append(f'<a href="{base}daily/{prev}.html" class="{btn}">← {prev[5:]}</a>')
    if next_:
        parts.append(f'<a href="{base}daily/{next_}.html" class="{btn}">{next_[5:]} →</a>')
    return "  ".join(parts)


def render_day(date: str, snap: dict, base: str, dates: list[str],
               is_index: bool, tailwind: str) -> str:
    i = dates.index(date)
    prev = dates[i - 1] if i > 0 else None
    next_ = dates[i + 1] if i < len(dates) - 1 else None
    title = "LLM Top News" if is_index else f"{date} · LLM Top News"
    data_js = json.dumps(
        {"date": date, "snapshot": snapshot_payload(snap)},
        ensure_ascii=False).replace("</", "<\\/")
    generated = datetime.now(timezone.utc)
    keys = source_keys(snap)
    total = sum(len(snap.get(src, [])) for src in keys)
    source_count = sum(bool(snap.get(src)) for src in keys)
    display_date = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")
    return (PAGE_TMPL
            .replace("__TITLE__", title)
            .replace("__TAILWIND__", base + tailwind)
            .replace("__BASE__", base)
            .replace("__HOME_ACTIVE__",
                     "active" if is_index else "")
            .replace("__DAY_NAV__", _day_nav(base, prev, next_))
            .replace("__DATE__", display_date)
            .replace("__GENERATED__", generated.strftime("%b %d, %Y %H:%M UTC"))
            .replace("__TOTAL__", str(total))
            .replace("__SOURCES__", str(source_count))
            .replace("__DATA__", data_js))


def render_archive(dates: list[str], snaps: dict[str, dict], tailwind: str) -> str:
    payloads = {d: snapshot_payload(snaps[d]) for d in dates}
    registry = source_registry()
    keys = sorted({key for snap in snaps.values() for key in source_keys(snap)},
          key=lambda key: (registry.get(key, {}).get("order", 999), key))
    n_items = sum(sum(len(s["sources"].get(key, [])) for key in keys)
            for s in payloads.values())
    n_cross = sum(len(s["cross"]) for s in payloads.values())

    pill = {
        "github": ("bg-blue-50 text-blue-600 dark:bg-blue-950 dark:text-blue-300", "bg-blue-500"),
        "hf_papers": ("bg-violet-50 text-violet-600 dark:bg-violet-950 dark:text-violet-300", "bg-violet-500"),
        "hn": ("bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300", "bg-amber-500"),
        "reddit": ("bg-orange-50 text-orange-600 dark:bg-orange-950 dark:text-orange-300", "bg-orange-500"),
    }
    label = {"github": "GitHub", "hf_papers": "HF Papers", "hn": "HN", "reddit": "Reddit"}

    rows = []
    for d in reversed(dates):
        s = payloads[d]
        dt = datetime.strptime(d, "%Y-%m-%d")
        counts = {k: len(s["sources"].get(k, [])) for k in keys}
        total = sum(counts.values()) or 1
        pills = "".join(
          f'<span class="px-2 py-0.5 rounded-full font-semibold {pill.get(k, ("bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300", "bg-slate-500"))[0]}">'
          f'{label.get(k, registry.get(k, {}).get("label", k.title()))} {counts[k]}</span>'
          for k in keys if counts[k])
        bar = "".join(
          f'<div class="{pill.get(k, ("", "bg-slate-500"))[1]}" style="width:{counts[k] / total * 100:.1f}%"></div>'
          for k in keys if counts[k])
        cross = (f'<span class="px-2 py-0.5 rounded-full font-semibold '
                 f'bg-rose-50 text-rose-600 dark:bg-rose-950 dark:text-rose-300">'
                 f'{len(s["cross"])} cross-source</span>') if s["cross"] else ""
        rows.append(f"""    <a href="daily/{d}.html" class="news-item archive-row" style="--source-color: #3b82f6">
      <div class="news-rail"></div><div class="news-body">
        <div class="source-label mb-1">{dt.strftime('%A')} · {dt.strftime('%b')} {dt.day:02d}</div>
        <div class="flex items-baseline gap-2 flex-wrap">
          <span class="news-title">{d}</span>
          <span class="text-xs text-slate-400 dark:text-slate-500">{total} items</span>
        </div>
        <div class="news-meta mt-2 flex gap-1.5 flex-wrap items-center">{pills}{cross}</div>
      </div>
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

    total = sum(sum(len(snap.get(source, [])) for source in source_keys(snap))
          for snap in snaps.values())
    print(f"✓ {len(dates)} 天 / {total} 条 → {SITE_DIR.relative_to(ROOT)}/")
    print(f"  首页: file:///{(SITE_DIR / 'index.html').as_posix()}")
    print(f"  归档: file:///{(SITE_DIR / 'archive.html').as_posix()}")


if __name__ == "__main__":
    main()
