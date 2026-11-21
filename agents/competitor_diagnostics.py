"""Local, privacy-conscious diagnostics for public competitor page rendering.

Never copy cookies, request bodies, complete HTML, or the browser profile into
the report. A visible human price is not proof that it belongs to the merchant.
"""
from collections import Counter
import re
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error

from .competitor_dom import CONDITIONAL

RUBLE_AMOUNT = re.compile(
    r"(?<![\d.,])(?:\d{1,3}(?:[ \u00a0\u202f]+\d{3})+|\d+)"
    r"(?:[.,]\d{1,2})?[ \u00a0\u202f]*(?:₽|руб\.?(?!\w))",
    re.I,
)
OLD_PRICE = "s, del, strike, [data-old-price], [data-testid*='old-price']"


def visible_prices(page, lines):
    old = Counter(
        " ".join(match.group().split())
        for text in page.locator(OLD_PRICE).filter(visible=True).all_inner_texts()
        for match in RUBLE_AMOUNT.finditer(text)
    )
    amounts = Counter(" ".join(match.group().split())
                      for line in lines for match in RUBLE_AMOUNT.finditer(line))
    entries = []
    previous_label = ""
    for line in filter(None, lines):
        end = 0
        for match in RUBLE_AMOUNT.finditer(line):
            text = " ".join(match.group().split())
            label = line[end:match.start()] or (previous_label if end == 0 else "")
            kind = "regular"
            if CONDITIONAL.search(label):
                kind = "special"
            elif old[text]:
                # The flattened text cannot identify which copy of an equal amount was struck.
                kind = "crossed_out" if old[text] == amounts[text] else "ambiguous"
            entries.append({"text": text, "kind": kind})
            if len(entries) == 18:
                return entries
            end = match.end()
        previous_label = line if end == 0 and CONDITIONAL.search(line) else ""
    return entries


def page_snapshot(page, product, source):
    report = {
        "source": source,
        "product": f"{product.brand} {product.name} {product.volume_ml} мл",
        "browser_url": "",
        "title": "",
        "headings": [],
        "price_lines": [],
        "visible_prices": [],
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
        report["visible_prices"] = visible_prices(page, lines)
        report["price_lines"] = [entry["text"] for entry in report["visible_prices"]]
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
