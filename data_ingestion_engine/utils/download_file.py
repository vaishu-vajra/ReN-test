from data_ingestion_engine.utils.sanitizeFilename import sanitize_filename
import uuid
import requests
import os
import re
import tempfile
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import subprocess
import shutil
import zipfile

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('file_download')

class DownloadError(Exception):
    """Base class for all download failures."""
    category = "Failed URLs"

class HTMLPageError(DownloadError):
    """Server returned HTML — URL points to a web page, not a direct PDF."""
    category = "HTML Page URL"

class ProtectedContentError(DownloadError):
    """Server returned non-PDF binary — likely CDN/DRM that requires auth."""
    category = "Server Protected"


class NetworkError(DownloadError):
    """DNS resolution or TCP connection failed."""
    category = "Network Error"


class HTTPError(DownloadError):
    """Server returned a transient HTTP error status (5xx / unspecified 4xx)."""
    category = "HTTP Error"


class HTTPForbiddenError(HTTPError):
    """HTTP 403 — server explicitly denies access; won't succeed on retry."""
    category = "HTTP 403"


class HTTPNotFoundError(HTTPError):
    """HTTP 404 — resource does not exist; won't succeed on retry."""
    category = "HTTP 404"


class OfficeConversionError(DownloadError):
    """An office document (.doc/.docx/.xls…) could not be converted to PDF."""
    category = "Office Conversion Failed"


class ArchiveError(DownloadError):
    """An ESEF .zip could not be opened or held no renderable report."""
    category = "ESEF Archive Failed"



def _create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=None,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


SESSION = _create_session()




def _is_html_content_type(content_type: str) -> bool:
    return "text/html" in content_type.lower()


# URL path extensions that are web pages rather than direct file downloads.
# These are routed straight to Playwright (see `download_file`) instead of the
# streaming PDF downloader, because the server returns rendered HTML.
_HTML_EXTS = {".html", ".htm", ".xhtml", ".aspx", ".asp", ".php", ".jsp"}

# Office documents — downloaded raw then converted to PDF via LibreOffice.
_OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                ".rtf", ".odt", ".ods", ".odp"}

# ESEF report archives — a zip whose `reports/*.xhtml` inline-XBRL document is
# the human-readable annual report; extracted then rendered to PDF.
_ARCHIVE_EXTS = {".zip"}

# Inline-XBRL / ESEF single-file reports (xhtml under a non-standard extension).
# Rendered to PDF via Chromium, same as an HTML page.
_XBRL_EXTS = {".xbrl", ".xbri", ".ixbrl"}


def _url_ext(url: str) -> str:
    """Return the lowercased file extension of a URL's path (e.g. '.html'), or ''."""
    try:
        path = urlparse(url).path
        return os.path.splitext(path)[1].lower()
    except Exception:
        return ""


def _has_pdf_header(data: bytes) -> bool:
    """
    A valid PDF starts with %PDF within the first 8 bytes (allows for a 3-byte BOM).
    Checking the full 1024-byte sample is too loose — CDN-protected streams and
    error pages sometimes contain '%PDF' as a substring without being valid PDFs.
    """
    return b'%PDF' in data[:8]


def _build_headers(url: str) -> dict:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/pdf,application/octet-stream,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Referer': origin + '/',
        'Origin': origin,
        'DNT': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Upgrade-Insecure-Requests': '1',
    }




def _curl_download(url: str, local_file: str, timeout: int = 120) -> bool:
    """Download via system curl. Returns True on success, False otherwise."""
    logger.info("Attempting curl fallback for %s -> %s", url, local_file)
    dir_part = os.path.dirname(local_file)
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    cmd = [
        "curl", "-L", "--fail", "--max-time", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "-H", f"Referer: {origin}/",
        "-H", "Accept: application/pdf,application/octet-stream,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "DNT: 1",
        "-o", local_file, url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=False)
        logger.info("curl fallback succeeded for %s", url)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, Exception) as e:
        logger.error("curl fallback failed for %s: %s", url, e)
        return False


