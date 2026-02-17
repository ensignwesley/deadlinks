#!/usr/bin/env python3
"""
deadlinks — Dead Link Hunter CLI
Ensign Wesley, Daily Challenge #3

Usage:
  deadlinks https://example.com
  deadlinks https://example.com --depth 2
  deadlinks --file urls.txt --format markdown
  deadlinks https://example.com --fix
  deadlinks https://example.com --format json > report.json
"""

import argparse
import json
import re
import sys
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────

@dataclass
class LinkResult:
    url: str
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    redirect_url: Optional[str] = None
    is_broken: bool = False
    skipped: bool = False


# ──────────────────────────────────────────────────────────────
# Core checker
# ──────────────────────────────────────────────────────────────

class DeadLinkChecker:
    def __init__(self, timeout=10, rate_limit=0.1, max_workers=10,
                 verbose=False, follow_external=False):
        self.timeout = timeout
        self.rate_limit = rate_limit
        self.max_workers = max_workers
        self.verbose = verbose
        self.follow_external = follow_external

        self._cache: dict[str, LinkResult] = {}
        self._cache_lock = threading.Lock()
        self._crawled: set = set()
        self._crawled_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_request: dict[str, float] = defaultdict(float)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "deadlinks/1.0 (+https://github.com/ensignwesley/deadlinks; "
                "link-checker bot)"
            )
        })
        # Follow redirects automatically
        self.session.max_redirects = 10

    def _rate_limit_host(self, host: str):
        """Enforce per-host rate limiting."""
        with self._rate_lock:
            elapsed = time.time() - self._last_request[host]
            if elapsed < self.rate_limit:
                time.sleep(self.rate_limit - elapsed)
            self._last_request[host] = time.time()

    def check_link(self, url: str) -> LinkResult:
        """Check a single URL. Returns cached result if already checked."""
        # Cache check (thread-safe)
        with self._cache_lock:
            if url in self._cache:
                return self._cache[url]

        result = self._fetch(url)

        with self._cache_lock:
            self._cache[url] = result

        return result

    def _fetch(self, url: str) -> LinkResult:
        """Actually fetch and check a URL."""
        # Skip anchor-only links
        if url.startswith("#"):
            return LinkResult(url=url, skipped=True, error="anchor-only")

        parsed = urlparse(url)

        # Skip non-HTTP schemes (mailto:, tel:, ftp:, data:, etc.)
        if parsed.scheme not in ("http", "https"):
            return LinkResult(url=url, skipped=True,
                              error=f"skipped:{parsed.scheme}")

        host = parsed.netloc
        self._rate_limit_host(host)

        start = time.time()

        try:
            # Try HEAD first (fast, low bandwidth)
            resp = self.session.head(
                url, timeout=self.timeout, allow_redirects=True
            )
            elapsed_ms = (time.time() - start) * 1000

            # Some servers refuse HEAD — fall back to GET (stream to avoid
            # downloading the entire body)
            if resp.status_code in (405, 501):
                resp = self.session.get(
                    url, timeout=self.timeout,
                    allow_redirects=True, stream=True
                )
                # Close connection immediately — we only need the status
                resp.close()
                elapsed_ms = (time.time() - start) * 1000

            # Detect redirect
            redirect_url = resp.url if resp.history else None
            if redirect_url == url:
                redirect_url = None

            # 429 = rate limited — mark as broken with specific message
            if resp.status_code == 429:
                return LinkResult(
                    url=url,
                    status_code=429,
                    response_time_ms=elapsed_ms,
                    error="rate-limited (429)",
                    is_broken=True,
                )

            is_broken = resp.status_code >= 400

            return LinkResult(
                url=url,
                status_code=resp.status_code,
                response_time_ms=elapsed_ms,
                redirect_url=redirect_url,
                is_broken=is_broken,
            )

        except requests.exceptions.SSLError as e:
            elapsed_ms = (time.time() - start) * 1000
            return LinkResult(
                url=url,
                response_time_ms=elapsed_ms,
                error=f"SSL error: {_truncate(str(e), 80)}",
                is_broken=True,
            )

        except requests.exceptions.TooManyRedirects:
            elapsed_ms = (time.time() - start) * 1000
            return LinkResult(
                url=url,
                response_time_ms=elapsed_ms,
                error="Too many redirects",
                is_broken=True,
            )

        except requests.exceptions.ConnectionError as e:
            elapsed_ms = (time.time() - start) * 1000
            return LinkResult(
                url=url,
                response_time_ms=elapsed_ms,
                error=f"Connection error: {_truncate(str(e), 80)}",
                is_broken=True,
            )

        except requests.exceptions.Timeout:
            elapsed_ms = (time.time() - start) * 1000
            return LinkResult(
                url=url,
                response_time_ms=elapsed_ms,
                error=f"Timeout (>{self.timeout}s)",
                is_broken=True,
            )

        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            return LinkResult(
                url=url,
                response_time_ms=elapsed_ms,
                error=_truncate(str(e), 80),
                is_broken=True,
            )

    def _get_page(self, url: str) -> Optional[str]:
        """Fetch a page and return HTML content, or None on failure."""
        parsed = urlparse(url)
        host = parsed.netloc
        self._rate_limit_host(host)

        try:
            resp = self.session.get(
                url, timeout=self.timeout, allow_redirects=True
            )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "text/html" in content_type:
                return resp.text
        except Exception:
            pass
        return None

    def extract_links(self, base_url: str, html: str) -> list[str]:
        """Extract all links from HTML, resolved to absolute URLs."""
        soup = BeautifulSoup(html, "html.parser")
        links = set()

        # <a href>, <link href>
        for tag in soup.find_all(["a", "link"], href=True):
            href = tag["href"].strip()
            if href and not href.startswith(("mailto:", "tel:", "javascript:")):
                abs_url = _resolve(base_url, href)
                if abs_url:
                    links.add(abs_url)

        # <img src>, <script src>, <iframe src>, <video src>, <source src>
        for tag in soup.find_all(
            ["img", "script", "iframe", "video", "audio", "source"], src=True
        ):
            src = tag["src"].strip()
            if src and not src.startswith(("data:", "javascript:")):
                abs_url = _resolve(base_url, src)
                if abs_url:
                    links.add(abs_url)

        return sorted(links)

    def crawl(self, start_url: str, max_depth: int = 1) -> dict[str, list[LinkResult]]:
        """
        Crawl start_url up to max_depth levels deep.
        Returns: {source_url: [LinkResult, ...]}
        """
        results: dict[str, list[LinkResult]] = {}
        # queue items: (url, depth)
        queue = [(start_url, 0)]
        base_domain = urlparse(start_url).netloc

        while queue:
            url, depth = queue.pop(0)

            with self._crawled_lock:
                if url in self._crawled:
                    continue
                self._crawled.add(url)

            _log(f"Crawling [{depth}]: {url}", self.verbose)

            html = self._get_page(url)
            if not html:
                _log(f"  ↳ Could not fetch page", self.verbose)
                continue

            links = self.extract_links(url, html)
            _log(f"  ↳ Found {len(links)} links", self.verbose)

            # Check all links on this page concurrently
            page_results = self._check_links_concurrent(links)
            results[url] = page_results

            # Enqueue same-domain pages for deeper crawl
            if depth < max_depth:
                for result in page_results:
                    if result.skipped or result.is_broken:
                        continue
                    link_domain = urlparse(result.url).netloc
                    if link_domain != base_domain:
                        continue
                    with self._crawled_lock:
                        already = result.url in self._crawled
                    if not already:
                        queue.append((result.url, depth + 1))

        return results

    def _check_links_concurrent(self, links: list[str]) -> list[LinkResult]:
        """Check a list of links concurrently."""
        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.check_link, url): url for url in links}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    url = futures[future]
                    results.append(LinkResult(url=url, error=str(e), is_broken=True))
        return results


