import json
from datetime import date, timedelta
from math import ceil, sqrt
from typing import Literal
from uuid import uuid4

import numpy as np
from matplotlib import colormaps
from PIL import Image
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.visualization import AsinhStretch, ImageNormalize
from astropy.wcs import WCS
from pydantic import BaseModel, ConfigDict, Field, model_validator
from reproject import reproject_interp

import config
from archive import load_cutout, search_observations


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


# ---------- Sequence generation ----------

def build_sequence(request):
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

    print("Searching the requested sky region...", flush=True)

    results = search_observations(
        coord,
        search_radius_arcsec,
        config.COLLECTIONS,
        start_time,
        end_time,
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

    for index, observation in enumerate(unique_candidates, start=1):
        source_url = str(observation["access_url"])

        print(
            f"[{index}/{len(unique_candidates)}] Loading cutout...",
            flush=True,
        )

        try:
            cutout = load_cutout(
                observation,
                coord,
                request.fov_arcmin,
                cache=config.CACHE_DOWNLOADS,
                timeout_seconds=config.TIMEOUT_SECONDS,
            )

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

            aligned[~valid] = np.nan

            # A partial image may not contain the requested center.
            height, width = cutout["image"].shape
            x = cutout["target_x"]
            y = cutout["target_y"]

            center_inside = (
                -0.5 <= x < width - 0.5
                and -0.5 <= y < height - 0.5
            )

            wavelength = None
            bandwidth = None

            if center_inside:
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
                "coverage_fraction": float(valid.mean()),
                "source_url": source_url,
                "cutout_url": cutout["cutout_url"],
            })

            print(
                f"  Retained: {cutout['date_utc']} UTC | "
                f"coverage {valid.mean():.1%}",
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

        norm = ImageNormalize(
            vmin=float(vmin),
            vmax=float(vmax),
            stretch=AsinhStretch(a=0.1),
            clip=True,
        )

        cmap = colormaps["gray"].copy()
        cmap.set_bad((0, 0, 0, 0))

        first_time = frames[0]["time_mjd"]

        for index, frame in enumerate(frames):
            filename = f"frame_{index:03d}.png"

            masked = np.ma.masked_invalid(frame["image"])
            rgba = np.asarray(cmap(norm(masked), bytes=True)).copy()
            # ImageNormalize can drop the masked-array mask. Set alpha from
            # the aligned image explicitly so uncovered sky is never shown
            # as a bright pixel in the exported PNG.
            rgba[~np.isfinite(frame["image"]), 3] = 0

            # Image arrays have north at the upper end of the y axis.
            # Flip rows when writing a PNG with a top-left origin.
            Image.fromarray(np.flipud(rgba).copy()).save(
                output_dir / filename
            )

            metadata = {
                key: value
                for key, value in frame.items()
                if key != "image"
            }

            metadata.update({
                "index": index,
                "image_url": f"/outputs/{sequence_id}/{filename}",
                "elapsed_days": frame["time_mjd"] - first_time,
            })

            exported_frames.append(metadata)

        display = {
            "brightness_unit": brightness_unit,
            "vmin": float(vmin),
            "vmax": float(vmax),
            "stretch": "asinh",
            "stretch_a": 0.1,
            "missing_pixels": "transparent",
        }

    if not frames:
        status = "empty"
    elif failures:
        status = "partial"
    else:
        status = "ready"

    manifest = {
        "sequence_id": sequence_id,
        "status": status,
        "request": request.model_dump(mode="json"),
        "date_policy": "UTC calendar dates; both selected days included",
        "selection_policy": "individual exposures from the selected detector",
        "candidate_count": len(unique_candidates),
        "frame_count": len(exported_frames),
        "can_animate": len(exported_frames) >= 2,
        "grid": {
            "width_pixels": shape[1],
            "height_pixels": shape[0],
            "pixel_scale_arcsec": pixel_scale,
            "orientation": "north up, east left",
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
        "limitations": [
            "The wavelength at a sky position can differ between exposures.",
            "Instrument quality flags have not yet been applied.",
            "Background and PSF differences have not been corrected.",
            "Some frames may cover only part of the requested field.",
            "PNG values are for visualization, not photometry.",
        ],
    }

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print(f"\nExported {len(exported_frames)} frames to {output_dir}")

    return manifest
