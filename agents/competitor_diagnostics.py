"""Local, privacy-conscious diagnostics for public competitor page rendering.

Never copy cookies, request bodies, complete HTML, or the browser profile into
the report. A visible human price is not proof that it belongs to the merchant.
"""
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error


def page_snapshot(page, product, source):
    report = {
        "source": source,
        "product": f"{product.brand} {product.name} {product.volume_ml} мл",
        "browser_url": "",
        "title": "",
        "headings": [],
        "price_lines": [],
        "seller_lines": [],
        "volume_lines": [],
    }
    if page is None:
        return report
    try:
        url = urlsplit(page.url)
        report["browser_url"] = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
        report["title"] = page.title()[:180]
        report["headings"] = [t[:180] for t in page.locator("h1:visible").all_inner_texts()[:5]]
        lines = [line.strip() for line in page.locator("body").inner_text(timeout=2000).splitlines()]
        report["price_lines"] = [line[:130] for line in lines if "₽" in line or "руб" in line.lower()][:18]
        report["seller_lines"] = [text[:130] for i, line in enumerate(lines)
                                  if "продавец" in line.lower()
                                  for text in lines[i:i + 2]][:6]
        report["volume_lines"] = [line[:80] for line in lines
                                  if line.lower().endswith(("мл", "ml"))][:12]
    except (Error, ValueError) as exc:
        report["snapshot_error"] = type(exc).__name__
    return report


def resource_summary(events):
    skipped = Counter()
    for event in events:
        if "skipped" not in event:
            continue
        host = urlsplit(event.get("url", "")).hostname or "unknown"
        skipped[(event.get("method", "unknown"), event.get("resource_type", "unknown"), host)] += 1
    return [{"method": method, "type": kind, "host": host, "count": count}
            for (method, kind, host), count in skipped.most_common(25)]