# ──────────────────────────────────────────────────────────────
# URL fix suggestions
# ──────────────────────────────────────────────────────────────

def suggest_fix(url: str) -> Optional[str]:
    """
    Suggest a corrected URL for common mistakes.
    Returns a suggestion string, or None if no fix found.
    """
    parsed = urlparse(url)

    # 1. HTTP → HTTPS upgrade
    if parsed.scheme == "http":
        suggestion = url.replace("http://", "https://", 1)
        return f"Try HTTPS: {suggestion}"

    # 2. Double slashes in path (not counting ://)
    if "//" in parsed.path:
        fixed = parsed._replace(path=re.sub(r"/+", "/", parsed.path)).geturl()
        return f"Double slash in path: {fixed}"

    # 3. Missing www
    if parsed.netloc and not parsed.netloc.startswith("www."):
        suggestion = parsed._replace(
            netloc=f"www.{parsed.netloc}"
        ).geturl()
        return f"Try with www: {suggestion}"

    # 4. Trailing space / invisible chars
    stripped = url.strip()
    if stripped != url:
        return f"URL has leading/trailing whitespace: {stripped}"

    # 5. Common domain typos (very basic)
    typos = {
        "gogle.com": "google.com",
        "googel.com": "google.com",
        "githb.com": "github.com",
        "gituhb.com": "github.com",
        "stackoverlfow.com": "stackoverflow.com",
    }
    for wrong, right in typos.items():
        if wrong in url:
            return f"Domain typo? {url.replace(wrong, right)}"

    return None