def _curl_fallback(url: str, local_file: str, strict_pdf: bool) -> bool:
    """Run curl and verify the result is a valid PDF. Returns True on success."""
    if not _curl_download(url, local_file):
        return False
    try:
        with open(local_file, "rb") as f:
            sample = f.read(1024)
        if strict_pdf and not _has_pdf_header(sample):
            logger.warning("curl fallback downloaded non-PDF content. Deleting.")
            try:
                os.remove(local_file)
            except OSError:
                pass
            return False
        return True
    except Exception as e:
        logger.error("Error verifying curl file: %s", e)
        return False


def _cffi_fallback(url: str, local_file: str, strict_pdf: bool) -> bool:
    """
    Final fallback using curl_cffi, which mimics a real Chrome TLS fingerprint.
    Bypasses bot detection that fingerprints Python's / curl's TLS handshake
    (Cloudflare, Akamai, and corporate CDNs like Umbraco often do this).
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.debug("curl_cffi not installed — skipping TLS-impersonation fallback")
        return False

    logger.info("Attempting curl_cffi fallback (Chrome TLS impersonation) for %s", url)
    try:
        r = cffi_requests.get(
            url,
            impersonate="chrome120",
            timeout=60,
            allow_redirects=True,
        )
        if r.status_code >= 400:
            logger.warning("curl_cffi got HTTP %s for %s", r.status_code, url)
            return False

        body = r.content
        if not body:
            logger.warning("curl_cffi got empty body for %s", url)
            return False

        if strict_pdf and not _has_pdf_header(body):
            logger.warning(
                "curl_cffi got non-PDF for %s. First bytes: %r", url, body[:16])
            return False

        dir_part = os.path.dirname(local_file)
        if dir_part:
            os.makedirs(dir_part, exist_ok=True)
        with open(local_file, "wb") as f:
            f.write(body)
        logger.info(
            "curl_cffi fallback succeeded for %s (%d bytes)", url, len(body))
        return True
    except Exception as e:
        logger.error("curl_cffi fallback error for %s: %s", url, e)
        return False


def _launch_browser():
    """
    Launch a FRESH Playwright + Chromium in the CURRENT thread.

    Playwright's sync API is thread-bound: a browser created in one thread cannot
    be driven from another (raises "greenlet.error: Cannot switch to a different
    thread"). Downloads run in a ThreadPoolExecutor, so we never cache/share a
    browser across threads — each render launches and tears down its own, in the
    thread that uses it. Returns (playwright, browser), or (None, None) on failure.
    The caller MUST pass both to _close_browser() when done.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.error("[playwright] import failed: %s", e)
        return None, None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-infobars',
                '--window-size=1280,800',
            ]
        )
        return pw, browser
    except Exception as e:
        logger.error("[playwright] Failed to launch browser: %s", e)
        return None, None


def _close_browser(pw, browser) -> None:
    """Tear down a browser + Playwright instance created by _launch_browser."""
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass


