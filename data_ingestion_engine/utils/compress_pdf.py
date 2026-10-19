import logging
import os
import shutil
import subprocess

import pikepdf

logger = logging.getLogger(__name__)

# Ghostscript PDFSETTINGS presets ordered from least lossy to most lossy.
# We escalate through these only as needed to hit a size target — each step
# downsamples images more aggressively than the previous.
#   /printer — 300 DPI color/gray, near-print quality
#   /ebook   — 150 DPI color/gray, Adobe's recommended balance
#   /screen  —  72 DPI, web quality (last resort)
_GS_PRESETS = ("/printer", "/ebook", "/screen")


def compress_pdf(pdf_path: str, min_savings_bytes: int = 1024) -> dict:
    """
    Re-save a PDF in place with pikepdf's lossless compression.

    Uses object_stream_mode=generate and compress_streams=True to repack
    the file. Image data and content stream bytes are unchanged — savings
    come from object stream consolidation and stripping uncompressed
    metadata. Typical reduction: 5-30% on reports authored in older tools.

    The compressed file replaces the original only if it is at least
    `min_savings_bytes` smaller; otherwise the original is kept.

    Returns a dict with keys: original_size, compressed_size, saved_bytes,
    ratio, replaced (bool), error (str | None).
    """
    result = {
        "original_size": 0,
        "compressed_size": 0,
        "saved_bytes": 0,
        "ratio": 0.0,
        "replaced": False,
        "error": None,
    }

    if not os.path.exists(pdf_path):
        result["error"] = f"File not found: {pdf_path}"
        return result

    original_size = os.path.getsize(pdf_path)
    result["original_size"] = original_size

    tmp_path = f"{pdf_path}.compressed"
    try:
        with pikepdf.open(pdf_path) as pdf:
            pdf.save(
                tmp_path,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                compress_streams=True,
                linearize=False,
            )

        compressed_size = os.path.getsize(tmp_path)
        saved = original_size - compressed_size
        result["compressed_size"] = compressed_size
        result["saved_bytes"] = saved
        result["ratio"] = (saved / original_size) if original_size else 0.0

        if saved >= min_savings_bytes:
            os.replace(tmp_path, pdf_path)
            result["replaced"] = True
            logger.info(
                "Compressed %s: %d → %d bytes (%.1f%% saved)",
                pdf_path, original_size, compressed_size, result["ratio"] * 100,
            )
        else:
            os.remove(tmp_path)
            result["compressed_size"] = original_size
            result["saved_bytes"] = 0
            result["ratio"] = 0.0
            logger.info(
                "Skipped replacing %s — savings %d B below threshold %d B",
                pdf_path, saved, min_savings_bytes,
            )

    except Exception as e:
        result["error"] = str(e)
        logger.warning("Failed to compress %s: %s", pdf_path, e)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return result


def compress_pdf_aggressive(
    pdf_path: str,
    target_bytes: int,
    timeout_sec: int = 300,
) -> dict:
    """
    Lossy compress a PDF down to under `target_bytes` using Ghostscript.

    Escalates through PDFSETTINGS presets from least lossy to most lossy
    (/printer → /ebook → /screen), stopping at the first preset whose output
    is under target_bytes. The original file is replaced in place by the
    smallest passing output. If even /screen can't get under target, the
    original is left untouched and `under_target=False` is returned.

    Requires the `gs` (Ghostscript) binary on PATH.

    Returns dict with: original_size, compressed_size, preset_used,
    under_target (bool), replaced (bool), error (str | None).
    """
    result = {
        "original_size": 0,
        "compressed_size": 0,
        "preset_used": None,
        "under_target": False,
        "replaced": False,
        "error": None,
    }

    if not os.path.exists(pdf_path):
        result["error"] = f"File not found: {pdf_path}"
        return result

    if shutil.which("gs") is None:
        result["error"] = "Ghostscript (gs) binary not found on PATH"
        logger.warning("Aggressive compression skipped — gs not installed")
        return result

    original_size = os.path.getsize(pdf_path)
    result["original_size"] = original_size
    result["compressed_size"] = original_size

    if original_size <= target_bytes:
        result["under_target"] = True
        return result

    tmp_path = f"{pdf_path}.gs"
    for preset in _GS_PRESETS:
        try:
            subprocess.run(
                [
                    "gs",
                    "-sDEVICE=pdfwrite",
                    "-dCompatibilityLevel=1.4",
                    f"-dPDFSETTINGS={preset}",
                    "-dNOPAUSE",
                    "-dQUIET",
                    "-dBATCH",
                    f"-sOutputFile={tmp_path}",
                    pdf_path,
                ],
                check=True,
                timeout=timeout_sec,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            result["error"] = f"Ghostscript timed out after {timeout_sec}s on {preset}"
            logger.warning("gs %s timed out for %s", preset, pdf_path)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return result
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", errors="replace")[:500]
            result["error"] = f"gs {preset} failed: {stderr}"
            logger.warning("gs %s failed for %s: %s", preset, pdf_path, stderr)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            continue

        new_size = os.path.getsize(tmp_path)
        logger.info(
            "gs %s for %s: %d → %d bytes (%.1f%% of original)",
            preset, pdf_path, original_size, new_size,
            (new_size / original_size) * 100 if original_size else 0,
        )

        if new_size <= target_bytes:
            os.replace(tmp_path, pdf_path)
            result["preset_used"] = preset
            result["compressed_size"] = new_size
            result["under_target"] = True
            result["replaced"] = True
            result["error"] = None
            return result

        # Not under target yet — try the next, more aggressive preset.
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Exhausted all presets without hitting target.
    logger.warning(
        "Aggressive compression exhausted all presets for %s — still over target",
        pdf_path,
    )
    return result
