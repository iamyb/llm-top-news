"""独立测试 Hacker News 今日 Top Stories。

用法:
    python pipeline/test_hn_top.py
    python pipeline/test_hn_top.py --limit 100 --all --output data/hn_top_test.json
    python pipeline/test_hn_top.py --classify-existing data/hn_top_test.json --top-k 10

默认读取 HN 官方 Firebase API 的 Top Stories，并只显示 UTC 今天发布的条目。
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import certifi

from semantic_filter import rank_records


API_ROOT = "https://hacker-news.firebaseio.com/v0"
HTTP_TIMEOUT = 20
USER_AGENT = "llm-top-news-hn-top-test/0.1"


def http_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=HTTP_TIMEOUT, context=context) as response:
        return json.load(response)


def fetch_top_stories(limit: int, today_only: bool) -> list[dict]:
    story_ids = http_json(f"{API_ROOT}/topstories.json")
    if not isinstance(story_ids, list):
        raise RuntimeError("HN topstories API returned an unexpected response")

    today = datetime.now(timezone.utc).date()
    stories = []
    for story_id in story_ids:
        if len(stories) >= limit:
            break
        item = http_json(f"{API_ROOT}/item/{story_id}.json")
        if not isinstance(item, dict) or item.get("type") != "story":
            continue
        created_at = datetime.fromtimestamp(item.get("time", 0), timezone.utc)
        if today_only and created_at.date() != today:
            continue
        stories.append({
            "id": item.get("id"),
            "title": item.get("title") or "(untitled)",
            "score": item.get("score", 0),
            "comments": item.get("descendants", 0),
            "created_at": created_at.isoformat(),
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id')}",
            "hn_url": f"https://news.ycombinator.com/item?id={item.get('id')}",
        })
    return stories


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="最多显示多少条")
    parser.add_argument("--all", action="store_true", help="不按 UTC 今天过滤")
    parser.add_argument("--output", type=Path, help="将结果保存为 JSON 文件")
    parser.add_argument(
        "--classify-existing",
        type=Path,
        metavar="JSON",
        help="使用 MiniLM 为已有 JSON 条目打分并写回原文件",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="按 MiniLM 分数排序后保留前多少条（默认: 10）",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be greater than 0")
    if args.top_k < 1:
        parser.error("--top-k must be greater than 0")

    if args.classify_existing:
        records = json.loads(args.classify_existing.read_text(encoding="utf-8"))
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            parser.error("--classify-existing must point to a JSON list of objects")
        records = rank_records(records, min(args.top_k, len(records)))
        args.classify_existing.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        selected = [item for item in records if item["minilm_relevant"]]
        print(f"已写回: {args.classify_existing}")
        print(f"MiniLM top-{len(selected)}: {len(selected)}/{len(records)} 条")
        for index, story in enumerate(
            sorted(selected, key=lambda item: item["minilm_score"], reverse=True), 1
        ):
            print(
                f"{index}. [{story['minilm_score']:.6f}] "
                f"{story.get('title', '(untitled)')}"
            )
            print(f"   {story.get('url', '')}")
        return

    stories = fetch_top_stories(args.limit, today_only=not args.all)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(stories, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存: {args.output}")
    print(f"HN Top Stories: {len(stories)} 条")
    for index, story in enumerate(stories, 1):
        print(f"{index}. [{story['score']} points, {story['comments']} comments] {story['title']}")
        print(f"   {story['url']}")
        print(f"   HN: {story['hn_url']} · {story['created_at']}")


if __name__ == "__main__":
    main()