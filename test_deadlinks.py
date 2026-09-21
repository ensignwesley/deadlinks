import unittest
from unittest.mock import Mock

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


class HeadFallbackTests(unittest.TestCase):
    @staticmethod
    def response(status_code, url="https://example.com/resource"):
        response = Mock()
        response.status_code = status_code
        response.url = url
        response.history = []
        return response

    def checker_with_responses(self, head_status, get_status):
        checker = DeadLinkChecker(rate_limit=0)
        checker.session.head = Mock(return_value=self.response(head_status))
        checker.session.get = Mock(return_value=self.response(get_status))
        return checker

    def test_failing_head_is_confirmed_by_streamed_get(self):
        checker = self.checker_with_responses(404, 200)

        result = checker.check_link("https://example.com/resource")

        self.assertEqual(200, result.status_code)
        self.assertFalse(result.is_broken)
        checker.session.get.assert_called_once_with(
            "https://example.com/resource",
            timeout=10,
            allow_redirects=True,
            stream=True,
        )
        checker.session.get.return_value.close.assert_called_once_with()

    def test_failing_get_remains_broken_after_head_confirmation(self):
        checker = self.checker_with_responses(500, 404)

        result = checker.check_link("https://example.com/resource")

        self.assertEqual(404, result.status_code)
        self.assertTrue(result.is_broken)
        checker.session.get.return_value.close.assert_called_once_with()

    def test_successful_head_does_not_issue_get(self):
        checker = self.checker_with_responses(200, 200)

        result = checker.check_link("https://example.com/resource")

        self.assertEqual(200, result.status_code)
        self.assertFalse(result.is_broken)
        checker.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
