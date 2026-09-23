"""llm-top-news 采集管道。

用法:
    python pipeline/collect.py              # 采集当日快照（HN 回溯 1 天）
    python pipeline/collect.py --days 7     # HN 回溯 7 天（配合周报用）

输出:
    data/raw/YYYY-MM-DD.json   当日快照（重跑覆盖, 无 seen 状态）

数据源（全部限定 LLM/AI 相关, 关键词/阈值见 config.yaml）:
    - GitHub Search API   近 7 天新建 + AI topic/关键词, 按 star 排序（新晋 star 榜）
    - HN Algolia API      search_by_date + 标题短语精确匹配, 分数阈值过滤
    - Reddit top RSS      r/<sub>/top/.rss?t=day, 需代理（config.yaml reddit.proxy）, 未配置则跳过
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CONFIG_FILE = ROOT / "config.yaml"


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

HTTP_TIMEOUT = 30
UA = "llm-top-news/0.1 (personal tracking; contact: local)"


def _ssl_context() -> ssl.SSLContext:
    """不加载 Windows 证书存储的 SSL 上下文。

    本机证书存储存在损坏证书, 默认上下文 load_default_certs 会抛
    ssl.SSLError: ASN1 NOT_ENOUGH_DATA; 改用 certifi 的 CA 包（requests 依赖, 必装）。
    """
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


SSL_CTX = _ssl_context()


def http_json(url: str) -> object:
    headers = {"User-Agent": UA}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CTX) as resp:
        return json.load(resp)


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ───────────────────────── GitHub Search ─────────────────────────

def _github_search(q: str, per_page: int) -> list[dict]:
    url = "https://api.github.com/search/repositories?" + urlencode({
        "q": q, "sort": "stars", "order": "desc", "per_page": per_page,
    })
    data = http_json(url)
    return data.get("items", [])


def collect_github(cfg: dict) -> list[dict]:
    """近 N 天新建 + AI 相关, 按 star 排序的新晋 star 榜。

    GitHub Search 限制: qualifier (topic:/in:) 之间不能用 OR,
    所以拆成两次查询（单 topic + 单关键词）再合并去重。
    """
    g = cfg["github"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=g.get("lookback_days", 7))).strftime("%Y-%m-%d")
    limit = g.get("limit", 30)

    # 查询 1: 单 topic（抓规范打标的 repo）
    topic = g.get("primary_topic", "llm")
    q1 = f"created:>{cutoff} topic:{topic}"
    # 查询 2: 单关键词 in:name,description（抓描述里提到 LLM 的）
    keyword = g.get("primary_keyword", "LLM")
    q2 = f'created:>{cutoff} "{keyword}" in:name,description'

    raw_items = _github_search(q1, limit) + _github_search(q2, limit)

    # 按 full_name 去重, 保留 star 最高的
    seen: dict[str, dict] = {}
    for r in raw_items:
        fn = r.get("full_name", "")
        if fn not in seen or (r.get("stargazers_count") or 0) > (seen[fn].get("stargazers_count") or 0):
            seen[fn] = r

    items = []
    for r in sorted(seen.values(), key=lambda x: x.get("stargazers_count") or 0, reverse=True)[:limit]:
        items.append({
            "source": "github",
            "full_name": r.get("full_name"),
            "description": (r.get("description") or "")[:300],
            "stars": r.get("stargazers_count"),
            "created_at": r.get("created_at"),
            "url": r.get("html_url"),
            "language": r.get("language"),
            "topics": (r.get("topics") or [])[:10],
        })
    return items


# ───────────────────────── Hacker News ─────────────────────────

def collect_hn(cfg: dict, days: int, min_points: int) -> list[dict]:
    """对每个关键词做标题短语精确匹配, 单次运行内按 objectID 去重。"""
    h = cfg["hn"]
    ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    seen: dict[str, dict] = {}
    for kw in h.get("keywords", []):
        query = urlencode({
            "query": f'"{kw}"',
            "tags": "story",
            "numericFilters": f"created_at_i>{ts},points>{min_points}",
            "hitsPerPage": 20,
        })
        try:
            data = http_json(f"https://hn.algolia.com/api/v1/search_by_date?{query}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! hn keyword {kw!r}: {e}", file=sys.stderr)
            continue
        for hit in data.get("hits", []):
            oid = hit.get("objectID")
            if not oid:
                continue
            item = seen.get(oid)
            if item is None:
                item = {
                    "source": "hn",
                    "title": hit.get("title"),
                    "points": hit.get("points"),
                    "num_comments": hit.get("num_comments"),
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "hn_url": f"https://news.ycombinator.com/item?id={oid}",
                    "created_at": hit.get("created_at"),
                    "matched_keywords": [],
                }
                seen[oid] = item
            if kw not in item["matched_keywords"]:
                item["matched_keywords"].append(kw)
        time.sleep(0.3)
    return list(seen.values())


# ───────────────────────── Reddit ─────────────────────────

def _http_get(url: str, proxy: str | None) -> bytes:
    """GET 请求, 可选走 HTTP 代理（Reddit 直连被墙, 必须代理）。

    Reddit 对脚本 UA 限流严格（429）, 用浏览器 UA。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = Request(url, headers=headers)
    if proxy:
        opener = build_opener(
            ProxyHandler({"http": proxy, "https": proxy}),
            HTTPSHandler(context=SSL_CTX),
        )
        resp = opener.open(req, timeout=HTTP_TIMEOUT)
    else:
        resp = urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CTX)
    with resp:
        return resp.read()


