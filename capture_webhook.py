#!/usr/bin/env python3
"""Screenshot the webhook.site dashboard for a webhook URL.

Usage:
    python capture_webhook.py https://webhook.site/<uuid> [output.png]

The webhook URL (https://webhook.site/<uuid>) is turned into its dashboard view
(https://webhook.site/#!/view/<uuid>). Any other URL is opened as given, which is
handy for a local receiver page. Requires: python -m playwright install chromium
"""

import re
import sys

from playwright.sync_api import sync_playwright

UUID_RE = re.compile(r"webhook\.site/(?:#!/view/)?([0-9a-f-]{36})", re.I)


def dashboard_url(url):
    match = UUID_RE.search(url)
    return f"https://webhook.site/#!/view/{match.group(1)}" if match else url


# Blur the sender's public IP address, geo-location and ISP before taking the screenshot.
BLUR_PRIVATE_JS = r"""() => {
  const ip = /\b\d{1,3}(\.\d{1,3}){3}\b/;
  document.querySelectorAll('body *').forEach(el => {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
    if (ip.test(own)) el.style.filter = 'blur(5px)';
  });
  document.querySelectorAll('tr, .row, div').forEach(row => {
    const first = row.firstElementChild;
    if (first && first.textContent.trim() === 'Location' && row.children.length <= 3)
      [...row.children].slice(1).forEach(c => c.style.filter = 'blur(5px)');
  });
}"""


def capture(url, output="webhook-test.png", wait_ms=4000):
    """Save the latest request (output) and the oldest one (output with -first suffix)."""
    target = dashboard_url(url)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(target, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(wait_ms)  # let the request list render
        page.evaluate(BLUR_PRIVATE_JS)
        page.screenshot(path=output)
        print(f"Saved {output} ({target})")

        items = page.locator("a.select")  # entries in the INBOX list, newest first
        if items.count() > 1:
            items.last.click()
            page.wait_for_timeout(1500)
            page.evaluate(BLUR_PRIVATE_JS)
            first = output.replace(".png", "-first.png")
            page.screenshot(path=first)
            print(f"Saved {first} (oldest request)")
        browser.close()


def main(argv):
    if len(argv) < 2 or not argv[1].strip():
        print(__doc__)
        return 2
    capture(argv[1], argv[2] if len(argv) > 2 else "webhook-test.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
