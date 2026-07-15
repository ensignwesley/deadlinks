import unittest

from deadlinks import DeadLinkChecker, LinkResult


class FakeChecker(DeadLinkChecker):
    def __init__(self, pages, *args, **kwargs):
        super().__init__(*args, rate_limit=0, max_workers=1, **kwargs)
        self.pages = pages

    def _get_page(self, url):
        return self.pages.get(url)

    def check_link(self, url):
        return LinkResult(url=url, status_code=200, is_broken=False)


class CrawlFrontierTests(unittest.TestCase):
    def test_default_crawl_stays_on_start_domain(self):
        pages = {
            "https://example.com/": """
                <a href="/about/">about</a>
                <a href="https://other.test/page/">external</a>
            """,
            "https://example.com/about/": "<p>internal</p>",
            "https://other.test/page/": "<p>external</p>",
        }
        checker = FakeChecker(pages)

        results = checker.crawl("https://example.com/", max_depth=1)

        self.assertIn("https://example.com/", results)
        self.assertIn("https://example.com/about/", results)
        self.assertNotIn("https://other.test/page/", results)

    def test_external_flag_expands_crawl_frontier(self):
        pages = {
            "https://example.com/": """
                <a href="/about/">about</a>
                <a href="https://other.test/page/">external</a>
            """,
            "https://example.com/about/": "<p>internal</p>",
            "https://other.test/page/": "<p>external</p>",
        }
        checker = FakeChecker(pages, follow_external=True)

        results = checker.crawl("https://example.com/", max_depth=1)

        self.assertIn("https://example.com/", results)
        self.assertIn("https://example.com/about/", results)
        self.assertIn("https://other.test/page/", results)


if __name__ == "__main__":
    unittest.main()
