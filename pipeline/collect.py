"""llm-top-news 采集管道。

用法:
    python pipeline/collect.py              # 采集当日快照（HN 回溯 1 天）
    python pipeline/collect.py --days 7     # HN 回溯 7 天（配合周报用）

输出:
    data/raw/YYYY-MM-DD.json   当日快照（重跑覆盖, 无 seen 状态）

数据源（全部限定 LLM/AI 相关, 关键词/阈值见 config.yaml）:
    - GitHub Search API   近 7 天新建 + AI topic/关键词, 按 star 排序（新晋 star 榜）
    - HN Firebase API     Top Stories 按排名取前 N（默认 20, 不做关键词/语义过滤）
                          另存 data/raw/YYYY-MM-DD.hn_candidates.json
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
from html.parser import HTMLParser
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
        cfg = yaml.safe_load(f)
    # 环境变量覆盖 Reddit 代理: 本机用 config.yaml 的 127.0.0.1 代理;
    # GitHub Actions 上设 REDDIT_PROXY="" 强制直连（runner 在美国, 无需代理）
    if "REDDIT_PROXY" in os.environ and isinstance(cfg.get("reddit"), dict):
        cfg["reddit"]["proxy"] = os.environ["REDDIT_PROXY"]
    if "GITHUB_PROXY" in os.environ and isinstance(cfg.get("github"), dict):
        cfg["github"]["proxy"] = os.environ["GITHUB_PROXY"]
    return cfg


# ───────────────────────── GitHub Search ─────────────────────────

def _github_search(q: str, per_page: int) -> list[dict]:
    url = "https://api.github.com/search/repositories?" + urlencode({
        "q": q, "sort": "stars", "order": "desc", "per_page": per_page,
    })
    data = http_json(url)
    return data.get("items", [])


def collect_github(cfg: dict) -> list[dict]:
    """采集新建项目榜（近 N 天新建 + AI topic, 按 star 排序）。

    GitHub Search 限制: qualifier (topic:/in:) 之间不能用 OR,
    所以拆成多次查询（单 topic）再合并去重。
    """
    g = cfg["github"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=g.get("lookback_days", 7))).strftime("%Y-%m-%d")
    limit = g.get("limit", 30)
    request_delay = max(0.0, float(g.get("request_delay_seconds", 1.0)))
    search_count = 0

    def collect_pool(query_prefix: str, per_page: int, kind: str) -> list[dict]:
        nonlocal search_count
        raw_items = []
        for topic in g.get("topics", ["llm"]):
            if search_count:
                time.sleep(request_delay)
            raw_items.extend(_github_search(f"{query_prefix} topic:{topic}", per_page))
            search_count += 1

        seen: dict[str, dict] = {}
        for item in raw_items:
            full_name = item.get("full_name", "")
            if full_name not in seen or (item.get("stargazers_count") or 0) > (seen[full_name].get("stargazers_count") or 0):
                seen[full_name] = item

        return [{
            "source": f"github_{kind}",
            "full_name": item.get("full_name"),
            "description": (item.get("description") or "")[:300],
            "stars": item.get("stargazers_count"),
            "created_at": item.get("created_at"),
            "pushed_at": item.get("pushed_at"),
            "url": item.get("html_url"),
            "language": item.get("language"),
            "topics": (item.get("topics") or [])[:10],
            "github_kind": kind,
        } for item in sorted(seen.values(), key=lambda value: value.get("stargazers_count") or 0, reverse=True)[:per_page]]

    return collect_pool(f"created:>{cutoff}", limit, "new")


class _GithubTrendingParser(HTMLParser):
    """Parse repository rows from GitHub's daily Trending HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._article: dict | None = None
        self._article_depth = 0
        self._capture: str | None = None
        self._capture_depth = 0
        self._text: list[str] = []

    @staticmethod
    def _has_class(attrs: dict[str, str | None], class_name: str) -> bool:
        return class_name in (attrs.get("class") or "").split()

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag == "article" and self._has_class(attrs, "Box-row") and self._article is None:
            self._article = {"full_name": "", "description": "", "language": "", "text": ""}
            self._article_depth = 1
            return
        if self._article is None:
            return
        self._article_depth += 1
        if tag == "h2":
            self._capture = "repo_heading"
            self._capture_depth = self._article_depth
            self._text = []
        elif tag == "p" and "col-9" in (attrs.get("class") or "").split():
            self._capture = "description"
            self._capture_depth = self._article_depth
            self._text = []
        elif tag == "span" and attrs.get("itemprop") == "programmingLanguage":
            self._capture = "language"
            self._capture_depth = self._article_depth
            self._text = []
        elif tag == "a" and "/stargazers" in (attrs.get("href") or ""):
            self._capture = "total_stars"
            self._capture_depth = self._article_depth
            self._text = []
        elif tag == "a" and self._capture == "repo_heading":
            href = attrs.get("href") or ""
            if href.count("/") == 2:
                self._article["full_name"] = href.strip("/")
        elif self._capture is None:
            href = attrs.get("href") or ""
            if self._article.get("full_name") == "" and self._article_depth >= 2 and href.count("/") == 2:
                self._article["full_name"] = href.strip("/")

    def handle_endtag(self, tag: str) -> None:
        if self._article is None:
            return
        if self._capture and self._article_depth == self._capture_depth:
            self._article[self._capture] = " ".join(self._text).strip()
            self._capture = None
            self._text = []
        if tag == "article" and self._article_depth == 1:
            if self._article.get("full_name"):
                self.items.append(self._article)
            self._article = None
            self._article_depth = 0
            self._capture = None
            self._text = []
            return
        self._article_depth = max(0, self._article_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._article is None:
            return
        if self._capture:
            self._text.append(data)
        else:
            self._article["text"] += f" {data}"


def _parse_github_trending(page: str) -> list[dict]:
    parser = _GithubTrendingParser()
    parser.feed(page)
    parser.close()
    items = []
    for row in parser.items:
        stars_today_match = re.search(r"([\d,]+)\s+stars\s+today", row["text"], re.I)
        total_stars_match = re.search(r"([\d,]+)\s*$", row.get("total_stars", ""))
        items.append({
            "source": "github_trending",
            "full_name": row["full_name"],
            "description": row["description"][:300],
            "stars": int(total_stars_match.group(1).replace(",", "")) if total_stars_match else 0,
            "stars_today": int(stars_today_match.group(1).replace(",", "")) if stars_today_match else 0,
            "url": f"https://github.com/{row['full_name']}",
            "language": row["language"],
            "topics": [],
            "github_kind": "trending",
        })
    return sorted(items, key=lambda item: item["stars_today"], reverse=True)


def collect_github_trending(cfg: dict) -> list[dict]:
    """抓取 GitHub Trending (daily) HTML 页面, 用 HTMLParser 解析 repository rows。

    github.com 无官方 Trending API, 只能抓 HTML; 直连被墙, 需代理
    （config.yaml github.proxy, 可被 GITHUB_PROXY 覆盖, 留空则直连）。
    失败时静默降级返回空列表, 不阻塞其他源。
    """
    g = cfg["github"]
    proxy = g.get("proxy") or None
    limit = int(g.get("trending_limit", 15))
    url = "https://github.com/trending?since=daily"
    try:
        page = _http_get(url, proxy).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"  ! github trending 抓取失败: {e}", file=sys.stderr)
        return []
    items = _parse_github_trending(page)
    print(f"  GitHub Trending: {len(items)} 条 → 取前 {min(limit, len(items))}", file=sys.stderr)
    return items[:limit]


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

