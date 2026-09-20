from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds, transform_geom


MAX_PROCESSING_SIZE = 1024


def _same_grid(first: rasterio.DatasetReader, other: rasterio.DatasetReader) -> bool:
    return (
        first.width == other.width
        and first.height == other.height
        and first.crs == other.crs
        and first.transform.almost_equals(other.transform)
    )


def _coverage_fraction(reference: rasterio.DatasetReader, other: rasterio.DatasetReader) -> float:
    """Return how much of the RGB reference bounds is covered by another raster."""
    west, south, east, north = transform_bounds(
        other.crs, reference.crs, *other.bounds, densify_pts=21
    )
    r_west, r_south, r_east, r_north = reference.bounds
    overlap_width = max(0.0, min(east, r_east) - max(west, r_west))
    overlap_height = max(0.0, min(north, r_north) - max(south, r_south))
    reference_area = max((r_east - r_west) * (r_north - r_south), 1e-9)
    return float(np.clip((overlap_width * overlap_height) / reference_area, 0, 1))


def _align_to_rgb(
    stack: ExitStack,
    source: rasterio.DatasetReader,
    rgb: rasterio.DatasetReader,
) -> rasterio.DatasetReader:
    if _same_grid(rgb, source):
        return source
    return stack.enter_context(
        WarpedVRT(
            source,
            crs=rgb.crs,
            transform=rgb.transform,
            width=rgb.width,
            height=rgb.height,
            resampling=Resampling.bilinear,
            dtype="float32",
            nodata=np.nan,
        )
    )


def _preview_shape(width: int, height: int) -> tuple[int, int]:
    scale = max(width, height) / MAX_PROCESSING_SIZE
    if scale <= 1:
        return height, width
    return max(1, round(height / scale)), max(1, round(width / scale))


def _display_band(band: np.ndarray) -> np.ndarray:
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros_like(band, dtype=np.uint8)
    low, high = np.percentile(valid, [2, 98])
    if high <= low:
        high = low + 1
    scaled = np.clip((band - low) / (high - low), 0, 1)
    return (scaled * 255).astype(np.uint8)


def _colour_ramp(values: np.ndarray, minimum: float, maximum: float) -> np.ndarray:
    normalized = np.clip((values - minimum) / max(maximum - minimum, 0.001), 0, 1)
    coloured = cv2.applyColorMap((normalized * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    return cv2.cvtColor(coloured, cv2.COLOR_BGR2RGB)


def _clean_mask(mask: np.ndarray, close_size: int = 5, open_size: int = 3) -> np.ndarray:
    output = mask.astype(np.uint8) * 255
    if close_size:
        kernel = np.ones((close_size, close_size), np.uint8)
        output = cv2.morphologyEx(output, cv2.MORPH_CLOSE, kernel)
    if open_size:
        kernel = np.ones((open_size, open_size), np.uint8)
        output = cv2.morphologyEx(output, cv2.MORPH_OPEN, kernel)
    return output > 0


def _mask_features(
    mask: np.ndarray,
    feature_type: str,
    raster_transform: Any,
    source_crs: Any,
    minimum_pixels: int,
    confidence_surface: np.ndarray,
    risk_context: dict[str, np.ndarray],
) -> list[dict]:
    result: list[dict] = []
    counter = 1
    for geometry, value in shapes(mask.astype(np.uint8), mask=mask, transform=raster_transform):
        if value != 1:
            continue
        # Pixel area provides a stable small-object filter at the processing resolution.
        coordinates = geometry.get("coordinates", [])
        if not coordinates:
            continue
        exterior = coordinates[0]
        if len(exterior) < 4:
            continue
        # Estimate pixel count from mask bounding box. Exact area remains in source units below.
        xs = [point[0] for point in exterior]
        ys = [point[1] for point in exterior]
        approx_pixels = abs((max(xs) - min(xs)) / raster_transform.a) * abs(
            (max(ys) - min(ys)) / raster_transform.e
        )
        if approx_pixels < minimum_pixels:
            continue
        component = ~geometry_mask(
            [geometry], out_shape=mask.shape, transform=raster_transform, invert=False
        )
        component &= mask
        if not component.any():
            continue
        confidence = float(np.clip(np.mean(confidence_surface[component]), 0.05, 0.99))
        risk = _risk_for_feature(feature_type, component, confidence, risk_context)
        pixel_area = abs(raster_transform.a * raster_transform.e)
        geometry_wgs84 = transform_geom(source_crs, "EPSG:4326", geometry, precision=7)
        result.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_id": f"{feature_type[:3].upper()}-{counter:03d}",
                    "feature_type": feature_type,
                    "confidence": round(confidence, 3),
                    "confidence_band": _confidence_band(confidence),
                    "evidence": _evidence_for(feature_type),
                    "area_m2": round(float(component.sum() * pixel_area), 2),
                    "risk_type": risk["risk_type"],
                    "risk_severity": risk["risk_severity"],
                    "risk_score": risk["risk_score"],
                    "risk_explanation": risk["risk_explanation"],
                    "recommended_action": risk["recommended_action"],
                    "review_status": "field_required" if risk["recommended_action"] == "field_verification" else "unreviewed",
                    "source": "baseline_geospatial_extraction",
                    "risk_disclaimer": "Screening indicator from drone evidence; not a legal determination.",
                },
                "geometry": geometry_wgs84,
            }
        )
        counter += 1
    return result


