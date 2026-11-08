"""Article links turned into Telegraph pages: the readable text only, without ads or trackers."""
import json
import re
from typing import NamedTuple, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import lxml.html
import requests
from readability import Document

try:
    # A browser's TLS fingerprint: sites behind Cloudflare and the like refuse plain requests with a 403.
    from curl_cffi import requests as browser
except ImportError:
    browser = None

TELEGRAPH_API = "https://api.telegra.ph"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# Below this a page is a home, a gallery or a paywall teaser, not an article worth reading elsewhere.
MIN_TEXT_CHARS = 400
# Telegraph refuses a page whose content is over 64 KB.
CONTENT_LIMIT = 60_000

_TRACKING_PARAM = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|gbraid|wbraid|msclkid|mc_cid|mc_eid|igshid|igsh|yclid|_hsenc|_hsmi"
    r"|ref_src|ref_url|si|feature|pp|_ga|_gl|spm|scm|share_id|mibextid|rdt|rcm|trk|trackingId)$",
    re.IGNORECASE,
)
# Parameters that only track on these hosts: elsewhere `t` is a timestamp, `s` a search.
_HOST_TRACKING = {"x.com": {"s", "t"}, "twitter.com": {"s", "t"}, "tiktok.com": {"is_from_webapp", "sender_device", "_r", "_t"}}
_SHORTENERS = {
    "bit.ly", "t.co", "amzn.to", "amzn.eu", "a.co", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly",
    "is.gd", "rebrand.ly", "cutt.ly", "shorturl.at", "s.id", "tiny.cc", "t.ly", "vm.tiktok.com",
}
# Pages that only bounce to another site, carrying it in a parameter, with a tracking token next to it.
_REDIRECTORS = {
    "linkedin.com": ("/safety/go", "url"), "l.facebook.com": ("/l.php", "u"), "lm.facebook.com": ("/l.php", "u"),
    "l.instagram.com": ("/", "u"), "google.com": ("/url", "q"), "youtube.com": ("/redirect", "q"),
    "l.threads.net": ("/", "u"), "out.reddit.com": ("/", "url"),
}
_AMAZON_ITEM = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.IGNORECASE)
_TELEGRAPH_TAGS = {
    "a", "aside", "b", "blockquote", "br", "code", "em", "figcaption", "figure", "h3", "h4",
    "hr", "i", "img", "li", "ol", "p", "pre", "s", "strong", "u", "ul",
}
# Telegraph has only two heading levels; the rest maps onto the closest tag it knows.
_RENAMED = {"h1": "h3", "h2": "h3", "h5": "h4", "h6": "h4", "del": "s", "strike": "s", "ins": "u", "cite": "i", "q": "i"}
_DROPPED = {"script", "style", "noscript", "iframe", "form", "button", "svg", "nav", "input", "select", "textarea"}


class Article(NamedTuple):
    title: str
    site: str
    text: str
    nodes: list


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    host = site_of(url)
    # An Amazon item is fully named by its ASIN: the rest of the path and query is referral and affiliate tagging.
    item = _AMAZON_ITEM.search(parts.path) if re.match(r"amazon\.[a-z.]+$", host) else None
    if item:
        return f"https://{parts.hostname}/dp/{item.group(1).upper()}"
    extra = next((v for h, v in _HOST_TRACKING.items() if host == h or host.endswith("." + h)), set())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING_PARAM.match(k) and k not in extra]
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def has_trackers(url: str) -> bool:
    parts = urlsplit(url)
    same = urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query, keep_blank_values=True)), fragment=""))
    return clean_url(url) != same


def _unwrap(url: str) -> str:
    parts = urlsplit(url)
    host = site_of(url)
    for redirector, (path, param) in _REDIRECTORS.items():
        if host == redirector and parts.path.rstrip("/").startswith(path.rstrip("/")):
            target = dict(parse_qsl(parts.query)).get(param, "")
            if target.startswith("http"):
                return target
    return url


_REFRESH = re.compile(r'http-equiv="refresh"\s+content="\d+;\s*URL=([^"]+)"', re.IGNORECASE)


