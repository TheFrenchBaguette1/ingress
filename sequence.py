import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from math import ceil, sqrt
from pathlib import PurePosixPath
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import numpy as np
from matplotlib import colormaps
from PIL import Image
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time
from astropy.visualization import AsinhStretch, ImageNormalize
from astropy.wcs import WCS
from pydantic import BaseModel, ConfigDict, Field, model_validator
from reproject import reproject_interp

import config
from archive import ArchiveUnavailable, load_cutout, search_observations
from archive_cache import cached_search_observations
from saved_sources import find_saved_sources


# ---------- Request validation ----------

class SequenceRequest(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
    )

    ra_deg: float = Field(ge=0, lt=360)
    dec_deg: float = Field(ge=-90, le=90)

    start_date: date
    end_date: date

    fov_arcmin: float = Field(
        gt=0,
        le=config.MAX_FOV_ARCMIN,
    )

    band: Literal["D1", "D2", "D3", "D4", "D5", "D6"]

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date.")

        if self.end_date == date.max:
            raise ValueError("end_date is outside the supported range.")

        return self


# ---------- Shared image grid ----------

def make_output_grid(coord, fov_arcmin):
    """Return a square, north-up/east-left grid."""
    pixels = max(
        1,
        ceil(
            fov_arcmin * 60
            / config.OUTPUT_PIXEL_SCALE_ARCSEC
        ),
    )

    # Preserve the requested field width despite rounding pixel count.
    scale_deg = (fov_arcmin / 60) / pixels

    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.cunit = ["deg", "deg"]
    wcs.wcs.crval = [coord.ra.deg, coord.dec.deg]
    wcs.wcs.crpix = [(pixels + 1) / 2, (pixels + 1) / 2]
    wcs.wcs.cdelt = [-scale_deg, scale_deg]

    return wcs, (pixels, pixels), scale_deg * 3600


def finite_number(value):
    """Return a JSON-safe float, or None for an invalid value."""
    value = float(value)
    return value if np.isfinite(value) else None


def product_id_for_source(source_url):
    """Keep the archive's FITS filename as a stable, inspectable product ID."""
    return PurePosixPath(urlsplit(source_url).path).name


def sky_footprint(output_wcs, shape):
    """Requested grid's four WCS edge corners, in RA/Dec degrees."""
    height, width = shape
    x = [-0.5, width - 0.5, width - 0.5, -0.5]
    y = [-0.5, -0.5, height - 0.5, height - 0.5]
    ra, dec = output_wcs.pixel_to_world_values(x, y)
    return [
        {"ra_deg": float(a), "dec_deg": float(d)}
        for a, d in zip(ra, dec)
    ]


def write_display_png(science, filename, display):
    """Apply one fixed sequence stretch, with the original NaN mask as alpha."""
    norm = ImageNormalize(
        vmin=display["vmin"], vmax=display["vmax"],
        stretch=AsinhStretch(a=display["stretch_a"]), clip=True,
    )
    cmap = colormaps["gray"].copy()
    cmap.set_bad((0, 0, 0, 0))
    rgba = np.asarray(cmap(norm(np.ma.masked_invalid(science)), bytes=True)).copy()
    rgba[~np.isfinite(science), 3] = 0
    # Astronomical image rows start at the south edge; PNG rows start at top.
    Image.fromarray(np.flipud(rgba).copy()).save(filename)


def time_spread_indexes(times, count):
    """Try distinct epochs first, then fill the largest gaps if they fail."""
    if len(times) <= count:
        return list(range(len(times)))
    times = np.asarray(times, dtype=float)
    chosen = []
    for target in np.linspace(times[0], times[-1], min(config.MAX_USABLE_FRAMES, count)):
        for index in np.argsort(np.abs(times - target), kind="stable"):
            if int(index) not in chosen:
                chosen.append(int(index))
                break
    while len(chosen) < count:
        # This also spreads observations when many have an identical timestamp.
        span = times[-1] - times[0]
        positions = times if span > 0 else np.arange(len(times))
        scores = np.min(np.abs(positions[:, None] - positions[chosen]), axis=1)
        scores[chosen] = -1
        chosen.append(int(np.argmax(scores)))
    return chosen


# ---------- Sequence generation ----------

