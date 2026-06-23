"""
Unit tests for the INDD Processor (InDesign API / Firefly Services).
All Adobe API calls are fully mocked — no credentials or network needed.
Run with:  pytest tests/ -v
"""

import io
import json
import os
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Inject dummy env vars before any package import
os.environ.update({
    "ADOBE_CLIENT_ID":      "test_client_id",
    "ADOBE_CLIENT_SECRET":  "test_secret",
    "ADOBE_SCOPES":         "openid,AdobeID,indesign_services",
    "ADOBE_ORG_ID":         "test_org_id",
    "IMS_TOKEN_URL":        "https://ims-na1.adobelogin.com/ims/token/v3",
    "REGISTER_SCRIPT_URL":  "https://example.com/register",
})

from src.adobe_client import AdobeAuthClient, InDesignAPIClient


# ─────────────────────────────────────────────────────────────────────
# Response factories
# ─────────────────────────────────────────────────────────────────────

def _token_resp():
    r = MagicMock()
    r.json.return_value = {"access_token": "fake_token", "expires_in": 86400}
    r.raise_for_status = MagicMock()
    return r

def _ok_resp(body: dict = None):
    r = MagicMock()
    r.json.return_value = body or {}
    r.raise_for_status = MagicMock()
    return r

def _job_resp(status: str = "succeeded", outputs=None):
    r = MagicMock()
    default_output = [{
        "destination": {"url": "https://s3.example.com/output.file"},
        "source": "output/page-1.jpg",
    }]
    r.json.return_value = {
        "status": status,
        "outputs": outputs if outputs is not None else default_output,
    }
    r.raise_for_status = MagicMock()
    return r

def _dl_resp(content: bytes = b"fake-bytes"):
    r = MagicMock()
    r.content = content
    r.raise_for_status = MagicMock()
    return r

def _preloaded_auth():
    auth = AdobeAuthClient()
    auth._access_token = "fake_token"
    auth._token_expiry = time.time() + 86400
    return auth


# ─────────────────────────────────────────────────────────────────────
# AdobeAuthClient
# ─────────────────────────────────────────────────────────────────────

class TestAdobeAuthClient:

    def test_get_access_token(self):
        auth = AdobeAuthClient()
        with patch("requests.post", return_value=_token_resp()) as mp:
            assert auth.get_access_token() == "fake_token"
        mp.assert_called_once()

    def test_token_cached(self):
        auth = AdobeAuthClient()
        with patch("requests.post", return_value=_token_resp()) as mp:
            auth.get_access_token()
            auth.get_access_token()
        assert mp.call_count == 1

    def test_token_refreshed_when_expired(self):
        auth = AdobeAuthClient()
        with patch("requests.post", return_value=_token_resp()) as mp:
            auth.get_access_token()
            auth._token_expiry = time.time() - 1
            auth.get_access_token()
        assert mp.call_count == 2

    def test_auth_headers(self):
        auth = _preloaded_auth()
        h = auth.auth_headers
        assert h["Authorization"] == "bearer fake_token"
        assert h["x-api-key"] == "test_client_id"
        assert h["x-gw-ims-org-id"] == "test_org_id"
        assert h["Content-Type"] == "application/json"

    def test_missing_env_var_raises(self):
        saved = os.environ.pop("ADOBE_CLIENT_ID")
        try:
            with pytest.raises(KeyError):
                AdobeAuthClient()
        finally:
            os.environ["ADOBE_CLIENT_ID"] = saved


# ─────────────────────────────────────────────────────────────────────
# InDesignAPIClient
# ─────────────────────────────────────────────────────────────────────

class TestInDesignAPIClient:

    def _client(self):
        return InDesignAPIClient(_preloaded_auth())

    def test_submit_job_returns_status_url(self):
        client = self._client()
        resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/abc"})
        with patch("requests.post", return_value=resp):
            url = client.submit_job("/create-rendition", {})
        assert url == "https://indesign.adobe.io/v3/status/abc"

    def test_submit_job_missing_status_url_raises(self):
        client = self._client()
        with patch("requests.post", return_value=_ok_resp({"jobId": "abc"})):
            with pytest.raises(RuntimeError, match="No statusUrl"):
                client.submit_job("/create-rendition", {})

    def test_poll_job_success(self):
        client = self._client()
        with patch("requests.get", return_value=_job_resp("succeeded")):
            result = client.poll_job("https://indesign.adobe.io/v3/status/abc")
        assert result["status"] == "succeeded"

    def test_poll_job_failure_raises(self):
        client = self._client()
        with patch("requests.get", return_value=_job_resp("failed")):
            with pytest.raises(RuntimeError, match="InDesign API job failed"):
                client.poll_job("https://indesign.adobe.io/v3/status/abc")

    def test_poll_job_timeout(self):
        client = self._client()
        with patch("requests.get", return_value=_job_resp("running")), \
             patch("time.sleep"):
            with pytest.raises(TimeoutError):
                client.poll_job("https://indesign.adobe.io/v3/status/abc", max_wait=0)

    def test_download_url(self, tmp_path):
        client = self._client()
        dest = tmp_path / "out.pdf"
        with patch("requests.get", return_value=_dl_resp(b"%PDF-1.4")):
            client.download_url("https://s3.example.com/out.pdf", str(dest))
        assert dest.read_bytes() == b"%PDF-1.4"


