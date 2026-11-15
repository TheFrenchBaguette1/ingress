#!/usr/bin/env python3
"""Audit saved SPHEREx sequences for one sky position across field widths.

Reads local output manifests and PNG alpha masks only; it never queries IRSA.
The WCS corners describe the requested output grid. A frame's actual valid
footprint can be irregular, so its alpha-mask bounding box and valid fraction
are reported separately rather than represented as an exact polygon. Original
native FITS dimensions cannot be inferred from a processed output manifest;
the aligned science dimensions and PNG dimensions are reported separately.
"""

import argparse
import json
import math
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np
from astropy.io.fits import Header
from astropy.wcs import WCS
from PIL import Image


def sky_corners(wcs, x0, y0, x1, y1):
    pixels = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [
        {"pixel_edge_xy": [x, y], "ra_dec_deg": [float(ra), float(dec)]}
        for (x, y), (ra, dec) in zip(
            pixels,
            zip(*wcs.all_pix2world(
                [p[0] for p in pixels], [p[1] for p in pixels], 0
            )),
        )
    ]


def product_id(source_url):
    filename = Path(unquote(urlsplit(source_url or "").path)).name
    return filename or None


def science_metrics(frame, manifest_dir):
    """Read the aligned science array when the manifest references one."""
    filename = frame.get("science_file")
    if not filename or Path(filename).name != filename:
        return {"aligned_science_file": filename, "aligned_science_pixel_dimensions": None}
    path = manifest_dir / filename
    if not path.is_file():
        return {"aligned_science_file": filename, "aligned_science_pixel_dimensions": None}
    try:
        values = np.load(path, mmap_mode="r", allow_pickle=False)
        dimensions = list(values.shape[::-1]) if values.ndim == 2 else list(values.shape)
        return {
            "aligned_science_file": filename,
            "aligned_science_pixel_dimensions": dimensions,
            "aligned_science_valid_fraction": float(np.isfinite(values).mean()),
        }
    except (OSError, ValueError) as error:
        return {"aligned_science_file": filename, "aligned_science_error": str(error)}


def image_metrics(frame, root, sequence_id, wcs, nx, ny):
    url = frame.get("image_url", "")
    prefix = f"/outputs/{sequence_id}/"
    relative = Path(url.removeprefix(prefix))
    path = root / sequence_id / relative
    if (not url.startswith(prefix) or ".." in relative.parts or not path.is_file()):
        return {"rendered_png": url, "error": "PNG is missing or is outside this sequence"}
    try:
        with Image.open(path) as image:
            actual = list(image.size)
            if image.mode != "RGBA":
                return {"rendered_png": url, "rendered_pixel_dimensions": actual,
                        "error": f"Expected RGBA PNG, got {image.mode}"}
            alpha = image.getchannel("A")
            bbox = alpha.getbbox()  # left, top, right, bottom; exclusive right/bottom
            valid_pixels = actual[0] * actual[1] - alpha.histogram()[0]
        result = {
            "rendered_png": url,
            "rendered_pixel_dimensions": actual,
            "valid_pixel_fraction_from_mask": valid_pixels / (actual[0] * actual[1]),
            "valid_mask_bbox_png_xy": list(bbox) if bbox else None,
        }
        if bbox and actual == [nx, ny] and wcs:
            left, top, right, bottom = bbox
            # PNG rows were flipped when exported; WCS y grows northward.
            result["valid_mask_bbox_wcs_sky_corners"] = sky_corners(
                wcs, left - 0.5, ny - bottom - 0.5,
                right - 0.5, ny - top - 0.5,
            )
        return result
    except (OSError, ValueError) as error:
        return {"rendered_png": url, "error": str(error)}


def describe(path, manifest, root):
    grid = manifest.get("grid") or {}
    nx = int(grid.get("width_pixels") or 0)
    ny = int(grid.get("height_pixels") or 0)
    wcs = None
    wcs_error = None
    try:
        wcs = WCS(Header.fromstring(grid["wcs_header"], sep="\n"))
    except (KeyError, ValueError, TypeError) as error:
        wcs_error = str(error)
    footprint = sky_corners(wcs, -0.5, -0.5, nx - 0.5, ny - 0.5) if wcs else None
    sequence_id = manifest.get("sequence_id") or path.parent.name
    frames = []
    for frame in manifest.get("frames") or []:
        frames.append({
            "product_id": frame.get("product_id") or product_id(frame.get("source_url")),
            "product_id_from_fits_filename": product_id(frame.get("source_url")),
            "source_url": frame.get("source_url"),
            "cutout_url": frame.get("cutout_url"),
            "observed_utc": frame.get("date_utc"),
            "detector_band": frame.get("band"),
            "detector_name": frame.get("detector"),
            "wavelength_at_target_um": frame.get("center_wavelength_um"),
            "coverage_fraction_in_manifest": frame.get("coverage_fraction"),
            "frame_footprint_from_manifest": frame.get("sky_footprint"),
            "pixel_dimensions_from_manifest": frame.get("pixel_dimensions"),
            "display_scale": frame.get("display") or manifest.get("display"),
            "original_fits_pixel_dimensions": frame.get("original_fits_pixel_dimensions"),
            "source_cutout_pixel_dimensions": frame.get("native_cutout_dimensions") or
                                               frame.get("source_cutout_pixel_dimensions"),
            "zoom_cutout_pixel_dimensions": frame.get("zoom_native_cutout_dimensions"),
            **science_metrics(frame, path.parent),
            **image_metrics(frame, root, sequence_id, wcs, nx, ny),
        })
    return {
        "manifest_path": str(path),
        "sequence_id": sequence_id,
        "manifest_id": sequence_id,
        "manifest_kind": "zoom" if path.parent.name.startswith("zoom_") else "independent_search",
        "zoom_source_sequence_id": manifest.get("zoom_source_sequence_id"),
        "zoom_source_fov_arcmin": manifest.get("zoom_source_fov_arcmin"),
        "modified_utc_epoch_seconds": path.stat().st_mtime,
        "request": manifest.get("request"),
        "status": manifest.get("status"),
        "frame_count": manifest.get("frame_count"),
        "candidate_count": manifest.get("candidate_count"),
        "attempted_count": manifest.get("attempted_count"),
        "selection_policy": manifest.get("selection_policy"),
        "output_grid_pixels_xy": [nx, ny],
        "output_pixel_scale_arcsec": grid.get("pixel_scale_arcsec"),
        "requested_output_wcs_corners": footprint,
        "requested_output_wcs_error": wcs_error,
        "display_scale": manifest.get("display"),
        "frames": frames,
    }