def _playwright_fallback(url: str, local_file: str, strict_pdf: bool, mode: str) -> bool:
    """
    Playwright-based fallback for 403 and HTML page failures.
    mode='direct'      — load URL directly in headless browser (for 403s)
    mode='scrape_html' — find PDF link in page DOM (for HTML page URLs)
    Returns True if a valid PDF was written to local_file, else False.
    """
    logger.info("[playwright_fallback] Invoked for url=%s mode=%s", url, mode)
    try:
        from playwright_stealth import Stealth
    except ImportError:
        logger.warning("[playwright_fallback] playwright or playwright-stealth not installed.")
        return False

    dir_part = os.path.dirname(local_file)
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)

    pw = browser = None
    try:
        pw, browser = _launch_browser()
        if browser is None:
            return False

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = context.new_page()
            Stealth().apply_stealth_sync(page)

            if mode == "direct":
                # Try expect_download first (handles Content-Disposition: attachment)
                try:
                    with page.expect_download(timeout=15000) as download_info:
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        except Exception as e:
                            logger.warning("[playwright_fallback] goto error: %s", e)
                    download = download_info.value
                    download.save_as(local_file)
                    if os.path.exists(local_file):
                        with open(local_file, "rb") as f:
                            sample = f.read(8)
                        if _has_pdf_header(sample):
                            logger.info("[playwright_fallback] PDF saved via download dialog.")
                            return True
                except Exception:
                    pass

                
                pdf_bytes = []

                def on_response(resp):
                    try:
                        ct = resp.headers.get("content-type", "")
                        if "application/pdf" in ct:
                            pdf_bytes.append(resp.body())
                    except Exception:
                        pass

                page.on("response", on_response)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                except Exception as e:
                    logger.warning("[playwright_fallback] goto error: %s", e)

                if pdf_bytes:
                    data = pdf_bytes[0]
                    if _has_pdf_header(data):
                        with open(local_file, "wb") as f:
                            f.write(data)
                        logger.info("[playwright_fallback] PDF saved via response listener.")
                        return True

                logger.warning("[playwright_fallback] direct mode: no PDF captured.")
                return False

            elif mode == "scrape_html":
                try:
                    with page.expect_download(timeout=15000) as download_info:
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        except Exception as e:
                            logger.warning("[playwright_fallback] goto error: %s", e)
                    download = download_info.value
                    download.save_as(local_file)
                    if os.path.exists(local_file):
                        with open(local_file, "rb") as f:
                            sample = f.read(8)
                        if _has_pdf_header(sample):
                            logger.info("[playwright_fallback] PDF saved via direct download on HTML page.")
                            return True
                except Exception:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2000)
                    except Exception as e:
                        logger.warning("[playwright_fallback] goto error: %s", e)
                candidate_links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.href).filter(h => h.toLowerCase().endsWith('.pdf'))"
                )

                if not candidate_links:
                    logger.warning("[playwright_fallback] scrape_html: no PDF links found.")
                    return False

                keywords = ["annual", "report", "esg", "sustainability", "10-k", "ar", "proxy"]
                ranked = [l for l in candidate_links if any(k in l.lower() for k in keywords)]
                pdf_url = ranked[0] if ranked else candidate_links[0]

                logger.info("[playwright_fallback] scrape_html: trying PDF link %s", pdf_url)
                return _playwright_fallback(pdf_url, local_file, strict_pdf, mode="direct")

            return False

        finally:
            context.close()

    except Exception as e:
        logger.error("[playwright_fallback] unexpected error: %s", e)
        return False
    finally:
        _close_browser(pw, browser)


def _save_captured_download(dl, captured: dict) -> None:
    """Download-event handler: save the file to a temp path and record it."""
    try:
        suffix = os.path.splitext(dl.suggested_filename or "")[1] or ".bin"
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        dl.save_as(tmp)
        captured["path"] = tmp
        captured["name"] = dl.suggested_filename
    except Exception as e:
        logger.warning("[playwright_render] failed to save captured download: %s", e)


def _goto_tolerant(page, url: str, captured: dict) -> None:
    """
    Navigate to `url`, tolerating the case where the navigation turns into a file
    download (Content-Disposition: attachment) instead of loading a page. A
    registered download handler populates `captured`; here we just avoid crashing
    on the "Download is starting" navigation error and give the download a moment
    to finish saving.
    """
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        if "Download is starting" in str(e) or captured.get("path"):
            page.wait_for_timeout(2000)
        else:
            logger.warning("[playwright] networkidle goto failed (%s), retrying domcontentloaded", e)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e2:
                if "Download is starting" in str(e2) or captured.get("path"):
                    page.wait_for_timeout(2000)
                else:
                    raise


def _render_page_to_pdf(page, local_file: str) -> bool:
    """
    Given a loaded page, print it to PDF at local_file (screen media, lazy images
    loaded). Returns True if a valid PDF was written.
    """
    _scroll_and_wait_images(page)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    # Render with SCREEN media so images/styling match what a user sees. page.pdf()
    # defaults to PRINT media, under which many sites hide images/backgrounds.
    page.emulate_media(media="screen")
    page.pdf(
        path=local_file,
        format="A4",
        print_background=True,
        margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
    )
    if os.path.exists(local_file) and os.path.getsize(local_file) > 0:
        with open(local_file, "rb") as f:
            head = f.read(8)
        if _has_pdf_header(head):
            return True
        logger.warning("[playwright_render] Output was not a valid PDF.")
    return False


