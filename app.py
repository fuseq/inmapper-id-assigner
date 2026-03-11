"""
ID Assigner — Web API
FastAPI backend for SVG ID assignment tool.
"""
import uuid
import tempfile
import os
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

from id_assigner import process_svg, ProcessParams, ProcessResult

app = FastAPI(title="ID Assigner", version="1.0.0")

JOBS: dict[str, bytes] = {}
MAX_JOBS = 50

STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/process")
async def api_process(
    file: UploadFile = File(...),
    layers: str = Form("Stand,Food,Service"),
    assign_door_ids: bool = Form(False),
    door_layer: str = Form("Doors"),
    rotation: str = Form("auto"),
    font_small: int = Form(12),
    font_medium: int = Form(24),
    font_large: int = Form(36),
):
    if not file.filename or not file.filename.lower().endswith(".svg"):
        raise HTTPException(400, "Only .svg files are accepted.")

    input_bytes = await file.read()
    if len(input_bytes) > 50 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 50MB).")

    layer_list = [l.strip() for l in layers.split(",") if l.strip()]
    rot = None if rotation.strip().lower() == "auto" else float(rotation)

    params = ProcessParams(
        layers=layer_list,
        assign_door_ids=assign_door_ids,
        door_layer=door_layer,
        rotation=rot,
        font_small=font_small,
        font_medium=font_medium,
        font_large=font_large,
    )

    try:
        result = process_svg(input_bytes, params)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Processing failed: {e}")

    job_id = uuid.uuid4().hex[:12]

    if len(JOBS) >= MAX_JOBS:
        oldest = next(iter(JOBS))
        del JOBS[oldest]
    JOBS[job_id] = result.output_svg

    return JSONResponse({
        "job_id": job_id,
        "stats": {
            "total_units": result.total_units,
            "renamed_paths": result.renamed_paths,
            "renamed_doors": result.renamed_doors,
            "unmatched_doors": result.unmatched_doors,
            "rotation_deg": round(result.rotation_deg, 1),
            "threshold_small": result.threshold_small,
            "threshold_large": result.threshold_large,
            "font_counts": {str(k): v for k, v in sorted(result.font_counts.items())},
            "layers_found": result.layers_found,
        },
        "output_size": len(result.output_svg),
    })


@app.get("/api/download/{job_id}")
async def api_download(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found or expired.")
    return Response(
        content=JOBS[job_id],
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="output_{job_id}.svg"'},
    )


@app.get("/api/preview/{job_id}")
async def api_preview(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found or expired.")
    return Response(content=JOBS[job_id], media_type="image/svg+xml")
