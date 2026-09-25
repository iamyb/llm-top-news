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
import html
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

from semantic_filter import DEFAULT_QUERY, rank_records

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
        cfg = yaml.safe_load(f)
    # 环境变量覆盖 Reddit 代理: 本机用 config.yaml 的 127.0.0.1 代理;
    # GitHub Actions 上设 REDDIT_PROXY="" 强制直连（runner 在美国, 无需代理）
    if "REDDIT_PROXY" in os.environ and isinstance(cfg.get("reddit"), dict):
        cfg["reddit"]["proxy"] = os.environ["REDDIT_PROXY"]
    return cfg


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

    raw_items = []
    for topic in g.get("topics", ["llm"]):
        query = f"created:>{cutoff} topic:{topic}"
        raw_items.extend(_github_search(query, limit))

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


def collect_github_releases(cfg: dict, source: str) -> list[dict]:
    """采集指定仓库最新的一条 GitHub Release。"""
    repo = cfg[source]["repo"]
    data = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    if not isinstance(data, list):
        return []

    items = []
    include_prereleases = cfg[source].get("include_prereleases", False)
    for release in data:
        if release.get("draft") or (release.get("prerelease") and not include_prereleases):
            continue
        published_at = release.get("published_at") or ""
        tag = release.get("tag_name") or ""
        items.append({
            "source": source,
            "title": release.get("name") or tag,
            "summary": (release.get("body") or "")[:1000],
            "tag_name": tag,
            "url": release.get("html_url"),
            "published_at": published_at,
        })
    return sorted(items, key=lambda item: item["published_at"], reverse=True)[:1]


# ───────────────────────── Hacker News ─────────────────────────

def collect_hn(cfg: dict, days: int, min_points: int) -> list[dict]:
    """获取 HN 候选并按配置使用 MiniLM 排序后取 Top-K。"""
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
                    "story_text": hit.get("story_text") or "",
                    "matched_keywords": [],
                }
                seen[oid] = item
            if kw not in item["matched_keywords"]:
                item["matched_keywords"].append(kw)
        time.sleep(0.3)
    items = list(seen.values())
    semantic = h.get("semantic_filter", {})
    if not semantic.get("enabled", False) or not items:
        return items
    ranked = rank_records(
        items,
        top_k=int(semantic.get("top_k", 10)),
        model_name=semantic.get("model", "sentence-transformers/all-MiniLM-L6-v2"),
        query=semantic.get("query") or DEFAULT_QUERY,
    )
    selected = [item for item in ranked if item["minilm_relevant"]]
    print(f"  MiniLM 排序: {len(items)} 条候选 → Top-{len(selected)}", file=sys.stderr)
    return selected


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
        # content 是 HTML 表格（ET 解析后引号已反转义）; 帖子链接在 [comments] 锚点里
        # （Atom 的 <link> 通常就是帖子页, 此处作为兜底）
        m_thread = re.search(r'href="(https://www\.reddit\.com/r/[^"]+/comments/[^"]+)"[^>]*>\[comments\]', content)
        thread_url = m_thread.group(1) if m_thread else link
        # 缩略图: content 里 preview.redd.it / i.redd.it 的图片。
        # 注意: ET 解析后引号已反转义成真实 ", 但 URL 内的 & 仍是 &amp;（双重转义）
        m_img = re.search(r'src="(https://(?:preview|i)\.redd\.it/[^"]+)"', content)
        thumbnail = m_img.group(1).replace("&amp;", "&") if m_img else None
        # 正文: content 里 <div class="md">…</div>（纯链接帖没有, 为空）
        m_md = re.search(r'<div class="md">(.*?)</div>', content, re.S)
        selftext = ""
        if m_md:
            selftext = re.sub(r'<[^>]+>', ' ', m_md.group(1))
            selftext = html.unescape(selftext).replace("&amp;", "&")
            selftext = re.sub(r'\s+', ' ', selftext).strip()[:300]
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
            "thumbnail": thumbnail,
            "selftext": selftext,
            "score": None,
            "num_comments": None,
            "created_at": created,
        })
    return entries