def _scroll_and_wait_images(page) -> None:
    """Scroll the full page to trigger lazy images, then wait for them to decode."""
    try:
        page.evaluate(
            """async () => {
                await new Promise((resolve) => {
                    let y = 0;
                    const step = 600;
                    const startedAt = Date.now();
                    const timer = setInterval(() => {
                        window.scrollBy(0, step);
                        y += step;
                        // Stop at the bottom OR after 8s. Huge docs (e.g. a
                        // 300-page ESEF report) don't need a full scroll — this
                        // only exists to trigger lazy images near the top; page.pdf
                        // lays out the whole document regardless.
                        if (y >= document.body.scrollHeight || Date.now() - startedAt > 8000) {
                            clearInterval(timer);
                            resolve();
                        }
                    }, 80);
                });
                window.scrollTo(0, 0);
            }"""
        )
    except Exception as e:
        logger.debug("[playwright_render] scroll pass skipped: %s", e)
    try:
        page.evaluate(
            """async () => {
                const imgs = Array.from(document.images || []);
                await Promise.all(imgs.map((img) => {
                    if (img.complete && img.naturalWidth > 0) return Promise.resolve();
                    return img.decode().catch(() => {});
                }));
            }"""
        )
    except Exception as e:
        logger.debug("[playwright_render] image-wait skipped: %s", e)


def _playwright_render_pdf(url: str, local_file: str) -> bool:
    """
    Render an HTML page in headless Chromium and print it to PDF.

    This is how we ingest pages that ARE the content (e.g. FCA/RNS announcement
    pages) rather than pages that merely link to a PDF. Some ".html" URLs, though,
    actually serve a FILE download (Content-Disposition: attachment) — navigating
    them raises "Download is starting" instead of loading a page. We capture that
    download and route the real file through _process_local_file_to_pdf.
    Returns True if a valid PDF was written to local_file, else False.
    """
    logger.info("[playwright_render] Rendering page to PDF: %s", url)
    try:
        from playwright_stealth import Stealth
    except ImportError:
        logger.warning("[playwright_render] playwright-stealth not installed.")
        return False

    dir_part = os.path.dirname(local_file)
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)

    pw = browser = None
    captured = {"path": None, "name": None}
    rendered = False
    try:
        pw, browser = _launch_browser()
        if browser is None:
            return False

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )
        try:
            page = context.new_page()
            page.on("download", lambda dl: _save_captured_download(dl, captured))
            Stealth().apply_stealth_sync(page)
            _goto_tolerant(page, url, captured)
            if not captured["path"]:
                rendered = _render_page_to_pdf(page, local_file)
        finally:
            context.close()
    except Exception as e:
        logger.error("[playwright_render] unexpected error for %s: %s", url, e)
    finally:
        _close_browser(pw, browser)

    # The URL served a file download instead of a page → process it by real type.
    if captured["path"]:
        try:
            logger.info(
                "[playwright_render] URL served a download (%s); processing by type: %s",
                captured["name"], url)
            return _process_local_file_to_pdf(
                captured["path"], captured["name"], local_file, url)
        finally:
            _safe_unlink(captured["path"])

    if rendered:
        logger.info("[playwright_render] Rendered page to PDF: %s", local_file)
        return True
    return False


