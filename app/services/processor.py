"""File processing pipeline.

`process_dataset` is pure CPU/IO work with no database access: it can be unit tested on its
own and runs in a worker thread so it never blocks the event loop. `run_processing` is
the async orchestrator that moves a file through PENDING -> PROCESSING -> COMPLETED/FAILED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry.base import BaseGeometry

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.crs import crs_label
from app.services.measurement import MeasurementStatus, measure_geometry
from app.services.readers import read_layers
from app.services.upload import FileType, prepare_source

logger = logging.getLogger(__name__)


class FileStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class ProcessingResult:
    features: list[dict[str, Any]]
    layers: list[dict[str, Any]]
    crs: str | None
    geometry_type_counts: dict[str, int]
    measurement_status_counts: dict[str, int]
    summary: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _geojson(geom: BaseGeometry | None) -> dict | None:
    if geom is None or geom.is_empty:
        return None
    return json.loads(shapely.to_geojson(geom))


def _index_location(lonlat: BaseGeometry | None) -> dict | None:
    """Geometry for MongoDB's 2dsphere index, only when MongoDB will accept it."""
    if lonlat is None or lonlat.is_empty or not lonlat.is_valid:
        return None
    minx, miny, maxx, maxy = lonlat.bounds
    if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
        return None
    return _geojson(shapely.force_2d(lonlat))


def process_dataset(
    filename: str, data: bytes, file_type: FileType, settings: Settings
) -> ProcessingResult:
    features: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    warnings: list[str] = []
    type_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    totals = {"area_m2": 0.0, "geodesic_area_m2": 0.0, "length_m": 0.0, "geodesic_length_m": 0.0}

    with tempfile.TemporaryDirectory(prefix="geo-upload-") as tmp:
        paths = prepare_source(filename, data, Path(tmp))
        for layer, raw_features in read_layers(paths, is_kml=file_type == FileType.KML):
            source_label = crs_label(layer.crs)
            layers.append(
                {
                    "name": layer.name,
                    "source": layer.source,
                    "crs": source_label,
                    "crs_assumed": layer.crs_assumed,
                    "feature_count": len(raw_features),
                }
            )
            warnings.extend(f"[{layer.name}] {w}" for w in layer.warnings)

            for raw in raw_features:
                geom = raw.geometry
                gtype = geom.geom_type if geom is not None else None
                type_counts[gtype or "None"] += 1

                measurement, lonlat = measure_geometry(
                    geom, layer.crs, max_span_deg=settings.utm_max_span_deg
                )
                status_counts[measurement.status.value] += 1
                if measurement.status is MeasurementStatus.MEASURED:
                    if measurement.kind == "area":
                        totals["area_m2"] += measurement.value or 0.0
                        totals["geodesic_area_m2"] += measurement.geodesic_value or 0.0
                    else:
                        totals["length_m"] += measurement.value or 0.0
                        totals["geodesic_length_m"] += measurement.geodesic_value or 0.0

                # GeoJSON (RFC 7946) is WGS84; keep source coordinates only when we
                # cannot reproject (unknown CRS) and say so explicitly.
                out_geom, geom_crs = (
                    (lonlat, "EPSG:4326")
                    if lonlat is not None
                    else (
                        geom,
                        source_label,
                    )
                )
                doc: dict[str, Any] = {
                    "feature_index": len(features),
                    "layer": raw.layer,
                    "layer_feature_index": raw.layer_index,
                    "geometry_type": gtype,
                    "geometry": _geojson(out_geom),
                    "geometry_crs": geom_crs,
                    "source_crs": source_label,
                    "properties": raw.properties,
                    "measurement": measurement.to_dict(),
                    "warnings": measurement.warnings,
                }
                location = _index_location(lonlat)
                if location is not None:
                    doc["location"] = location
                features.append(doc)

    distinct_crs = {layer["crs"] for layer in layers}
    if not layers:
        crs = None
    elif len(distinct_crs) == 1:
        crs = next(iter(distinct_crs)) or "UNKNOWN"
    else:
        crs = "MIXED"

    summary = {k: round(v, 4) for k, v in totals.items()}
    summary["area_km2"] = round(totals["area_m2"] / 1e6, 6)
    summary["length_km"] = round(totals["length_m"] / 1e3, 6)
    return ProcessingResult(
        features=features,
        layers=layers,
        crs=crs,
        geometry_type_counts=dict(type_counts),
        measurement_status_counts=dict(status_counts),
        summary=summary,
        warnings=warnings,
    )


async def run_processing(file_id: str, files_repo, features_repo, settings: Settings) -> None:
    """Background job: process an uploaded file and persist its features."""
    started = time.perf_counter()
    try:
        record = await files_repo.mark_processing(file_id)
        if record is None:
            logger.warning("File %s vanished before processing started", file_id)
            return
        data = await files_repo.read_blob(record["blob_id"])
        result = await asyncio.to_thread(
            process_dataset, record["filename"], data, FileType(record["file_type"]), settings
        )
        await features_repo.replace_for_file(file_id, result.features, settings.insert_batch_size)
        await files_repo.mark_completed(
            file_id,
            {
                "feature_count": len(result.features),
                "crs": result.crs,
                "layers": result.layers,
                "geometry_type_counts": result.geometry_type_counts,
                "measurement_status_counts": result.measurement_status_counts,
                "summary": result.summary,
                "warnings": result.warnings,
                "processed_at": datetime.now(timezone.utc),
                "processing_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        logger.info("File %s processed: %d features", file_id, len(result.features))
    except AppError as exc:
        logger.warning("File %s rejected during processing: %s", file_id, exc.message)
        await files_repo.mark_failed(file_id, exc.message)
    except Exception as exc:
        logger.exception("File %s failed to process", file_id)
        await files_repo.mark_failed(file_id, f"Could not read geospatial data: {exc}")
