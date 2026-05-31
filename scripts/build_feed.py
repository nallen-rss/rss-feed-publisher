from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import requests
from dateutil import parser as date_parser
from lxml import html


TEMPLATE_DIR = Path(os.environ.get("TEMPLATE_DIR", "templates"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "public"))
FEEDS_DIR = OUTPUT_DIR / "feeds"
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50"))
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "").strip().rstrip("/")
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 RSS feed generator for personal use",
)


@dataclass(frozen=True)
class FeedTemplate:
    title: str
    source_url: str
    description: str
    slug: str
    item_xpath: str
    title_xpath: str
    link_xpath: str
    content_xpath: str
    author_xpath: str
    timestamp_xpath: str
    thumbnail_xpath: str
    categories_xpath: str
    uid_xpath: str
    date_format: str


def local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def attrs_by_local_name(element: ElementTree.Element) -> dict[str, str]:
    return {local_name(key): value for key, value in element.attrib.items()}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "feed"


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def cdata(value: str) -> str:
    return value.replace("]]>", "]]]]><![CDATA[>")


def xpath_values(node, expression: str) -> list[str]:
    if not expression:
        return []

    results = node.xpath(expression)
    values = []
    for result in results:
        if isinstance(result, html.HtmlElement):
            values.append(normalize_space(result.text_content()))
        else:
            values.append(normalize_space(str(result)))
    return [value for value in values if value]


def xpath_first(node, expression: str) -> str:
    values = xpath_values(node, expression)
    return values[0] if values else ""


def parse_pub_date(value: str, date_format: str) -> str:
    if not value:
        return ""

    if date_format:
        parsed = datetime.strptime(value, date_format)
    else:
        parsed = date_parser.parse(value)

    return format_datetime(parsed)


def find_template_outlines(root: ElementTree.Element) -> list[ElementTree.Element]:
    outlines = []
    for outline in root.iter():
        if local_name(outline.tag) != "outline":
            continue

        attrs = attrs_by_local_name(outline)
        outline_type = attrs.get("type", "").lower()
        if outline_type in {"html+xpath", "html_xpath"} or attrs.get("xPathItem"):
            outlines.append(outline)

    return outlines


def template_from_outline(outline: ElementTree.Element, used_slugs: set[str]) -> FeedTemplate:
    attrs = attrs_by_local_name(outline)
    title = attrs.get("title") or attrs.get("text") or attrs.get("xmlUrl") or "Feed"
    source_url = attrs.get("xmlUrl") or attrs.get("htmlUrl") or attrs.get("url") or ""
    if not source_url:
        raise ValueError(f"Missing source URL for template: {title}")

    slug_base = attrs.get("slug") or title
    slug = slugify(slug_base)
    original_slug = slug
    counter = 2
    while slug in used_slugs:
        slug = f"{original_slug}-{counter}"
        counter += 1
    used_slugs.add(slug)

    item_xpath = attrs.get("xPathItem", "")
    title_xpath = attrs.get("xPathItemTitle", "")
    link_xpath = attrs.get("xPathItemUri", "")
    if not item_xpath or not title_xpath or not link_xpath:
        raise ValueError(f"Missing required XPath fields for template: {title}")

    return FeedTemplate(
        title=title,
        source_url=source_url,
        description=attrs.get("description", ""),
        slug=slug,
        item_xpath=item_xpath,
        title_xpath=title_xpath,
        link_xpath=link_xpath,
        content_xpath=attrs.get("xPathItemContent", ""),
        author_xpath=attrs.get("xPathItemAuthor", ""),
        timestamp_xpath=attrs.get("xPathItemTimestamp", ""),
        thumbnail_xpath=attrs.get("xPathItemThumbnail", ""),
        categories_xpath=attrs.get("xPathItemCategories", ""),
        uid_xpath=attrs.get("xPathItemUid", ""),
        date_format=attrs.get("xPathItemTimeFormat", ""),
    )


def load_templates() -> list[FeedTemplate]:
    used_slugs: set[str] = set()
    templates = []
    for path in sorted(TEMPLATE_DIR.glob("*.opml")):
        root = ElementTree.parse(path).getroot()
        for outline in find_template_outlines(root):
            templates.append(template_from_outline(outline, used_slugs))
    return templates


