#!/usr/bin/env python
"""
Check the citation pipeline without spending a model call.

Everything here is offline: it exercises the link builder, the token grammar and
the streaming rewriter against fixtures, so it can run on every change to
utils/citations.py or to the citation block in a prompt.

What it does NOT check is whether the model's quote actually appears in the
document — that needs the PDF's text layer and is the verification step this
pipeline still lacks. A citation that renders correctly here can still highlight
nothing in the reader.

Usage:
    python -m data_ingestion_engine.scripts.verify_citations
"""

import random
import re
import sys
from urllib.parse import parse_qs, unquote, urlparse

from data_ingestion_engine.utils.citations import (
    CITE_RE,
    _clip,
    build_document_link,
    make_lazy_resolver,
    make_resolver,
    normalize_quote,
    rewrite_text,
    stream_rewrite,
)

FAILURES = []


def check(name, got, want=True):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got:  {got!r}")
        print(f"        want: {want!r}")
        FAILURES.append(name)


COMPANY = {"company_id": 2699, "name": "WALMART INC.", "exchange": "NASDAQ"}
DOC = {"report_id": 1313647, "document_title": "10-Q - Walmart Inc. - 30 Apr 2026",
       "document_type": "10-Q", "year": "2026.0"}
QUOTE = ("income items. Effective May 1, 2026, Sam's Club U.S. increased its annual "
         "membership fees for Club and Plus memberships from $50 to $60 and")

# The link the web app itself produces for this document and highlight. The path
# encodes spaces as %20 and the query as "+" — both halves matter.
EXPECTED = ("https://app.myrenx.ai/admin/company/WALMART%20INC./NASDAQ/2699/document-analysis"
            "?selectedDoc=1313647&docTitle=10-Q+-+Walmart+Inc.+-+30+Apr+2026&docType=10-Q"
            "&docYear=2026&highlight=income+items.+Effective+May+1%2C+2026%2C+Sam%27s+Club"
            "+U.S.+increased+its+annual+membership+fees+for+Club+and+Plus+memberships+from"
            "+%2450+to+%2460+and&sidebar=read")


