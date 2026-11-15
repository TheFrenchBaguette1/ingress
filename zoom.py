"""Lossless display zoom of an already selected SPHEREx exposure sequence.

The selected source URLs are immutable. We keep the initially reprojected
floating-point science pixels, including NaNs, and never perform a new SIA
search to satisfy a zoom request. Expanding uses the very same parent FITS
product, while copying every old pixel and mask into the new centered grid.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic
from uuid import uuid4

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from reproject import reproject_interp

import config
from archive import load_cutout
from sequence import make_output_grid, product_id_for_source, sky_footprint, write_display_png
from saved_sources import _trusted_source


_backfill_lock = Lock()


class ZoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    fov_arcmin: int = Field(ge=1, le=60)


class ZoomUnavailable(ValueError):
    """Original science arrays or a compatible WCS grid are unavailable."""


def _science_arrays(folder, frames, shape):
    arrays = []
    for index, frame in enumerate(frames):
        if frame.get("science_file") != f"science_{index:03d}.npy":
            raise ZoomUnavailable("The selected sequence has no saved science pixels. Refresh its observations.")
        path = folder / frame["science_file"]
        try:
            image = np.load(path, mmap_mode="r", allow_pickle=False)
            if image.shape != shape or image.dtype != np.dtype("float64"):
                raise ZoomUnavailable("Saved science pixels do not match the selected image grid.")
            arrays.append(image)
        except (OSError, ValueError) as error:
            if isinstance(error, ZoomUnavailable):
                raise
            raise ZoomUnavailable("Saved science pixels are unavailable. Refresh its observations.") from error
    return arrays


def _read_original(sequence_id):
    folder = config.OUTPUT_ROOT / sequence_id
    if folder.resolve().parent != config.OUTPUT_ROOT.resolve():
        raise ZoomUnavailable("Unknown selected sequence.")
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ZoomUnavailable("The selected sequence is unavailable. Search again.") from error
    if (manifest.get("sequence_id") != sequence_id
            or manifest.get("status") not in {"ready", "partial"}
            or not manifest.get("frames") or not manifest.get("display")):
        raise ZoomUnavailable("This sequence cannot be zoomed. Search again to save its science pixels.")
    if manifest.get("science_format") != "aligned-npy-v1":
        manifest = backfill_legacy_sequence(folder, manifest)
    return folder, manifest


def backfill_legacy_sequence(folder, manifest, *, deadline=None):
    """Upgrade a saved PNG-only sequence using its SAME verified FITS products.

    This is an explicit, bounded download of old selected IDs; no archive
    listing, new time sampling, or inferred data from display PNGs occurs.
    """
    with _backfill_lock:
        path = folder / "manifest.json"
        try:
            latest = json.loads(path.read_text(encoding="utf-8"))
            if latest.get("science_format") == "aligned-npy-v1":
                return latest
            if (latest.get("sequence_id") != folder.name
                    or latest.get("frames") != manifest.get("frames")):
                raise ZoomUnavailable("The saved observation list changed during science recovery.")
            grid = latest["grid"]
            shape = _grid_shape(grid)
            if (len(shape) != 2 or shape[0] < 2 or shape[0] > 600
                    or shape[0] != shape[1] or len(latest["frames"]) > config.MAX_USABLE_FRAMES):
                raise ZoomUnavailable("The old sky grid cannot be recovered safely.")
            original_wcs = WCS(fits.Header.fromstring(grid["wcs_header"], sep="\n"))
            coord = SkyCoord(latest["request"]["ra_deg"], latest["request"]["dec_deg"],
                             unit="deg", frame="icrs")
            frames = latest["frames"]
            if not all(_trusted_source(frame["source_url"]) for frame in frames):
                raise ZoomUnavailable("A saved archive product URL is not trusted.")

            def recover(frame):
                remaining = config.TIMEOUT_SECONDS if deadline is None else deadline - monotonic()
                if remaining <= 0:
                    raise ZoomUnavailable("The finite preparation budget ended during science recovery.")
                observation = {
                    "access_url": frame["source_url"], "t_min": frame["time_mjd"],
                    "energy_bandpassname": frame["detector"],
                }
                cutout = load_cutout(
                    observation, coord, latest["request"]["fov_arcmin"],
                    cache=config.CACHE_DOWNLOADS,
                    timeout_seconds=min(config.TIMEOUT_SECONDS, max(1, remaining)),
                )
                if (cutout.get("source_url", frame["source_url"]) != frame["source_url"]
                        or str(cutout["brightness_unit"]).strip() != latest["display"]["brightness_unit"]):
                    raise ZoomUnavailable("The old parent exposure or science unit changed.")
                pixels, coverage = reproject_interp(
                    (cutout["image"], cutout["wcs"]), original_wcs,
                    shape_out=shape, order="bilinear",
                )
                pixels = np.asarray(pixels, dtype=np.float64)
                pixels[(coverage <= 0) | ~np.isfinite(pixels)] = np.nan
                center_y = slice((shape[0] - 1)//2, shape[0]//2+1)
                center_x = slice((shape[1] - 1)//2, shape[1]//2+1)
                if not np.isfinite(pixels[center_y, center_x]).all():
                    raise ZoomUnavailable("The archived parent exposure no longer covers its old target center.")
                return pixels, {"width": int(cutout["image"].shape[1]),
                                "height": int(cutout["image"].shape[0])}

            with ThreadPoolExecutor(max_workers=config.MAX_CUTOUT_WORKERS) as workers:
                recovered = list(workers.map(recover, frames))
            footprint = sky_footprint(original_wcs, shape)
            upgraded_frames = []
            for index, (frame, (pixels, native_dimensions)) in enumerate(zip(frames, recovered)):
                science_file = f"science_{index:03d}.npy"
                _atomic_science_and_preview(folder, science_file,
                                            f"frame_{index:03d}.png", pixels, latest["display"])
                upgraded_frames.append({
                    **frame, "science_file": science_file,
                    "product_id": product_id_for_source(frame["source_url"]),
                    "pixel_dimensions": {"width": shape[1], "height": shape[0]},
                    "native_cutout_dimensions": native_dimensions,
                    "sky_footprint": footprint,
                    "coverage_fraction": float(np.isfinite(pixels).mean()),
                })
            upgraded = {
                **latest, "science_format": "aligned-npy-v1",
                "frames": upgraded_frames,
                "grid": {**grid, "sky_footprint": footprint},
                "limitations": [
                    "Recovered aligned science pixels and masks by rereading the same selected parent FITS products.",
                    *latest.get("limitations", []),
                ],
            }
            _atomic_manifest(path, upgraded)
            return upgraded
        except ZoomUnavailable:
            raise
        except Exception as error:
            raise ZoomUnavailable(f"Could not recover the exact selected parent exposures: {error}") from error


def _grid_shape(grid):
    return int(grid["height_pixels"]), int(grid["width_pixels"])


def _grid_matches(coord, manifest):
    """Reject a cached master whose declared pixel locations have drifted."""
    try:
        expected, shape, scale = make_output_grid(coord, manifest["request"]["fov_arcmin"])
        actual_grid = manifest["grid"]
        actual = WCS(fits.Header.fromstring(actual_grid["wcs_header"], sep="\n"))
        return (shape == _grid_shape(actual_grid)
                and np.isclose(float(actual_grid["pixel_scale_arcsec"]), scale, atol=1e-9)
                and all(np.allclose(getattr(actual.wcs, name), getattr(expected.wcs, name),
                                    rtol=0, atol=1e-9)
                        for name in ("crval", "crpix", "cdelt")))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _pngs_intact(folder, manifest):
    try:
        shape = _grid_shape(manifest["grid"])
        for index, frame in enumerate(manifest["frames"]):
            filename = f"frame_{index:03d}.png"
            if not frame["image_url"].endswith("/" + filename):
                return False
            with Image.open(folder / filename) as image:
                if image.format != "PNG" or image.mode != "RGBA" or image.size != shape[::-1]:
                    return False
                image.verify()
        return True
    except (OSError, ValueError, KeyError):
        return False


def _png_masks_match(folder, science):
    """Catch a damaged preview whose transparency differs from saved science."""
    try:
        for index, pixels in enumerate(science):
            with Image.open(folder / f"frame_{index:03d}.png") as preview:
                missing = np.asarray(preview)[::-1, :, 3] == 0
            if not np.array_equal(missing, ~np.isfinite(pixels)):
                return False
        return True
    except (OSError, ValueError, IndexError):
        return False


def _atomic_science_and_preview(folder, science_file, png_file, pixels, display):
    """Keep a previously served zoom readable during same-parent retries."""
    token = uuid4().hex
    temp_science = folder / f".{token}.{science_file}"
    temp_png = folder / f".{token}.{png_file}"
    try:
        np.save(temp_science, pixels, allow_pickle=False)
        write_display_png(pixels, temp_png, display)
        os.replace(temp_science, folder / science_file)
        os.replace(temp_png, folder / png_file)
    finally:
        temp_science.unlink(missing_ok=True)
        temp_png.unlink(missing_ok=True)


def _atomic_manifest(path, manifest):
    temporary = path.with_name(f".{uuid4().hex}.manifest.json")
    try:
        temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _same_sources(original, other):
    return (other.get("sequence_id") == original["sequence_id"]
            and len(other.get("frames", [])) == len(original["frames"])
            and all(a["source_url"] == b["source_url"]
                    and a["date_utc"] == b["date_utc"]
                    and a["band"] == b["band"]
                    and a["center_wavelength_um"] == b["center_wavelength_um"]
                    for a, b in zip(other["frames"], original["frames"])))


def _available_master(folder, original, coord, *, skip_incomplete=False):
    """Prefer widest complete aligned image so zoom out/zoom in are identical."""
    candidates = [(folder, original)]
    for path in folder.glob("zoom_*/manifest.json"):
        if not re.fullmatch(r"zoom_\d{3}", path.parent.name):
            continue
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if (_same_sources(original, candidate)
                    and candidate["display"] == original["display"]
                    and (not skip_incomplete or candidate.get("zoom_missing_outer_count", 0) == 0)
                    and _grid_matches(coord, candidate)
                    and _pngs_intact(path.parent, candidate)):
                candidates.append((path.parent, candidate))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for candidate_dir, manifest in sorted(
        candidates, key=lambda item: _grid_shape(item[1]["grid"])[1], reverse=True
    ):
        try:
            if not _grid_matches(coord, manifest) or not _pngs_intact(candidate_dir, manifest):
                continue
            shape = _grid_shape(manifest["grid"])
            arrays = _science_arrays(candidate_dir, manifest["frames"], shape)
            if not _png_masks_match(candidate_dir, arrays):
                continue
            return candidate_dir, manifest, arrays
        except (ZoomUnavailable, KeyError, TypeError, ValueError):
            continue
    raise ZoomUnavailable("The selected sequence's science pixels are unavailable. Search again.")


def _expand_frame(frame, coord, target_wcs, target_shape, old_pixels, old_shape, display):
    """Load ONLY the same parent product, then preserve old pixels exactly."""
    new_pixels = np.full(target_shape, np.nan, dtype=np.float64)
    note = None
    cutout_url = None
    native_dimensions = None
    try:
        if not _trusted_source(frame["source_url"]):
            raise ValueError("The saved archive product URL is not trusted.")
        observation = {
            "access_url": frame["source_url"], "t_min": frame["time_mjd"],
            "energy_bandpassname": frame["detector"],
        }
        cutout = load_cutout(
            observation, coord, target_shape[1] * config.OUTPUT_PIXEL_SCALE_ARCSEC / 60,
            cache=config.CACHE_DOWNLOADS, timeout_seconds=config.TIMEOUT_SECONDS,
        )
        if (cutout.get("source_url", frame["source_url"]) != frame["source_url"]
                or str(cutout["brightness_unit"]).strip() != display["brightness_unit"]):
            raise ValueError("The parent exposure or brightness unit changed unexpectedly.")
        native_dimensions = {"width": int(cutout["image"].shape[1]),
                             "height": int(cutout["image"].shape[0])}
        reprojected, footprint = reproject_interp(
            (cutout["image"], cutout["wcs"]), target_wcs,
            shape_out=target_shape, order="bilinear",
        )
        valid = (footprint > 0) & np.isfinite(reprojected)
        new_pixels[valid] = np.asarray(reprojected, dtype=np.float64)[valid]
        cutout_url = cutout["cutout_url"]
    except Exception as error:
        # A failed parent fetch must never make us substitute another product.
        # The previously viewed sky remains exact and new outskirts are masked.
        note = f"Outer sky could not be read from the same parent exposure: {type(error).__name__}: {error}"

    old_height, old_width = old_shape
    start_y = (target_shape[0] - old_height) // 2
    start_x = (target_shape[1] - old_width) // 2
    new_pixels[start_y:start_y + old_height, start_x:start_x + old_width] = old_pixels
    return new_pixels, cutout_url, note, native_dimensions


def build_zoom(request, *, refresh=False):
    """Return a sequence-shaped manifest with the same observations at new FOV."""
    folder, original = _read_original(request.sequence_id)
    requested_fov = request.fov_arcmin
    coord = SkyCoord(original["request"]["ra_deg"], original["request"]["dec_deg"], unit="deg", frame="icrs")
    if not _grid_matches(coord, original):
        raise ZoomUnavailable("The saved WCS grid is incompatible with precise zoom.")
    target_wcs, target_shape, pixel_scale = make_output_grid(coord, requested_fov)
    if (not np.isclose(pixel_scale, config.OUTPUT_PIXEL_SCALE_ARCSEC, rtol=0, atol=1e-9)
            or not np.isclose(original["grid"]["pixel_scale_arcsec"], pixel_scale, rtol=0, atol=1e-9)
            or target_shape[0] != target_shape[1]):
        raise ZoomUnavailable("Zoom requires a compatible 6-arcsecond sky grid.")

    # A request for the original field itself needs no new FITS or PNG data.
    if _grid_shape(original["grid"]) == target_shape:
        arrays = _science_arrays(folder, original["frames"], target_shape)
        if not _pngs_intact(folder, original) or not _png_masks_match(folder, arrays):
            raise ZoomUnavailable("The saved observation preview or mask is damaged. Search again.")
        return {**original, "zoom_source_sequence_id": original["sequence_id"],
                "zoom_policy": "original aligned science pixels; no new exposure search"}

    target_dir = folder / f"zoom_{requested_fov:03d}"
    target_manifest_path = target_dir / "manifest.json"
    if target_manifest_path.is_file() and not refresh:
        try:
            cached = json.loads(target_manifest_path.read_text(encoding="utf-8"))
            if (_same_sources(original, cached)
                    and cached.get("display") == original["display"]
                    and _grid_shape(cached["grid"]) == target_shape
                    and _grid_matches(coord, cached)
                    and _pngs_intact(target_dir, cached)):
                arrays = _science_arrays(target_dir, cached["frames"], target_shape)
                if _png_masks_match(target_dir, arrays):
                    return cached
        except (OSError, ValueError, KeyError, TypeError, ZoomUnavailable):
            pass

    master_dir, master, science = _available_master(
        folder, original, coord, skip_incomplete=refresh,
    )
    old_shape = _grid_shape(master["grid"])
    if ((target_shape[0] - old_shape[0]) % 2
            or (target_shape[1] - old_shape[1]) % 2):
        raise ZoomUnavailable("Zoom cannot align the center of this older sky grid without resampling.")

    expanding = target_shape[0] > old_shape[0]
    if expanding:
        with ThreadPoolExecutor(max_workers=config.MAX_CUTOUT_WORKERS) as workers:
            futures = [workers.submit(
                _expand_frame, frame, coord, target_wcs, target_shape, old_pixels,
                old_shape, original["display"],
            ) for frame, old_pixels in zip(original["frames"], science)]
            transformed = [future.result() for future in futures]
    else:
        start_y = (old_shape[0] - target_shape[0]) // 2
        start_x = (old_shape[1] - target_shape[1]) // 2
        transformed = [(
            np.asarray(pixels[start_y:start_y + target_shape[0],
                              start_x:start_x + target_shape[1]], dtype=np.float64).copy(),
            None,
            master["frames"][index].get("zoom_note") if target_shape[0] > _grid_shape(original["grid"])[0] else None,
            None,
        ) for index, pixels in enumerate(science)]

    target_dir.mkdir(parents=True, exist_ok=True)
    new_frames = []
    footprint = sky_footprint(target_wcs, target_shape)
    limitations = list(original.get("limitations", []))
    for index, (old_frame, (pixels, cutout_url, note, native_dimensions)) in enumerate(zip(original["frames"], transformed)):
        science_file = f"science_{index:03d}.npy"
        png_file = f"frame_{index:03d}.png"
        _atomic_science_and_preview(target_dir, science_file, png_file,
                                    pixels, original["display"])
        if note:
            limitations.append(f"{old_frame['source_url']}: {note}")
        new_frames.append({
            **old_frame,
            "product_id": old_frame.get("product_id") or product_id_for_source(old_frame["source_url"]),
            "image_url": f"/outputs/{request.sequence_id}/zoom_{requested_fov:03d}/{png_file}",
            "science_file": science_file,
            "coverage_fraction": float(np.isfinite(pixels).mean()),
            "pixel_dimensions": {"width": target_shape[1], "height": target_shape[0]},
            "sky_footprint": footprint,
            "zoom_native_cutout_dimensions": native_dimensions,
            "zoom_cutout_url": cutout_url,
            "cutout_url": cutout_url or old_frame.get("cutout_url"),
            "zoom_outer_region_status": "unavailable" if note else "loaded" if expanding else "cropped",
            "zoom_note": note,
        })

    manifest = {
        **original,
        "status": "partial" if any(frame["zoom_note"] for frame in new_frames) else original["status"],
        "zoom_missing_outer_count": sum(bool(frame["zoom_note"]) for frame in new_frames),
        "request": {**original["request"], "fov_arcmin": requested_fov},
        "zoom_source_sequence_id": original["sequence_id"],
        "zoom_source_fov_arcmin": original["request"]["fov_arcmin"],
        "zoom_policy": "same parent exposures, preserved aligned floats and NaN masks; no new SIA selection",
        "grid": {
            "width_pixels": target_shape[1], "height_pixels": target_shape[0],
            "pixel_scale_arcsec": pixel_scale, "orientation": "north up, east left",
            "sky_footprint": footprint,
            "wcs_header": target_wcs.to_header().tostring(sep="\n", endcard=False, padding=False),
        },
        "display": original["display"],
        "frames": new_frames,
        "limitations": limitations,
        "manifest_url": f"/outputs/{request.sequence_id}/zoom_{requested_fov:03d}/manifest.json",
    }
    _atomic_manifest(target_manifest_path, manifest)
    return manifest