def _follow_refresh(url: str) -> str:
    # Substack share links: a stub page redirecting in JavaScript; "r" is the sharer's referral id.
    try:
        found = _REFRESH.search(_fetch(url).text)
    except Exception:
        return url
    if not found:
        return url
    target = urlsplit(lxml.html.fromstring(f"<p>{found.group(1)}</p>").text_content())
    query = urlencode([(k, v) for k, v in parse_qsl(target.query) if k not in ("triedRedirect", "r")])
    return urlunsplit(target._replace(query=query))


def expand(url: str) -> str:
    """Where a shortened or bouncing link really leads; any other link, or one that cannot be followed, as is."""
    url = _unwrap(url)
    if site_of(url) == "open.substack.com":
        return _follow_refresh(url)
    if site_of(url) not in _SHORTENERS:
        return url
    try:
        resp = requests.head(url, headers={"User-Agent": USER_AGENT}, allow_redirects=True, timeout=10)
        return _unwrap(resp.url or url)
    except requests.RequestException:
        return url


def _fetch(url: str):
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "it,en;q=0.8"}
    if browser is not None:
        return browser.get(url, headers={"Accept-Language": headers["Accept-Language"]}, impersonate="chrome", timeout=20)
    return requests.get(url, headers=headers, timeout=20)


_NAVIGATION_HOSTS = ("maps.google.", "maps.app.goo.gl", "maps.apple.com", "waze.com", "openstreetmap.org",
                     "osm.org", "wego.here.com", "share.here.com", "citymapper.com", "maps.me", "moovitapp.com")


def is_navigation(url: str) -> bool:
    """Map and directions links: sent to share a place, not to be read."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    if any(host == h or host.startswith(h) or host.endswith("." + h) for h in _NAVIGATION_HOSTS):
        return True
    return bool(re.match(r"(google\.[a-z.]+|goo\.gl)$", host)) and parts.path.startswith("/maps")


class FeedEntry(NamedTuple):
    id: str
    link: str
    title: str


_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/rdf+xml")
_FEED_GUESSES = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feeds/posts/default")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_feed(content: bytes) -> Optional[tuple[str, list[FeedEntry]]]:
    """(title, entries newest first) of an RSS 1/2 or Atom document; None if it is not one."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    if _local(root.tag) not in ("rss", "feed", "RDF"):
        return None
    child = lambda el, name: next((c for c in el if _local(c.tag) == name), None)
    text = lambda el, name: ((child(el, name).text if child(el, name) is not None else "") or "").strip()
    channel = child(root, "channel")
    title = text(channel if channel is not None else root, "title")
    entries = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = text(el, "link")
        if not link:
            # Atom keeps the link in an attribute; rel="alternate" (or none) is the post itself.
            links = [c for c in el if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate"]
            link = links[0].get("href", "") if links else ""
        if link:
            entries.append(FeedEntry(text(el, "guid") or text(el, "id") or link, link, text(el, "title")))
    return title, entries


def discover_feed(url: str) -> Optional[tuple[str, str]]:
    """(feed url, site title) for a site, any of its pages or a feed itself."""
    resp = _fetch(url)
    parsed = _parse_feed(resp.content) if resp.ok else None
    if parsed:
        return url, parsed[0] or site_of(url)
    candidates = []
    if resp.ok and "html" in resp.headers.get("content-type", ""):
        page = lxml.html.fromstring(resp.content)
        for link in page.xpath('//link[@rel="alternate"][@href]'):
            href = urljoin(url, link.get("href"))
            # Comment feeds are listed next to the posts' one, often before it.
            if (link.get("type") or "").lower() in _FEED_TYPES and "comment" not in href.lower():
                candidates.append(href)
    root = urlunsplit(urlsplit(url)._replace(path="", query="", fragment=""))
    candidates += [root + guess for guess in _FEED_GUESSES]
    for candidate in dict.fromkeys(candidates):
        try:
            r = _fetch(candidate)
        except Exception:
            continue
        parsed = _parse_feed(r.content) if r.ok else None
        if parsed and parsed[1]:
            return candidate, parsed[0] or site_of(url)
    return None


def feed_entries(feed_url: str) -> tuple[str, list[FeedEntry]]:
    resp = _fetch(feed_url)
    resp.raise_for_status()
    parsed = _parse_feed(resp.content)
    if parsed is None:
        raise ValueError(f"Not a feed anymore: {feed_url}")
    return parsed


def site_of(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def _nodes(el, base: str) -> list:
    out = [el.text] if el.text else []
    for child in el:
        out += _node(child, base)
        if child.tail:
            out.append(child.tail)
    return out


def _node(el, base: str) -> list:
    if not isinstance(el.tag, str):  # comments and processing instructions
        return []
    tag = el.tag.lower()
    if tag in _DROPPED:
        return []
    tag = _RENAMED.get(tag, tag)
    if tag == "img":
        src = el.get("src") or el.get("data-src") or ""
        if not src or src.startswith("data:"):
            return []
        return [{"tag": "img", "attrs": {"src": urljoin(base, src)}}]
    children = _nodes(el, base)
    if tag not in _TELEGRAPH_TAGS:
        return children  # div, span, section…: keep the content, drop the wrapper
    node: dict = {"tag": tag}
    if tag == "a":
        href = urljoin(base, el.get("href") or "")
        if href.startswith("http"):
            node["attrs"] = {"href": clean_url(href)}
    if children:
        node["children"] = children
    return [node]


def _fit(nodes: list, url: str) -> list:
    size = lambda n: len(json.dumps(n, ensure_ascii=False).encode())
    if size(nodes) <= CONTENT_LIMIT:
        return nodes
    while nodes and size(nodes) > CONTENT_LIMIT - 300:
        nodes.pop()
    nodes.append({"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": url}, "children": ["Continua a leggere sull'originale"]},
    ]})
    return nodes


