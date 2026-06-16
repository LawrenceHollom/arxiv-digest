#!/usr/bin/env python3

# Calling this "vibe coding" is actually *over*-emphasising how much
# input I had in writing this.

"""
Robust scraper for arXiv listing pages (e.g. https://arxiv.org/list/math.CO/recent).

Extracts every paper on the page as a record with its title, authors and abstract
(plus a few useful identifiers), and emits the result as JSON.

Usage
-----
  # From a saved HTML file:
  python arxiv_scraper.py page.html > papers.json

  # From stdin:
  cat page.html | python arxiv_scraper.py > papers.json

  # Directly from a live URL (optional, stdlib only):
  python arxiv_scraper.py --url https://arxiv.org/list/math.CO/recent > papers.json

The parser depends only on the page's structural markup (the <dl id='articles'>
lists of <dt>/<dd> pairs), not on the exact number of entries or sections, so it
degrades gracefully when fields are missing or the layout shifts slightly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
import arxiv

print(arxiv.__file__)

from html.parser import HTMLParser
from typing import Optional

from bs4 import BeautifulSoup, Tag

# arXiv ids look like 2606.14877 (new style) or math/0211159 (old style),
# optionally with a version suffix such as v2.
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)
_WS_RE = re.compile(r"\s+")
URL = "https://arxiv.org/list/math.CO/new?skip=0&show=100"
DELAY = 3


def _make_soup(html: str) -> BeautifulSoup:
    """Build a parser, preferring lxml and falling back to the stdlib parser."""
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    # html.parser ships with CPython, so this last attempt should always work.
    return BeautifulSoup(html, "html.parser")


def _clean(text: Optional[str]) -> str:
    """Collapse all runs of whitespace (incl. newlines from <br>) to single spaces."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def _text_without_descriptor(node: Tag) -> str:
    """
    Return a node's text with its leading 'Title:'/'Authors:'/... descriptor
    span removed. Works on a copy so the original tree is untouched.
    """
    node = node.__copy__()
    for desc in node.select("span.descriptor"):
        desc.extract()
    return _clean(node.get_text(separator=" "))


def _extract_arxiv_id(dt: Tag) -> Optional[str]:
    """Pull the arXiv identifier out of a <dt> block."""
    # Most reliable: the abstract anchor whose href is /abs/<id> and id="<id>".
    abs_link = dt.find("a", href=re.compile(r"/abs/"))
    if abs_link is not None:
        if abs_link.get("id"):
            return abs_link["id"].strip()
        m = _ARXIV_ID_RE.search(abs_link.get("href", ""))
        if m:
            return m.group(0)
        m = _ARXIV_ID_RE.search(abs_link.get_text())
        if m:
            return m.group(0)
    # Fallback: scan the whole <dt> text for an arXiv id.
    m = _ARXIV_ID_RE.search(dt.get_text())
    return m.group(0) if m else None


def _link(dt: Tag, kind: str) -> Optional[str]:
    """Return an absolute URL for pdf/html/abs links found in the <dt>."""
    patterns = {
        "abs": r"/abs/",
        "pdf": r"/pdf/",
        "html": r"arxiv\.org/html/|/html/",
    }
    a = dt.find("a", href=re.compile(patterns[kind]))
    if a is None:
        return None
    href = a.get("href", "")
    if href.startswith("http"):
        return href
    return "https://arxiv.org" + href


def _authors(dd: Tag) -> list[str]:
    """Extract the author list from a <dd> block."""
    block = dd.select_one(".list-authors")
    if block is None:
        return []
    # Each author is normally its own <a>; this also drops the 'Authors:' label.
    names = [_clean(a.get_text()) for a in block.find_all("a")]
    names = [n for n in names if n]
    if names:
        return names
    # Fallback for pages that render authors as plain text.
    raw = _text_without_descriptor(block)
    return [_clean(n) for n in raw.split(",") if _clean(n)]


def _subjects(dd: Tag) -> dict:
    """Extract primary and full subject classification, if present."""
    block = dd.select_one(".list-subjects")
    if block is None:
        return {"primary": None, "all": []}
    primary_tag = block.select_one(".primary-subject")
    primary = _clean(primary_tag.get_text()) if primary_tag else None
    full = _text_without_descriptor(block)
    subjects = [_clean(s) for s in full.split(";") if _clean(s)]
    return {"primary": primary, "all": subjects}


def _abstract(dd: Tag) -> Optional[str]:
    """
    Extract the abstract. On full-text listing pages it is the <p class='mathjax'>
    inside .meta; on compact pages it may be absent.
    """
    meta = dd.select_one(".meta") or dd
    # The abstract is a <p> (the title uses a <div>), so target <p> specifically.
    for p in meta.find_all("p"):
        txt = _clean(p.get_text(separator=" "))
        if txt:
            return txt
    return None


def _title(dd: Tag) -> Optional[str]:
    block = dd.select_one(".list-title")
    if block is None:
        return None
    return _text_without_descriptor(block) or None


def _comments(dd: Tag) -> Optional[str]:
    block = dd.select_one(".list-comments")
    if block is None:
        return None
    return _text_without_descriptor(block) or None


def parse_arxiv_listing(html: str) -> list[dict]:
    """
    Parse an arXiv listing page and return a list of paper records.

    Each record has: arxiv_id, title, authors, abstract, primary_subject,
    subjects, comments, listing_type, and abs/pdf/html URLs.
    """
    soup = _make_soup(html)
    papers: list[dict] = []

    # Each batch of papers lives in a <dl id="articles">. There can be several
    # (New submissions, Cross submissions, Replacements, ...).
    dls = soup.find_all("dl", id="articles") or soup.find_all("dl")
    for dl in dls:
        # The section label is the <h3> heading for this <dl>, e.g.
        # "New submissions (showing 50 of 50 entries)" -> "New submissions".
        heading_tag = dl.find("h3")
        listing_type = _clean(heading_tag.get_text()) if heading_tag else None
        if listing_type:
            listing_type = re.sub(r"\s*\(.*?\)\s*$", "", listing_type)

        # A paper is a <dd>; its metadata <dt> is the immediately preceding sibling.
        for dd in dl.find_all("dd"):
            dt = dd.find_previous_sibling("dt")
            if dt is None:
                continue

            record = {
                "arxiv_id": _extract_arxiv_id(dt),
                "title": _title(dd),
                "authors": _authors(dd),
                "abstract": _abstract(dd),
                "comments": _comments(dd),
                "listing_type": listing_type,
                "abs_url": _link(dt, "abs"),
                "pdf_url": _link(dt, "pdf"),
                "html_url": _link(dt, "html"),
            }
            subj = _subjects(dd)
            record["primary_subject"] = subj["primary"]
            record["subjects"] = subj["all"]

            # Keep only records that actually look like a paper.
            if record["title"] or record["arxiv_id"]:
                papers.append(record)

    return papers

def fetch(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "arxiv-daily-digest/1.0 (private research tool)"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                raise


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape papers from an arXiv listing page.")
    ap.add_argument("path", nargs="?", default="-",
                    help="HTML file to parse, or '-' for stdin (default).")
    ap.add_argument("--url", help="Fetch and parse this arXiv listing URL directly.")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent (default 2).")
    args = ap.parse_args()

    html = fetch(URL)
    papers = parse_arxiv_listing(html)
    json.dump(papers, sys.stdout, ensure_ascii=False, indent=args.indent)
    sys.stdout.write("\n")
    print(f"[parsed {len(papers)} papers]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
