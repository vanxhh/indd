"""
Adobe IMS authentication + InDesign API (Firefly Services) base client.

Endpoint: https://indesign.adobe.io/v3/
Auth:     OAuth Server-to-Server via IMS (client_credentials grant)

Docs:
  https://developer.adobe.com/firefly-services/docs/indesign-apis/
  https://developer.adobe.com/firefly-services/docs/indesign-apis/getting-started/
"""

import os
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

INDESIGN_BASE_URL = "https://indesign.adobe.io/v3"


class AdobeAuthClient:
    """
    Handles Adobe IMS OAuth Server-to-Server token management.
    Reads credentials from environment variables.
    """

    def __init__(self):
        self.client_id     = os.environ["ADOBE_CLIENT_ID"]
        self.client_secret = os.environ["ADOBE_CLIENT_SECRET"]
        self.scopes        = os.environ["ADOBE_SCOPES"]
        self.org_id        = os.environ["ADOBE_ORG_ID"]
        self.ims_token_url = os.environ["IMS_TOKEN_URL"]

        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0

    def get_access_token(self) -> str:
        """Return a valid IMS access token, refreshing when within 60 s of expiry."""
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token

        logger.info("Fetching new IMS access token…")
        resp = requests.post(
            self.ims_token_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "scope":         self.scopes,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        self._access_token = payload["access_token"]
        expires_in = payload.get("expires_in", 86400)
        self._token_expiry = time.time() + expires_in
        logger.info("IMS token obtained (expires in %ss).", expires_in)
        return self._access_token

    @property
    def auth_headers(self) -> dict:
        """Standard headers required by every InDesign API call."""
        return {
            "Authorization":  f"bearer {self.get_access_token()}",
            "x-api-key":      self.client_id,
            "x-gw-ims-org-id": self.org_id,
            "Content-Type":   "application/json",
        }


class InDesignAPIClient:
    """
    Thin wrapper around the Adobe InDesign REST API v3.

    Key differences from PDF Services:
    - Input assets are referenced as pre-signed URLs (S3 / Azure / Dropbox / HTTP),
      NOT uploaded as binary blobs.
    - Output assets are returned as pre-signed download URLs in the job status response.
    - Base URL: https://indesign.adobe.io/v3
    """

    def __init__(self, auth: AdobeAuthClient):
        self.auth = auth

    # ------------------------------------------------------------------
    # Job submission & polling
    # ------------------------------------------------------------------

    def submit_job(self, endpoint: str, payload: dict) -> str:
        """
        POST a job to `endpoint` and return the statusUrl for polling.
        """
        url = f"{INDESIGN_BASE_URL}{endpoint}"
        resp = requests.post(url, json=payload, headers=self.auth.auth_headers, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        status_url = body.get("statusUrl") or body.get("status_url")
        if not status_url:
            raise RuntimeError(
                f"No statusUrl in InDesign API response for {endpoint}. Body: {body}"
            )
        logger.info("Job submitted. Status URL: %s", status_url)
        return status_url

    def poll_job(
        self,
        status_url: str,
        poll_interval: int = 5,
        max_wait: int = 600,
    ) -> dict:
        """
        Poll `status_url` until the job succeeds or fails.
        Returns the final job JSON on success.
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            resp = requests.get(status_url, headers=self.auth.auth_headers, timeout=30)
            resp.raise_for_status()
            job = resp.json()
            status = job.get("status", "").lower()
            logger.debug("Job status: %s", status)

            if status == "succeeded":
                return job
            if status in ("failed", "error"):
                raise RuntimeError(f"InDesign API job failed: {job}")

            time.sleep(poll_interval)

        raise TimeoutError(f"InDesign API job did not complete within {max_wait}s.")

    def download_url(self, url: str, dest_path: str) -> None:
        """Download a pre-signed output URL to a local file."""
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            fh.write(resp.content)
        logger.info("Downloaded → %s", dest_path)
