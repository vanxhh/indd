"""
Script registration and caching for the Adobe InDesign Custom Scripts API.

Flow
────
1. Bundle the ExtendScript directory into a ZIP file.
2. POST the ZIP to https://indesign.adobe.io/v3/scripts  (multipart/form-data).
3. Parse the response URL to extract {SCRIPT_ID} and {SCRIPT_NAME}.
4. Cache the registered script URL in a local JSON file so we don't re-register
   on every run (scripts persist on Adobe's side until explicitly deleted).

Docs:
  https://developer.adobe.com/firefly-services/docs/indesign-apis/guides/working-with-custom-scripts-api/
"""

import io
import json
import logging
import os
import zipfile
from pathlib import Path

import requests

from .adobe_client import AdobeAuthClient, INDESIGN_BASE_URL

logger = logging.getLogger(__name__)

# Local cache file – stores {script_name: script_url}
_CACHE_FILE = Path(__file__).parent.parent / ".script_cache.json"

# Built-in script bundles shipped with this project
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
EXTRACT_IMAGES_SCRIPT = "extract_images"


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _bundle_script(script_dir: Path) -> bytes:
    """
    ZIP the contents of `script_dir` (manifest.json + script.js + any extras).
    Returns the ZIP as bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(script_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(script_dir))
    return buf.getvalue()


def register_script(
    script_dir: str | Path,
    auth: AdobeAuthClient,
    *,
    force: bool = False,
) -> str:
    """
    Register a custom script bundle with the InDesign API.

    Parameters
    ----------
    script_dir : str | Path
        Directory containing manifest.json + script.js.
    auth : AdobeAuthClient
        Auth client (provides IMS headers).
    force : bool
        If True, skip the cache and always re-register.

    Returns
    -------
    str
        The full execution URL: ``https://indesign.adobe.io/v3/{SCRIPT_ID}/{SCRIPT_NAME}``
    """
    script_dir = Path(script_dir)
    script_name = script_dir.name

    cache = _load_cache()
    if not force and script_name in cache:
        logger.info("Using cached script URL for '%s': %s", script_name, cache[script_name])
        return cache[script_name]

    logger.info("Bundling script '%s'…", script_name)
    zip_bytes = _bundle_script(script_dir)

    logger.info("Registering script '%s' with InDesign API…", script_name)
    headers = {k: v for k, v in auth.auth_headers.items() if k != "Content-Type"}

    resp = requests.post(
        f"{INDESIGN_BASE_URL}/scripts",
        files={"file": (f"{script_name}.zip", zip_bytes, "application/zip")},
        headers=headers,
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()

    script_url = body.get("url")
    if not script_url:
        raise RuntimeError(f"No URL in script registration response: {body}")

    logger.info("Script registered at: %s", script_url)
    cache[script_name] = script_url
    _save_cache(cache)
    return script_url


def get_extract_images_script_url(auth: AdobeAuthClient, *, force: bool = False) -> str:
    """
    Return the execution URL for the bundled extract-images script,
    registering it first if not already cached.
    """
    return register_script(SCRIPTS_DIR / EXTRACT_IMAGES_SCRIPT, auth, force=force)
