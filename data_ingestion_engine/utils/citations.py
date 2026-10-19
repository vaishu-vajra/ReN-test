"""
Inline citations in generated analysis.

The model does not build links. It emits a compact token carrying the page and
the verbatim sentence it read:

    [[cite:p12::Sam's Club U.S. increased its annual membership fees]]

and, in the sections that read several documents in one call, the document that
sentence came from:

    [[cite:D3 p12::Sam's Club U.S. increased its annual membership fees]]

This module turns those tokens into Markdown links into the web app's document
reader, with the sentence passed as the `highlight` argument so the reader can
scroll to it and mark it.

Building the link here rather than in the prompt is the whole point. Percent-
encoding a sentence carrying apostrophes, commas, em dashes or non-Latin script
is something the model gets wrong often enough to matter, a wrong encoding fails
silently (the reader simply highlights nothing), and a full URL costs ~80 tokens
every time it is repeated. A token costs a handful.

Nothing here validates that the quote really appears in the document — that is a
separate step, and until it exists a citation points at text the model believes
it read.
"""

import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote as urlquote, urlencode

# The web app the links point into. Overridable so dev and staging builds cite
# their own front end rather than sending a reviewer to production.
APP_BASE_URL = os.getenv("REN_APP_BASE_URL", "https://app.myrenx.ai").rstrip("/")

# Longest highlight we will put in a link.
#
# Generous on purpose. The reader finds the passage with PDF.js's find, which
# normalizes BOTH the page text and the query before matching: an end-of-line
# "\n" becomes a space, a word hyphenated across a line break is rejoined, and
# every run of whitespace in the query is matched as "[ ]+". A quote spanning
# several lines, or a whole paragraph, matches as readily as one sentence — so
# there is no matching reason to cut evidence short, and a claim resting on a
# paragraph should cite the paragraph.
#
# What this bounds is URL length, not match quality. The route and the other
# parameters run to ~250 characters, and ~2000 is the practical ceiling across
# browsers and proxies, so 1200 leaves comfortable headroom for a long paragraph
# while keeping the link inside it.
MAX_QUOTE_CHARS = int(os.getenv("CITATION_MAX_QUOTE_CHARS", "1200"))

# Shortest span worth linking. Below this a "quote" is a fragment like "revenue"
# that would highlight the first of fifty occurrences.
MIN_QUOTE_CHARS = 12

TOKEN_START = "[[cite:"

# [[cite:<locator>::<quote>]] — the quote runs to the first "]]". DOTALL because
# the model wraps long quotes over lines.
#
# The separator is "::" rather than "|" because three of these analyses put their
# citations inside Markdown table cells, and a "|" there ends the column.
#
# Backticks wrapping the token are consumed with it. The prompts show the token
# format in a code span, and the model sometimes copies that: it emits
# `[[cite:...]]`, and rewriting only the inside leaves the link in a code span,
# rendered as raw Markdown and unclickable. Only a matched pair is taken — a
# lone backtick touching a token belongs to someone else's code span
# (`FFO`[[cite:...]]) and stays where it is.
CITE_RE = re.compile(
    r"(?P<ticks>`*)\[\[cite:(?P<locator>[^\]]{0,48}?)::(?P<quote>(?:(?!\]\]).)*)\]\](?P=ticks)",
    re.DOTALL,
)

_DOC_REF_RE = re.compile(r"\bD(?P<index>\d{1,2})\b", re.IGNORECASE)
_PAGE_RE = re.compile(r"\bp(?:age|p|g)?\.?\s*(?P<page>\d{1,5}(?:\s*[-–,]\s*\d{1,5})*)",
                      re.IGNORECASE)

# Markdown the model wraps quotes in, stripped before the quote becomes a
# highlight: the document itself contains none of it.
_STRIP_EDGE = " \t\r\n\"'“”‘’*_`"


def normalize_quote(text: str) -> str:
    """
    A model-emitted quote reduced to something the reader can match.

    Collapses the line breaks the model introduces when it wraps a long span,
    drops the quotation marks and Markdown emphasis it likes to add, and caps
    the length at a word boundary.
    """
    if not text:
        return ""
    # Words hyphenated across a line break in the PDF come back as "member-\nship"
    collapsed = re.sub(r"(\w)[-­]\s*\n\s*(\w)", r"\1\2", text)
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    collapsed = collapsed.strip(_STRIP_EDGE).strip()

    return _clip(collapsed, MAX_QUOTE_CHARS)


# Where a too-long quote can be cut without reading as though it broke off.
# Sentence ends first, then clause punctuation; the cut lands BEFORE the mark so
# the highlight ends on a word, which is also what the PDF text layer holds.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_CLAUSE_END = re.compile(r"[,;:](?=\s|$)")


