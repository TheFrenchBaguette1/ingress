from math import isfinite
from time import perf_counter

from astropy.table import Table
from astropy.time import Time

from archive import load_cutout


def build_candidate_table(
    results,
    coord,
    size_arcmin,
    detector,
    cache=True,
    timeout_seconds=120,
):
    """Inspect one detector's observations, ordered by time."""
    candidates = results[
        results["energy_bandpassname"] == detector
    ].copy()
    candidates.sort("t_min")

    records = []
    failures = []

    print(f"\nInspecting {len(candidates)} observations from {detector}...")

    for i, observation in enumerate(candidates, start=1):
        date = Time(
            float(observation["t_min"]),
            format="mjd",
            scale="utc",
        ).iso

        print(f"[{i}/{len(candidates)}] {date} UTC", flush=True)
        start = perf_counter()

        try:
            cutout = load_cutout(
                observation,
                coord,
                size_arcmin,
                cache=cache,
                timeout_seconds=timeout_seconds,
            )

            wavelength = cutout["wavelength_um"]
            bandwidth = cutout["bandwidth_um"]

            if not (
                isfinite(wavelength)
                and isfinite(bandwidth)
                and wavelength > 0
                and bandwidth > 0
            ):
                raise ValueError("Invalid wavelength or bandwidth.")

            records.append((
                cutout["time_mjd"],
                cutout["date_utc"],
                cutout["detector"],
                wavelength,
                bandwidth,
                cutout["cutout_url"],
            ))

            print(
                f"  {wavelength:.4f} µm | "
                f"bandwidth {bandwidth:.4f} µm | "
                f"{perf_counter() - start:.2f} seconds"
            )

        except Exception as error:
            # Record failures so they can be inspected or retried later.
            failures.append({
                "date_utc": date,
                "source_url": str(observation["access_url"]),
                "error": f"{type(error).__name__}: {error}",
            })
            print(f"  FAILED: {failures[-1]['error']}")

    table = Table(
        rows=records,
        names=(
            "time_mjd",
            "date_utc",
            "detector",
            "wavelength_um",
            "bandwidth_um",
            "cutout_url",
        ),
        dtype=("f8", "U32", "U20", "f8", "f8", "U2048"),
    )

    table["wavelength_um"].format = ".4f"
    table["bandwidth_um"].format = ".4f"

    return table, failures