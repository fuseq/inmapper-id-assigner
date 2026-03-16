"""
ID Assigner — Web API
FastAPI backend for SVG ID assignment tool.
"""
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

from id_assigner import process_svg, ProcessParams, ProcessResult

app = FastAPI(title="ID Assigner", version="1.0.0")

JOBS: dict[str, dict] = {}
MAX_JOBS = 50

STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _store_job(result: ProcessResult, input_svg: bytes, params_dict: dict) -> tuple[str, dict]:
    job_id = uuid.uuid4().hex[:12]
    if len(JOBS) >= MAX_JOBS:
        oldest = next(iter(JOBS))
        del JOBS[oldest]

    stats = {
        "total_units": result.total_units,
        "renamed_paths": result.renamed_paths,
        "renamed_doors": result.renamed_doors,
        "unmatched_doors": result.unmatched_doors,
        "rotation_deg": round(result.rotation_deg, 1),
        "threshold_small": result.threshold_small,
        "threshold_large": result.threshold_large,
        "font_counts": {str(k): v for k, v in sorted(result.font_counts.items())},
        "layers_found": result.layers_found,
    }

    JOBS[job_id] = {
        "output_svg": result.output_svg,
        "input_svg": input_svg,
        "params": params_dict,
    }

    return job_id, stats


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
    compute_area_size: bool = Form(False),
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
        compute_area_size=compute_area_size,
    )

    try:
        result = process_svg(input_bytes, params)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Processing failed: {e}")

    params_dict = {
        "layers": layer_list,
        "assign_door_ids": assign_door_ids,
        "door_layer": door_layer,
        "rotation": rot,
        "font_small": font_small,
        "font_medium": font_medium,
        "font_large": font_large,
    }

    job_id, stats = _store_job(result, input_bytes, params_dict)

    return JSONResponse({
        "job_id": job_id,
        "stats": stats,
        "output_size": len(result.output_svg),
        "area_size_data": result.area_size_data,
        "font_groups": result.font_groups,
    })


@app.post("/api/reprocess/{job_id}")
async def api_reprocess(job_id: str, body: dict = Body(...)):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found or expired.")

    job = JOBS[job_id]
    font_overrides = body.get("font_overrides", {})
    threshold_small = body.get("threshold_small")
    threshold_large = body.get("threshold_large")

    p = job["params"]
    params = ProcessParams(
        layers=p["layers"],
        assign_door_ids=p["assign_door_ids"],
        door_layer=p["door_layer"],
        rotation=p["rotation"],
        font_small=p["font_small"],
        font_medium=p["font_medium"],
        font_large=p["font_large"],
        threshold_small=int(threshold_small) if threshold_small is not None else None,
        threshold_large=int(threshold_large) if threshold_large is not None else None,
        compute_area_size=True,
        font_overrides=font_overrides if font_overrides else None,
    )

    try:
        result = process_svg(job["input_svg"], params)
    except Exception as e:
        raise HTTPException(500, f"Reprocessing failed: {e}")

    job["output_svg"] = result.output_svg

    stats = {
        "total_units": result.total_units,
        "renamed_paths": result.renamed_paths,
        "renamed_doors": result.renamed_doors,
        "unmatched_doors": result.unmatched_doors,
        "rotation_deg": round(result.rotation_deg, 1),
        "threshold_small": result.threshold_small,
        "threshold_large": result.threshold_large,
        "font_counts": {str(k): v for k, v in sorted(result.font_counts.items())},
        "layers_found": result.layers_found,
    }

    return JSONResponse({
        "job_id": job_id,
        "stats": stats,
        "output_size": len(result.output_svg),
        "area_size_data": result.area_size_data,
        "font_groups": result.font_groups,
    })


@app.get("/api/download/{job_id}")
async def api_download(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found or expired.")
    return Response(
        content=JOBS[job_id]["output_svg"],
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="output_{job_id}.svg"'},
    )


@app.get("/api/preview/{job_id}")
async def api_preview(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found or expired.")
    return Response(content=JOBS[job_id]["output_svg"], media_type="image/svg+xml")
