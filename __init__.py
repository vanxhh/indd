"""INDD Processor – convert and extract images from Adobe InDesign files
via the Adobe InDesign API (Firefly Services)."""

from .adobe_client import AdobeAuthClient, InDesignAPIClient
from .converter import convert_indd_to_pdf
from .extractor import extract_images_from_indd
from .script_manager import register_script, get_extract_images_script_url

__all__ = [
    "AdobeAuthClient",
    "InDesignAPIClient",
    "convert_indd_to_pdf",
    "extract_images_from_indd",
    "register_script",
    "get_extract_images_script_url",
]
