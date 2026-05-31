from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as date_parser
from lxml import html
from readability import Document


TEMPLATE_DIR = Path(os.environ.get("TEMPLATE_DIR", "templates"))
CUSTOM_FEED_DIR = Path(os.environ.get("CUSTOM_FEED_DIR", "custom_feeds"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "public"))
FEEDS_DIR = OUTPUT_DIR / "feeds"
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50"))
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "").strip().rstrip("/")
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Android 15; Mobile; rv:139.0) Gecko/139.0 Firefox/139.0",
)
DEFAULT_TIMEZONE = ZoneInfo(os.environ.get("DEFAULT_TIMEZONE", "America/New_York"))
EXTRACT_FULL_CONTENT = os.environ.get("EXTRACT_FULL_CONTENT", "").lower() in {
    "1",
    "true",
    "yes",
    "readability",
}


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
    css_full_content: str
    css_content_filter: str


@dataclass(frozen=True)
class CustomFeed:
    kind: str
    title: str
    slug: str
    description: str
    source_url: str
    max_issues: int


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

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DEFAULT_TIMEZONE)

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
        css_full_content=attrs.get("cssFullContent", ""),
        css_content_filter=attrs.get("cssContentFilter", ""),
    )


def fetch_html(url: str, user_agent: str) -> bytes:
    response = requests.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.content


def load_templates() -> list[FeedTemplate]:
    used_slugs: set[str] = set()
    templates = []
    paths = sorted({*TEMPLATE_DIR.glob("*.opml"), *TEMPLATE_DIR.glob("*.opml.xml")})
    for path in paths:
        root = ElementTree.parse(path).getroot()
        for outline in find_template_outlines(root):
            templates.append(template_from_outline(outline, used_slugs))
    return templates


def load_custom_feeds() -> list[CustomFeed]:
    feeds = []
    for path in sorted(CUSTOM_FEED_DIR.glob("*.json")):
        config = json.loads(path.read_text(encoding="utf-8"))
        kind = config.get("kind", "")
        title = config.get("title") or config.get("backissues_url") or path.stem
        source_url = config.get("backissues_url") or config.get("source_url") or ""
        if not kind or not source_url:
            raise ValueError(f"Missing required custom feed fields in {path}")

        feeds.append(
            CustomFeed(
                kind=kind,
                title=title,
                slug=slugify(config.get("slug") or title),
                description=config.get("description", ""),
                source_url=source_url,
                max_issues=int(config.get("max_issues", MAX_ITEMS)),
            )
        )
    return feeds


def extract_with_css_selector(content: bytes, url: str, selector: str, remove_selector: str) -> str:
    document = html.fromstring(content, base_url=url)
    document.make_links_absolute(url)
    for node in document.cssselect(remove_selector) if remove_selector else []:
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)

    parts = []
    for node in document.cssselect(selector):
        parts.append(html.tostring(node, encoding="unicode", method="html"))
    return "".join(parts).strip()


def extract_with_readability(content: bytes, url: str) -> str:
    article = Document(content.decode("utf-8", errors="replace")).summary(html_partial=True)
    fragment = html.fragment_fromstring(article, create_parent="div", base_url=url)
    fragment.make_links_absolute(url)
    return "".join(
        html.tostring(child, encoding="unicode", method="html")
        for child in fragment
    ).strip()


def full_article_content(template: FeedTemplate, link: str) -> str:
    if not EXTRACT_FULL_CONTENT or not link:
        return ""

    try:
        content = fetch_html(link, USER_AGENT)
        if template.css_full_content:
            extracted = extract_with_css_selector(
                content,
                link,
                template.css_full_content,
                template.css_content_filter,
            )
            if extracted:
                return extracted
        return extract_with_readability(content, link)
    except Exception as error:
        print(f"Full-content extraction failed for {link}: {error}")
        return ""


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
    article_content = full_article_content(template, link)
    if article_content:
        description_parts.append(article_content)
    elif content:
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
    content = fetch_html(template.source_url, USER_AGENT)
    document = html.fromstring(content, base_url=template.source_url)
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


def text_content(node) -> str:
    return normalize_space(node.text_content()) if node is not None else ""


def article_link_near_heading(document, heading_text: str) -> html.HtmlElement | None:
    headings = document.xpath(
        f"//*[self::h1 or self::h2 or self::h3 or self::h4][normalize-space()='{heading_text}']"
    )
    if not headings:
        headings = document.xpath(
            f"//*[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{heading_text.lower()}')]"
        )

    for heading in headings:
        for sibling in heading.itersiblings():
            links = sibling.xpath(".//a[.//text()[normalize-space()] or normalize-space()]")
            for link in links:
                href = link.get("href", "")
                if href and "/magazine/" in href and "/toc/" not in href:
                    return link
            if getattr(sibling, "tag", "") in {"h2", "h3"}:
                break
    return None


