"""llm-top-news 日报/周报生成。

用法:
    python pipeline/digest.py --mode daily    # 汇总当日快照 → log/daily/YYYY-MM-DD.md
    python pipeline/digest.py --mode weekly   # 合并近 7 天快照 → log/weekly/YYYY-Www.md

聚合:
    标题归一化后相似度 ≥0.75 或 URL 相同 → 判同题; 同题出现在 ≥2 个源 → 🔥 跨源热点置顶

状态:
    data/processed_dates.json   已生成的 (mode, date) 键, 防重复生成
    data/summary_cache.json     LLM 摘要缓存（按内容 hash, 重跑不重复耗 token）

依赖 LLM 摘要（可选）:
    环境变量 LLM_API_BASE / LLM_API_KEY / LLM_MODEL
    （任意 OpenAI 兼容端点；未配置时退化为截断原文，不阻塞生成）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
LOG_DIR = ROOT / "log"
PROCESSED_FILE = ROOT / "data" / "processed_dates.json"
SUMMARY_CACHE = ROOT / "data" / "summary_cache.json"
CONFIG_FILE = ROOT / "config.yaml"

SIMILARITY_THRESHOLD = 0.75  # 标题归一化后判同题的相似度下限


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_dotenv() -> None:
    """轻量 .env 加载（无第三方依赖）: 已存在的环境变量优先, 不覆盖。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()


# ───────────────────────── 状态 ─────────────────────────

def load_processed() -> set:
    if PROCESSED_FILE.exists():
        return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
    return set()


