"""
Extract individual embedded/linked images from an INDD document.

Two modes
─────────
Mode A – Custom Script (default, recommended)
  Uses the InDesign Custom Scripts API to run an ExtendScript that:
    1. Opens the document server-side.
    2. Iterates every document.links entry.
    3. Unembeds fully-embedded assets, then copies each source file out.
  Result: the actual source image files (TIFF, JPEG, PNG, EPS, PSD…)
  at their original resolution, one file per placed image.

Mode B – Page Renditions (fallback / explicit opt-in)
  Uses /create-rendition to render whole pages as JPEG or PNG.
  Result: one flat composite image per page.
  Use this when you want a "screenshot" view rather than source assets.

Usage
─────
    from src.extractor import extract_images_from_indd

    # Mode A (default) – actual embedded images
    images = extract_images_from_indd("https://s3.example.com/doc.indd?...")

    # Mode B – page renditions
    images = extract_images_from_indd(
        "https://s3.example.com/doc.indd?...",
        mode="rendition",
        image_format="png",
        resolution=300,
    )
"""

import json
import logging
import os
from pathlib import Path

import requests

from .adobe_client import AdobeAuthClient, InDesignAPIClient, INDESIGN_BASE_URL
from .converter import _resolve_to_url
from .script_manager import get_extract_images_script_url

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mode A: Custom Script – actual embedded images
# ─────────────────────────────────────────────────────────────────────────────