def _download_html_as_pdf(url: str, local_file: str) -> str:
    """
    Obtain a PDF from an HTML page URL in a SINGLE Playwright session:
      1. If navigating the URL triggers a file download (attachment), capture and
         convert it by type.
      2. Else if the page links to a real PDF (annual-report landing pages),
         follow that link and capture the PDF.
      3. Else render the page itself to PDF (the page IS the content).

    One browser launch + one navigation (vs. the old two-session scrape+render),
    which roughly halves the CPU/RAM cost per HTML doc. The old 15s download-wait
    is gone entirely — downloads are detected event-driven via `page.on(download)`.
    Raises HTMLPageError if nothing yields a valid PDF.
    """
    logger.info("Routing HTML page URL to Playwright (single session): %s", url)
    try:
        from playwright_stealth import Stealth
    except ImportError:
        raise HTMLPageError(f"playwright-stealth not installed: {url}")

    dir_part = os.path.dirname(local_file)
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)

    def _finish_capture(cap: dict) -> str:
        if _process_local_file_to_pdf(cap["path"], cap["name"], local_file, url):
            return local_file
        raise HTMLPageError(
            f"HTML page served a download but it could not be converted: {url}")

    pw = browser = None
    captured = {"path": None, "name": None}
    try:
        pw, browser = _launch_browser()
        if browser is None:
            raise HTMLPageError(f"Could not launch a browser to render: {url}")

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )
        try:
            page = context.new_page()
            page.on("download", lambda dl: _save_captured_download(dl, captured))
            Stealth().apply_stealth_sync(page)

            # 1. Navigate — may load a page or trigger a download.
            _goto_tolerant(page, url, captured)
            if captured["path"]:
                return _finish_capture(captured)

            # 2. Prefer an embedded PDF link (landing pages that point at a report).
            try:
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.href).filter(h => h.toLowerCase().endsWith('.pdf'))",
                )
            except Exception:
                links = []
            if links:
                keywords = ["annual", "report", "esg", "sustainability", "10-k", "ar", "proxy"]
                ranked = [l for l in links if any(k in l.lower() for k in keywords)]
                pdf_url = ranked[0] if ranked else links[0]
                logger.info("[html] following embedded PDF link: %s", pdf_url)
                _goto_tolerant(page, pdf_url, captured)
                if captured["path"]:
                    return _finish_capture(captured)
                # The link didn't yield a download — return to the original page
                # so we can still render it below.
                _goto_tolerant(page, url, captured)
                if captured["path"]:
                    return _finish_capture(captured)

            # 3. Render the page itself.
            if _render_page_to_pdf(page, local_file):
                logger.info("[playwright_render] Rendered page to PDF: %s", local_file)
                return local_file
            raise HTMLPageError(
                f"Could not obtain a PDF from HTML page — no download, no PDF link, "
                f"and page-render failed: {url}")
        finally:
            context.close()
    finally:
        _close_browser(pw, browser)
        if captured["path"]:
            _safe_unlink(captured["path"])


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _file_url(path: str) -> str:
    return "file://" + os.path.abspath(path)


def _download_raw(url: str, suffix: str) -> str:
    """
    Download `url` verbatim (no PDF validation) to a temp file with `suffix`.
    Reuses the full download pipeline (requests + curl + curl_cffi fallbacks) by
    calling download_file with strict_pdf=False. Returns the temp path.
    """
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    download_file(url, tmp, strict_pdf=False)
    return tmp