def _clip(text: str, limit: int) -> str:
    """
    `text` shortened to `limit`, ending somewhere a reader would expect.

    This is a backstop, not a routine trim — at the current limit a quote has to
    run well past a paragraph to reach it. When it does fire, a plain character
    cut would leave the highlight hanging mid-clause ("...as supplemented by
    an"), which reads as a broken citation rather than a deliberate excerpt.
    Preferring a sentence end, then a clause, then a word keeps it readable.
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    for pattern in (_SENTENCE_END, _CLAUSE_END):
        marks = list(pattern.finditer(window))
        # Only worth it if the cut still leaves a usefully long span
        if marks and marks[-1].start() > limit // 2:
            return window[:marks[-1].start()].strip()
    space = window.rfind(" ")
    return (window[:space] if space > limit // 2 else window).strip()


def _safe_year(document: Dict[str, Any]) -> Optional[int]:
    """The reporting year, tolerating 2026, '2026' and '2026.0'."""
    try:
        return int(float(document["year"]))
    except (KeyError, TypeError, ValueError):
        return None


def build_document_link(company: Dict[str, Any], document: Dict[str, Any],
                        quote: str, sidebar: str = "read") -> Optional[str]:
    """
    A reader link for `document`, highlighting `quote`.

    Returns None when the record is missing something the route needs, so the
    caller can fall back to a plain page reference rather than emit a link that
    lands on a 404.

    The path segments and the query string are encoded differently on purpose —
    the route carries spaces as %20, the query as "+" — which is what the app's
    own links look like.
    """
    company_id = (company.get("company_id") or company.get("id")
                  or document.get("company_id"))
    report_id = document.get("report_id") or document.get("id")
    name = str(company.get("name") or company.get("company_name") or "").strip()
    exchange = str(company.get("exchange") or "").strip()

    if not (company_id and report_id and name and exchange and quote):
        return None

    path = "/".join([
        APP_BASE_URL, "admin", "company",
        urlquote(name, safe=""), urlquote(exchange, safe=""),
        str(company_id), "document-analysis",
    ])

    params: List[Tuple[str, str]] = [("selectedDoc", str(report_id))]
    title = document.get("document_title")
    if title:
        params.append(("docTitle", str(title)))
    doc_type = document.get("document_type") or document.get("report_type")
    if doc_type:
        params.append(("docType", str(doc_type)))
    year = _safe_year(document)
    if year:
        params.append(("docYear", str(year)))
    params.append(("highlight", quote))
    params.append(("sidebar", sidebar))

    return f"{path}?{urlencode(params)}"


def _parse_locator(locator: str) -> Tuple[Optional[int], Optional[str]]:
    """('D3 p12') -> (3, '12'). Either half may be missing."""
    doc_match = _DOC_REF_RE.search(locator or "")
    page_match = _PAGE_RE.search(locator or "")
    doc_ref = int(doc_match.group("index")) if doc_match else None
    page = page_match.group("page").strip() if page_match else None
    return doc_ref, page


class _Numberer:
    """
    Numbers the citations in one response, in the order they appear.

    Citations are shown as [1], [2], [3] rather than as page numbers. The page a
    passage sits on is the one part of a citation the model gets wrong — it reads
    the printed folio, which rarely matches the physical page, or simply
    misremembers across a long document — and it does no navigational work: the
    reader locates the passage by searching for the quoted text, not by page. A
    sequential marker carries no claim that can be false.

    One of these per response, so numbering restarts with each analysis.
    """

    def __init__(self):
        self.count = 0

    def next(self) -> int:
        self.count += 1
        return self.count


def _linked(number: int, url: str) -> str:
    """
    A citation link, bracketed.

    The brackets separate one citation from the next. A claim resting on two
    passages emits two tokens, and rendered bare they ran together — "[1] [2]"
    stays legible without the rewriter needing to know whether another citation
    follows, which it cannot, since it emits each token as that token arrives.

    The brackets are escaped: unescaped they are legal in Markdown link text
    only when balanced, and an escape is what every renderer agrees on.
    """
    return f"[\\[{number}\\]]({url})"


def _pick_document(documents: List[Dict[str, Any]], doc_ref: Optional[int]):
    if not documents:
        return None
    if doc_ref is None:
        return documents[0]
    index = doc_ref - 1
    if 0 <= index < len(documents):
        return documents[index]
    # The model referenced a document that was not in the call. Better to
    # degrade to a plain page reference than to cite the wrong filing.
    return None


def make_resolver(company: Dict[str, Any],
                  documents: List[Dict[str, Any]]) -> Callable:
    """
    Maps a token's document reference onto one of the documents in the call.

    `documents` is in the same order the model was shown them, so D1 is the
    first. A token with no document reference means the only document there is,
    which is what every single-document section emits.
    """
    def resolve(doc_ref: Optional[int]):
        document = _pick_document(documents, doc_ref)
        return (company, document) if document is not None else None
    return resolve


def make_lazy_resolver(load_company: Callable[[], Optional[Dict[str, Any]]],
                       documents: List[Dict[str, Any]]) -> Callable:
    """
    A resolver that looks the company up the first time a citation needs it.

    The company record is only needed to build a link, so fetching it before the
    response starts puts a network call between the caller and the first byte of
    a stream — and an analysis with no citations pays for it having got nothing.
    Deferring it means a slow lookup shows up as a pause inside the stream, and
    a failed one costs the citations rather than the analysis.

    Looked up at most once per request, whatever the outcome: a lookup that
    failed is remembered as a failure rather than retried on every token.
    """
    cache: Dict[str, Any] = {}

    def resolve(doc_ref: Optional[int]):
        document = _pick_document(documents, doc_ref)
        if document is None:
            return None
        if "company" not in cache:
            try:
                cache["company"] = load_company() or {}
            except Exception:
                cache["company"] = {}
        return cache["company"], document
    return resolve


def _render(match: "re.Match", resolve: Callable, numberer: "_Numberer") -> str:
    """
    One citation token, as a numbered link — or as nothing at all.

    A token that cannot become a link is dropped rather than degraded. The old
    fallback printed the model's page number as plain text, which is exactly the
    value we stopped trusting; and a bare "[4]" with nothing behind it tells the
    reader less than the sentence already did. The number is only spent on a
    citation that actually resolves, so the sequence has no gaps.
    """
    doc_ref, _page = _parse_locator(match.group("locator"))
    quote = normalize_quote(match.group("quote"))
    if len(quote) < MIN_QUOTE_CHARS:
        return ""

    resolved = resolve(doc_ref)
    if not resolved:
        return ""

    company, document = resolved
    url = build_document_link(company, document, quote)
    if not url:
        return ""
    return _linked(numberer.next(), url)


def rewrite_text(text: str, resolve: Callable,
                 numberer: Optional["_Numberer"] = None) -> str:
    """
    Replace every complete citation token in `text` with a numbered link.

    `numberer` carries the running count. A streamed response passes one in so
    numbering continues across chunks; a one-off call gets a fresh sequence.
    """
    if not text or TOKEN_START not in text:
        return text
    counter = numberer if numberer is not None else _Numberer()
    return CITE_RE.sub(lambda m: _render(m, resolve, counter), text)


def _hold_from(buffer: str) -> int:
    """
    The index from which `buffer` might still be part of an unfinished token.

    Everything before it is safe to rewrite and send to the client; everything
    from it has to wait for more chunks. Without this the client would see the
    raw "[[cite:" of a token whose closing "]]" has not arrived yet.

    The backticks CITE_RE strips around a token count as part of it, so they
    are held too: a leading "`" sent ahead of its token, or a token released
    before its closing "`" arrives, would leave a stray backtick that opens a
    code span over the rest of the line.
    """
    start = buffer.rfind(TOKEN_START)
    if start != -1:
        close = buffer.find("]]", start)
        # Unclosed, or closed with nothing after it yet but backticks
        if close == -1 or not buffer[close + 2:].strip("`"):
            return _before_backticks(buffer, start)
    # A token start split across the chunk boundary: "...[[ci" + "te:p12|..."
    for length in range(min(len(TOKEN_START) - 1, len(buffer)), 0, -1):
        if TOKEN_START.startswith(buffer[-length:]):
            return _before_backticks(buffer, len(buffer) - length)
    # Trailing backticks may be the opening of a token in the next chunk
    return _before_backticks(buffer, len(buffer))


def _before_backticks(buffer: str, index: int) -> int:
    """
    `index` moved back over any backticks immediately before it.

    When those backticks follow a finished token, some of them may be its
    closing pair (`[[cite:A]]``[[cite:B]]`), so that token is held as well —
    split from its closing backtick it would no longer match as wrapped.
    """
    while True:
        start = index
        while start > 0 and buffer[start - 1] == "`":
            start -= 1
        if start == index or not buffer.endswith("]]", 0, start):
            return start
        token = buffer.rfind(TOKEN_START, 0, start)
        if token == -1 or buffer.find("]]", token) != start - 2:
            return start
        index = token


# A token this long is not a token — the model opened one and never closed it.
# Release it as plain text rather than holding the stream open forever.
MAX_HELD_CHARS = int(os.getenv("CITATION_MAX_HELD_CHARS", "4000"))


def stream_rewrite(gen, resolve: Callable):
    """
    Wrap a text-chunk generator, turning citation tokens into links as they go.

    Tokens routinely straddle chunk boundaries, so this holds back the tail of
    the buffer whenever it could still be the beginning of one. Non-string
    chunks — the truncation sentinel, an error payload — pass straight through
    untouched.

    Sits between the model stream and collect_and_patch, so what the client sees
    and what is persisted are the same text.
    """
    buffer = ""
    # One sequence for the whole response: tokens are rendered as they arrive,
    # so the count has to survive across chunks.
    numberer = _Numberer()
    try:
        for chunk in gen:
            if not isinstance(chunk, str):
                yield chunk
                continue

            # Split before rewriting: whether a finished token is still waiting
            # on a closing backtick can only be seen in the raw text.
            buffer += chunk
            hold = _hold_from(buffer)

            if len(buffer) - hold > MAX_HELD_CHARS:
                # Unclosed token; stop waiting for a "]]" that is not coming.
                hold = len(buffer)

            if hold:
                yield rewrite_text(buffer[:hold], resolve, numberer)
            buffer = buffer[hold:]
    finally:
        if buffer:
            yield rewrite_text(buffer, resolve, numberer)