def _extract_via_custom_script(
    indd_url: str,
    indd_filename: str,
    out_dir: Path,
    client: InDesignAPIClient,
    auth: AdobeAuthClient,
    *,
    force_register: bool = False,
) -> list[str]:
    """
    Run the bundled extract-images ExtendScript via the Custom Scripts API
    and download all returned image files.
    """
    # 1. Ensure script is registered and get its execution URL
    script_url = get_extract_images_script_url(auth, force=force_register)
    logger.info("Executing extract-images script on: %s", indd_filename)

    # 2. Build job payload
    payload = {
        "assets": [
            {
                "source": {"url": indd_url},
                "destination": indd_filename,
            }
        ],
        "params": {
            "targetDocument": indd_filename,
        },
    }

    # 3. Submit job (POST to the script's execution URL directly)
    resp = requests.post(
        script_url,
        json=payload,
        headers=auth.auth_headers,
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    status_url = body.get("statusUrl") or body.get("status_url")
    if not status_url:
        raise RuntimeError(f"No statusUrl in script execution response: {body}")

    # 4. Poll until done
    job = client.poll_job(status_url)
    logger.debug("Script job result: %s", job)

    # 5. Download each extracted image from the output pre-signed URLs
    #
    #    The Custom Scripts API returns outputs in the same shape as other
    #    InDesign API jobs:
    #      job["outputs"] = [
    #        { "destination": {"url": "https://..."}, "source": "extracted_images/image_p1_1_photo.jpg" },
    #        ...
    #      ]
    #
    #    Additionally job["data"] may contain the extraction_summary.json.

    outputs = job.get("outputs") or []
    if not outputs:
        logger.warning("No output assets returned by extract-images script.")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    for entry in outputs:
        dl_url = (
            entry.get("destination", {}).get("url")
            or entry.get("url")
        )
        source_path = entry.get("source", "")

        # Skip the summary JSON – we only want image files
        if source_path.endswith("extraction_summary.json"):
            # Optionally download summary for logging
            try:
                if dl_url:
                    r = requests.get(dl_url, timeout=30)
                    summary = r.json()
                    logger.info(
                        "Extraction summary: %d extracted, %d skipped",
                        len(summary.get("extracted", [])),
                        len(summary.get("skipped", [])),
                    )
                    if summary.get("skipped"):
                        for s in summary["skipped"]:
                            logger.warning("  Skipped: %s (%s)", s.get("name"), s.get("reason"))
            except Exception:
                pass
            continue

        if not dl_url:
            logger.warning("No download URL for output: %s", entry)
            continue

        filename = Path(source_path).name
        dest = out_dir / filename
        client.download_url(dl_url, str(dest))
        saved.append(str(dest.resolve()))
        logger.info("  Saved: %s", dest)

    return sorted(saved)


# ─────────────────────────────────────────────────────────────────────────────
# Mode B: Page Renditions – flat per-page images
# ─────────────────────────────────────────────────────────────────────────────

def _extract_via_rendition(
    indd_url: str,
    indd_filename: str,
    out_dir: Path,
    client: InDesignAPIClient,
    *,
    image_format: str = "jpeg",
    resolution: int = 150,
    quality: str = "high",
    page_range: str = "All",
) -> list[str]:
    """Render each page as a flat image (JPEG or PNG)."""

    media_type = "image/jpeg" if image_format.lower() == "jpeg" else "image/png"
    ext = "jpg" if image_format.lower() == "jpeg" else "png"

    logger.info("Submitting page-rendition job (%s, %s DPI)…", media_type, resolution)
    payload = {
        "assets": [
            {"source": {"url": indd_url}, "destination": indd_filename}
        ],
        "params": {
            "outputMediaType":  media_type,
            "targetDocuments":  [indd_filename],
            "pageRange":        page_range,
            "resolution":       resolution,
            "outputFolderPath": "output",
            **({"quality": quality} if media_type == "image/jpeg" else {}),
        },
    }

    status_url = client.submit_job("/create-rendition", payload)
    job = client.poll_job(status_url)
    outputs = job.get("outputs") or []

    if not outputs:
        raise RuntimeError(f"No page-rendition outputs in job result: {job}")

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, out in enumerate(outputs):
        dl_url = out.get("destination", {}).get("url") or out.get("url")
        if not dl_url:
            continue
        source_path = out.get("source", f"output/page-{i + 1}.{ext}")
        dest = out_dir / f"{Path(source_path).stem}.{ext}"
        client.download_url(dl_url, str(dest))
        saved.append(str(dest.resolve()))
        logger.info("  Saved: %s", dest)

    return sorted(saved)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_images_from_indd(
    indd_source: str,
    output_dir: str | None = None,
    *,
    mode: str = "script",           # "script" | "rendition"
    image_format: str = "jpeg",     # rendition mode only
    resolution: int = 150,          # rendition mode only
    quality: str = "high",          # rendition mode only
    page_range: str = "All",        # rendition mode only
    force_register: bool = False,   # script mode: re-register even if cached
    auth: AdobeAuthClient | None = None,
) -> list[str]:
    """
    Extract images from an INDD file.

    Parameters
    ----------
    indd_source : str
        Pre-signed URL or local file path to the .indd file.
    output_dir : str, optional
        Directory to save extracted images.
        Defaults to ``output/images/<stem>/``.
    mode : str
        ``"script"`` (default) — extract actual embedded/linked source images
        using the Custom Scripts API (one file per placed image).
        ``"rendition"`` — render each page as a flat JPEG/PNG composite.
    image_format : str
        ``"jpeg"`` or ``"png"`` — rendition mode only.
    resolution : int
        DPI for page renditions (default 150).
    quality : str
        JPEG quality ``"low"`` | ``"medium"`` | ``"high"`` — rendition mode only.
    page_range : str
        Page range for renditions, e.g. ``"All"``, ``"1-3"``, ``"1,3,5"``.
    force_register : bool
        Re-register the ExtendScript even if a cached URL exists.
    auth : AdobeAuthClient, optional
        Shared auth client; created from env vars if omitted.

    Returns
    -------
    list[str]
        Sorted list of absolute paths to saved image files.
    """
    if mode not in ("script", "rendition"):
        raise ValueError(f"mode must be 'script' or 'rendition', got: {mode!r}")

    auth = auth or AdobeAuthClient()
    client = InDesignAPIClient(auth)

    # Resolve source to a URL + stem
    indd_url, stem = _resolve_to_url(indd_source, auth)
    indd_filename = f"{stem}.indd"

    # Output directory
    if output_dir is None:
        out_dir = Path("output/images") / stem
    else:
        out_dir = Path(output_dir)

    if mode == "script":
        logger.info("Mode: custom script — extracting actual embedded images")
        return _extract_via_custom_script(
            indd_url, indd_filename, out_dir, client, auth,
            force_register=force_register,
        )
    else:
        logger.info("Mode: page rendition — rendering pages as flat images")
        return _extract_via_rendition(
            indd_url, indd_filename, out_dir, client,
            image_format=image_format,
            resolution=resolution,
            quality=quality,
            page_range=page_range,
        )
