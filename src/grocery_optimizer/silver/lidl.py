"""Parse Lidl ESI HTML (bronze) into structured records.

``parse_overview`` extracts the list of leaflets from an esi-overview page.

Flyer-detail (product-level) parsing is DEFERRED: the esi-flyer detail page is a
React SPA shell (``<div id="root">`` + a JS bundle on lidl.leaflets.schwarz)
whose products load client-side, and the overview also links image-based PNG
flyer pages. Per the spec, image/JS-walled ads defer to a Playwright render or
the leaflets.schwarz JSON API (a later refinement); the overview prober is the
working deliverable for now.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_LIDL_BASE = "https://www.lidl.com"
_FLYER_HREF = "/flyer/esi-flyer/"
# e.g. "Weekly Ad 7/8/2026-7/14/2026"
_DATE_RANGE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})")


def parse_overview(content: bytes | str) -> list[dict]:
    """Return the leaflets linked from an esi-overview page.

    Each dict: ``title``, ``detail_url``, and ``valid_from``/``valid_to`` (raw
    M/D/YYYY strings) when a date range is present in the title.
    """
    soup = BeautifulSoup(content, "html.parser")
    flyers: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _FLYER_HREF not in href:
            continue
        url = href if href.startswith("http") else _LIDL_BASE + href
        if url in seen:
            continue
        seen.add(url)
        # Title: link text, else an <img alt>, else the slug.
        title = a.get_text(strip=True)
        if not title:
            img = a.find("img")
            if img and img.get("alt"):
                title = img["alt"].strip()
        if not title:
            title = url.rsplit("/", 3)[-3] if "/ar/" in url else url

        valid_from = valid_to = None
        m = _DATE_RANGE.search(title)
        if m:
            valid_from, valid_to = m.group(1), m.group(2)
        flyers.append(
            {
                "title": title,
                "detail_url": url,
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )
    return flyers