def main():
    resolve = make_resolver(COMPANY, [DOC])

    print("— link construction —")
    check("link matches the app's own format", build_document_link(COMPANY, DOC, QUOTE), EXPECTED)

    text = f"Fees rose in the quarter [[cite:p12::{QUOTE}]] which supports margin."
    rendered = f"Fees rose in the quarter [\\[1\\]]({EXPECTED}) which supports margin."
    check("token becomes a Markdown link", rewrite_text(text, resolve), rendered)

    print("\n— streaming —")
    # Tokens straddle chunk boundaries constantly; the rewriter has to hold back
    # anything that could still turn into one.
    identical = True
    for seed in range(300):
        random.seed(seed)
        cuts = sorted(random.sample(range(1, len(text)), random.randint(1, 12)))
        chunks, prev = [], 0
        for cut in cuts:
            chunks.append(text[prev:cut])
            prev = cut
        chunks.append(text[prev:])
        if "".join(stream_rewrite(iter(chunks), resolve)) != rendered:
            identical = False
            print(f"        diverged on split {cuts}")
            break
    check("identical output across 300 random chunk splits", identical)
    check("identical at one character per chunk",
          "".join(stream_rewrite(iter(text), resolve)), rendered)
    check("text with no tokens passes through untouched",
          "".join(stream_rewrite(iter(["## Heading\n\n| table | row |\n"]), resolve)),
          "## Heading\n\n| table | row |\n")
    unclosed = "Claim [[cite:p12::never closed and the model moved on"
    check("an unclosed token is released rather than stalling the stream",
          "".join(stream_rewrite(iter([unclosed]), resolve)), unclosed)
    check("non-string chunks pass through",
          list(stream_rewrite(iter(["a", {"__truncated__": True}, "b"]), resolve)),
          ["a", {"__truncated__": True}, "b"])

    print("\n— backtick-wrapped tokens —")
    # The prompts show the token in a code span and the model copies it; a link
    # left inside backticks renders as raw Markdown and cannot be clicked.
    ticked = f"| Gap | `[[cite:p12::{QUOTE}]]` `[[cite:p13::{QUOTE}]]` |"
    ticked_rendered = (f"| Gap | [\\[1\\]]({EXPECTED}) "
                       f"[\\[2\\]]({EXPECTED}) |")
    check("backticks around a token are dropped with it",
          rewrite_text(ticked, resolve), ticked_rendered)
    survived = True
    for seed in range(300):
        random.seed(seed)
        cuts = sorted(random.sample(range(1, len(ticked)), random.randint(1, 12)))
        chunks, prev = [], 0
        for cut in cuts:
            chunks.append(ticked[prev:cut])
            prev = cut
        chunks.append(ticked[prev:])
        if "".join(stream_rewrite(iter(chunks), resolve)) != ticked_rendered:
            survived = False
            print(f"        diverged on split {cuts}")
            break
    check("backticks dropped across 300 random chunk splits", survived)
    check("backticks dropped at one character per chunk",
          "".join(stream_rewrite(iter(ticked), resolve)), ticked_rendered)
    check("inline code elsewhere is left alone",
          "".join(stream_rewrite(iter("use `get_instruction` here"), resolve)),
          "use `get_instruction` here")
    check("a quote never runs past its own ]] looking for a closing backtick",
          rewrite_text(f"`[[cite:p1::{QUOTE}]] [[cite:p2::{QUOTE}]]`", resolve),
          f"`[\\[1\\]]({EXPECTED}) [\\[2\\]]({EXPECTED})`")
    check("a code span that merely touches a token keeps its backtick",
          "".join(stream_rewrite(iter(f"core `FFO`[[cite:p12::{QUOTE}]] rose"), resolve)),
          f"core `FFO`[\\[1\\]]({EXPECTED}) rose")

    print("\n— multi-document windows —")
    docs = [dict(DOC, report_id=n) for n in (1, 2, 3)]
    r3 = make_resolver(COMPANY, docs)
    out = rewrite_text("Guidance slipped [[cite:D3 p4::management lowered the full year outlook]].", r3)
    check("D3 resolves to the third document in the window",
          "selectedDoc=3" in out and "\\[1\\]" in out)
    check("a document outside the window is dropped, never miscited",
          rewrite_text("[[cite:D9 p4::management lowered the full year outlook]]", r3), "")

    print("\n— the company lookup never blocks the analysis —")
    # A blocking lookup in front of a streamed response is what made these
    # endpoints appear to hang, so the resolver has to defer it, do it once, and
    # survive it failing.
    calls = []

    def loader():
        calls.append(1)
        return COMPANY

    lazy = make_lazy_resolver(loader, [DOC])
    check("no lookup happens when nothing cites", calls, [])
    rewrite_text(f"a [[cite:p1::{QUOTE}]] b [[cite:p2::{QUOTE}]]", lazy)
    check("looked up once, however many citations", calls, [1])

    def failing_loader():
        raise RuntimeError("DataX unreachable")

    check("a failed lookup costs the citation, not the analysis",
          rewrite_text(f"claim [[cite:p4::{QUOTE}]] stands",
                       make_lazy_resolver(failing_loader, [DOC])),
          "claim  stands")
    slow_calls = []

    def slow_then_fail():
        slow_calls.append(1)
        return None

    r = make_lazy_resolver(slow_then_fail, [DOC])
    rewrite_text(f"[[cite:p1::{QUOTE}]] [[cite:p2::{QUOTE}]] [[cite:p3::{QUOTE}]]", r)
    check("a failed lookup is not retried per citation", slow_calls, [1])

    print("\n— adjacent citations stay separate —")
    run = rewrite_text(f"performance [[cite:p4::{QUOTE}]] [[cite:p8::{QUOTE}]].", resolve)
    check("two citations in a row are individually bracketed",
          "\\[1\\]" in run and "\\[2\\]" in run)
    check("they no longer run together as one reference", "p. 4 p. 8" in run, False)
    check("no page number is shown at all", "p. " in run, False)

    print("\n— dropped, never a broken link —")
    check("missing exchange drops the citation",
          rewrite_text("[[cite:p7::a long enough quote to pass the minimum]]",
                       make_resolver({"company_id": 1, "name": "X"}, [DOC])), "")
    check("a quote too short to locate is dropped",
          rewrite_text("[[cite:p7::revenue]]", resolve), "")
    check("a token with no page still links (the page is unused now)",
          "\\[1\\]](http" in rewrite_text(f"[[cite:::{QUOTE}]]", resolve))

    print("\n— quote hygiene —")
    check("collapses the model's line wrapping",
          normalize_quote("the company\n  reported  a\nloss"), "the company reported a loss")
    check("rejoins a word hyphenated across a line break",
          normalize_quote("annual member-\nship fees rose"), "annual membership fees rose")
    check("strips quotation marks and emphasis",
          normalize_quote('  **"a quoted claim"**  '), "a quoted claim")
    check("caps an absurdly long quote", len(normalize_quote("word " * 500)) <= 1200)
    # PDF.js normalizes line breaks and hyphenation on both the page text and
    # the query, so a paragraph matches as readily as a sentence. Evidence that
    # spans a paragraph must survive intact.
    para = (" ".join([
        "The Notes were issued pursuant to an Indenture with Computershare Trust Company, N.A.,",
        "as successor to Wells Fargo Bank, National Association, as trustee, dated as of",
        "September 16, 2016, as supplemented by an indenture supplemental thereto.",
        "Interest on the Notes is payable semi-annually in arrears on June 15 and December 15",
        "of each year, beginning December 15, 2026, and the Notes will mature on June 15, 2036",
        "unless earlier redeemed or repurchased in accordance with their terms."]))
    check("a paragraph-length quote is not truncated", normalize_quote(para), para)
    check("a paragraph still fits a usable URL",
          len(build_document_link(COMPANY, DOC, normalize_quote(para))) < 2000)
