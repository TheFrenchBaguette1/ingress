"""Build immutable, aligned SPHEREx frame files for a progressive band."""

from math import hypot, isfinite
import re
from time import monotonic
from uuid import uuid4

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from reproject import reproject_interp

import config
from archive import load_cutout
from saved_sources import _trusted_source
from sequence import finite_number, make_output_grid, product_id_for_source, sky_footprint
from sequence_disk_cache import STABLE_FRAME_STORAGE
from zoom import _atomic_manifest, _atomic_science_and_preview


MASTER_WIDTH_ARCMIN = 20
# IRSA sends a square on the native detector axes. The diagonal plus an
# interpolation margin encloses our north-up square even at a 45° rotation.
CUTOUT_WIDTH_ARCMIN = hypot(MASTER_WIDTH_ARCMIN, MASTER_WIDTH_ARCMIN) + 0.4
PADDED_CUTOUT_TIMEOUT_SECONDS = 7
BRIGHTNESS_UNIT = u.MJy / u.sr
PROGRESSIVE_PIPELINE = "progressive-v1"
DETECTOR_IN_PRODUCT = re.compile(r"level2_.+_\d+(?P<band>D[1-6])_spx_.+\.fits")


def archive_product_matches_band(source_url, band):
    """Verify the independent detector label in an official parent FITS name."""
    if not _trusted_source(source_url):
        return False
    _, parent, product = source_url.rsplit("/", 2)
    detector = DETECTOR_IN_PRODUCT.fullmatch(product)
    return bool(detector and detector["band"] == band
                and (not parent.isdigit() or parent == band[1:]))