def markdown(records):
    lines = ["# SPHEREx field-of-view exposure audit", "",
             "WCS corners describe the requested output grid. The valid data footprint ",
             "may be irregular; each PNG alpha mask is logged separately. Native FITS "
             "dimensions are unknown unless the manifest explicitly records them.", ""]
    for record in records:
        req = record["request"]
        lines += [f"## {req['fov_arcmin']}′ · {record['frame_count']} frames · "
                  f"{record['sequence_id']} · {record['manifest_kind']}", "",
                  f"- Manifest: `{record['manifest_path']}`",
                  f"- Query: RA {req['ra_deg']}°, Dec {req['dec_deg']}°, {req['band']}, "
                  f"{req['start_date']} to {req['end_date']}",
                  f"- Output grid: {record['output_grid_pixels_xy']} pixels at "
                  f"{record['output_pixel_scale_arcsec']}″/pixel",
                  f"- WCS corners (pixel edge → RA, Dec degrees): "
                  f"`{json.dumps(record['requested_output_wcs_corners'])}`",
                  f"- Display scale: `{json.dumps(record['display_scale'])}`", "",
                  "| Product ID (FITS basename) | UTC | Band / detector | λ at target (µm) | Science pixels | PNG pixels | Valid fraction | Valid mask bounds (PNG px) |",
                  "|---|---|---|---:|---|---|---:|---|"]
        for f in record["frames"]:
            lines.append("| " + " | ".join(str(x) for x in (
                f["product_id_from_fits_filename"], f["observed_utc"],
                f"{f['detector_band']} / {f['detector_name']}",
                f["wavelength_at_target_um"], f.get("aligned_science_pixel_dimensions"),
                f.get("rendered_pixel_dimensions"),
                f.get("valid_pixel_fraction_from_mask"), f.get("valid_mask_bbox_png_xy"),
            )) + " |")
        lines.append("")
        for f in record["frames"]:
            lines.append(
                f"  - `{f['product_id']}` valid-mask WCS bounding corners: "
                f"`{json.dumps(f.get('valid_mask_bbox_wcs_sky_corners'))}`; "
                f"cutout pixel dimensions: `{f.get('source_cutout_pixel_dimensions')}`; "
                f"zoom cutout pixel dimensions: `{f.get('zoom_cutout_pixel_dimensions')}`; "
                f"original FITS pixel dimensions: `{f.get('original_fits_pixel_dimensions')}`"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("output"))
    parser.add_argument("--ra", type=float, default=12.0)
    parser.add_argument("--dec", type=float, default=75.0)
    parser.add_argument("--fov", type=float, nargs="+", default=[20.0, 10.0, 5.0])
    parser.add_argument("--band", help="Optional detector filter, e.g. D4")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--latest-per-fov", action="store_true",
                        help="Show newest matching manifest for each FOV, band, and manifest kind")
    parser.add_argument("--include-zoom", action="store_true",
                        help="Also inspect zoom manifests under output/<sequence_id>/zoom_<FOV>/")
    args = parser.parse_args()
    matches = []
    paths = list(args.root.glob("*/manifest.json"))
    if args.include_zoom:
        paths.extend(args.root.glob("*/zoom_*/manifest.json"))
    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            request = manifest["request"]
            if (math.isclose(float(request["ra_deg"]), args.ra, abs_tol=1e-7)
                and math.isclose(float(request["dec_deg"]), args.dec, abs_tol=1e-7)
                and float(request["fov_arcmin"]) in args.fov
                and (not args.band or request.get("band") == args.band)):
                matches.append((path, manifest))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    # Show 20′, 10′, then 5′, newest saved run first within each width.
    matches.sort(key=lambda item: (-float(item[1]["request"]["fov_arcmin"]),
                                   -item[0].stat().st_mtime))
    if args.latest_per_fov:
        latest = {}
        for path, manifest in matches:
            request = manifest["request"]
            kind = "zoom" if path.parent.name.startswith("zoom_") else "independent_search"
            latest.setdefault((request["fov_arcmin"], request["band"], kind), (path, manifest))
        matches = list(latest.values())
    records = [describe(path, manifest, args.root) for path, manifest in matches]
    if args.format == "json":
        print(json.dumps(records, indent=2, allow_nan=False))
    else:
        print(markdown(records), end="")
    if not records:
        parser.exit(1, "No matching local manifests; first load the three views in the running app.\n")


if __name__ == "__main__":
    main()
