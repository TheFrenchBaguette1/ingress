"""Build immutable, aligned SPHEREx frame files for a progressive band."""

from math import ceil, floor, hypot, isfinite
import re
from time import monotonic
from uuid import uuid4

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.wcs import WCS
from reproject import reproject_interp

import config
from archive import load_cutout
from henry_fits import BAD_FLAG_BITS, BAD_FLAG_MASK, FLAG_NAMES, read_parent, read_wavelength
from saved_sources import _trusted_source
from sequence import finite_number, make_output_grid, product_id_for_source, sky_footprint
from sequence_disk_cache import STABLE_FRAME_STORAGE
from zoom import _atomic_manifest, _atomic_science_and_preview


MASTER_WIDTH_ARCMIN = 20
# IRSA sends a square on the native detector axes. The diagonal plus an
# interpolation margin encloses our north-up square even at a 45° rotation.
CUTOUT_WIDTH_ARCMIN = hypot(MASTER_WIDTH_ARCMIN, MASTER_WIDTH_ARCMIN) + 0.4
PADDED_CUTOUT_TIMEOUT_SECONDS = 7
NATIVE_READ_TIMEOUT_SECONDS = 10
WAVELENGTH_TIMEOUT_SECONDS = 3
BRIGHTNESS_UNIT = u.MJy / u.sr
PROGRESSIVE_PIPELINE = "progressive-v1"
DETECTOR_IN_PRODUCT = re.compile(r"level2_.+_\d+(?P<band>D[1-6])_spx_.+\.fits")