def build_sequence(request, *, refresh=False, deadline=None):
    coord = SkyCoord(
        request.ra_deg,
        request.dec_deg,
        unit="deg",
        frame="icrs",
    )

    start_time = Time(
        request.start_date.isoformat(),
        scale="utc",
    )
    start_mjd = start_time.mjd

    # Include the entire selected end day.
    end_exclusive = request.end_date + timedelta(days=1)
    end_time = Time(end_exclusive.isoformat(), scale="utc")
    end_mjd = end_time.mjd

    detector = f"SPHEREx-{request.band}"

    # Search a circle enclosing the requested square field.
    search_radius_arcsec = request.fov_arcmin * 60 / sqrt(2)
    deadline = min(deadline, monotonic() + config.MAX_BUILD_SECONDS) if deadline is not None else monotonic() + config.MAX_BUILD_SECONDS

    # The user may enter dates before launch or in the future. Preserve that
    # requested interval in the manifest, but avoid searching empty years.
    available_start = max(request.start_date, config.MISSION_START_DATE)
    available_end_exclusive = min(
        end_exclusive, datetime.now(timezone.utc).date() + timedelta(days=1)
    )
    if available_start >= available_end_exclusive:
        results = Table(
            names=["t_min", "energy_bandpassname", "access_url"],
            dtype=[float, "U20", "U1"],
        )
    else:
        print("Searching the requested sky region...", flush=True)
        def lookup_with_saved_sources(coord, radius, collections, start, end, *, band):
            return search_observations(
                coord, radius, collections, start, end, band=band,
                known_sources=lambda: find_saved_sources(coord, band, start, end),
            )

        results = cached_search_observations(
            coord,
            search_radius_arcsec,
            config.COLLECTIONS,
            Time(available_start.isoformat(), scale="utc"),
            Time(available_end_exclusive.isoformat(), scale="utc"),
            fetch=lookup_with_saved_sources,
            band=request.band,
            refresh=refresh,
        )

    times = np.asarray(results["t_min"], dtype=float)

    selected = (
        (results["energy_bandpassname"] == detector)
        & np.isfinite(times)
        & (times >= start_mjd)
        & (times < end_mjd)
    )

    candidates = results[selected].copy()
    candidates.sort("t_min")

    # Avoid processing an identical source URL more than once.
    unique_candidates = []
    seen_urls = set()

    for row in candidates:
        url = str(row["access_url"])
        if url not in seen_urls:
            unique_candidates.append(row)
            seen_urls.add(url)

    output_wcs, shape, pixel_scale = make_output_grid(
        coord,
        request.fov_arcmin,
    )

    frames = []
    failures = []
    brightness_unit = None

    print(
        f"Found {len(unique_candidates)} observations "
        f"for {detector} within the date range.",
        flush=True,
    )

    total_candidates = len(unique_candidates)
    if not total_candidates and results.meta.get("archive_listing_partial"):
        # An incomplete date sample cannot establish that the selected
        # detector has no exposures in the full requested interval.
        raise ArchiveUnavailable(
            "The archive answered only part of this date range, with no "
            "confirmed images for this detector. Try a shorter date range."
        )
    plan = time_spread_indexes(
        [float(row["t_min"]) for row in unique_candidates],
        min(total_candidates, config.MAX_CUTOUT_ATTEMPTS),
    )
    attempted_count = 0

    # Download a bounded batch at a time. Process each batch in sample order so
    # metadata, failures and the reference brightness unit stay deterministic.
    # Joining each batch also ensures no worker survives the request/API lock.
    with ThreadPoolExecutor(max_workers=config.MAX_CUTOUT_WORKERS) as workers:
        plan_cursor = 0
        while (
            plan_cursor < len(plan)
            and len(frames) < config.MAX_USABLE_FRAMES
            and monotonic() < deadline
        ):
            batch_size = min(
                config.MAX_CUTOUT_WORKERS,
                config.MAX_USABLE_FRAMES - len(frames),
                len(plan) - plan_cursor,
            )
            batch = []
            for candidate_index in plan[plan_cursor:plan_cursor + batch_size]:
                if monotonic() >= deadline:
                    break
                observation = unique_candidates[candidate_index]
                source_url = str(observation["access_url"])
                timeout = min(
                    config.TIMEOUT_SECONDS, max(1, deadline - monotonic())
                )
                attempted_count += 1
                print(
                    f"[{attempted_count}/{len(plan)}] Loading cutout...",
                    flush=True,
                )
                future = workers.submit(
                    load_cutout,
                    observation,
                    coord,
                    request.fov_arcmin,
                    cache=config.CACHE_DOWNLOADS,
                    timeout_seconds=timeout,
                )
                batch.append((source_url, future))

            plan_cursor += len(batch)
            if not batch:
                break

            for source_url, future in batch:
                try:
                    cutout = future.result()

                    unit = str(cutout["brightness_unit"]).strip()

                    if brightness_unit is not None and unit != brightness_unit:
                        raise ValueError(
                            f"Different brightness units: {unit} "
                            f"versus {brightness_unit}."
                        )

                    aligned, footprint = reproject_interp(
                        (cutout["image"], cutout["wcs"]),
                        output_wcs,
                        shape_out=shape,
                        order="bilinear",
                    )

                    valid = (footprint > 0) & np.isfinite(aligned)

                    if not np.any(valid):
                        raise ValueError("No usable coverage within the output field.")

                    # A partial image may not contain the requested center.
                    height, width = cutout["image"].shape
                    x = cutout["target_x"]
                    y = cutout["target_y"]

                    center_inside = (
                        -0.5 <= x < width - 0.5
                        and -0.5 <= y < height - 0.5
                    )
                    coverage = float(valid.mean())
                    if coverage < config.MIN_FRAME_COVERAGE_FRACTION:
                        raise ValueError(
                            f"Only {coverage:.1%} of the requested sky patch "
                            "has valid image pixels."
                        )
                    if not center_inside:
                        raise ValueError("The target lies outside this observation.")

                    # The output grid centers the requested coordinates between
                    # four pixels for even sizes. All neighboring pixels must be
                    # valid to show this as an observation of the target.
                    center_rows = slice((shape[0] - 1) // 2, shape[0] // 2 + 1)
                    center_cols = slice((shape[1] - 1) // 2, shape[1] // 2 + 1)
                    if not np.all(valid[center_rows, center_cols]):
                        raise ValueError(
                            "The target center has no fully valid aligned image."
                        )

                    aligned[~valid] = np.nan

                    wavelength = None
                    bandwidth = None

                    wavelength = finite_number(cutout["wavelength_um"])
                    bandwidth = finite_number(cutout["bandwidth_um"])

                    if wavelength is not None and wavelength <= 0:
                        wavelength = None

                    if bandwidth is not None and bandwidth <= 0:
                        bandwidth = None

                    brightness_unit = unit

                    frames.append({
                        "image": aligned,
                        "date_utc": cutout["date_utc"],
                        "time_mjd": cutout["time_mjd"],
                        "band": request.band,
                        "detector": detector,
                        "center_wavelength_um": wavelength,
                        "center_bandwidth_um": bandwidth,
                        "coverage_fraction": coverage,
                        "native_cutout_dimensions": {
                            "width": int(width), "height": int(height),
                        },
                        "source_url": source_url,
                        "cutout_url": cutout["cutout_url"],
                    })

                    print(
                        f"  Retained: {cutout['date_utc']} UTC | "
                        f"coverage {coverage:.1%}",
                        flush=True,
                    )

                except Exception as error:
                    message = f"{type(error).__name__}: {error}"

                    failures.append({
                        "source_url": source_url,
                        "error": message,
                    })

                    print(f"  FAILED: {message}", flush=True)

    frames.sort(key=lambda frame: frame["time_mjd"])

    budget_exhausted = monotonic() >= deadline and attempted_count < total_candidates
    if budget_exhausted and not frames:
        raise ArchiveUnavailable(
            "The preparation time budget ended before any verified image "
            "could be built for this detector. Try a narrower date interval."
        )

    if not frames and results.meta.get("archive_listing_partial"):
        raise ArchiveUnavailable(
            "The archive listing is incomplete, and none of its real image "
            "cutouts could be loaded at this field width. "
            "Try a smaller field or a shorter date range."
        )

    # Each request gets its own output folder.
    sequence_id = uuid4().hex
    output_dir = config.OUTPUT_ROOT / sequence_id
    output_dir.mkdir(parents=True, exist_ok=False)

    exported_frames = []
    display = None

    if frames:
        values = np.concatenate([
            frame["image"][np.isfinite(frame["image"])]
            for frame in frames
        ])

        vmin, vmax = np.percentile(
            values,
            [
                config.DISPLAY_LOWER_PERCENTILE,
                config.DISPLAY_UPPER_PERCENTILE,
            ],
        )

        if vmax <= vmin:
            vmin = float(values.min())
            vmax = float(values.max())

        if vmax <= vmin:
            vmax = vmin + max(abs(vmin) * 1e-6, 1e-12)

        first_time = frames[0]["time_mjd"]

        display = {
            "brightness_unit": brightness_unit,
            "vmin": float(vmin),
            "vmax": float(vmax),
            "stretch": "asinh",
            "stretch_a": 0.1,
            "missing_pixels": "transparent",
        }

        for index, frame in enumerate(frames):
            filename = f"frame_{index:03d}.png"
            # The PNG is a visualization, not the science data. Keep the
            # original aligned floats and NaN mask for lossless zoom crops.
            science_file = f"science_{index:03d}.npy"
            np.save(output_dir / science_file, np.asarray(frame["image"], dtype=np.float64),
                    allow_pickle=False)
            write_display_png(frame["image"], output_dir / filename, display)

            metadata = {
                key: value
                for key, value in frame.items()
                if key != "image"
            }

            metadata.update({
                "index": index,
                "image_url": f"/outputs/{sequence_id}/{filename}",
                "science_file": science_file,
                "product_id": product_id_for_source(frame["source_url"]),
                "pixel_dimensions": {"width": shape[1], "height": shape[0]},
                "sky_footprint": sky_footprint(output_wcs, shape),
                "elapsed_days": frame["time_mjd"] - first_time,
            })

            exported_frames.append(metadata)


    if not frames:
        status = "empty"
    elif failures or results.meta.get("archive_listing_partial") or budget_exhausted:
        status = "partial"
    else:
        status = "ready"

    limitations = [
        "The wavelength at a sky position can differ between exposures.",
        "Instrument quality flags have not yet been applied.",
        "Background and PSF differences have not been corrected.",
        "Some frames may cover only part of the requested field.",
        f"Frames with less than {config.MIN_FRAME_COVERAGE_FRACTION:.0%} valid "
        "coverage or no valid target center are excluded.",
        "PNG values are for visualization, not photometry.",
    ]
    if results.meta.get("archive_listing_cached"):
        limitations.insert(
            0,
            "Reused a saved archive observation listing; newly released "
            "exposures may not appear until the listing refreshes.",
        )
    if results.meta.get("archive_listing_partial"):
        limitations.insert(0, results.meta["archive_listing_window_note"])
    if budget_exhausted:
        limitations.insert(0, "Preparation time budget ended before all available exposure candidates were checked.")
    if total_candidates > attempted_count:
        limitations.insert(
            0,
            f"Time-spread sample: attempted {attempted_count} of "
            f"{total_candidates} available exposures; some dates were skipped "
            "to keep the search responsive.",
        )

    manifest = {
        "sequence_id": sequence_id,
        "status": status,
        "archive_listing_partial": bool(results.meta.get("archive_listing_partial")),
        "request": request.model_dump(mode="json"),
        "date_policy": "UTC calendar dates; both selected days included",
        "selection_policy": "time-spread sample of individual exposures from the selected detector",
        "science_format": "aligned-npy-v1",
        "candidate_count": total_candidates,
        "attempted_count": attempted_count,
        "skipped_count": total_candidates - attempted_count,
        "frame_count": len(exported_frames),
        "can_animate": len(exported_frames) >= 2,
        "grid": {
            "width_pixels": shape[1],
            "height_pixels": shape[0],
            "pixel_scale_arcsec": pixel_scale,
            "orientation": "north up, east left",
            "sky_footprint": sky_footprint(output_wcs, shape),
            "wcs_header": output_wcs.to_header().tostring(
                sep="\n",
                endcard=False,
                padding=False,
            ),
        },
        "display": display,
        "frames": exported_frames,
        "failures": failures,
        "manifest_url": f"/outputs/{sequence_id}/manifest.json",
        "limitations": limitations,
    }

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print(f"\nExported {len(exported_frames)} frames to {output_dir}")

    return manifest
