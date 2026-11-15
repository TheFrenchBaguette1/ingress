"""Recover completed real observations when the API process restarts."""

import json
import re
from itertools import islice


MAX_MANIFESTS_TO_SCAN = 512
MAX_MANIFEST_BYTES = 1_000_000
CACHED_DATA_NOTE = "Cached real observations; newer archive images may be available."


def find_saved_sequence(request, output_root):
    """Find an intact completed sequence for this exact request, if available.

    A successful build already writes its manifest and PNGs under output/. The
    API's memory cache disappears on restart, so reuse those same real frames
    without a fresh IRSA lookup. Never reuse an empty or mismatched sequence.
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
        if not re.fullmatch(r"[a-f0-9]{32}", sequence_id):
            continue
        try:
            if path.parent.resolve().parent != output_root.resolve():
                continue
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            manifest = json.loads(path.read_text(encoding="utf-8"))
            frames = manifest.get("frames")
            if (
                manifest.get("request") != expected_request
                or manifest.get("sequence_id") != sequence_id
                or manifest.get("status") not in ("ready", "partial")
                or not isinstance(frames, list)
                or not frames
                or manifest.get("frame_count") != len(frames)
            ):
                continue
            if any(
                not isinstance(frame, dict)
                or not re.fullmatch(
                    rf"/outputs/{sequence_id}/frame_\d{{3}}\.png",
                    str(frame.get("image_url", "")),
                )
                or not (path.parent / frame["image_url"].split("/")[-1]).is_file()
                or (path.parent / frame["image_url"].split("/")[-1]).resolve().parent != path.parent.resolve()
                for frame in frames
            ):
                continue
            notes = manifest.get("limitations")
            if not isinstance(notes, list):
                notes = []
            # Keep the on-disk record intact; the response states its age.
            return {**manifest, "limitations": [CACHED_DATA_NOTE, *notes]}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # Partial writes and damaged records must not block a fresh build.
            continue
    return None
