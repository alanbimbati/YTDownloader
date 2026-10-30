"""Article links turned into Telegraph pages: the readable text only, without ads or trackers."""
import json
import re
from typing import NamedTuple, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import lxml.html
import requests
from readability import Document

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


def expand(url: str) -> str:
    """Where a shortened link really leads; any other link, or one that cannot be followed, is returned as is."""
    if site_of(url) not in _SHORTENERS:
        return url
    try:
        resp = requests.head(url, headers={"User-Agent": USER_AGENT}, allow_redirects=True, timeout=10)
        return resp.url or url
    except requests.RequestException:
        return url


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
    resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "it,en;q=0.8"}, timeout=20)
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