class _UnusableCoverage(ValueError):
    """The available science data fails the viewer's coverage checks."""


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
        # Sampling the edges accounts for curved projected boundaries from
        # the native detector SIP model. Extra pixels cover bilinear taps.
        xs = np.linspace(-0.5, width - 0.5, 9)
        ys = np.linspace(-0.5, height - 0.5, 9)
        self.boundary_sky = self.wcs.pixel_to_world(
            np.r_[xs, xs, np.full(9, -0.5), np.full(9, width - 0.5)],
            np.r_[np.full(9, -0.5), np.full(9, height - 0.5), ys, ys],
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
        time_mjd = float(row["t_min"])
        if not isfinite(time_mjd):
            raise ValueError("The exposure has no valid observation time.")

        started = monotonic()
        deadline = started + timeout_seconds
        native_deadline = started + min(NATIVE_READ_TIMEOUT_SECONDS, timeout_seconds * 0.6)
        try:
            return self._align_native(source_url, time_mjd, native_deadline, deadline)
        except _UnusableCoverage:
            # A successful native read may flag the target as unusable. An
            # unchecked archive cutout must not make that target valid again.
            raise
        except Exception as native_error:
            now = monotonic()
            if deadline <= now:
                raise
            try:
                return self._align_archive(row, source_url, deadline, now)
            except Exception as archive_error:
                raise archive_error from native_error

    def _native_window(self, header):
        native_wcs = WCS(header)
        x, y = native_wcs.world_to_pixel(self.boundary_sky)
        if not (np.isfinite(x).all() and np.isfinite(y).all()):
            raise ValueError("The native FITS WCS could not locate the requested sky patch.")
        x0 = max(0, floor(x.min()) - 2)
        x1 = min(int(header["NAXIS1"]), ceil(x.max()) + 3)
        y0 = max(0, floor(y.min()) - 2)
        y1 = min(int(header["NAXIS2"]), ceil(y.max()) + 3)
        if not (x0 < x1 and y0 < y1):
            raise _UnusableCoverage("The requested patch lies outside this observation.")
        return x0, x1, y0, y1

    def _align_native(self, source_url, time_mjd, native_deadline, deadline):
        header, image, flags, x0, y0 = read_parent(
            source_url, self._native_window, deadline=native_deadline,
        )
        if image.ndim != 2 or flags.shape != image.shape or flags.dtype.kind not in "iu":
            raise ValueError("The native FITS image and quality flags do not match.")
        native_height, native_width = image.shape
        native_wcs = WCS(header)
        x, y = (float(value) for value in native_wcs.world_to_pixel(self.coord))
        local_x, local_y = x - x0, y - y0
        self._check_center(local_x, local_y, image.shape)
        cropped_wcs = native_wcs.slice((slice(y0, y0 + native_height), slice(x0, x0 + native_width)))
        image = np.array(image, dtype=np.float64, copy=True)
        image[(flags & BAD_FLAG_MASK) != 0] = np.nan
        aligned, coverage = self._align_image(image, cropped_wcs, header["BUNIT"])
        center_x = min(max(round(local_x), 0), native_width - 1)
        center_y = min(max(round(local_y), 0), native_height - 1)
        center_flags = int(flags[center_y, center_x])
        flag_names = [FLAG_NAMES.get(bit, f"bit {bit}") for bit in range(32)
                      if center_flags & (1 << bit)]

        wavelength = bandwidth = None
        now = monotonic()
        if now < deadline:
            try:
                wavelength, bandwidth = read_wavelength(
                    source_url, x, y, header, deadline=min(deadline, now + WAVELENGTH_TIMEOUT_SECONDS),
                )
            except Exception:
                # Wavelength lookup is ancillary. Keep the valid science
                # pixels even if the spectral table cannot be read in time.
                pass

        return self._frame(
            aligned, coverage, image.shape, time_mjd, source_url, wavelength, bandwidth,
            cutout_url=None, processing_method="native_fits_range", quality_mask_applied=True,
            center_flag_names=flag_names, masked_flag_bits=list(BAD_FLAG_BITS),
            native_pixel_origin={"x": int(x0), "y": int(y0)},
        )

    def _align_archive(self, row, source_url, deadline, started):
        # Reserve time for the original width. Some archive cells respond
        # only to a smaller cutout, and this URL may already be cached.
        remaining = deadline - started
        padded_timeout = min(PADDED_CUTOUT_TIMEOUT_SECONDS, remaining / 2)
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
        image = cutout["image"]
        self._check_center(cutout["target_x"], cutout["target_y"], image.shape)
        aligned, coverage = self._align_image(image, cutout["wcs"], cutout["brightness_unit"])
        return self._frame(
            aligned, coverage, image.shape, float(row["t_min"]), source_url,
            cutout.get("wavelength_um"), cutout.get("bandwidth_um"),
            cutout_url=cutout["cutout_url"], native_cutout_width_arcmin=width,
            processing_method="irsa_cutout", quality_mask_applied=False,
        )

    @staticmethod
    def _check_center(x, y, shape):
        native_height, native_width = shape
        if not (-0.5 <= x < native_width - 0.5 and -0.5 <= y < native_height - 0.5):
            raise _UnusableCoverage("The target lies outside this observation.")

    def _align_image(self, image, wcs, brightness_unit):
        # Each worker gets its own WCS object. wcslib can initialize internal
        # WCS state when transforming coordinates.
        scale = u.Unit(brightness_unit).to(BRIGHTNESS_UNIT)
        pixels, footprint = reproject_interp(
            (image, wcs), self.wcs.deepcopy(),
            shape_out=self.shape, order="bilinear",
        )
        pixels = np.asarray(pixels, dtype=np.float64) * scale
        valid = (footprint > 0) & np.isfinite(pixels)
        coverage = float(np.count_nonzero(valid) / valid.size)
        if coverage < config.MIN_FRAME_COVERAGE_FRACTION:
            raise _UnusableCoverage(f"Only {coverage:.1%} of the requested sky patch has valid pixels.")
        if not np.all(valid[self.center]):
            raise _UnusableCoverage("The target center has no fully valid aligned pixels.")
        pixels[~valid] = np.nan
        return pixels, coverage

    def _frame(self, pixels, coverage, shape, time_mjd, source_url, wavelength, bandwidth, **provenance):
        native_height, native_width = shape
        wavelength = finite_number(wavelength) if wavelength is not None else None
        bandwidth = finite_number(bandwidth) if bandwidth is not None else None
        return {
            "image": pixels,
            "brightness_unit": str(BRIGHTNESS_UNIT),
            "date_utc": Time(time_mjd, format="mjd", scale="utc", precision=6).iso,
            "time_mjd": time_mjd,
            "band": self.band,
            "detector": self.detector,
            "center_wavelength_um": wavelength if wavelength and wavelength > 0 else None,
            "center_bandwidth_um": bandwidth if bandwidth and bandwidth > 0 else None,
            "coverage_fraction": coverage,
            "native_cutout_dimensions": {"width": native_width, "height": native_height},
            "source_url": source_url,
            **provenance,
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
        checked = sum(frame.get("quality_mask_applied") is True for frame in frames)
        if checked:
            cutout_notes.append(
                f"{checked} exposure(s) use native FITS image sections and mask archive flag bits "
                f"{', '.join(map(str, BAD_FLAG_BITS))} before WCS alignment."
            )
        if checked < len(frames):
            cutout_notes.append("Archive cutout fallback frames retain transparency but did not apply instrument quality flags.")
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
                "Background and PSF differences have not been corrected.",
                "Unobserved pixels are transparent. PNG brightness is for visualization.",
            ],
        }
        _atomic_manifest(self.folder / "manifest.json", manifest)
        return manifest
