#!/usr/bin/env python3
"""
INDD Processor CLI — powered by Adobe InDesign API (Firefly Services)
======================================================================

Commands
--------
  register  Register (or re-register) the extract-images ExtendScript.
  convert   Convert an INDD file to PDF.
  extract   Extract images from an INDD file.
  all       Convert to PDF AND extract images.

Image extraction modes (--mode)
--------------------------------
  script      (default) Runs an ExtendScript on the server to pull each
              placed/embedded source image at its original resolution.
              → Gives you the actual TIFFs, JPEGs, PNGs, PSDs etc.

  rendition   Renders entire pages as flat JPEG or PNG composites.
              → Gives you one image per page (like a screenshot).

Examples
--------
  python main.py register
  python main.py convert https://s3.example.com/doc.indd
  python main.py extract https://s3.example.com/doc.indd
  python main.py extract https://s3.example.com/doc.indd --mode rendition --resolution 300
  python main.py all https://s3.example.com/doc.indd --output out/doc.pdf --output-dir out/imgs/
"""

import argparse
import logging
import sys

from src import (
    AdobeAuthClient,
    convert_indd_to_pdf,
    extract_images_from_indd,
    get_extract_images_script_url,
)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
        datefmt="%H:%M:%S",
    )


# ──────────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────────

def cmd_register(args: argparse.Namespace, auth: AdobeAuthClient) -> None:
    print("\n📦  Registering extract-images script with InDesign API…")
    url = get_extract_images_script_url(auth, force=args.force)
    print(f"✅  Script registered at:\n    {url}\n")


def cmd_convert(args: argparse.Namespace, auth: AdobeAuthClient) -> None:
    print(f"\n🔄  Converting INDD → PDF: {args.source}")
    pdf_path = convert_indd_to_pdf(args.source, output_path=args.output, auth=auth)
    print(f"✅  PDF saved to: {pdf_path}\n")


def _print_images(images: list[str]) -> None:
    if images:
        print(f"✅  {len(images)} image(s) extracted:")
        for img in images:
            print(f"    • {img}")
    else:
        print("⚠️   No images were extracted from this document.")
    print()


def cmd_extract(args: argparse.Namespace, auth: AdobeAuthClient) -> None:
    mode_label = "embedded source images" if args.mode == "script" else "page renditions"
    print(f"\n🖼️   Extracting {mode_label} from: {args.source}")

    images = extract_images_from_indd(
        args.source,
        output_dir=args.output_dir,
        mode=args.mode,
        image_format=args.format,
        resolution=args.resolution,
        quality=args.quality,
        page_range=args.pages,
        force_register=args.force_register,
        auth=auth,
    )
    _print_images(images)


def cmd_all(args: argparse.Namespace, auth: AdobeAuthClient) -> None:
    print(f"\n⚙️   Processing INDD (convert + extract): {args.source}")

    pdf_path = convert_indd_to_pdf(args.source, output_path=args.output, auth=auth)
    print(f"✅  PDF saved to: {pdf_path}")

    images = extract_images_from_indd(
        args.source,
        output_dir=args.output_dir,
        mode=args.mode,
        image_format=args.format,
        resolution=args.resolution,
        quality=args.quality,
        page_range=args.pages,
        force_register=args.force_register,
        auth=auth,
    )
    _print_images(images)


# ──────────────────────────────────────────────────────────────────────
# Parser helpers
# ──────────────────────────────────────────────────────────────────────

def add_extract_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--output-dir", metavar="DIR",
                   help="Directory to save extracted images")
    p.add_argument("--mode", choices=["script", "rendition"], default="script",
                   help="'script' = actual embedded images (default); 'rendition' = flat page images")
    p.add_argument("--format", choices=["jpeg", "png"], default="jpeg", metavar="FMT",
                   help="Image format for rendition mode (default: jpeg)")
    p.add_argument("--resolution", type=int, default=150, metavar="DPI",
                   help="Render DPI for rendition mode (default: 150)")
    p.add_argument("--quality", choices=["low", "medium", "high"], default="high",
                   help="JPEG quality for rendition mode (default: high)")
    p.add_argument("--pages", default="All", metavar="RANGE",
                   help="Page range for rendition mode, e.g. '1-3' (default: All)")
    p.add_argument("--force-register", action="store_true",
                   help="Re-register the ExtendScript even if already cached")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indd-processor",
        description="Convert INDD → PDF and extract images via the Adobe InDesign API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    # register
    p_reg = sub.add_parser("register", help="Register the extract-images ExtendScript")
    p_reg.add_argument("--force", action="store_true",
                       help="Re-register even if already cached")

    # convert
    p_conv = sub.add_parser("convert", help="Convert INDD → PDF")
    p_conv.add_argument("source", help="Pre-signed URL or local path to the .indd file")
    p_conv.add_argument("--output", metavar="PATH", help="Output PDF path")

    # extract
    p_ext = sub.add_parser("extract", help="Extract images from INDD")
    p_ext.add_argument("source", help="Pre-signed URL or local path to the .indd file")
    add_extract_args(p_ext)

    # all
    p_all = sub.add_parser("all", help="Convert to PDF AND extract images")
    p_all.add_argument("source", help="Pre-signed URL or local path to the .indd file")
    p_all.add_argument("--output", metavar="PATH", help="Output PDF path")
    add_extract_args(p_all)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        auth = AdobeAuthClient()
    except KeyError as exc:
        print(f"❌  Missing required environment variable: {exc}", file=sys.stderr)
        sys.exit(1)

    dispatch = {
        "register": cmd_register,
        "convert":  cmd_convert,
        "extract":  cmd_extract,
        "all":      cmd_all,
    }
    try:
        dispatch[args.command](args, auth)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌  {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        logging.exception("Unexpected error")
        print(f"❌  {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
