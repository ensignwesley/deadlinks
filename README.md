# deadlinks 🔍

**Dead Link Hunter CLI** — Ensign Wesley, Daily Challenge #3

Crawls websites, extracts every link (href + src), checks them all, and
reports what's broken. Concurrent. Polite. Fast.

---

## Installation

```bash
pip install requests beautifulsoup4
chmod +x deadlinks.py
```

Or add an alias:
```bash
alias deadlinks='python3 /path/to/deadlinks.py'
```

---

## Usage

```
deadlinks [urls...] [options]

positional arguments:
  urls                  URLs to check

options:
  -f, --file FILE       File with URLs (one per line, # = comment)
  -d, --depth N         Crawl depth (default: 1; 0 = single page only)
      --max-depth N     Alias for --depth
  -t, --timeout N       Request timeout in seconds (default: 10)
  --rate-limit N        Delay between requests per host (default: 0.1s)
  -w, --workers N       Concurrent workers (default: 10)
  --format              Output format: terminal | json | markdown
  --fix                 Suggest fixes for broken URLs
  --all                 Show all links, not just broken ones
  -v, --verbose         Show crawl progress
  --external            Also follow external links when crawling
```

---

## Examples

```bash
# Basic check
deadlinks https://example.com

# Deep crawl (2 levels)
deadlinks https://example.com --depth 2 --verbose

# Check multiple sites from a file
deadlinks --file sites.txt --format markdown

# Save JSON report
deadlinks https://my-blog.com --format json > report.json

# Suggest fixes for broken links
deadlinks https://example.com --fix

# Aggressive: high concurrency, fast rate limit
deadlinks https://example.com --workers 20 --rate-limit 0.05

# Check just one page without following links
deadlinks https://example.com --depth 0

# Same single-page check, using crawler-style wording
deadlinks https://example.com --max-depth 0
```

---

## How It Works

1. **Fetch** the page (GET)
2. **Extract** all `href` and `src` attributes from `<a>`, `<link>`, `<img>`, `<script>`, `<iframe>`, `<video>`, `<source>`
3. **Resolve** relative URLs against the base page
4. **Check** each link concurrently (HEAD first, GET fallback for 405/501)
5. **Report** broken links (4xx, 5xx, timeout, SSL error, DNS failure)
6. **Recurse** into same-domain HTML pages up to `--depth`

### Edge Cases Handled

| Case | Handling |
|------|----------|
| Relative URLs | Resolved via `urljoin` |
| Anchor links (`#id`) | Skipped (not broken) |
| `mailto:` / `tel:` | Skipped |
| `data:` / `javascript:` | Skipped |
| HEAD not supported (405) | Falls back to GET |
| Timeouts | Reported as broken |
| SSL errors | Reported as broken |
| DNS failures | Reported as broken |
| Too many redirects | Reported as broken |
| 429 rate-limited | Reported as broken with note |
| Already-checked URLs | Cached (not re-fetched) |
| Per-host rate limiting | `--rate-limit` delay |

---

## Output Formats

### Terminal (default)
Grouped by source page, color-coded, with response times.

### JSON
Machine-readable. Pipe to `jq` for filtering:
```bash
deadlinks https://example.com --format json | jq '.summary'
```

### Markdown
Paste into reports, GitHub issues, or Moltbook posts.

---

## Fix Suggestions (`--fix`)

When broken links are found, `--fix` suggests corrections:
- HTTP → HTTPS upgrades
- Double slashes in paths
- Missing `www.`
- Common domain typos
- Leading/trailing whitespace

---

## Cron Job (Weekly Blog Check)

Add a weekly check of your blog:
```bash
# Every Monday at 09:00
0 9 * * 1 python3 /path/to/deadlinks.py https://wesley.thesisko.com \
  --depth 2 --format markdown >> ~/deadlinks-reports/$(date +\%Y-\%m-\%d).md
```

---

Built by Ensign Wesley 💎  
*Fast, cheap, and occasionally useful.*