class BandWriter:
    def __init__(self, output_root, job_request, band):
        self.sequence_id = uuid4().hex
        self.folder = output_root / self.sequence_id
        self.band = band
        self.detector = f"SPHEREx-{band}"
        self.coord = SkyCoord(job_request.ra_deg, job_request.dec_deg,
                              unit="deg", frame="icrs")
        self.request = {
            "ra_deg": job_request.ra_deg,
            "dec_deg": job_request.dec_deg,
            "start_date": job_request.start_date.isoformat(),
            "end_date": job_request.end_date.isoformat(),
            "fov_arcmin": MASTER_WIDTH_ARCMIN,
            "band": band,
        }
        self.wcs, self.shape, scale = make_output_grid(self.coord, MASTER_WIDTH_ARCMIN)
        height, width = self.shape
        self.dimensions = {"width": width, "height": height}
        self.footprint = sky_footprint(self.wcs, self.shape)
        self.grid = {
            "width_pixels": width,
            "height_pixels": height,
            "pixel_scale_arcsec": scale,
            "orientation": "north up, east left",
            "sky_footprint": self.footprint,
            "wcs_header": self.wcs.to_header().tostring(sep="\n", endcard=False, padding=False),
        }
        self.center = (
            slice((height - 1) // 2, height // 2 + 1),
            slice((width - 1) // 2, width // 2 + 1),
        )
        self.display = None
        self._records = {}

    def align(self, row, timeout_seconds):
        """Fetch an archive parent and put its covered pixels on our grid."""
        source_url = str(row["access_url"])
        if not _trusted_source(source_url):
            raise ValueError("The archive supplied an unrecognized SPHEREx product URL.")
        if (str(row["energy_bandpassname"]) != self.detector
                or not archive_product_matches_band(source_url, self.band)):
            raise ValueError("This exposure belongs to a different SPHEREx detector.")
        if not isfinite(float(row["t_min"])):
            raise ValueError("The exposure has no valid observation time.")

        deadline = monotonic() + timeout_seconds
        # Reserve time for the original width. Some archive cells respond
        # only to a smaller cutout, and this URL may already be cached.
        padded_timeout = min(PADDED_CUTOUT_TIMEOUT_SECONDS, timeout_seconds / 2)
        try:
            return self._align_cutout(row, source_url, CUTOUT_WIDTH_ARCMIN, padded_timeout)
        except Exception as padded_error:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            try:
                return self._align_cutout(row, source_url, MASTER_WIDTH_ARCMIN, remaining)
            except Exception as narrow_error:
                raise narrow_error from padded_error

    def _align_cutout(self, row, source_url, width, timeout_seconds):
        cutout = load_cutout(row, self.coord, width,
                             cache=config.CACHE_DOWNLOADS, timeout_seconds=timeout_seconds)
        if cutout["source_url"] != source_url or cutout["detector"] != self.detector:
            raise ValueError("The retrieved exposure differs from the selected parent product.")
        native_height, native_width = cutout["image"].shape
        x, y = cutout["target_x"], cutout["target_y"]
        if not (-0.5 <= x < native_width - 0.5 and -0.5 <= y < native_height - 0.5):
            raise ValueError("The target lies outside this observation.")

        # Each worker gets its own WCS object. wcslib can initialize internal
        # WCS state when transforming coordinates.
        scale = u.Unit(cutout["brightness_unit"]).to(BRIGHTNESS_UNIT)
        pixels, footprint = reproject_interp(
            (cutout["image"], cutout["wcs"]), self.wcs.deepcopy(),
            shape_out=self.shape, order="bilinear",
        )
        pixels = np.asarray(pixels, dtype=np.float64) * scale
        valid = (footprint > 0) & np.isfinite(pixels)
        coverage = float(np.count_nonzero(valid) / valid.size)
        if coverage < config.MIN_FRAME_COVERAGE_FRACTION:
            raise ValueError(f"Only {coverage:.1%} of the requested sky patch has valid pixels.")
        if not np.all(valid[self.center]):
            raise ValueError("The target center has no fully valid aligned pixels.")
        pixels[~valid] = np.nan

        wavelength = finite_number(cutout.get("wavelength_um") or np.nan)
        bandwidth = finite_number(cutout.get("bandwidth_um") or np.nan)
        return {
            "image": pixels,
            "brightness_unit": str(BRIGHTNESS_UNIT),
            "date_utc": cutout["date_utc"],
            "time_mjd": cutout["time_mjd"],
            "band": self.band,
            "detector": self.detector,
            "center_wavelength_um": wavelength if wavelength and wavelength > 0 else None,
            "center_bandwidth_um": bandwidth if bandwidth and bandwidth > 0 else None,
            "coverage_fraction": coverage,
            "native_cutout_width_arcmin": width,
            "native_cutout_dimensions": {"width": native_width, "height": native_height},
            "source_url": source_url,
            "cutout_url": cutout["cutout_url"],
        }

    def _set_display(self, frames):
        values = np.concatenate([frame["image"][np.isfinite(frame["image"])] for frame in frames])
        low, high = np.percentile(values, [config.DISPLAY_LOWER_PERCENTILE, config.DISPLAY_UPPER_PERCENTILE])
        if high <= low:
            low, high = values.min(), values.max()
        if high <= low:
            high = low + max(abs(low) * 1e-6, 1e-12)
        self.display = {
            "brightness_unit": str(BRIGHTNESS_UNIT),
            "vmin": float(low),
            "vmax": float(high),
            "stretch": "asinh",
            "stretch_a": 0.1,
            "missing_pixels": "transparent",
        }

    def manifest(self, aligned_frames, *, candidate_count, attempted_count,
                 failure_count, status, limitations=(), listing_partial=False,
                 complete=True):
        """Publish newly ready exposures without replacing files already in use.

        The band worker calls this serially. Storage slots are assigned once;
        a later exposure can appear earlier in the timeline without changing
        previously served image or science URLs.
        """
        if aligned_frames and self.display is None:
            self._set_display(aligned_frames)
        self.folder.mkdir(parents=True, exist_ok=True)
        for frame in aligned_frames:
            source = frame["source_url"]
            if source in self._records:
                continue
            slot = len(self._records)
            if slot >= 1000:
                raise ValueError("A band sequence can store at most 1,000 exposures.")
            science_name = f"science_{slot:03d}.npy"
            preview_name = f"frame_{slot:03d}.png"
            _atomic_science_and_preview(
                self.folder, science_name, preview_name, frame["image"], self.display,
            )
            self._records[source] = {
                **{key: value for key, value in frame.items() if key not in {"image", "brightness_unit"}},
                "index": slot,
                "image_url": f"/outputs/{self.sequence_id}/{preview_name}",
                "science_file": science_name,
                "product_id": product_id_for_source(source),
                "pixel_dimensions": self.dimensions,
                "sky_footprint": self.footprint,
            }
        ordered = sorted(self._records.values(), key=lambda frame: (frame["time_mjd"], frame["source_url"]))
        frames = []
        if ordered:
            first = ordered[0]["time_mjd"]
            frames = [{**frame, "elapsed_days": frame["time_mjd"] - first} for frame in ordered]
        cutout_notes = []
        if any(frame.get("native_cutout_width_arcmin") == MASTER_WIDTH_ARCMIN for frame in frames):
            cutout_notes.append(
                "Some exposures use the original 20′ cutout after a wider cutout failed. "
                "Pixels outside the available archive images remain transparent."
            )
        manifest = {
            "sequence_id": self.sequence_id,
            "pipeline": PROGRESSIVE_PIPELINE,
            "pipeline_complete": complete,
            "frame_storage": STABLE_FRAME_STORAGE,
            "status": status if frames else "empty",
            "archive_listing_partial": listing_partial,
            "request": self.request,
            "date_policy": "UTC calendar dates; both selected days included",
            "selection_policy": "Individual exposures from the selected detector, ordered by observation time.",
            "science_format": "aligned-npy-v1",
            "candidate_count": candidate_count,
            "attempted_count": attempted_count,
            "skipped_count": max(0, candidate_count - attempted_count),
            "frame_count": len(frames),
            "can_animate": len(frames) >= 2,
            "grid": self.grid,
            "display": self.display,
            "frames": frames,
            "failure_count": failure_count,
            "failures": [{"error": "Exposure cutout or target coverage could not be verified."}
                         for _ in range(failure_count)],
            "manifest_url": f"/outputs/{self.sequence_id}/manifest.json",
            "limitations": [
                *limitations,
                *cutout_notes,
                "The wavelength at a sky position can differ between exposures.",
                "Instrument quality flags have not yet been applied.",
                "Background and PSF differences have not been corrected.",
                "Unobserved pixels are transparent. PNG brightness is for visualization.",
            ],
        }
        _atomic_manifest(self.folder / "manifest.json", manifest)
        return manifest
