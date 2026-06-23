"""
Convert an INDD file to PDF using the Adobe InDesign Rendition API.

Endpoint: POST https://indesign.adobe.io/v3/create-rendition
Docs:     https://developer.adobe.com/firefly-services/docs/indesign-apis/guides/working-with-rendition-api/

The InDesign API works exclusively with pre-signed URLs — it does NOT accept
binary file uploads.  If the user passes a local file path we first upload it
to a temporary location (using the REGISTER_SCRIPT_URL env var as an upload
endpoint, if configured) or raise a helpful error explaining they need to
provide a publicly-accessible / pre-signed URL.
"""

import logging
import os
from pathlib import Path

import requests

from .adobe_client import AdobeAuthClient, InDesignAPIClient

logger = logging.getLogger(__name__)


def _resolve_to_url(indd_source: str, auth: AdobeAuthClient) -> tuple[str, str]:
    """
    Return (presigned_url, filename_stem) for the INDD source.

    - If it's already a URL → use it directly.
    - If it's a local path  → upload via REGISTER_SCRIPT_URL (if set), else raise.
    """
    is_url = indd_source.startswith("http://") or indd_source.startswith("https://")

    if is_url:
        stem = Path(indd_source.split("?")[0]).stem or "document"
        return indd_source, stem

    # --- local file path ---
    if not os.path.isfile(indd_source):
        raise FileNotFoundError(f"INDD file not found: {indd_source}")

    register_url = os.environ.get("REGISTER_SCRIPT_URL", "")
    if not register_url:
        raise ValueError(
            "Local INDD file detected but REGISTER_SCRIPT_URL is not set.\n"
            "The InDesign API requires assets to be accessible as pre-signed URLs "
            "(S3, Azure Blob, Dropbox, etc.).\n"
            "Either:\n"
            "  1. Set REGISTER_SCRIPT_URL to an upload endpoint that returns a "
            "pre-signed GET URL, or\n"
            "  2. Upload the file to cloud storage yourself and pass its pre-signed URL."
        )

    stem = Path(indd_source).stem
    logger.info("Uploading local INDD to REGISTER_SCRIPT_URL…")
    with open(indd_source, "rb") as fh:
        up = requests.post(
            register_url,
            files={"file": (Path(indd_source).name, fh, "application/octet-stream")},
            headers={"Authorization": f"bearer {auth.get_access_token()}",
                     "x-api-key": auth.client_id},
            timeout=120,
        )
    up.raise_for_status()
    presigned_url = up.json().get("url") or up.json().get("presignedUrl") or up.json().get("downloadUrl")
    if not presigned_url:
        raise RuntimeError(
            f"Upload succeeded but no URL found in response: {up.json()}"
        )
    logger.info("Upload complete. Pre-signed URL obtained.")
    return presigned_url, stem


def convert_indd_to_pdf(
    indd_source: str,
    output_path: str | None = None,
    *,
    auth: AdobeAuthClient | None = None,
) -> str:
    """
    Convert an INDD file to PDF via the InDesign Rendition API.

    Parameters
    ----------
    indd_source : str
        Pre-signed URL **or** local file path to the .indd file.
        Local files require REGISTER_SCRIPT_URL to be set.
    output_path : str, optional
        Destination for the generated PDF.
        Defaults to output/pdf/<stem>.pdf.
    auth : AdobeAuthClient, optional
        Shared auth client; created from env vars if omitted.

    Returns
    -------
    str
        Absolute path to the saved PDF.
    """
    auth = auth or AdobeAuthClient()
    client = InDesignAPIClient(auth)

    # ------------------------------------------------------------------
    # 1. Resolve INDD to a URL the API can fetch
    # ------------------------------------------------------------------
    indd_url, stem = _resolve_to_url(indd_source, auth)
    indd_filename = f"{stem}.indd"

    # ------------------------------------------------------------------
    # 2. Determine output path
    # ------------------------------------------------------------------
    if output_path is None:
        out_dir = Path("output/pdf")
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(out_dir / f"{stem}.pdf")

    # ------------------------------------------------------------------
    # 3. Submit Rendition job (PDF)
    # ------------------------------------------------------------------
    logger.info("Submitting INDD → PDF rendition job…")
    payload = {
        "assets": [
            {
                "source": {"url": indd_url},
                "destination": indd_filename,
            }
        ],
        "params": {
            "outputMediaType": "application/pdf",
            "targetDocuments": [indd_filename],
            "outputFolderPath": "output",
        },
    }

    status_url = client.submit_job("/create-rendition", payload)

    # ------------------------------------------------------------------
    # 4. Poll until done
    # ------------------------------------------------------------------
    job = client.poll_job(status_url)
    logger.debug("Job result: %s", job)

    # ------------------------------------------------------------------
    # 5. Download the output PDF
    #    The API returns pre-signed GET URLs in job["outputs"]
    # ------------------------------------------------------------------
    outputs = job.get("outputs") or []
    if not outputs:
        # Fall back to data.outputs (older response shape)
        data = job.get("data", {})
        for entry in data.get("outputs", []):
            for rendition in entry.get("renditions", []):
                outputs.append(rendition)

    if not outputs:
        raise RuntimeError(f"No outputs found in job result: {job}")

    # Take the first (and usually only) PDF output
    pdf_output = outputs[0]
    download_url = (
        pdf_output.get("destination", {}).get("url")
        or pdf_output.get("url")
        or pdf_output.get("downloadUrl")
    )
    if not download_url:
        raise RuntimeError(f"Could not find a download URL in output: {pdf_output}")

    client.download_url(download_url, output_path)
    logger.info("PDF saved to: %s", output_path)
    return os.path.abspath(output_path)