# ─────────────────────────────────────────────────────────────────────
# converter._resolve_to_url
# ─────────────────────────────────────────────────────────────────────

class TestResolveToUrl:

    def test_url_passthrough(self):
        from src.converter import _resolve_to_url
        url, stem = _resolve_to_url("https://example.com/my-doc.indd", _preloaded_auth())
        assert url == "https://example.com/my-doc.indd"
        assert stem == "my-doc"

    def test_url_strips_query_string_for_stem(self):
        from src.converter import _resolve_to_url
        _, stem = _resolve_to_url("https://s3.example.com/doc.indd?X-Amz=1", _preloaded_auth())
        assert stem == "doc"

    def test_local_missing_raises(self):
        from src.converter import _resolve_to_url
        with pytest.raises(FileNotFoundError):
            _resolve_to_url("/nonexistent/path.indd", _preloaded_auth())

    def test_local_without_register_url_raises(self, tmp_path):
        from src.converter import _resolve_to_url
        indd = tmp_path / "test.indd"
        indd.write_bytes(b"fake")
        saved = os.environ.pop("REGISTER_SCRIPT_URL", None)
        try:
            with pytest.raises(ValueError, match="REGISTER_SCRIPT_URL"):
                _resolve_to_url(str(indd), _preloaded_auth())
        finally:
            if saved:
                os.environ["REGISTER_SCRIPT_URL"] = saved

    def test_local_file_uploads_via_register_url(self, tmp_path):
        from src.converter import _resolve_to_url
        indd = tmp_path / "test.indd"
        indd.write_bytes(b"fake indd")
        up = MagicMock()
        up.json.return_value = {"url": "https://s3.example.com/presigned"}
        up.raise_for_status = MagicMock()
        with patch("requests.post", return_value=up):
            url, stem = _resolve_to_url(str(indd), _preloaded_auth())
        assert url == "https://s3.example.com/presigned"
        assert stem == "test"


# ─────────────────────────────────────────────────────────────────────
# converter.convert_indd_to_pdf
# ─────────────────────────────────────────────────────────────────────

class TestConverter:

    def test_convert_from_url(self, tmp_path):
        from src.converter import convert_indd_to_pdf
        out_pdf = tmp_path / "out.pdf"
        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/xyz"})
        job_resp = _job_resp("succeeded", outputs=[{
            "destination": {"url": "https://s3.example.com/result.pdf"},
            "source": "output/result.pdf",
        }])
        with patch("requests.post", return_value=submit_resp), \
             patch("requests.get", side_effect=[job_resp, _dl_resp(b"%PDF-1.4")]):
            result = convert_indd_to_pdf(
                "https://example.com/doc.indd",
                output_path=str(out_pdf),
                auth=_preloaded_auth(),
            )
        assert out_pdf.read_bytes() == b"%PDF-1.4"
        assert result == str(out_pdf.resolve())

    def test_missing_local_file_raises(self):
        from src.converter import convert_indd_to_pdf
        with pytest.raises(FileNotFoundError):
            convert_indd_to_pdf("/does/not/exist.indd", auth=_preloaded_auth())

    def test_no_outputs_raises(self, tmp_path):
        from src.converter import convert_indd_to_pdf
        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/xyz"})
        empty_job = _job_resp("succeeded", outputs=[])
        with patch("requests.post", return_value=submit_resp), \
             patch("requests.get", return_value=empty_job):
            with pytest.raises(RuntimeError, match="No outputs"):
                convert_indd_to_pdf(
                    "https://example.com/doc.indd",
                    output_path=str(tmp_path / "out.pdf"),
                    auth=_preloaded_auth(),
                )


# ─────────────────────────────────────────────────────────────────────
# script_manager
# ─────────────────────────────────────────────────────────────────────

