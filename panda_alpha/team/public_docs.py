"""Capture one explicitly selected public PandaAI guide. No auth, APIs or crawling."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import canonical, now
from .sources import MAX_TEXT_BYTES


class Article(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.depth += 1
        if self.depth and tag in {"script", "style", "noscript", "button"}:
            self.hidden += 1
        if self.depth and tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "pre", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag in {"script", "style", "noscript", "button"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "article":
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.depth and not self.hidden:
            self.parts.append(data)

    def text(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Public document redirect refused; verify the intended document URL")


def validate_url(url):
    parsed = urlsplit(url)
    # Only static guide pages, never /api endpoints, authenticated docs/API
    # parameters, query strings or a link supplied inside page content.
    import re
    if (parsed.scheme != "https" or parsed.netloc != "www.pandaaiquant.com"
            or not re.fullmatch(r"/docs/[a-z][a-z0-9-]*", parsed.path)
            or parsed.path == "/docs/api" or parsed.query or parsed.fragment):
        raise ValueError("Select one https://www.pandaaiquant.com/docs/<guide> URL without parameters")


def capture(url, output, *, timeout=20):
    validate_url(url)
    root = Path(output).resolve()
    if root.exists():
        raise ValueError("Use a new capture directory; historical evidence is immutable")
    request = Request(url, headers={"User-Agent": "PandaAlpha-PublicDocumentReview", "Accept": "text/html"}, method="GET")
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if response.status != 200 or response.headers.get_content_type() != "text/html":
            raise ValueError("Expected a public HTML guide")
        data = response.read(2_000_001)
        encoding = response.headers.get_content_charset() or "utf-8"
    if len(data) > 2_000_000:
        raise ValueError("Public document exceeds capture size limit")
    parser = Article()
    parser.feed(data.decode(encoding))
    text = parser.text()
    if not 100 <= len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
        raise ValueError("No bounded public article body; authenticated/dynamic documentation needs explicit text intake")
    root.mkdir(parents=True)
    raw, body = root / "response.html", root / "article.txt"
    raw.write_bytes(data)
    body.write_text(text, encoding="utf-8")
    artifact = {"path": str(body), "sha256": hashlib.sha256(body.read_bytes()).hexdigest()}
    receipt = {"origin": url, "observed_at": now(), "kind": "official_document", "artifact": artifact,
               "raw_response": {"path": str(raw), "sha256": hashlib.sha256(data).hexdigest()},
               "method": "unauthenticated_GET", "followed_links": 0,
               "limitations": ["Public guide text is not evidence of a particular Run's input, PIT or worker.",
                               "No cookies, account API, data subscription or website backtest was used."]}
    (root / "capture.json").write_text(canonical(receipt), encoding="utf-8")
    return receipt