# ──────────────────────────────────────────────────────────────
# Output formatters
# ──────────────────────────────────────────────────────────────

def format_terminal(
    results: dict[str, list[LinkResult]],
    show_all: bool = False,
    show_fix: bool = False,
) -> str:
    lines = []
    total_broken = 0
    total_checked = 0
    total_skipped = 0

    for source_url, link_results in sorted(results.items()):
        broken = [r for r in link_results if r.is_broken]
        skipped = [r for r in link_results if r.skipped]
        checked = [r for r in link_results if not r.skipped]
        total_broken += len(broken)
        total_checked += len(checked)
        total_skipped += len(skipped)

        has_content = broken or (show_all and checked)
        if not has_content:
            continue

        lines.append(f"\n┌─ {source_url}")
        lines.append(f"│  Checked: {len(checked)} | Broken: {len(broken)} | Skipped: {len(skipped)}")

        if broken:
            lines.append("│  BROKEN:")
            for r in sorted(broken, key=lambda x: x.url):
                badge = f"[{r.status_code}]" if r.status_code else f"[ERR]"
                time_str = f"{r.response_time_ms:.0f}ms" if r.response_time_ms else "N/A"
                error_str = f" — {r.error}" if r.error else ""
                lines.append(f"│  ❌ {badge} {r.url} ({time_str}){error_str}")
                if r.redirect_url:
                    lines.append(f"│     ↳ redirected → {r.redirect_url}")
                if show_fix:
                    fix = suggest_fix(r.url)
                    if fix:
                        lines.append(f"│     💡 {fix}")
        
        if show_all:
            ok_links = [r for r in checked if not r.is_broken]
            for r in sorted(ok_links, key=lambda x: x.url):
                badge = f"[{r.status_code}]" if r.status_code else "[OK]"
                time_str = f"{r.response_time_ms:.0f}ms" if r.response_time_ms else "N/A"
                redirect = f" → {r.redirect_url}" if r.redirect_url else ""
                lines.append(f"│  ✅ {badge} {r.url} ({time_str}){redirect}")

        lines.append("└" + "─" * 60)

    if not lines:
        lines.append("\n✅ No broken links found!")

    # Summary header
    summary = [
        "",
        "=" * 62,
        "  DEAD LINK REPORT",
        "=" * 62,
        f"  Pages crawled : {len(results)}",
        f"  Links checked : {total_checked}",
        f"  Broken links  : {total_broken}",
        f"  Skipped       : {total_skipped}",
        "=" * 62,
    ]

    return "\n".join(summary + lines)


def format_json(
    results: dict[str, list[LinkResult]],
    show_all: bool = False,
) -> str:
    total_broken = sum(len([r for r in v if r.is_broken]) for v in results.values())
    total_checked = sum(len([r for r in v if not r.skipped]) for v in results.values())

    output = {
        "summary": {
            "pages_crawled": len(results),
            "links_checked": total_checked,
            "broken_links": total_broken,
        },
        "pages": {},
    }

    for source_url, link_results in sorted(results.items()):
        broken = [r for r in link_results if r.is_broken]
        checked = [r for r in link_results if not r.skipped]

        page_data: dict = {
            "links_checked": len(checked),
            "broken_count": len(broken),
            "broken": [_result_to_dict(r) for r in broken],
        }

        if show_all:
            ok = [r for r in checked if not r.is_broken]
            page_data["ok"] = [_result_to_dict(r) for r in ok]

        output["pages"][source_url] = page_data

    return json.dumps(output, indent=2)