def _confidence_band(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


def _touches(component: np.ndarray, other: np.ndarray, pixels: int = 7) -> bool:
    kernel = np.ones((pixels, pixels), np.uint8)
    neighbourhood = cv2.dilate(component.astype(np.uint8), kernel) > 0
    return bool(np.any(neighbourhood & other))


def _risk_for_feature(
    feature_type: str,
    component: np.ndarray,
    confidence: float,
    context: dict[str, np.ndarray],
) -> dict[str, Any]:
    """Return a screening risk, never a legal conclusion.

    Legal encroachment needs an authoritative parcel/road reference layer. These
    rules only identify spatial patterns that deserve administrative attention.
    """
    touches_edge = bool(
        component[0].any() or component[-1].any() or component[:, 0].any() or component[:, -1].any()
    )
    if feature_type == "building_candidate" and _touches(component, context["corridors"], 11):
        severity, score, kind = "high", 0.82, "possible_access_corridor_conflict"
        explanation = "An elevated structure candidate is immediately beside a detected access corridor. Verify the approved road and parcel boundary before treating this as encroachment."
    elif feature_type == "vegetation" and _touches(component, context["corridors"], 13):
        severity, score, kind = "medium", 0.63, "possible_corridor_obstruction"
        explanation = "Dense vegetation touches a detected corridor and may hide its edge or obstruct access. A field check is recommended."
    elif feature_type == "surface_corridor_candidate" and confidence < 0.65:
        severity, score, kind = "medium", 0.58, "corridor_continuity_uncertain"
        explanation = "The corridor signal is weak or visually interrupted. Confirm whether the path continues on the ground."
    elif touches_edge:
        severity, score, kind = "medium", 0.52, "incomplete_survey_context"
        explanation = "This feature reaches the survey boundary, so part of its geometry may lie outside the uploaded data."
    elif confidence < 0.60:
        severity, score, kind = "medium", 0.50, "low_geometry_reliability"
        explanation = "The feature has low geometric confidence and should be checked before approval."
    else:
        severity, score, kind = "low", 0.20, "no_automatic_conflict_detected"
        explanation = "No immediate spatial conflict was found by the screening rules. Administrative review is still required."

    if severity == "high" or confidence < 0.60:
        action = "field_verification"
    elif severity == "medium" or confidence < 0.80:
        action = "admin_review"
    else:
        action = "ready_for_review"
    return {
        "risk_type": kind,
        "risk_severity": severity,
        "risk_score": round(score, 2),
        "risk_explanation": explanation,
        "recommended_action": action,
    }


def _evidence_for(feature_type: str) -> list[str]:
    return {
        "building_candidate": ["height_above_ground", "low_vegetation_signal"],
        "vegetation": ["vdvi_signal"],
        "surface_corridor_candidate": ["near_ground_height", "low_vegetation_signal"],
    }[feature_type]


def process_survey(rgb_path: Path, dsm_path: Path, dtm_path: Path, output_dir: Path) -> dict:
    """Process aligned RGB, DSM and DTM GeoTIFFs into visible prototype layers.

    This is a real raster pipeline and intentionally labels its outputs as baseline
    candidates. It does not claim that untrained DeepLabV3+/D-LinkNet weights ran.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        rgb_src = stack.enter_context(rasterio.open(rgb_path))
        dsm_src = stack.enter_context(rasterio.open(dsm_path))
        dtm_src = stack.enter_context(rasterio.open(dtm_path))
        if rgb_src.crs is None or dsm_src.crs is None or dtm_src.crs is None:
            raise ValueError("All GeoTIFF files must contain a coordinate reference system (CRS).")
        if rgb_src.count < 3:
            raise ValueError("The RGB GeoTIFF must contain at least three bands: red, green and blue.")
        dsm_coverage = _coverage_fraction(rgb_src, dsm_src)
        dtm_coverage = _coverage_fraction(rgb_src, dtm_src)
        if dsm_coverage < 0.05 or dtm_coverage < 0.05:
            raise ValueError(
                "The files use valid map coordinates but do not cover the same geographic area. "
                "Choose an RGB, DSM and DTM from the same survey site."
            )

        grids_originally_matched = _same_grid(rgb_src, dsm_src) and _same_grid(rgb_src, dtm_src)
        dsm_aligned = _align_to_rgb(stack, dsm_src, rgb_src)
        dtm_aligned = _align_to_rgb(stack, dtm_src, rgb_src)

        out_height, out_width = _preview_shape(rgb_src.width, rgb_src.height)
        processing_transform = rgb_src.transform * rgb_src.transform.scale(
            rgb_src.width / out_width, rgb_src.height / out_height
        )
        rgb_raw = rgb_src.read(
            [1, 2, 3], out_shape=(3, out_height, out_width), resampling=Resampling.bilinear,
            masked=True,
        ).filled(0).astype(np.float32)
        dsm = dsm_aligned.read(
            1, out_shape=(out_height, out_width), resampling=Resampling.bilinear, masked=True
        ).filled(np.nan).astype(np.float32)
        dtm = dtm_aligned.read(
            1, out_shape=(out_height, out_width), resampling=Resampling.bilinear, masked=True
        ).filled(np.nan).astype(np.float32)

        rgb_display = np.stack([_display_band(rgb_raw[index]) for index in range(3)], axis=-1)
        rgb_normalized = rgb_display.astype(np.float32) / 255.0
        red, green, blue = [rgb_normalized[:, :, index] for index in range(3)]
        vdvi = (2 * green - red - blue) / (2 * green + red + blue + 1e-6)
        valid_elevation = np.isfinite(dsm) & np.isfinite(dtm)
        ndsm = np.where(valid_elevation, np.clip(dsm - dtm, 0, None), 0).astype(np.float32)
        brightness = rgb_normalized.mean(axis=2)
        grey = cv2.cvtColor(rgb_display, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        gx = cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3)
        edge_strength = np.clip(np.sqrt(gx * gx + gy * gy) * 2.0, 0, 1)

        vegetation = _clean_mask((vdvi > 0.08) & valid_elevation, close_size=5, open_size=3)
        buildings = _clean_mask((ndsm > 2.5) & ~vegetation & valid_elevation, close_size=7, open_size=3)
        corridors = _clean_mask(
            valid_elevation & (ndsm < 0.6) & (vdvi < 0.04) & (brightness > 0.20) & (brightness < 0.88),
            close_size=9,
            open_size=5,
        )

        shadow_penalty = np.where(brightness < 0.16, 0.18, 0.0)
        building_confidence = np.clip(
            0.45 + 0.32 * np.clip(ndsm / 8.0, 0, 1) + 0.23 * edge_strength - shadow_penalty,
            0.25,
            0.96,
        )
        vegetation_confidence = np.clip(
            0.48 + 0.48 * np.clip((vdvi - 0.08) / 0.35, 0, 1) - shadow_penalty,
            0.25,
            0.96,
        )
        corridor_confidence = np.clip(
            0.42
            + 0.24 * np.clip(1.0 - ndsm / 0.6, 0, 1)
            + 0.18 * np.clip(1.0 - np.abs(brightness - 0.52) / 0.35, 0, 1)
            + 0.16 * edge_strength,
            0.25,
            0.92,
        )

        classification = np.zeros((out_height, out_width, 3), dtype=np.uint8)
        classification[:] = [235, 232, 220]
        classification[corridors] = [70, 130, 180]
        classification[vegetation] = [57, 150, 84]
        classification[buildings] = [235, 112, 64]

        Image.fromarray(rgb_display).save(output_dir / "rgb.png")
        Image.fromarray(_colour_ramp(ndsm, 0, max(12.0, float(np.percentile(ndsm, 98))))).save(output_dir / "ndsm.png")
        Image.fromarray(_colour_ramp(vdvi, -0.5, 0.5)).save(output_dir / "vdvi.png")
        Image.fromarray(classification).save(output_dir / "classification.png")

        features = []
        context = {"buildings": buildings, "vegetation": vegetation, "corridors": corridors}
        features.extend(_mask_features(buildings, "building_candidate", processing_transform, rgb_src.crs, 20, building_confidence, context))
        features.extend(_mask_features(vegetation, "vegetation", processing_transform, rgb_src.crs, 30, vegetation_confidence, context))
        features.extend(_mask_features(corridors, "surface_corridor_candidate", processing_transform, rgb_src.crs, 80, corridor_confidence, context))
        collection = {"type": "FeatureCollection", "features": features}
        (output_dir / "features.geojson").write_text(json.dumps(collection, indent=2), encoding="utf-8")

        bounds_wgs84 = transform_bounds(rgb_src.crs, "EPSG:4326", *rgb_src.bounds, densify_pts=21)
        counts = {
            "building_candidates": sum(f["properties"]["feature_type"] == "building_candidate" for f in features),
            "vegetation_features": sum(f["properties"]["feature_type"] == "vegetation" for f in features),
            "corridor_candidates": sum(
                f["properties"]["feature_type"] == "surface_corridor_candidate" for f in features
            ),
        }
        risk_counts = {
            level: sum(f["properties"]["risk_severity"] == level for f in features)
            for level in ("critical", "high", "medium", "low")
        }
        confidence_counts = {
            band: sum(f["properties"]["confidence_band"] == band for f in features)
            for band in ("high", "medium", "low")
        }
        metadata = {
            "status": "completed",
            "input": {
                "width": rgb_src.width,
                "height": rgb_src.height,
                "crs": str(rgb_src.crs),
                "bands": rgb_src.count,
            },
            "processing_size": {"width": out_width, "height": out_height},
            "alignment": {
                "reference_grid": "RGB orthomosaic",
                "auto_aligned": not grids_originally_matched,
                "method": "on-the-fly CRS reprojection and bilinear resampling to the RGB grid",
                "dsm_rgb_coverage_percent": round(dsm_coverage * 100, 1),
                "dtm_rgb_coverage_percent": round(dtm_coverage * 100, 1),
            },
            "bounds_wgs84": list(bounds_wgs84),
            "layers": {
                "rgb": "/survey-files/latest/rgb.png",
                "ndsm": "/survey-files/latest/ndsm.png",
                "vdvi": "/survey-files/latest/vdvi.png",
                "classification": "/survey-files/latest/classification.png",
                "features": "/api/surveys/latest/features",
            },
            "counts": counts,
            "risk_counts": risk_counts,
            "confidence_counts": confidence_counts,
            "disclosure": (
                "Real nDSM/VDVI preprocessing with rule-based baseline candidates. "
                "DeepLabV3+ and D-LinkNet require trained aerial checkpoints before their names can be claimed in inference."
            ),
        }
        (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return metadata