def _parse_rss(xml_bytes: bytes) -> list[dict]:
    """解析 Reddit RSS（实际是 Atom 格式: <feed>/<entry>）。

    标题/链接/发布时间取 Atom 字段; 分数和评论数从 content HTML 里抠。
    """
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_bytes)
    entries = []
    for item in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = (item.findtext("a:title", namespaces=ns) or "").strip()
        link_el = item.find("a:link", ns)
        link = (link_el.get("href") if link_el is not None else "") or ""
        pub = (item.findtext("a:published", namespaces=ns)
               or item.findtext("a:updated", namespaces=ns) or "").strip() or None
        content = item.findtext("a:content", namespaces=ns) or ""
        # content 是 HTML 实体转义的表格; 帖子链接在 [comments] 锚点里
        # （Atom 的 <link> 是外部链接, 不是帖子页）
        m_thread = re.search(r'href=&quot;(https://www\.reddit\.com/r/[^&]+/comments/[^&]+)&quot;[^>]*>\[comments\]', content)
        thread_url = m_thread.group(1) if m_thread else link
        created = None
        if pub:
            try:
                created = datetime.fromisoformat(pub).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
        # 注意: Reddit RSS 不提供 score/num_comments, 只能置 None
        entries.append({
            "title": title,
            "url": link,
            "thread_url": thread_url,
            "score": None,
            "num_comments": None,
            "created_at": created,
        })
    return entries


def collect_reddit(cfg: dict) -> list[dict]:
    """r/<sub>/top/.rss?t=day（RSS 端点, 无需 OAuth）。

    本机直连 reddit.com 超时, .json 端点走代理也 403, 但 RSS 走代理可用（已验证）。
    代理地址在 config.yaml reddit.proxy; 未配置或请求失败时静默降级不阻塞。
    """
    r = cfg["reddit"]
    proxy = r.get("proxy")
    if not proxy:
        print("  ~ reddit: 未配置 reddit.proxy, 跳过", file=sys.stderr)
        return []
    items = []
    for sub in r.get("subreddits", []):
        url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit={r.get('limit', 25)}"
        try:
            xml_bytes = _http_get(url, proxy)
            entries = _parse_rss(xml_bytes)
        except Exception as e:  # noqa: BLE001
            print(f"  ! reddit r/{sub}: {e}", file=sys.stderr)
            continue
        for e in entries[:r.get("limit", 25)]:
            items.append({
                "source": "reddit",
                "title": e["title"],
                "score": e["score"],
                "num_comments": e["num_comments"],
                "url": e["url"],
                "reddit_url": e["thread_url"],
                "subreddit": sub,
                "created_at": e["created_at"],
            })
        time.sleep(1)
    return items


# ───────────────────────── 主流程 ─────────────────────────

def main() -> None:
    # Windows GBK 控制台兜底: 打印 ✓/→ 等字符不崩
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="HN 回溯天数（周报用 7）")
    args = ap.parse_args()

    cfg = load_config()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    print("→ GitHub 新晋 star 榜")
    github_items = collect_github(cfg)
    print(f"  {len(github_items)} 条")

    print(f"→ HN 高分帖（回溯 {args.days} 天, ≥{cfg['hn']['daily_min_points']} 分）")
    hn_items = collect_hn(cfg, days=args.days, min_points=cfg["hn"]["daily_min_points"])
    print(f"  {len(hn_items)} 条")

    print("→ Reddit 热帖")
    reddit_items = collect_reddit(cfg)
    print(f"  {len(reddit_items)} 条")

    raw = {"date": today, "github": github_items, "hn": hn_items, "reddit": reddit_items}
    out = RAW_DIR / f"{today}.json"
    out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(github_items) + len(hn_items) + len(reddit_items)
    print(f"\n✓ {total} 条快照 → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
