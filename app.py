from __future__ import annotations

import json
import io
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from processing import process_survey


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "parcels.geojson"
STATIC_DIR = BASE_DIR / "static"
SURVEY_ROOT = BASE_DIR / "data" / "surveys"
LATEST_SURVEY = SURVEY_ROOT / "latest"
LATEST_INPUT = LATEST_SURVEY / "input"
data_lock = Lock()

PROJECT = {
    "name": "Dharani",
    "team": "PARSEK",
    "tagline": "From aerial evidence to verified land records.",
    "problem_statement": "SIH 2026 PS 26012",
}

app = FastAPI(
    title="Dharani Prototype",
    description=PROJECT["tagline"],
    version="1.2.0",
)
SURVEY_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/survey-files", StaticFiles(directory=SURVEY_ROOT), name="survey-files")


class ReviewUpdate(BaseModel):
    status: Literal["pending", "verified", "field_required", "rejected"]
    reviewer: str = Field(min_length=2, max_length=80)


class FieldNote(BaseModel):
    officer: str = Field(min_length=2, max_length=80)
    language: str = Field(default="English", max_length=30)
    note: str = Field(min_length=3, max_length=1000)
    latitude: float
    longitude: float


def read_collection() -> dict:
    with data_lock:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def write_collection(collection: dict) -> None:
    with data_lock:
        DATA_FILE.write_text(json.dumps(collection, indent=2), encoding="utf-8")


def find_feature(collection: dict, parcel_id: str) -> dict:
    for feature in collection["features"]:
        if feature["properties"]["parcel_id"] == parcel_id:
            return feature
    raise HTTPException(status_code=404, detail="Parcel not found")


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "Dharani Prototype", "team": "PARSEK"}


@app.get("/api/project")
def project() -> dict:
    """Return the identity shown by clients and demo integrations."""
    return PROJECT