def nearby_text_after_link(link: html.HtmlElement) -> str:
    parent = link.getparent()
    for _ in range(4):
        if parent is None:
            return ""
        candidates = [
            node
            for node in parent.xpath(".//*[self::p or self::div]")
            if link not in node.iterdescendants()
        ]
        for candidate in candidates:
            value = text_content(candidate)
            if value and value != text_content(link):
                return value
        parent = parent.getparent()
    return ""


def author_after_link(link: html.HtmlElement) -> str:
    parent = link.getparent()
    for _ in range(5):
        if parent is None:
            return ""
        links = parent.xpath(".//a[contains(@href, '/author/') or contains(@href, '/authors/')]")
        for author_link in links:
            value = text_content(author_link)
            if value and value != text_content(link):
                return value
        parent = parent.getparent()
    return ""


def issue_date_from_url(url: str) -> str:
    match = re.search(r"/toc/(\d{4})/(\d{2})/", url)
    if not match:
        return ""
    year, month = match.groups()
    return format_datetime(datetime(int(year), int(month), 1, tzinfo=DEFAULT_TIMEZONE))


def build_atlantic_cover_feed(config: CustomFeed) -> str:
    content = fetch_html(config.source_url, USER_AGENT)
    document = html.fromstring(content, base_url=config.source_url)
    document.make_links_absolute(config.source_url)

    issue_links = []
    seen = set()
    for link in document.xpath("//a[contains(@href, '/magazine/toc/')]"):
        href = link.get("href", "")
        if href and href not in seen:
            seen.add(href)
            issue_links.append(href)

    items = []
    for issue_url in issue_links[: config.max_issues]:
        issue_content = fetch_html(issue_url, USER_AGENT)
        issue_doc = html.fromstring(issue_content, base_url=issue_url)
        issue_doc.make_links_absolute(issue_url)

        cover_link = article_link_near_heading(issue_doc, "Cover Story")
        if cover_link is None:
            print(f"No cover story found for {issue_url}")
            continue

        title = text_content(cover_link)
        link = cover_link.get("href", "")
        description = nearby_text_after_link(cover_link)
        author = author_after_link(cover_link)
        pub_date = issue_date_from_url(issue_url)
        guid = f"{issue_url}#cover-story"

        optional_xml = []
        if pub_date:
            optional_xml.append(f"      <pubDate>{escape(pub_date)}</pubDate>")
        if author:
            optional_xml.append(f"      <author>{escape(author)}</author>")
        optional_block = "\n" + "\n".join(optional_xml) if optional_xml else ""

        items.append(
            f"""    <item>
      <title>{escape(title)}</title>
      <link>{escape(link)}</link>
      <guid isPermaLink="false">{escape(guid)}</guid>
      <description><![CDATA[{cdata(escape(description))}]]></description>{optional_block}
    </item>"""
        )

    now = datetime.now().astimezone()
    feed_path = f"feeds/{config.slug}.xml"
    feed_url = f"{FEED_BASE_URL}/{feed_path}" if FEED_BASE_URL else ""
    atom_link = ""
    if feed_url:
        atom_link = (
            f'\n    <atom:link href="{escape(feed_url)}" rel="self" '
            'type="application/rss+xml" />'
        )

    description = config.description or f"Generated RSS feed for {config.source_url}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(config.title)}</title>
    <link>{escape(config.source_url)}</link>
    <description>{escape(description)}</description>
    <language>en-us</language>
    <lastBuildDate>{escape(format_datetime(now))}</lastBuildDate>{atom_link}
{chr(10).join(items)}
  </channel>
</rss>
"""


def write_index(feed_links: list[tuple[str, str]]) -> None:
    rows = []
    for title, slug in feed_links:
        path = f"feeds/{slug}.xml"
        url = f"{FEED_BASE_URL}/{path}" if FEED_BASE_URL else path
        rows.append(
            "      <li>"
            f'<a href="{escape(path)}">{escape(title)}</a>'
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
    custom_feeds = load_custom_feeds()
    print(f"Loaded {len(templates)} feed template(s) from {TEMPLATE_DIR}")
    print(f"Loaded {len(custom_feeds)} custom feed(s) from {CUSTOM_FEED_DIR}")

    first_feed = None
    feed_links = []
    for template in templates:
        feed = build_feed(template)
        feed_path = FEEDS_DIR / f"{template.slug}.xml"
        feed_path.write_text(feed, encoding="utf-8")
        print(f"Wrote {feed_path}")
        feed_links.append((template.title, template.slug))
        if first_feed is None:
            first_feed = feed

    for custom_feed in custom_feeds:
        if custom_feed.kind != "atlantic_magazine_cover_stories":
            raise ValueError(f"Unsupported custom feed kind: {custom_feed.kind}")
        feed = build_atlantic_cover_feed(custom_feed)
        feed_path = FEEDS_DIR / f"{custom_feed.slug}.xml"
        feed_path.write_text(feed, encoding="utf-8")
        print(f"Wrote {feed_path}")
        feed_links.append((custom_feed.title, custom_feed.slug))
        if first_feed is None:
            first_feed = feed

    if first_feed is not None:
        (OUTPUT_DIR / "feed.xml").write_text(first_feed, encoding="utf-8")

    write_index(feed_links)
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    main()
