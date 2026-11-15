"""Recover verified real SPHEREx image URLs when the archive index is down.

These manifests were created by successful image builds. Their URLs identify
actual FITS exposures; each fallback build requests a fresh cutout at the new
field width. A small saved source list is never complete sky/date coverage.
"""

import json
import math
import re
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

from astropy.table import Table

import config


BUNDLED_ROOT = Path(__file__).resolve().parent / "public" / "real-example"
MAX_MANIFESTS_PER_ROOT = 512
MAX_MANIFEST_BYTES = 1_000_000
MAX_SOURCES = 32
_FITS_PATH = re.compile(r"/ibe/data/spherex/(?:dr1|qr3)/level2/[^?#]+\.fits")
_FRAME_FILE = re.compile(r"frame_\d{3}\.png")


def _trusted_source(url):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "irsa.ipac.caltech.edu"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query and not parsed.fragment
        and _FITS_PATH.fullmatch(parsed.path) is not None
    )


def _matching_frames(root, ra, dec, band, start_mjd, end_mjd):
    if not root.is_dir():
        return
    for path in islice(root.glob("*/manifest.json"), MAX_MANIFESTS_PER_ROOT):
        try:
            if path.resolve().parent.parent != root.resolve():
                continue
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            manifest = json.loads(path.read_text(encoding="utf-8"))
            request = manifest["request"]
            frames = manifest["frames"]
            if (
                manifest.get("status") not in ("ready", "partial")
                or not isinstance(request, dict)
                or not isinstance(frames, list)
                or not frames or len(frames) != manifest.get("frame_count")
                or request.get("ra_deg") != ra
                or request.get("dec_deg") != dec
                or request.get("band") != band
            ):
                continue
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                mjd = frame.get("time_mjd")
                url = frame.get("source_url")
                image_url = frame.get("image_url")
                if (
                    frame.get("detector") != f"SPHEREx-{band}"
                    or not isinstance(mjd, (float, int))
                    or not math.isfinite(mjd)
                    or not start_mjd <= mjd < end_mjd
                    or not _trusted_source(url)
                    or not isinstance(image_url, str)
                ):
                    continue
                filename = image_url.rsplit("/", 1)[-1]
                if not _FRAME_FILE.fullmatch(filename):
                    continue
                if image_url not in (
                    f"/outputs/{path.parent.name}/{filename}",
                    f"/public/real-example/{path.parent.name}/{filename}",
                ):
                    continue
                image_file = path.parent / filename
                if (image_file.resolve().parent != path.parent.resolve()
                        or not image_file.is_file()
                        or image_file.stat().st_size < 8):
                    continue
                yield (float(mjd), f"SPHEREx-{band}", url)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue


def find_saved_sources(coord, band, start_time, end_time, *, output_root=None,
                       bundled_root=None):
    """Return only known real exposures for this exact center and detector."""
    output_root = config.OUTPUT_ROOT if output_root is None else output_root
    bundled_root = BUNDLED_ROOT if bundled_root is None else bundled_root
    ra, dec = float(coord.ra.deg), float(coord.dec.deg)
    start_mjd, end_mjd = float(start_time.mjd), float(end_time.mjd)
    sources = {}
    for root in (output_root, bundled_root):
        for mjd, detector, url in _matching_frames(
            root, ra, dec, band, start_mjd, end_mjd
        ):
            sources[url] = (mjd, detector, url)
            if len(sources) >= MAX_SOURCES:
                break
        if len(sources) >= MAX_SOURCES:
            break
    if not sources:
        return None
    listing = Table(
        rows=sorted(sources.values()),
        names=("t_min", "energy_bandpassname", "access_url"),
    )
    listing.meta["archive_listing_partial"] = True
    listing.meta["archive_listing_window_note"] = (
        "Reused previously verified real SPHEREx image URLs for this exact "
        "sky center and detector because the archive index did not answer. "
        "Only these saved observation dates are available; other dates may "
        "be missing. Cutouts were requested again at the selected field width."
    )
    return listing