def collect_reddit(cfg: dict) -> list[dict]:
    """多 sub 合并 top/.rss?t=day（RSS 端点, 无需 OAuth, 单次请求）。

    本机直连 reddit.com 超时, .json 端点走代理也 403, 但 RSS 走代理可用（已验证）。
    合并端点 `r/A+B+C/top/.rss` 返回的是**合并池按热度排序的 top N**（已实测）,
    大 sub 会淹没小 sub → 客户端按 per_sub_limit / min_per_sub 配额截取。
    代理地址在 config.yaml reddit.proxy（可被环境变量 REDDIT_PROXY 覆盖）;
    留空则直连（GitHub Actions 场景）; 请求失败时静默降级不阻塞。
    """
    r = cfg["reddit"]
    proxy = r.get("proxy") or None
    subs = r.get("subreddits", [])
    if not subs:
        return []
    limit = r.get("limit", 25)
    per_sub = r.get("per_sub_limit", limit)
    min_sub = r.get("min_per_sub", 0)
    # 候选池放大: 保证小 sub 的帖子有机会进池（合并端点按全局热度截断, RSS 上限 100）
    pool = min(100, max(limit * 3, len(subs) * per_sub * 2))

    def _to_item(e: dict, sub: str) -> dict:
        return {
            "source": "reddit",
            "title": e["title"],
            "score": e["score"],
            "num_comments": e["num_comments"],
            "url": e["url"],
            "reddit_url": e["thread_url"],
            "thumbnail": e["thumbnail"],
            "selftext": e["selftext"],
            "subreddit": sub,
            "created_at": e["created_at"],
        }

    def _quota(entries: list[dict]) -> list[dict]:
        """按 sub 配额截取: 每 sub 最多 per_sub 条、保底 min_sub 条, 去重后按热度截 limit。"""
        by_sub: dict[str, list[dict]] = {}
        seen: set[str] = set()
        for e in entries:  # entries 已按热度降序
            m = re.search(r"reddit\.com/r/([^/]+)/comments", e["thread_url"])
            sub = m.group(1) if m else ""
            key = e["thread_url"].split("/comments/")[0]
            if not sub or key in seen:
                continue
            seen.add(key)
            by_sub.setdefault(sub, []).append(e)
        picked: list[dict] = []
        cnt: dict[str, int] = {s: 0 for s in subs}
        # 保底轮: 每个 sub 先拿 min_sub 条（计入 per_sub 配额）
        for sub in subs:
            for e in by_sub.get(sub, [])[:min_sub]:
                if e not in picked:
                    picked.append(e)
                    cnt[sub] += 1
        # 填充轮: 按全局热度顺序补, 受 per_sub 上限约束
        for e in entries:
            if len(picked) >= limit:
                break
            m = re.search(r"reddit\.com/r/([^/]+)/comments", e["thread_url"])
            sub = m.group(1) if m else ""
            if e in picked or not sub:
                continue
            if cnt.get(sub, 0) >= per_sub:
                continue
            cnt[sub] = cnt.get(sub, 0) + 1
            picked.append(e)
        return [_to_item(e, re.search(r"reddit\.com/r/([^/]+)/comments", e["thread_url"]).group(1))
                for e in picked]

    # 主路径: 合并端点单次请求
    url = f"https://www.reddit.com/r/{'+'.join(subs)}/top/.rss?t=day&limit={pool}"
    try:
        entries = _parse_rss(_http_get(url, proxy))
        items = _quota(entries)
        dist = {}
        for it in items:
            dist[it["subreddit"]] = dist.get(it["subreddit"], 0) + 1
        print(f"  合并端点 {len(entries)} 条 → 配额截取 {len(items)} 条 {dist}", file=sys.stderr)
        return items
    except Exception as e:  # noqa: BLE001
        print(f"  ! reddit 合并端点失败, 回退逐 sub 抓取: {e}", file=sys.stderr)

    # 回退路径: 逐 sub 抓（429 风险高, 仅兜底）
    items = []
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit={per_sub}"
        try:
            entries = _parse_rss(_http_get(url, proxy))
        except Exception as e:  # noqa: BLE001
            print(f"  ! reddit r/{sub}: {e}", file=sys.stderr)
            continue
        items.extend(_to_item(e, sub) for e in entries[:per_sub])
        time.sleep(5)
    return items[:limit]


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

    # Harness Release sources are temporarily disabled in config.yaml.

    print(f"→ HN 高分帖（回溯 {args.days} 天, ≥{cfg['hn']['daily_min_points']} 分）")
    hn_items = collect_hn(cfg, days=args.days, min_points=cfg["hn"]["daily_min_points"])
    print(f"  {len(hn_items)} 条")

    print("→ Reddit 热帖")
    reddit_items = collect_reddit(cfg)
    print(f"  {len(reddit_items)} 条")

    raw = {"date": today, "github": github_items,
           "hn": hn_items, "reddit": reddit_items}
    out = RAW_DIR / f"{today}.json"
    out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(github_items) + len(hn_items) + len(reddit_items)
    print(f"\n✓ {total} 条快照 → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