class TestScriptManager:

    def test_bundle_creates_zip(self, tmp_path):
        from src.script_manager import _bundle_script
        script_dir = tmp_path / "myscript"
        script_dir.mkdir()
        (script_dir / "manifest.json").write_text('{"name":"test"}')
        (script_dir / "script.js").write_text("main();")

        zip_bytes = _bundle_script(script_dir)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        assert "manifest.json" in names
        assert "script.js" in names

    def test_register_script_calls_api(self, tmp_path):
        from src.script_manager import register_script, _CACHE_FILE

        script_dir = tmp_path / "myscript"
        script_dir.mkdir()
        (script_dir / "manifest.json").write_text('{"name":"myscript"}')
        (script_dir / "script.js").write_text("main();")

        reg_resp = MagicMock()
        reg_resp.json.return_value = {
            "url": "https://indesign.adobe.io/v3/SCRIPT_ID/myscript"
        }
        reg_resp.raise_for_status = MagicMock()

        # Temporarily override the cache file path to avoid polluting real cache
        import src.script_manager as sm
        original_cache = sm._CACHE_FILE
        sm._CACHE_FILE = tmp_path / ".test_cache.json"
        try:
            with patch("requests.post", return_value=reg_resp):
                url = register_script(script_dir, _preloaded_auth())
            assert url == "https://indesign.adobe.io/v3/SCRIPT_ID/myscript"
            # Cache should have been written
            cache = json.loads((tmp_path / ".test_cache.json").read_text())
            assert cache["myscript"] == url
        finally:
            sm._CACHE_FILE = original_cache

    def test_register_uses_cache(self, tmp_path):
        from src.script_manager import register_script
        import src.script_manager as sm

        script_dir = tmp_path / "cached_script"
        script_dir.mkdir()
        (script_dir / "manifest.json").write_text('{"name":"cached_script"}')
        (script_dir / "script.js").write_text("main();")

        cache_file = tmp_path / ".test_cache.json"
        cache_file.write_text(json.dumps({"cached_script": "https://indesign.adobe.io/v3/S/cached_script"}))

        original_cache = sm._CACHE_FILE
        sm._CACHE_FILE = cache_file
        try:
            with patch("requests.post") as mp:
                url = register_script(script_dir, _preloaded_auth())
            mp.assert_not_called()  # cache hit → no API call
            assert "cached_script" in url
        finally:
            sm._CACHE_FILE = original_cache

    def test_register_force_bypasses_cache(self, tmp_path):
        from src.script_manager import register_script
        import src.script_manager as sm

        script_dir = tmp_path / "cached_script"
        script_dir.mkdir()
        (script_dir / "manifest.json").write_text('{"name":"cached_script"}')
        (script_dir / "script.js").write_text("main();")

        cache_file = tmp_path / ".test_cache.json"
        cache_file.write_text(json.dumps({"cached_script": "https://old-url"}))

        reg_resp = MagicMock()
        reg_resp.json.return_value = {"url": "https://indesign.adobe.io/v3/NEW/cached_script"}
        reg_resp.raise_for_status = MagicMock()

        original_cache = sm._CACHE_FILE
        sm._CACHE_FILE = cache_file
        try:
            with patch("requests.post", return_value=reg_resp):
                url = register_script(script_dir, _preloaded_auth(), force=True)
            assert "NEW" in url
        finally:
            sm._CACHE_FILE = original_cache


# ─────────────────────────────────────────────────────────────────────
# extractor – Mode A: custom script
# ─────────────────────────────────────────────────────────────────────

