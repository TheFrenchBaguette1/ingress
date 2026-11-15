import logging
import json
import re
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from time import time

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
from archive import ArchiveUnavailable
from flipbook import (
    FlipbookBusy, FlipbookRequest, get_flipbook, get_flipbook_exposures, request_flipbook_band, start_flipbook,
)
from light_curve import LightCurveBusy, LightCurveRequest, get_light_curve, start_light_curve
from sequence import SequenceRequest, build_sequence
from sequence_disk_cache import (
    PARTIAL_TTL_SECONDS, cacheable_sequence, find_saved_sequence, intact_saved_frames,
    mark_cached_sequence,
)
from prepare import PrepareRequest, get_prepare, start_prepare
from zoom import ZoomRequest, ZoomUnavailable, build_zoom


app = FastAPI(title="Ingress")
router = APIRouter()
SITE_ROOT = Path(__file__).resolve().parent

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

config.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app.mount(
    "/outputs",
    StaticFiles(directory=str(config.OUTPUT_ROOT)),
    name="outputs",
)
app.mount("/src", StaticFiles(directory=SITE_ROOT / "src"), name="frontend")
app.mount("/public", StaticFiles(directory=SITE_ROOT / "public"), name="public")


@app.get("/", include_in_schema=False)
def viewer():
    return FileResponse(SITE_ROOT / "index.html")


@app.get("/healthz", include_in_schema=False)
def health():
    return {"status": "ok"}

# Keep this first prototype to one sequence build at a time.
build_lock = Lock()
cache_lock = Lock()
sequence_cache = OrderedDict()
MAX_CACHED_SEQUENCES = 32
zoom_lock = Lock()


def remember_sequence(key, result, created_at=None):
    # A partial sequence with real, generated frames is useful for a short
    # interval. Empty responses and failures without frames never enter cache.
    if (not cacheable_sequence(result)
            or result.get("status") == "ready" and result.get("archive_listing_partial")
            or result.get("request") != json.loads(key)
            or not intact_saved_frames(result, config.OUTPUT_ROOT)):
        return
    with cache_lock:
        sequence_cache[key] = (result, time() if created_at is None else created_at)
        sequence_cache.move_to_end(key)
        if len(sequence_cache) > MAX_CACHED_SEQUENCES:
            sequence_cache.popitem(last=False)


@router.get("/bands")
def get_bands():
    return {
        "bands": [
            {
                "value": f"D{i}",
                "label": f"SPHEREx D{i}",
            }
            for i in range(1, 7)
        ]
    }


@router.post("/sequence")
def create_sequence(request: SequenceRequest, refresh: bool = False):
    key = request.model_dump_json()
    if not refresh:
        with cache_lock:
            cached = sequence_cache.get(key)
            if cached is not None:
                result, created_at = cached
                age = time() - created_at
                if (not intact_saved_frames(result, config.OUTPUT_ROOT)
                        or result.get("status") == "partial" and age >= PARTIAL_TTL_SECONDS):
                    sequence_cache.pop(key)
                else:
                    sequence_cache.move_to_end(key)
                    return (mark_cached_sequence(result, age)
                            if result.get("status") == "partial" else result)

        saved = find_saved_sequence(request, config.OUTPUT_ROOT)
        if saved is not None:
            remember_sequence(key, saved, created_at=time() - saved["cache_age_seconds"])
            return saved

    if not build_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="A sequence is already being built. Try again when it finishes.",
        )

    try:
        result = build_sequence(request, refresh=True) if refresh else build_sequence(request)
        remember_sequence(key, result)
        return result

    except ArchiveUnavailable as error:
        logging.warning("Archive search unavailable: %s", error)
        raise HTTPException(status_code=502, detail=str(error)) from error

    except Exception:
        logging.exception("Sequence generation failed.")
        raise HTTPException(
            status_code=502,
            detail="Sequence generation failed. Check the backend terminal.",
        )

    finally:
        build_lock.release()


@router.post("/zoom")
def zoom_sequence(request: ZoomRequest, refresh: bool = False):
    # A concurrent zoom of the same sequence may write the same PNG paths.
    # Waiting here is safe; this never starts another archive index search.
    with zoom_lock:
        try:
            return build_zoom(request, refresh=refresh)
        except ZoomUnavailable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception:
            logging.exception("Sequence zoom failed.")
            raise HTTPException(status_code=502, detail="Could not zoom this sequence. Check the backend terminal.")


@router.post("/prepare")
def prepare_bands(request: PrepareRequest, refresh: bool = False):
    """Start one bounded 20′ real-image job; the requested detector runs first."""
    return start_prepare(request, refresh=refresh)


@router.get("/prepare/{job_id}")
def prepare_status(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Unknown preparation job.")
    result = get_prepare(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Preparation job has expired. Start it again.")
    return result


@router.post("/flipbook")
def create_flipbook(request: FlipbookRequest, refresh: bool = False):
    """Queue an observation timeline and return before archive or image work."""
    try:
        return start_flipbook(request, refresh=refresh)
    except FlipbookBusy as error:
        raise HTTPException(
            status_code=503, detail={"code": "flipbook_busy", "message": str(error)},
            headers={"Retry-After": "3"},
        ) from error


@router.get("/flipbook/{job_id}")
def flipbook_status(job_id: str):
    result = get_flipbook(job_id) if re.fullmatch(r"[a-f0-9]{32}", job_id) else None
    if result is None:
        raise HTTPException(status_code=404, detail="Flipbook search has expired. Start it again.")
    return result


@router.post("/flipbook/{job_id}/band/{band}")
def select_flipbook_band(job_id: str, band: str, refresh: bool = False):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not re.fullmatch(r"all|D[1-6]", band):
        raise HTTPException(status_code=404, detail="Unknown flipbook or detector.")
    try:
        result = request_flipbook_band(job_id, band, refresh=refresh)
    except FlipbookBusy as error:
        raise HTTPException(
            status_code=503, detail={"code": "flipbook_busy", "message": str(error)},
            headers={"Retry-After": "3"},
        ) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Flipbook search has expired. Start it again.")
    return result


@router.post("/flipbook/{job_id}/light-curve")
def create_light_curve(job_id: str, request: LightCurveRequest):
    selection = get_flipbook_exposures(job_id) if re.fullmatch(r"[a-f0-9]{32}", job_id) else None
    if selection is None:
        raise HTTPException(status_code=404, detail="Flipbook search has expired. Start it again.")
    try:
        return start_light_curve(*selection, request)
    except LightCurveBusy as error:
        raise HTTPException(
            status_code=503, detail={"code": "light_curve_busy", "message": str(error)},
            headers={"Retry-After": "3"},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/light-curve/{curve_id}")
def light_curve_status(curve_id: str):
    result = get_light_curve(curve_id) if re.fullmatch(r"[a-f0-9]{32}", curve_id) else None
    if result is None:
        raise HTTPException(status_code=404, detail="This light curve has expired. Measure again to reload it.")
    return result


app.include_router(router, prefix="/api")
# The existing local Node proxy forwards API calls without the /api prefix.
app.include_router(router, include_in_schema=False)