# The clipper only fires past the cap, so exercise it directly.
    long_clause = ("The Notes were issued pursuant to an Indenture with Computershare "
                   "Trust Company, N.A., as successor to Wells Fargo Bank, National "
                   "Association, as trustee, dated as of September 16, 2016")
    # Cuts at the LAST clause mark inside the limit, keeping as much evidence as
    # the limit allows, and lands before the comma so it ends on a word.
    check("clips at a clause rather than mid-phrase",
          _clip(long_clause, 160),
          "The Notes were issued pursuant to an Indenture with Computershare Trust "
          "Company, N.A., as successor to Wells Fargo Bank, National Association, "
          "as trustee")
    # A sentence end is preferred only when it still leaves over half the limit;
    # cutting "Revenue grew 12% to $4.1 billion." out of a 90-char budget would
    # throw away most of the evidence, so a word boundary wins instead.
    check("an early sentence end does not gut the excerpt",
          _clip("Revenue grew 12% to $4.1 billion. Operating margin fell 200 basis points "
                "on higher freight costs and a one-off charge.", 90),
          "Revenue grew 12% to $4.1 billion. Operating margin fell 200 basis points on higher")
    check("a late sentence end is preferred",
          _clip("Operating margin fell 200 basis points on higher freight costs. "
                "Management expects the pressure to persist into the second half.", 90),
          "Operating margin fell 200 basis points on higher freight costs")
    check("falls back to a word boundary with no punctuation",
          _clip("word " * 60, 100).endswith("word"))

    print("\n— scripts and encodings —")
    # The corpus is multi-language, and some PDFs carry a broken text layer whose
    # mojibake the model quotes verbatim. All of it has to survive the URL.
    cases = {
        "Turkish": "Sürdürülebilirlik hedeflerimiz doğrultusunda sera gazı emisyonlarımızı %15 azalttık",
        "Greek": "Οι εκπομπές αερίων του θερμοκηπίου μειώθηκαν κατά 15% σε σύγκριση με πέρυσι",
        "Arabic": "انخفضت انبعاثات الغازات الدفيئة بنسبة خمسة عشر بالمائة مقارنة بالعام الماضي",
        "Japanese": "当社の温室効果ガス排出量は前年度比で十五パーセント減少しました",
        "mojibake text layer": "William â€œBillâ€ Roberts as an independent non-executive director",
        "symbols and maths": "Scope 1 & 2 emissions fell 5% to 150,000 MT CO₂e — a record low (see §4.2)",
    }
    tr_company = {"company_id": 42, "name": "ŞİRKET A.Ş.", "exchange": "BIST"}
    tr_resolve = make_resolver(tr_company, [DOC])
    for label, quote in cases.items():
        out = rewrite_text(f"Claim [[cite:p9::{quote}]].", tr_resolve)
        match = re.search(r"\((https://[^)]+)\)", out)
        if not match:
            check(f"{label} produces a link", False)
            continue
        highlight = parse_qs(urlparse(match.group(1)).query)["highlight"][0]
        check(f"{label} survives the round trip", highlight, quote)
    link = rewrite_text(f"[[cite:p1::{cases['Turkish']}]]", tr_resolve)
    segment = unquote(urlparse(re.search(r"\((https://[^)]+)\)", link).group(1)).path.split("/")[3])
    check("a non-Latin company name survives the path", segment, "ŞİRKET A.Ş.")

    print("\n— prompts still speak the grammar the parser expects —")
    from data_ingestion_engine.utils.getInstruction import (
        JSON_FENCE_INSTRUCTIONS, _local_instruction)
    for section in ("summary", "data_points", "risk_analysis", "gap_analysis",
                    "priorities_and_sentiment_analysis", "earnings_delta"):
        prompt = _local_instruction(section, JSON_FENCE_INSTRUCTIONS)
        check(f"{section}: teaches the token", "[[cite:" in prompt)
        # Anything the prompt shows as an example must parse, or the model is
        # being taught a grammar the rewriter will not recognise.
        examples = [m for m in CITE_RE.finditer(prompt)]
        check(f"{section}: every example token parses",
              len(examples), prompt.count("[[cite:"))
        # The rules block names every old format in order to forbid it, and each
        # of those sits in backticks — so strip backtick spans before looking for
        # formats that are still being asked for.
        prose = re.sub(r"`[^`]*`", "", prompt)
        stale = re.findall(r"\(p\. \d+\)|\[p\. |\[cite: |\[Citations\]|\[Page\(s\)\]|Page Citation",
                           prose)
        check(f"{section}: no stale citation format left", stale, [])
        # A token shown in a code span gets copied with its backticks, and the
        # link it becomes renders as raw text.
        check(f"{section}: no token shown in backticks",
              re.findall(r"`\[\[cite:", prompt), [])
        check(f"{section}: tells the model to write tokens as plain text",
              "Write tokens as plain text" in prompt)

    print(f"\n{len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
