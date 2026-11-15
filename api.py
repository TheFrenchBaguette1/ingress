import logging
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import config
from sequence import SequenceRequest, build_sequence


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
def create_sequence(request: SequenceRequest):
    if not build_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="A sequence is already being built. Try again when it finishes.",
        )

    try:
        return build_sequence(request)

    except Exception:
        logging.exception("Sequence generation failed.")
        raise HTTPException(
            status_code=502,
            detail="Sequence generation failed. Check the backend terminal.",
        )

    finally:
        build_lock.release()