def collect_hn(cfg: dict, days: int, limit: int) -> tuple[list[dict], list[dict]]:
    """从 HN Top Stories 按排名取前 N 条（不做关键词/语义过滤）。

    仅保留时间过滤（回溯 days 天, 周报合并 7 天时避免重复旧帖）。
    返回 (入选条目, 全部候选)。
    """
    h = cfg["hn"]
    ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    story_ids = http_json("https://hacker-news.firebaseio.com/v0/topstories.json")
    if not isinstance(story_ids, list):
        raise RuntimeError("HN topstories API returned an unexpected response")

    items = []
    for story_id in story_ids:
        if len(items) >= limit:
            break
        item = http_json(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json")
        if not isinstance(item, dict) or item.get("type") != "story":
            continue
        created_timestamp = int(item.get("time") or 0)
        if created_timestamp <= ts:
            continue
        items.append({
            "source": "hn",
            "title": item.get("title") or "(untitled)",
            "points": item.get("score", 0),
            "num_comments": item.get("descendants", 0),
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={story_id}",
            "hn_url": f"https://news.ycombinator.com/item?id={story_id}",
            "created_at": datetime.fromtimestamp(created_timestamp, timezone.utc).isoformat(),
            "story_text": item.get("text") or "",
            "matched_keywords": [],
        })
    print(f"  HN Top Stories: 取前 {len(items)} 条（不做关键词/语义过滤）", file=sys.stderr)
    return items, items


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


# ───────────────────────── HF Daily Papers ─────────────────────────

def collect_hf_papers(cfg: dict) -> list[dict]:
    """Hugging Face Daily Papers: 当日精选 AI 论文, 按 upvote 排序取前 N。"""
    hf = cfg.get("hf_papers", {})
    limit = hf.get("limit", 15)
    data = http_json("https://huggingface.co/api/daily_papers?limit=100")
    if not isinstance(data, list):
        return []
    items = []
    for entry in data:
        paper = entry.get("paper") or {}
        paper_id = paper.get("id") or ""
        if not paper_id:
            continue
        items.append({
            "source": "hf_papers",
            "title": paper.get("title") or "(untitled)",
            "summary": (paper.get("summary") or "")[:500],
            "upvotes": paper.get("upvotes") or 0,
            "num_comments": entry.get("numComments") or 0,
            "url": f"https://huggingface.co/papers/{paper_id}",
            "arxiv_id": paper_id,
            "published_at": paper.get("publishedAt"),
            "authors": [a.get("name") for a in (paper.get("authors") or []) if a.get("name")][:8],
        })
    items.sort(key=lambda x: x["upvotes"], reverse=True)
    return items[:limit]


def collection_quality_warnings(cfg: dict, raw: dict) -> list[str]:
    """Return non-blocking warnings for missing, malformed, or oversized feeds."""
    limits = {
        "github_new": int(cfg.get("github", {}).get("limit", 30)),
        "github_trending": int(cfg.get("github", {}).get("trending_limit", 15)),
        "hf_papers": int(cfg.get("hf_papers", {}).get("limit", 15)),
        "hn": int(cfg.get("hn", {}).get("limit", 20)),
        "reddit": int(cfg.get("reddit", {}).get("limit", 20)),
    }
    warnings = []
    for source, limit in limits.items():
        items = raw.get(source)
        if not isinstance(items, list):
            warnings.append(f"{source}: result is not a list")
            continue
        if not items:
            warnings.append(f"{source}: no items collected")
        if len(items) > limit:
            warnings.append(f"{source}: {len(items)} items exceeds configured limit {limit}")
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                warnings.append(f"{source}: non-object item")
                continue
            identity = item.get("url") or item.get("full_name") or item.get("title")
            if identity and identity in seen:
                warnings.append(f"{source}: duplicate item {identity}")
                break
            if identity:
                seen.add(identity)
    trending = raw.get("github_trending", [])
    if isinstance(trending, list):
        stars_today = [item.get("stars_today", 0) for item in trending if isinstance(item, dict)]
        if stars_today != sorted(stars_today, reverse=True):
            warnings.append("github_trending: items are not sorted by stars_today")
    return warnings


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

    print("→ GitHub 新建项目榜")
    github_new = collect_github(cfg)
    print(f"  新建 {len(github_new)} 条")

    print("→ GitHub Trending (daily)")
    github_trending = collect_github_trending(cfg)
    print(f"  {len(github_trending)} 条")

    # Harness Release sources are temporarily disabled in config.yaml.

    print(f"→ HN Top {cfg['hn'].get('limit', 20)}（回溯 {args.days} 天, 不做关键词/语义过滤）")
    hn_items, hn_ranked = collect_hn(cfg, days=args.days, limit=int(cfg["hn"].get("limit", 20)))
    print(f"  {len(hn_items)} 条")
    candidates_out = RAW_DIR / f"{today}.hn_candidates.json"
    candidates_out.write_text(
        json.dumps(hn_ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  完整候选 {len(hn_ranked)} 条 → {candidates_out.relative_to(ROOT)}")

    print("→ HF Daily Papers")
    hf_items = collect_hf_papers(cfg)
    print(f"  {len(hf_items)} 条")

    print("→ Reddit 热帖")
    reddit_items = collect_reddit(cfg)
    print(f"  {len(reddit_items)} 条")

    raw = {"date": today, "github_new": github_new, "github_trending": github_trending,
           "hf_papers": hf_items, "hn": hn_items, "reddit": reddit_items}
    for warning in collection_quality_warnings(cfg, raw):
        print(f"  ! 质量检查: {warning}", file=sys.stderr)
    out = RAW_DIR / f"{today}.json"
    out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(github_new) + len(github_trending) + len(hf_items) + len(hn_items) + len(reddit_items)
    print(f"\n✓ {total} 条快照 → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