def _save_upload(upload: UploadFile, destination: Path) -> None:
    if not upload.filename or not upload.filename.lower().endswith((".tif", ".tiff")):
        raise HTTPException(status_code=400, detail=f"{destination.stem} must be a .tif or .tiff GeoTIFF file.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as target:
        shutil.copyfileobj(upload.file, target)


@app.post("/api/surveys/upload")
def upload_survey(
    project_name: str = Form(..., min_length=2, max_length=100),
    rgb: UploadFile = File(...),
    dsm: UploadFile = File(...),
    dtm: UploadFile = File(...),
) -> dict:
    """Accept mandatory georeferenced drone inputs and run baseline processing."""
    if LATEST_SURVEY.exists():
        shutil.rmtree(LATEST_SURVEY)
    LATEST_INPUT.mkdir(parents=True, exist_ok=True)
    rgb_path, dsm_path, dtm_path = [LATEST_INPUT / name for name in ("rgb.tif", "dsm.tif", "dtm.tif")]
    _save_upload(rgb, rgb_path)
    _save_upload(dsm, dsm_path)
    _save_upload(dtm, dtm_path)
    try:
        metadata = process_survey(rgb_path, dsm_path, dtm_path, LATEST_SURVEY)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Processing failed: {error}") from error
    metadata["project_name"] = project_name
    (LATEST_SURVEY / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (LATEST_SURVEY / "audit.json").write_text(
        json.dumps([{
            "event": "survey_processed",
            "project_name": project_name,
            "feature_count": sum(metadata["counts"].values()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }], indent=2),
        encoding="utf-8",
    )
    return metadata


@app.get("/api/surveys/latest")
def latest_survey() -> dict:
    metadata_file = LATEST_SURVEY / "metadata.json"
    if not metadata_file.exists():
        return {"status": "not_started"}
    return json.loads(metadata_file.read_text(encoding="utf-8"))


@app.get("/api/surveys/latest/features")
def latest_features() -> dict:
    feature_file = LATEST_SURVEY / "features.geojson"
    if not feature_file.exists():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(feature_file.read_text(encoding="utf-8"))


def _survey_feature_file() -> Path:
    return LATEST_SURVEY / "features.geojson"


def _audit_file() -> Path:
    return LATEST_SURVEY / "audit.json"


def _read_survey_features() -> dict:
    path = _survey_feature_file()
    if not path.exists():
        raise HTTPException(status_code=404, detail="No processed survey is available.")
    with data_lock:
        return json.loads(path.read_text(encoding="utf-8"))


def _write_survey_features(collection: dict) -> None:
    with data_lock:
        _survey_feature_file().write_text(json.dumps(collection, indent=2), encoding="utf-8")


def _find_survey_feature(collection: dict, feature_id: str) -> dict:
    for feature in collection["features"]:
        if feature["properties"]["feature_id"] == feature_id:
            return feature
    raise HTTPException(status_code=404, detail="Survey feature not found")


def _append_audit(event: dict) -> None:
    with data_lock:
        path = _audit_file()
        events = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        events.append(event)
        path.write_text(json.dumps(events, indent=2), encoding="utf-8")


@app.get("/api/surveys/latest/review-queue")
def survey_review_queue() -> dict:
    collection = _read_survey_features()
    features = [
        feature for feature in collection["features"]
        if feature["properties"].get("review_status") not in {"verified", "rejected"}
    ]
    features.sort(
        key=lambda feature: (
            {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(
                feature["properties"].get("risk_severity"), 0
            ),
            -feature["properties"].get("confidence", 0),
        ),
        reverse=True,
    )
    return {"type": "FeatureCollection", "features": features}


@app.patch("/api/surveys/latest/features/{feature_id}/review")
def review_survey_feature(feature_id: str, update: ReviewUpdate) -> dict:
    collection = _read_survey_features()
    feature = _find_survey_feature(collection, feature_id)
    props = feature["properties"]
    previous = props.get("review_status", "unreviewed")
    props["review_status"] = update.status
    props["reviewer"] = update.reviewer
    props["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    _write_survey_features(collection)
    _append_audit({
        "event": "survey_feature_reviewed", "feature_id": feature_id,
        "from_status": previous, "to_status": update.status, "by": update.reviewer,
    })
    return feature


@app.post("/api/surveys/latest/features/{feature_id}/field-notes")
def add_survey_field_note(feature_id: str, note: FieldNote) -> dict:
    collection = _read_survey_features()
    feature = _find_survey_feature(collection, feature_id)
    payload = note.model_dump()
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    props = feature["properties"]
    props.setdefault("field_notes", []).append(payload)
    props["review_status"] = "field_required"
    _write_survey_features(collection)
    _append_audit({
        "event": "field_note_added", "feature_id": feature_id,
        "by": note.officer, "language": note.language,
    })
    return {"message": "Field note saved", "field_note": payload}


@app.get("/api/surveys/latest/approved")
def approved_survey_features() -> dict:
    collection = _read_survey_features()
    return {
        "type": "FeatureCollection",
        "features": [
            feature for feature in collection["features"]
            if feature["properties"].get("review_status") == "verified"
        ],
    }


@app.get("/api/surveys/latest/audit")
def survey_audit() -> list[dict]:
    path = _audit_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@app.get("/api/surveys/latest/export")
def export_survey_geojson() -> FileResponse:
    if not _survey_feature_file().exists():
        raise HTTPException(status_code=404, detail="No processed survey is available.")
    return FileResponse(
        _survey_feature_file(), media_type="application/geo+json",
        filename="dharani_verified_features.geojson",
    )


@app.get("/api/surveys/latest/features/{feature_id}/qr")
def survey_feature_qr(feature_id: str, request: Request) -> StreamingResponse:
    feature = _find_survey_feature(_read_survey_features(), feature_id)
    if feature["properties"].get("review_status") != "verified":
        raise HTTPException(status_code=409, detail="Only verified records receive a QR code.")
    record_url = str(request.base_url).rstrip("/") + f"/?record={feature_id}"
    qr = qrcode.QRCode(version=3, box_size=7, border=3)
    qr.add_data(record_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#123c2e", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


@app.get("/api/parcels")
def parcels() -> dict:
    return read_collection()


@app.get("/api/parcels/{parcel_id}")
def parcel(parcel_id: str) -> dict:
    return find_feature(read_collection(), parcel_id)


@app.patch("/api/parcels/{parcel_id}/review")
def review_parcel(parcel_id: str, update: ReviewUpdate) -> dict:
    collection = read_collection()
    feature = find_feature(collection, parcel_id)
    props = feature["properties"]
    props["status"] = update.status
    props["reviewer"] = update.reviewer
    props.setdefault("audit_log", []).append(
        {"event": "review_updated", "status": update.status, "by": update.reviewer}
    )
    write_collection(collection)
    return feature


@app.post("/api/parcels/{parcel_id}/field-notes")
def add_field_note(parcel_id: str, note: FieldNote) -> dict:
    collection = read_collection()
    feature = find_feature(collection, parcel_id)
    payload = note.model_dump()
    feature["properties"].setdefault("field_notes", []).append(payload)
    feature["properties"]["status"] = "field_required"
    write_collection(collection)
    return {"message": "Field note saved", "field_note": payload}


@app.get("/api/statistics")
def statistics() -> dict:
    collection = read_collection()
    features = collection["features"]
    land_use_totals: dict[str, float] = {}
    confidence_bands = {"high": 0, "medium": 0, "low": 0}
    risk_totals: dict[str, int] = {}

    for feature in features:
        props = feature["properties"]
        land_class = props["land_use_class"]
        land_use_totals[land_class] = land_use_totals.get(land_class, 0) + props["area_m2"]

        confidence = props["confidence"]
        band = "high" if confidence >= 0.80 else "medium" if confidence >= 0.60 else "low"
        confidence_bands[band] += 1

        severity = props["risk_severity"]
        risk_totals[severity] = risk_totals.get(severity, 0) + 1

    return {
        "parcel_count": len(features),
        "area_by_land_use_class_m2": land_use_totals,
        "confidence_bands": confidence_bands,
        "risk_by_severity": risk_totals,
    }
