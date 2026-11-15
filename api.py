import logging
import json
from collections import OrderedDict
from threading import Lock
from time import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import config
from archive import ArchiveUnavailable
from sequence import SequenceRequest, build_sequence
from sequence_disk_cache import (
    PARTIAL_TTL_SECONDS, cacheable_sequence, find_saved_sequence, intact_saved_frames,
    mark_cached_sequence,
)


app = FastAPI(title="Ingress Backend")

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

# Keep this first prototype to one sequence build at a time.
build_lock = Lock()
cache_lock = Lock()
sequence_cache = OrderedDict()
MAX_CACHED_SEQUENCES = 32


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


@app.get("/bands")
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


@app.post("/sequence")
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
