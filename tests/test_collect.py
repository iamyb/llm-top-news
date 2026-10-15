import unittest
from unittest.mock import patch

from pipeline.collect import (
    _parse_github_trending,
    collect_github_trending,
    collection_quality_warnings,
)


TRENDING_HTML = """
<article class="Box-row">
  <h2><a href="/acme/fast-agent">acme / fast-agent</a></h2>
  <p class="col-9 color-fg-muted my-1">Fast &amp; reliable agent tooling.</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/acme/fast-agent/stargazers"><svg><path></path></svg> 12,345</a>
  <span>321 stars today</span>
</article>
<article class="Box-row border-top">
  <h2><a href="/example/small-repo">example / small-repo</a></h2>
  <p class="col-9">A small repository.</p>
  <span itemprop="programmingLanguage">Go</span>
  <a href="/example/small-repo/stargazers"><svg></svg> 99</a>
  <span>12 stars today</span>
</article>
"""


class GithubTrendingParserTests(unittest.TestCase):
    def test_parser_extracts_fields_and_sorts_by_daily_stars(self):
        items = _parse_github_trending(TRENDING_HTML)

        self.assertEqual([item["full_name"] for item in items], ["acme/fast-agent", "example/small-repo"])
        self.assertEqual(items[0]["description"], "Fast & reliable agent tooling.")
        self.assertEqual(items[0]["language"], "Python")
        self.assertEqual(items[0]["stars"], 12345)
        self.assertEqual(items[0]["stars_today"], 321)
        self.assertEqual(items[0]["url"], "https://github.com/acme/fast-agent")

    def test_parser_skips_articles_without_repository_link(self):
        items = _parse_github_trending('<article class="Box-row"><h2>Sponsored</h2></article>')

        self.assertEqual(items, [])

    @patch("pipeline.collect._http_get", return_value=TRENDING_HTML.encode())
    def test_collector_applies_configured_limit(self, http_get):
        items = collect_github_trending({"github": {"proxy": "", "trending_limit": 1}})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["full_name"], "acme/fast-agent")
        http_get.assert_called_once()


class CollectionQualityTests(unittest.TestCase):
    def test_quality_checks_find_duplicates_and_limit_overflow(self):
        cfg = {
            "github": {"limit": 1, "trending_limit": 1},
            "hf_papers": {"limit": 1},
            "hn": {"limit": 1},
            "reddit": {"limit": 1},
        }
        raw = {
            "github_new": [
                {"url": "https://github.com/acme/repo"},
                {"url": "https://github.com/acme/repo"},
            ],
            "github_trending": [{"full_name": "acme/repo", "stars_today": 1}],
            "hf_papers": [],
            "hn": [],
            "reddit": [],
        }

        warnings = collection_quality_warnings(cfg, raw)

        self.assertTrue(any("duplicate item" in warning for warning in warnings))
        self.assertTrue(any("exceeds configured limit" in warning for warning in warnings))
        self.assertTrue(any("hf_papers: no items" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