def format_markdown(
    results: dict[str, list[LinkResult]],
    show_fix: bool = False,
) -> str:
    lines = ["# Dead Link Report\n"]

    total_broken = sum(len([r for r in v if r.is_broken]) for v in results.values())
    total_checked = sum(len([r for r in v if not r.skipped]) for v in results.values())

    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Pages crawled | {len(results)} |")
    lines.append(f"| Links checked | {total_checked} |")
    lines.append(f"| Broken links | **{total_broken}** |")
    lines.append("")

    has_broken = any(any(r.is_broken for r in v) for v in results.values())

    if not has_broken:
        lines.append("✅ **No broken links found!**")
        return "\n".join(lines)

    for source_url, link_results in sorted(results.items()):
        broken = [r for r in link_results if r.is_broken]
        if not broken:
            continue

        lines.append(f"## `{source_url}`\n")
        lines.append("| URL | Status | Time | Error |")
        lines.append("|-----|--------|------|-------|")

        for r in sorted(broken, key=lambda x: x.url):
            status = str(r.status_code) if r.status_code else "—"
            time_str = f"{r.response_time_ms:.0f}ms" if r.response_time_ms else "N/A"
            error = r.error or ""
            url_cell = f"[link]({r.url})"
            lines.append(f"| {url_cell} | {status} | {time_str} | {error} |")

            if show_fix:
                fix = suggest_fix(r.url)
                if fix:
                    lines.append(f"\n> 💡 **Suggestion:** {fix}\n")

        lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _resolve(base: str, href: str) -> Optional[str]:
    """Resolve href relative to base, strip fragment, return clean URL."""
    try:
        abs_url = urljoin(base, href)
        parsed = urlparse(abs_url)
        # Strip fragment
        clean = parsed._replace(fragment="").geturl()
        return clean
    except Exception:
        return None


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[:max_len] + "…"


def _log(msg: str, verbose: bool = True):
    if verbose:
        print(f"  {msg}", file=sys.stderr)


def _result_to_dict(r: LinkResult) -> dict:
    return {
        "url": r.url,
        "status_code": r.status_code,
        "response_time_ms": round(r.response_time_ms, 1) if r.response_time_ms else None,
        "error": r.error,
        "redirect_url": r.redirect_url,
        "is_broken": r.is_broken,
    }


# ──────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="deadlinks",
        description="deadlinks — Hunt down broken links on websites",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  deadlinks https://example.com
  deadlinks https://example.com --depth 2 --verbose
  deadlinks --file urls.txt --format markdown
  deadlinks https://example.com --fix
  deadlinks https://example.com --format json > report.json
  deadlinks https://my-blog.com --depth 3 --workers 20
""",
    )

    parser.add_argument("urls", nargs="*", help="URLs to check")
    parser.add_argument("--file", "-f", metavar="FILE",
                        help="File with URLs, one per line")
    parser.add_argument("--depth", "-d", type=int, default=1,
                        help="Crawl depth (default: 1; 0 = only the given page)")
    parser.add_argument("--timeout", "-t", type=int, default=10,
                        help="Request timeout in seconds (default: 10)")
    parser.add_argument("--rate-limit", type=float, default=0.1,
                        help="Delay between requests per host in seconds (default: 0.1)")
    parser.add_argument("--workers", "-w", type=int, default=10,
                        help="Concurrent workers for link checking (default: 10)")
    parser.add_argument("--format", choices=["terminal", "json", "markdown"],
                        default="terminal",
                        help="Output format (default: terminal)")
    parser.add_argument("--fix", action="store_true",
                        help="Suggest fixes for broken URLs")
    parser.add_argument("--all", action="store_true",
                        help="Show all links, not just broken ones")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show crawl progress")
    parser.add_argument("--external", action="store_true",
                        help="Also follow external links when crawling")

    args = parser.parse_args()

    # Collect URLs
    urls = list(args.urls)
    if args.file:
        try:
            with open(args.file) as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
        except FileNotFoundError:
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)

    if not urls:
        parser.print_help()
        sys.exit(1)

    checker = DeadLinkChecker(
        timeout=args.timeout,
        rate_limit=args.rate_limit,
        max_workers=args.workers,
        verbose=args.verbose,
        follow_external=args.external,
    )

    all_results: dict[str, list[LinkResult]] = {}

    for url in urls:
        if args.verbose:
            print(f"\n[deadlinks] Starting: {url}", file=sys.stderr)
        results = checker.crawl(url, max_depth=args.depth)
        all_results.update(results)

    # Output
    if args.format == "json":
        print(format_json(all_results, show_all=args.all))
    elif args.format == "markdown":
        print(format_markdown(all_results, show_fix=args.fix))
    else:
        print(format_terminal(all_results, show_all=args.all, show_fix=args.fix))


if __name__ == "__main__":
    main()