def render_item(template: FeedTemplate, item_node) -> str:
    title = xpath_first(item_node, template.title_xpath)
    link = urljoin(template.source_url, xpath_first(item_node, template.link_xpath))
    if not title or not link:
        return ""

    content = xpath_first(item_node, template.content_xpath)
    author = xpath_first(item_node, template.author_xpath)
    timestamp = xpath_first(item_node, template.timestamp_xpath)
    thumbnail = urljoin(template.source_url, xpath_first(item_node, template.thumbnail_xpath))
    categories = xpath_values(item_node, template.categories_xpath)
    guid = xpath_first(item_node, template.uid_xpath) or link

    pub_date = ""
    if timestamp:
        try:
            pub_date = parse_pub_date(timestamp, template.date_format)
        except Exception:
            pub_date = ""

    description_parts = []
    if thumbnail:
        description_parts.append(f'<p><img src="{escape(thumbnail)}" alt="" /></p>')
    if content:
        description_parts.append(f"<p>{escape(content)}</p>")
    description = "".join(description_parts)

    category_xml = "\n".join(
        f"      <category>{escape(category)}</category>"
        for category in dict.fromkeys(categories)
    )

    optional_xml = []
    if pub_date:
        optional_xml.append(f"      <pubDate>{escape(pub_date)}</pubDate>")
    if author:
        optional_xml.append(f"      <author>{escape(author)}</author>")
    if category_xml:
        optional_xml.append(category_xml)

    optional_block = "\n" + "\n".join(optional_xml) if optional_xml else ""
    return f"""    <item>
      <title>{escape(title)}</title>
      <link>{escape(link)}</link>
      <guid isPermaLink="false">{escape(guid)}</guid>
      <description><![CDATA[{cdata(description)}]]></description>{optional_block}
    </item>"""


def build_feed(template: FeedTemplate) -> str:
    response = requests.get(
        template.source_url,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()

    document = html.fromstring(response.content, base_url=template.source_url)
    item_nodes = document.xpath(template.item_xpath)[:MAX_ITEMS]
    items = [render_item(template, item_node) for item_node in item_nodes]
    items = [item for item in items if item]

    now = datetime.now().astimezone()
    feed_path = f"feeds/{template.slug}.xml"
    feed_url = f"{FEED_BASE_URL}/{feed_path}" if FEED_BASE_URL else ""
    atom_link = ""
    if feed_url:
        atom_link = (
            f'\n    <atom:link href="{escape(feed_url)}" rel="self" '
            'type="application/rss+xml" />'
        )

    description = template.description or f"Generated RSS feed for {template.source_url}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(template.title)}</title>
    <link>{escape(template.source_url)}</link>
    <description>{escape(description)}</description>
    <language>en-us</language>
    <lastBuildDate>{escape(format_datetime(now))}</lastBuildDate>{atom_link}
{chr(10).join(items)}
  </channel>
</rss>
"""


def write_index(templates: list[FeedTemplate]) -> None:
    rows = []
    for template in templates:
        path = f"feeds/{template.slug}.xml"
        url = f"{FEED_BASE_URL}/{path}" if FEED_BASE_URL else path
        rows.append(
            "      <li>"
            f'<a href="{escape(path)}">{escape(template.title)}</a>'
            f" <code>{escape(url)}</code>"
            "</li>"
        )

    body = "\n".join(rows) or "      <li>No OPML templates found.</li>"
    index_html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Static RSS feeds</title>
  </head>
  <body>
    <h1>Static RSS feeds</h1>
    <ul>
{body}
    </ul>
  </body>
</html>
"""
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    templates = load_templates()

    first_feed = None
    for template in templates:
        feed = build_feed(template)
        feed_path = FEEDS_DIR / f"{template.slug}.xml"
        feed_path.write_text(feed, encoding="utf-8")
        if first_feed is None:
            first_feed = feed

    if first_feed is not None:
        (OUTPUT_DIR / "feed.xml").write_text(first_feed, encoding="utf-8")

    write_index(templates)
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    main()