class TestExtractorScriptMode:

    def _make_job_outputs(self, image_names):
        outputs = []
        for name in image_names:
            outputs.append({
                "destination": {"url": f"https://s3.example.com/{name}"},
                "source": f"extracted_images/{name}",
            })
        # Add summary JSON (should be skipped from saved images)
        outputs.append({
            "destination": {"url": "https://s3.example.com/extraction_summary.json"},
            "source": "extraction_summary.json",
        })
        return outputs

    def test_extracts_multiple_images(self, tmp_path):
        from src.extractor import extract_images_from_indd

        script_url = "https://indesign.adobe.io/v3/SCRIPT_ID/extract-images"
        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/abc"})
        job_resp = _job_resp("succeeded", self._make_job_outputs([
            "image_p1_1_photo.jpg",
            "image_p1_2_logo.png",
            "image_p2_3_hero.tif",
        ]))
        summary_resp = MagicMock()
        summary_resp.json.return_value = {"extracted": [], "skipped": []}
        summary_resp.content = b"{}"
        summary_resp.raise_for_status = MagicMock()
        dl1 = _dl_resp(b"jpg1")
        dl2 = _dl_resp(b"png2")
        dl3 = _dl_resp(b"tif3")

        with patch("src.extractor.get_extract_images_script_url", return_value=script_url), \
             patch("requests.post", return_value=submit_resp), \
             patch("requests.get", side_effect=[job_resp, summary_resp, dl1, dl2, dl3]):
            images = extract_images_from_indd(
                "https://example.com/doc.indd",
                output_dir=str(tmp_path / "out"),
                mode="script",
                auth=_preloaded_auth(),
            )

        # 3 images, summary JSON is excluded
        assert len(images) == 3
        exts = {Path(p).suffix.lower() for p in images}
        assert ".jpg" in exts
        assert ".png" in exts
        assert ".tif" in exts

    def test_no_outputs_returns_empty(self, tmp_path):
        from src.extractor import extract_images_from_indd
        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/abc"})
        job_resp = _job_resp("succeeded", [])

        with patch("src.extractor.get_extract_images_script_url",
                   return_value="https://indesign.adobe.io/v3/S/n"), \
             patch("requests.post", return_value=submit_resp), \
             patch("requests.get", return_value=job_resp):
            images = extract_images_from_indd(
                "https://example.com/doc.indd",
                output_dir=str(tmp_path / "out"),
                mode="script",
                auth=_preloaded_auth(),
            )
        assert images == []

    def test_force_register_passed_through(self, tmp_path):
        from src.extractor import extract_images_from_indd
        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/abc"})
        job_resp = _job_resp("succeeded", [])

        with patch("src.extractor.get_extract_images_script_url",
                   return_value="https://indesign.adobe.io/v3/S/n") as mock_reg, \
             patch("requests.post", return_value=submit_resp), \
             patch("requests.get", return_value=job_resp):
            extract_images_from_indd(
                "https://example.com/doc.indd",
                output_dir=str(tmp_path / "out"),
                mode="script",
                force_register=True,
                auth=_preloaded_auth(),
            )
        _, call_kwargs = mock_reg.call_args
        assert call_kwargs.get("force") is True


# ─────────────────────────────────────────────────────────────────────
# extractor – Mode B: page renditions
# ─────────────────────────────────────────────────────────────────────

class TestExtractorRenditionMode:

    def test_renders_multiple_pages(self, tmp_path):
        from src.extractor import extract_images_from_indd

        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/abc"})
        job_resp = _job_resp("succeeded", outputs=[
            {"destination": {"url": "https://s3.example.com/p1.jpg"}, "source": "output/page-1.jpg"},
            {"destination": {"url": "https://s3.example.com/p2.jpg"}, "source": "output/page-2.jpg"},
        ])

        with patch("requests.post", return_value=submit_resp), \
             patch("requests.get", side_effect=[job_resp, _dl_resp(b"jpg1"), _dl_resp(b"jpg2")]):
            images = extract_images_from_indd(
                "https://example.com/doc.indd",
                output_dir=str(tmp_path / "out"),
                mode="rendition",
                auth=_preloaded_auth(),
            )
        assert len(images) == 2

    def test_rendition_passes_correct_params(self, tmp_path):
        from src.extractor import extract_images_from_indd
        import requests as req_mod

        submit_resp = _ok_resp({"statusUrl": "https://indesign.adobe.io/v3/status/abc"})
        job_resp = _job_resp("succeeded", outputs=[
            {"destination": {"url": "https://s3.example.com/p1.png"}, "source": "output/page-1.png"},
        ])
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return submit_resp

        with patch.object(req_mod, "post", side_effect=fake_post), \
             patch.object(req_mod, "get", side_effect=[job_resp, _dl_resp(b"png")]):
            extract_images_from_indd(
                "https://example.com/doc.indd",
                output_dir=str(tmp_path / "out"),
                mode="rendition",
                image_format="png",
                resolution=300,
                page_range="1-5",
                auth=_preloaded_auth(),
            )
        assert captured["params"]["outputMediaType"] == "image/png"
        assert captured["params"]["resolution"] == 300
        assert captured["params"]["pageRange"] == "1-5"

    def test_invalid_mode_raises(self, tmp_path):
        from src.extractor import extract_images_from_indd
        with pytest.raises(ValueError, match="mode must be"):
            extract_images_from_indd(
                "https://example.com/doc.indd",
                mode="invalid",
                auth=_preloaded_auth(),
            )