def _find_soffice() -> str:
    """Locate the LibreOffice CLI, or return '' if not installed."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    mac_path = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if os.path.exists(mac_path):
        return mac_path
    return ""


def _convert_office_to_pdf(src: str, local_file: str) -> None:
    """Convert an office document to PDF via headless LibreOffice."""
    soffice = _find_soffice()
    if not soffice:
        raise OfficeConversionError(
            "LibreOffice (soffice) is not installed — cannot convert office docs"
        )

    outdir = tempfile.mkdtemp()
    try:
        proc = subprocess.run(
            [soffice, "--headless", "--nologo", "--nofirststartwizard",
             "--convert-to", "pdf", "--outdir", outdir, src],
            check=False, capture_output=True, timeout=180,
        )
        produced = os.path.join(
            outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
        if not os.path.exists(produced):
            # soffice occasionally names the output differently — take any PDF.
            pdfs = [f for f in os.listdir(outdir) if f.lower().endswith(".pdf")]
            if not pdfs:
                raise OfficeConversionError(
                    f"LibreOffice produced no PDF (rc={proc.returncode}): "
                    f"{proc.stderr.decode('utf-8', 'ignore')[:200]}"
                )
            produced = os.path.join(outdir, pdfs[0])

        dir_part = os.path.dirname(local_file)
        if dir_part:
            os.makedirs(dir_part, exist_ok=True)
        shutil.move(produced, local_file)
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _download_and_convert_office(url: str, local_file: str, ext: str) -> str:
    """Download an office document and convert it to PDF."""
    logger.info("Routing office document to LibreOffice: %s", url)
    tmp = _download_raw(url, ext)
    try:
        _convert_office_to_pdf(tmp, local_file)
        return local_file
    finally:
        _safe_unlink(tmp)


def _find_esef_report(root: str) -> str:
    """
    Find the primary inline-XBRL report inside an extracted ESEF package.
    Prefers .xhtml/.html files under a 'reports' directory, then the largest one.
    Returns the path, or '' if none found.
    """
    candidates = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith((".xhtml", ".html", ".htm")):
                full = os.path.join(dirpath, f)
                in_reports = 1 if "reports" in dirpath.lower() else 0
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                candidates.append((in_reports, size, full))
    if not candidates:
        return ""
    candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return candidates[0][2]


def _render_esef_zip_file(zip_path: str, local_file: str, url: str) -> str:
    """Extract an ESEF .zip (already on disk) and render its report to PDF."""
    extract_dir = tempfile.mkdtemp()
    try:
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(extract_dir)
        except zipfile.BadZipFile as e:
            raise ArchiveError(f"Not a valid zip archive ({e}): {url}")

        report = _find_esef_report(extract_dir)
        if not report:
            raise ArchiveError(
                f"No inline-XBRL/HTML report found inside ESEF zip: {url}")

        # Render via a `.html` sibling (same directory, so the report's relative
        # image/CSS resources still resolve) to force Chromium's lenient HTML
        # parser instead of the strict XHTML parser.
        if not report.lower().endswith((".html", ".htm")):
            html_sibling = report + ".html"
            try:
                shutil.copyfile(report, html_sibling)
                report = html_sibling
            except OSError:
                pass

        if _playwright_render_pdf(_file_url(report), local_file):
            return local_file
        raise ArchiveError(f"Failed to render ESEF report to PDF: {url}")
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _download_esef_zip_as_pdf(url: str, local_file: str) -> str:
    """Download an ESEF .zip, then render its inline-XBRL report to PDF."""
    logger.info("Routing ESEF archive to extractor: %s", url)
    tmp = _download_raw(url, ".zip")
    try:
        return _render_esef_zip_file(tmp, local_file, url)
    finally:
        _safe_unlink(tmp)


def _process_local_file_to_pdf(src: str, hint_name: str, local_file: str, url: str) -> bool:
    """
    Convert an already-downloaded file to a PDF at local_file, dispatching by the
    file's extension / magic bytes. Used when a page navigation actually triggered
    a file download (a ".html" URL that serves an attachment). Returns True/False.
    """
    ext = os.path.splitext(hint_name or src or "")[1].lower()
    try:
        with open(src, "rb") as f:
            head = f.read(8)
    except OSError:
        return False

    is_zip = head[:2] == b"PK"          # OOXML office docs AND ESEF/plain zips
    is_ole = head[:4] == b"\xd0\xcf\x11\xe0"  # legacy .doc/.xls

    # Already a PDF.
    if _has_pdf_header(head) or ext == ".pdf":
        dir_part = os.path.dirname(local_file)
        if dir_part:
            os.makedirs(dir_part, exist_ok=True)
        shutil.move(src, local_file)
        return True

    # Office document — OOXML (.docx/.xlsx, which are PK zips) or legacy OLE.
    # Terminal: don't fall through to other handlers on failure.
    if ext in _OFFICE_EXTS or is_ole:
        try:
            _convert_office_to_pdf(src, local_file)
            return True
        except Exception as e:
            logger.warning("[capture] office conversion failed: %s", e)
            return False

    # Any other ZIP -> ESEF package. Terminal: never fall through to rendering
    # raw ZIP bytes as HTML (that hangs Chromium — the bug this replaces).
    if is_zip or ext == ".zip":
        try:
            _render_esef_zip_file(src, local_file, url)
            return True
        except Exception as e:
            logger.warning("[capture] ESEF zip handling failed: %s", e)
            return False

    # Everything else: HTML / XML / inline-XBRL text -> render via lenient parser.
    render_src = src
    if not src.lower().endswith((".html", ".htm")):
        render_src = src + ".html"
        try:
            os.rename(src, render_src)
        except OSError:
            render_src = src
    return _playwright_render_pdf(_file_url(render_src), local_file)


def _download_xbrl_as_pdf(url: str, local_file: str) -> str:
    """
    Download a `.xbri`/`.xbrl`/`.ixbrl` report and convert it to PDF.

    The extension is NOT reliable: FCA serves ESEF packages (multi-MB ZIP archives
    containing reports/*.html) under a `.xbri` extension, as well as plain
    single-file inline-XBRL (XHTML). So we download the bytes and dispatch by
    actual content (magic bytes) via _process_local_file_to_pdf: PK -> ESEF zip
    extract+render, XHTML -> lenient-parser render. Rendering the raw ZIP bytes as
    HTML is what previously hung Chromium and timed out.
    """
    logger.info("Routing inline-XBRL/ESEF document by content: %s", url)
    ext = _url_ext(url) or ".xbri"
    tmp = _download_raw(url, ext)
    try:
        if _process_local_file_to_pdf(tmp, os.path.basename(urlparse(url).path), local_file, url):
            return local_file
        raise ProtectedContentError(
            f"Failed to convert inline-XBRL/ESEF document to PDF: {url}")
    finally:
        _safe_unlink(tmp)


def _try_fallbacks(url: str, local_file: str, strict_pdf: bool) -> bool:
    """Run curl, then curl_cffi. Returns True if any fallback succeeded."""
    if _curl_fallback(url, local_file, strict_pdf):
        return True
    if _cffi_fallback(url, local_file, strict_pdf):
        return True
    return False


# ---------------------------------------------------------------------------
# Google Drive ID extraction
# ---------------------------------------------------------------------------

def extract_drive_file_id(url: str) -> str:
    match_d = re.search(r'/d/([-\w]+)', url)
    if match_d:
        return match_d.group(1)
    match_id = re.search(r'[?&]id=([-\w]+)', url)
    if match_id:
        return match_id.group(1)
    return None


def download_file(url, local_file=None, *, strict_pdf=True):
    """
    Download a file from `url` to `local_file`.

    Returns the local file path on success.
    Raises a DownloadError subclass on failure — callers should catch these
    to get the specific failure category (e.g. HTMLPageError, ProtectedContentError).
    """
    if not url:
        raise DownloadError("No URL provided")

    if not local_file:
        unique_filename = f"{sanitize_filename(url)}_{uuid.uuid4().hex}.pdf"
        local_file = os.path.join('pdfs', unique_filename)

    if 'drive.google.com' in url:
        return download_file_from_google_drive(url, local_file, strict_pdf=strict_pdf)

    # Route non-PDF link types to type-specific handlers that still yield a PDF.
    # This only applies in strict_pdf mode (the ingestion path); raw downloads
    # (strict_pdf=False, used internally by these handlers) fall through to the
    # streaming pipeline so we don't recurse.
    if strict_pdf:
        ext = _url_ext(url)
        if ext in _HTML_EXTS:
            # Web page — grab a linked PDF or render the page itself.
            return _download_html_as_pdf(url, local_file)
        if ext in _OFFICE_EXTS:
            # .doc/.docx/.xls/… — convert via LibreOffice.
            return _download_and_convert_office(url, local_file, ext)
        if ext in _ARCHIVE_EXTS:
            # ESEF .zip — extract and render the inline-XBRL report.
            return _download_esef_zip_as_pdf(url, local_file)
        if ext in _XBRL_EXTS:
            # .xbri/.xbrl inline-XBRL single file — render to PDF.
            return _download_xbrl_as_pdf(url, local_file)

    logger.info("Starting standard download: %s", url)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
            with SESSION.get(url, headers=_build_headers(url), stream=True, timeout=(10, 60)) as response:
                # Map HTTP errors to typed exceptions before reading the body.
                if response.status_code == 403:
                    if _playwright_fallback(url, local_file, strict_pdf, mode="direct"):
                        return local_file
                    raise HTTPForbiddenError(f"HTTP 403 Forbidden (playwright also failed): {url}")
                if response.status_code == 404:
                    raise HTTPNotFoundError(f"HTTP 404 Not Found: {url}")
                if response.status_code >= 400:
                    raise HTTPError(f"HTTP {response.status_code}: {url}")

                # Reject HTML pages before streaming any bytes.
                if strict_pdf:
                    ct = response.headers.get("Content-Type", "")
                    if _is_html_content_type(ct):
                        if _playwright_fallback(url, local_file, strict_pdf, mode="scrape_html"):
                            return local_file
                        raise HTMLPageError(
                            f"Server returned HTML (Content-Type: {ct}) — "
                            f"URL is a web page, not a direct PDF link (playwright also failed): {url}"
                        )

                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        temp_file.write(chunk)

        # Empty file
        if os.path.getsize(temp_path) == 0:
            os.unlink(temp_path)
            temp_path = None
            logger.warning("Empty response body, trying curl fallback.")
            if _try_fallbacks(url, local_file, strict_pdf):
                return local_file
            raise DownloadError(f"Server returned empty body: {url}")

        # PDF header check
        with open(temp_path, 'rb') as f:
            sample = f.read(1024)

        if strict_pdf and not _has_pdf_header(sample):
            logger.warning("Non-PDF content. First bytes: %r — trying curl fallback.", sample[:16])
            os.unlink(temp_path)
            temp_path = None
            if _try_fallbacks(url, local_file, strict_pdf):
                return local_file
            raise ProtectedContentError(
                f"Server returned non-PDF binary (CDN/DRM protection), "
                f"curl also failed. First bytes: {sample[:16]!r} — {url}"
            )

        dir_part = os.path.dirname(local_file)
        if dir_part:
            os.makedirs(dir_part, exist_ok=True)
        os.replace(temp_path, local_file)
        logger.info("Successfully downloaded: %s", local_file)
        return local_file

    except (HTMLPageError, ProtectedContentError, HTTPError, NetworkError, DownloadError):
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise

    except requests.exceptions.ConnectionError as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if _try_fallbacks(url, local_file, strict_pdf):
            return local_file
        raise NetworkError(f"Connection failed ({e}): {url}") from e

    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if _try_fallbacks(url, local_file, strict_pdf):
            return local_file
        raise DownloadError(f"Download failed ({type(e).__name__}: {e}): {url}") from e


def download_file_from_google_drive(url, local_file, strict_pdf=True):
    """Download from Google Drive. Handles /file/d/ID, ?id=ID, and /uc?id=ID URLs."""
    logger.info("Starting Google Drive download: %s", url)

    file_id = extract_drive_file_id(url)
    if not file_id:
        raise DownloadError(f"Could not extract file ID from Google Drive URL: {url}")

    direct_download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://drive.google.com/',
    }

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name

            response = SESSION.get(direct_download_url, headers=headers, stream=True, timeout=(15, 180))
            response.raise_for_status()

            confirmation_token = None
            for k, v in response.cookies.items():
                if k.startswith('download_warning'):
                    confirmation_token = v
                    break

            if confirmation_token:
                logger.info("Large Drive file — confirming download.")
                response = SESSION.get(
                    "https://drive.google.com/uc",
                    params={'export': 'download', 'id': file_id, 'confirm': confirmation_token},
                    headers=headers, stream=True, timeout=(15, 180),
                )
                response.raise_for_status()

            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    temp_file.write(chunk)

        with open(temp_path, 'rb') as f:
            magic = f.read(1024)

        if _has_pdf_header(magic):
            dir_part = os.path.dirname(local_file)
            if dir_part:
                os.makedirs(dir_part, exist_ok=True)
            os.replace(temp_path, local_file)
            logger.info("Successfully downloaded Drive file: %s", local_file)
            return local_file

        if not strict_pdf:
            dir_part = os.path.dirname(local_file)
            if dir_part:
                os.makedirs(dir_part, exist_ok=True)
            os.replace(temp_path, local_file)
            return local_file

        os.unlink(temp_path)
        temp_path = None
        logger.warning("Drive file is not a valid PDF. First bytes: %r — trying gdown.", magic[:20])

        try:
            import gdown
            output = gdown.download(url, local_file, quiet=False, fuzzy=True)
            if output and os.path.exists(local_file):
                return local_file
        except ImportError:
            logger.debug("gdown not installed, skipping.")
        except Exception as e:
            logger.error("gdown fallback failed: %s", e)

        raise ProtectedContentError(
            f"Google Drive file is not a valid PDF and all fallbacks failed: {url}"
        )

    except (HTMLPageError, ProtectedContentError, HTTPError, NetworkError, DownloadError):
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise

    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise DownloadError(f"Google Drive download failed ({type(e).__name__}: {e}): {url}") from e