def save_processed(keys: set) -> None:
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(
        json.dumps(sorted(keys), ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────────────────── 数据加载 ─────────────────────────

def load_raw(mode: str) -> tuple[dict, str]:
    """返回 (合并后的 raw 数据, 输出键)。

    daily:  读当日 raw 文件, 键 = daily:YYYY-MM-DD
    weekly: 合并近 7 天 raw 文件（按 source+url 去重, 保留热度最高的一条）,
            键 = weekly:YYYY-Www
    """
    now = datetime.now(timezone.utc)
    if mode == "daily":
        date = now.strftime("%Y-%m-%d")
        f = RAW_DIR / f"{date}.json"
        if not f.exists():
            sys.exit(f"✗ 当日 raw 不存在: {f.relative_to(ROOT)}, 先跑 python pipeline/collect.py")
        raw = json.loads(f.read_text(encoding="utf-8"))
        return raw, f"daily:{date}"

    # weekly
    merged: dict = {"date": now.strftime("%Y-%m-%d"), "github": [], "hn": [], "reddit": []}
    seen: dict[str, dict] = {}
    for i in range(7):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        f = RAW_DIR / f"{d}.json"
        if not f.exists():
            continue
        day = json.loads(f.read_text(encoding="utf-8"))
        for src in ("github", "hn", "reddit"):
            for item in day.get(src, []):
                key = f"{src}|{item.get('url') or item.get('title')}"
                old = seen.get(key)
                if old is None or _heat(item) > _heat(old):
                    seen[key] = item
    for src in ("github", "hn", "reddit"):
        merged[src] = sorted(
            (it for k, it in seen.items() if k.startswith(src + "|")),
            key=_heat, reverse=True)
    iso = now.isocalendar()
    return merged, f"weekly:{iso[0]}-W{iso[1]:02d}"


def _heat(item: dict) -> int:
    return item.get("stars") or item.get("points") or item.get("score") or 0


# ───────────────────────── 跨源聚合 ─────────────────────────

def _norm_title(title: str) -> str:
    """归一化: lowercase + 去标点/多余空格, 用于相似度比较。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (title or "").lower()).strip()


def _match_key(item: dict) -> str:
    """GitHub 用 repo 短名参与匹配（'vllm-project/vllm' → 'vllm'）, 其余用标题。"""
    if item["source"] == "github":
        return item["full_name"].split("/")[-1]
    return item.get("title") or ""


def aggregate(items: list[dict]) -> list[dict]:
    """跨源同题聚合。返回 story 列表, 每个 story:
    {title, sources: [源名...], items: [各源条目], heat: 最高热度}
    同题判定: URL 相同, 或归一化标题相似度 ≥ SIMILARITY_THRESHOLD。
    """
    stories: list[dict] = []
    for item in items:
        key = _match_key(item)
        nkey = _norm_title(key)
        url = item.get("url") or ""
        matched = None
        for s in stories:
            if url and url in s["urls"]:
                matched = s
                break
            if nkey and _norm_title(s["title"]) and \
                    SequenceMatcher(None, nkey, _norm_title(s["title"])).ratio() >= SIMILARITY_THRESHOLD:
                matched = s
                break
        if matched is None:
            stories.append({
                "title": key, "sources": [item["source"]], "items": [item],
                "heat": _heat(item), "urls": {url} if url else set(),
            })
        else:
            matched["items"].append(item)
            matched["urls"].add(url)
            if item["source"] not in matched["sources"]:
                matched["sources"].append(item["source"])
            matched["heat"] = max(matched["heat"], _heat(item))
    # 排序: 跨源优先, 然后热度
    stories.sort(key=lambda s: (len(s["sources"]) > 1, s["heat"]), reverse=True)
    return stories


# ───────────────────────── LLM 摘要（可选） ─────────────────────────

def llm_summarize(text: str, label: str) -> str:
    base = os.environ.get("LLM_API_BASE")
    key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not (base and key and model):
        return text[:200].strip() or "（无摘要）"
    cache: dict = {}
    if SUMMARY_CACHE.exists():
        cache = json.loads(SUMMARY_CACHE.read_text(encoding="utf-8"))
    ck = hashlib.sha1(f"{label}|{text[:3000]}".encode("utf-8")).hexdigest()
    if ck in cache:
        return cache[ck]
    payload = {
        "model": model,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": "你是 AI 工具链编辑。把内容压缩成 1-2 句中文摘要：是什么、为什么值得关注。"},
            {"role": "user", "content": f"{label}\n{text[:3000]}"},
        ],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            summary = json.load(resp)["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        return f"（摘要失败: {e}）\n" + text[:200].strip()
    cache[ck] = summary
    SUMMARY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


# ───────────────────────── 渲染 ─────────────────────────

def render_github(stories: list[dict], top_n: int = 15) -> list[str]:
    lines = ["## 🚀 GitHub 新晋 star 榜", ""]
    shown = 0
    for s in stories:
        if shown >= top_n:
            break
        item = next((i for i in s["items"] if i["source"] == "github"), None)
        if not item:
            continue
        shown += 1
        lang = f" · `{item['language']}`" if item.get("language") else ""
        lines.append(f"### {shown}. [{item['full_name']}]({item['url']}) — ⭐ {item['stars']}{lang}")
        if item.get("description"):
            label = f"GitHub repo {item['full_name']} 的描述"
            lines.append(f"> {llm_summarize(item['description'], label)}")
        lines.append("")
    if not shown:
        lines.append("（无数据）")
        lines.append("")
    return lines


def render_hn(stories: list[dict], top_n: int = 15) -> list[str]:
    lines = ["## 💬 HN 高分帖", ""]
    shown = 0
    for s in stories:
        if shown >= top_n:
            break
        item = next((i for i in s["items"] if i["source"] == "hn"), None)
        if not item:
            continue
        shown += 1
        kws = f" · 命中: {', '.join(item['matched_keywords'])}" if item.get("matched_keywords") else ""
        lines.append(
            f"{shown}. [{item['title']}]({item['url']}) — ▲ {item['points']} · "
            f"[{item['num_comments']} 评论]({item['hn_url']}){kws}")
    if not shown:
        lines.append("（无数据）")
    lines.append("")
    return lines


def render_reddit(stories: list[dict], top_n: int = 15) -> list[str]:
    lines = ["## 📕 Reddit 热帖", ""]
    shown = 0
    for s in stories:
        if shown >= top_n:
            break
        item = next((i for i in s["items"] if i["source"] == "reddit"), None)
        if not item:
            continue
        shown += 1
        # RSS 源无 score/num_comments, 有值才显示
        heat = f"🔺 {item['score']} · " if item.get("score") is not None else ""
        comments = f" · {item['num_comments']} 评论" if item.get("num_comments") is not None else ""
        lines.append(
            f"{shown}. [{item['title']}]({item['reddit_url']}) — {heat}"
            f"r/{item['subreddit']}{comments}")
    if not shown:
        lines.append("（无数据）")
    lines.append("")
    return lines


def render_cross_source(stories: list[dict], mode: str = "daily") -> list[str]:
    lines = ["## 🔥 跨源热点", ""]
    shown = 0
    for s in stories:
        if len(s["sources"]) < 2:
            continue
        shown += 1
        src = " × ".join(s["sources"])
        lines.append(f"### {shown}. {s['title']}  `[{src}]`")
        for item in s["items"]:
            if item["source"] == "github":
                lines.append(f"  - GitHub: [{item['full_name']}]({item['url']}) ⭐{item['stars']}")
            elif item["source"] == "hn":
                lines.append(f"  - HN: [{item['title']}]({item['hn_url']}) ▲{item['points']}")
            elif item["source"] == "reddit":
                heat = f" 🔺{item['score']}" if item.get("score") is not None else ""
                lines.append(f"  - Reddit: [{item['title']}]({item['reddit_url']}){heat} (r/{item['subreddit']})")
        lines.append("")
    if not shown:
        lines.append("（本周无跨源热点）" if mode == "weekly" else "（今日无跨源热点）")
        lines.append("")
    return lines


def render(mode: str, raw: dict, stories: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    if mode == "daily":
        title = f"# LLM Top News 日报 · {now.strftime('%Y-%m-%d')}"
    else:
        iso = now.isocalendar()
        title = f"# LLM Top News 周报 · {iso[0]}-W{iso[1]:02d}"
    lines = [title, ""]
    if mode == "weekly":
        top = next((s for s in stories if any(i["source"] == "github" for i in s["items"])), None)
        if top:
            item = next(i for i in top["items"] if i["source"] == "github")
            lines.append(f"**本周 star 王**: [{item['full_name']}]({item['url']}) ⭐ {item['stars']}")
            lines.append("")
    lines += render_cross_source(stories, mode)
    lines += render_github(stories)
    lines += render_hn(stories)
    lines += render_reddit(stories)
    lines.append("---")
    lines.append("*llm-top-news 自动生成, 人工终审后发布。*")
    return "\n".join(lines) + "\n"


# ───────────────────────── 主流程 ─────────────────────────

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    args = ap.parse_args()

    raw, key = load_raw(args.mode)
    processed = load_processed()
    if key in processed:
        print(f"= {key} 已生成过, 跳过（如需重跑请从 data/processed_dates.json 删除该键）")
        return

    # 周报用更高阈值: raw 采集时统一按 daily_min_points 过滤,
    # 这里对 HN 段按 weekly_min_points 重新过滤（GitHub/Reddit 无分数阈值概念）
    if args.mode == "weekly":
        cfg = load_config()
        min_pts = cfg["hn"].get("weekly_min_points", 100)
        before = len(raw["hn"])
        raw["hn"] = [it for it in raw["hn"] if (it.get("points") or 0) >= min_pts]
        if before != len(raw["hn"]):
            print(f"  ~ HN 周报阈值 ≥{min_pts} 分: {before} → {len(raw['hn'])} 条")

    all_items = raw["github"] + raw["hn"] + raw["reddit"]
    stories = aggregate(all_items)
    md = render(args.mode, raw, stories)

    now = datetime.now(timezone.utc)
    if args.mode == "daily":
        out = LOG_DIR / "daily" / f"{now.strftime('%Y-%m-%d')}.md"
    else:
        iso = now.isocalendar()
        out = LOG_DIR / "weekly" / f"{iso[0]}-W{iso[1]:02d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    processed.add(key)
    save_processed(processed)
    cross = sum(1 for s in stories if len(s["sources"]) >= 2)
    print(f"✓ {args.mode} → {out.relative_to(ROOT)}（{len(all_items)} 条原始, {len(stories)} 个 story, {cross} 个跨源热点）")


if __name__ == "__main__":
    main()
