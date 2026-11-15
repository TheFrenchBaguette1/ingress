from pathlib import Path
from datetime import date

# ---------- Archive settings ----------

COLLECTIONS = ["spherex_dr1", "spherex_qr3"]
# SPHEREx Explanatory Supplement v2.1, Table 2. These are detector coverage
# envelopes, not the wavelength of a particular source pixel. The archive's
# BAND constraint is an intersection test; verify the detector name locally.
DETECTOR_RANGES_UM = {
    "D1": (0.744, 1.116),
    "D2": (1.099, 1.651),
    "D3": (1.636, 2.421),
    "D4": (2.423, 3.822),
    "D5": (3.809, 4.420),
    "D6": (4.412, 5.002),
}
MISSION_START_DATE = date(2025, 5, 1)
CACHE_DOWNLOADS = True
TIMEOUT_SECONDS = 25
MAX_BUILD_SECONDS = 120
MAX_CUTOUT_ATTEMPTS = 12
MAX_USABLE_FRAMES = 8
MAX_CUTOUT_WORKERS = 4

# ---------- Output settings ----------

OUTPUT_ROOT = Path(__file__).resolve().parent / "output"

# Grid sampling; this does not improve telescope resolution.
OUTPUT_PIXEL_SCALE_ARCSEC = 6.0

DISPLAY_LOWER_PERCENTILE = 1
DISPLAY_UPPER_PERCENTILE = 99

# Operational limit for this prototype, not an astronomy limit.
MAX_FOV_ARCMIN = 60.0

# ---------- Local test request ----------

DEFAULT_REQUEST = {
    "ra_deg": 133.795129167,
    "dec_deg": -7.245147222,
    "start_date": "2025-05-01",
    "end_date": "2026-05-31",
    "fov_arcmin": 10.0,
    "band": "D3",
}

# ---------- Frontend development ----------

# Add your teammate's frontend origin if it differs.
FRONTEND_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]
