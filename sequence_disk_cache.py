"""Recover completed real observations when the API process restarts."""

import json
import math
import re
from itertools import islice
from time import time

from PIL import Image

import config


MAX_MANIFESTS_TO_SCAN = 512
MAX_MANIFEST_BYTES = 1_000_000
CACHED_DATA_NOTE = "Cached real observations; newer archive images may be available."
PARTIAL_TTL_SECONDS = 15 * 60
CACHED_PARTIAL_NOTE = (
    "Saved partial real observations; some dates or exposures may be missing. "
    "Use Refresh observations to search the archive again."
)


def cacheable_sequence(manifest):
    """Only nonempty real frame sets can stand in for another archive search."""
    if not isinstance(manifest, dict):
        return False
    status = manifest.get("status")
    frames = manifest.get("frames")
    return (status in {"ready", "partial"} and isinstance(frames, list)
            and bool(frames) and manifest.get("frame_count") == len(frames)
            and not (status == "ready" and manifest.get("archive_listing_partial")))


def mark_cached_sequence(manifest, age_seconds):
    """Disclose reuse without modifying the saved on-disk provenance."""
    partial = manifest["status"] == "partial"
    note = CACHED_PARTIAL_NOTE if partial else CACHED_DATA_NOTE
    notes = manifest.get("limitations")
    if not isinstance(notes, list):
        notes = []
    return {
        **manifest,
        "cached_result": True,
        "cached_partial": partial,
        "cache_age_seconds": max(0, int(age_seconds)),
        "cache_ttl_seconds": PARTIAL_TTL_SECONDS if partial else None,
        "limitations": [note, *(item for item in notes if item != note)],
    }


def _intact_png(path):
    """A present filename alone does not mean its PNG is usable."""
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= 10_000_000:
            return False
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode != "RGBA":
                return False
            image.verify()
        # Legacy sequences could contain only a sliver at the image edge.
        # Their center marker would then point at sky never observed. Newly
        # built sequences enforce this before export; check saved ones too.
        with Image.open(path) as image:
            width, height = image.size
            if width < 2 or height < 2:
                return False
            for y in ((height - 1) // 2, height // 2):
                for x in ((width - 1) // 2, width // 2):
                    if image.getpixel((x, y))[3] == 0:
                        return False
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def intact_saved_frames(manifest, output_root):
    """Verify that each frame belongs to this sequence and is a readable PNG."""
    if not cacheable_sequence(manifest):
        return False
    sequence_id = manifest.get("sequence_id", "")
    if not isinstance(sequence_id, str) or not re.fullmatch(r"[a-f0-9]{32}", sequence_id):
        return False
    folder = output_root / sequence_id
    if folder.resolve().parent != output_root.resolve():
        return False
    for frame in manifest["frames"]:
        if not isinstance(frame, dict):
            return False
        image_url = str(frame.get("image_url", ""))
        if not re.fullmatch(rf"/outputs/{sequence_id}/frame_\d{{3}}\.png", image_url):
            return False
        coverage = frame.get("coverage_fraction")
        if (not isinstance(coverage, (float, int)) or not math.isfinite(coverage)
                or coverage < config.MIN_FRAME_COVERAGE_FRACTION or coverage > 1):
            return False
        image_path = folder / image_url.split("/")[-1]
        if (image_path.resolve().parent != folder.resolve()
                or not _intact_png(image_path)):
            return False
    return True


def find_saved_sequence(request, output_root):
    """Find an intact completed sequence for this exact request, if available.

    Builds with actual frames write their manifest and PNGs under output/.
    Partial builds expire quickly so newly available observations can appear.
    """
    expected_request = request.model_dump(mode="json")
    candidates = []
    for path in islice(output_root.glob("*/manifest.json"), MAX_MANIFESTS_TO_SCAN):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        sequence_id = path.parent.name
        try:
            if path.parent.resolve().parent != output_root.resolve():
                continue
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            age_seconds = time() - path.stat().st_mtime
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if (
                manifest.get("request") != expected_request
                or manifest.get("sequence_id") != sequence_id
                or not cacheable_sequence(manifest)
                or not 0 <= age_seconds
                or (manifest["status"] == "partial" and age_seconds >= PARTIAL_TTL_SECONDS)
            ):
                continue
            if not intact_saved_frames(manifest, output_root):
                continue
            # Keep the on-disk record intact; the response states its age.
            return mark_cached_sequence(manifest, age_seconds)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # Partial writes and damaged records must not block a fresh build.
            continue
    return None