def fetch_article(url: str) -> Optional[Article]:
    resp = _fetch(url)
    resp.raise_for_status()
    if "html" not in resp.headers.get("content-type", ""):
        return None
    doc = Document(resp.content, url=url)
    body = lxml.html.fromstring(doc.summary(html_partial=True))
    text = body.text_content().strip()
    if len(text) < MIN_TEXT_CHARS:
        return None

    page = lxml.html.fromstring(resp.content)
    meta = lambda name: page.xpath(f'string(//meta[@property="{name}" or @name="{name}"]/@content)').strip()
    declared = meta("og:site_name")
    # Some blogs declare a key (a Nostr npub) as their name: the domain reads better.
    site = declared if declared and len(declared) <= 60 and not declared.startswith("npub1") else site_of(url)
    title = meta("og:title") or doc.short_title() or doc.title() or site
    for sep in (" · ", " | ", " - ", " — "):
        if declared and title.endswith(sep + declared):
            title = title[: -len(sep + declared)]
    nodes = _nodes(body, url)
    # Readability often leaves out the lead image, which sits outside the article body.
    image = meta("og:image")
    if image and not body.xpath(".//img"):
        nodes.insert(0, {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": urljoin(url, image)}}]})
    return Article(title.strip(), site.strip(), text, _fit(nodes, clean_url(url)))


def create_account(short_name: str) -> str:
    resp = requests.post(f"{TELEGRAPH_API}/createAccount", data={"short_name": short_name}, timeout=20).json()
    if not resp.get("ok"):
        raise RuntimeError(f"Telegraph createAccount: {resp.get('error')}")
    return resp["result"]["access_token"]


def create_page(token: str, article: Article, source_url: str) -> str:
    resp = requests.post(
        f"{TELEGRAPH_API}/createPage",
        data={
            "access_token": token,
            "title": article.title[:256],
            "author_name": article.site[:128],
            "author_url": source_url,
            "content": json.dumps(article.nodes, ensure_ascii=False),
            "return_content": "false",
        },
        timeout=30,
    ).json()
    if not resp.get("ok"):
        raise RuntimeError(f"Telegraph createPage: {resp.get('error')}")
    return resp["result"]["url"]
