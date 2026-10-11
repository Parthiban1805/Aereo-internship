from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FileStatusT = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]


class LayerOut(BaseModel):
    name: str
    source: str
    crs: str | None
    crs_assumed: bool
    feature_count: int


class FileUploadResponse(BaseModel):
    id: str
    filename: str
    status: FileStatusT
    links: dict[str, str] = Field(
        description="Where to poll for status and fetch results once COMPLETED."
    )


class FileOut(BaseModel):
    id: str
    filename: str
    file_type: Literal["SHAPEFILE", "KML"]
    size_bytes: int
    status: FileStatusT
    error: str | None = None
    feature_count: int | None = None
    crs: str | None = Field(
        None, description="Source CRS of the data, 'MIXED' if layers differ, or 'UNKNOWN'."
    )
    layers: list[LayerOut] = []
    geometry_type_counts: dict[str, int] = {}
    measurement_status_counts: dict[str, int] = {}
    summary: dict[str, float] | None = None
    warnings: list[str] = []
    created_at: datetime
    processed_at: datetime | None = None
    processing_ms: float | None = None

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> FileOut:
        return cls(id=doc["_id"], **{k: v for k, v in doc.items() if k != "_id"})


class FileList(BaseModel):
    items: list[FileOut]
    total: int
    limit: int
    offset: int


class MeasurementOut(BaseModel):
    status: Literal["MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "EMPTY", "UNKNOWN_CRS", "ERROR"]
    kind: Literal["area", "length"] | None = None
    unit: str | None = None
    value: float | None = Field(None, description="Projected-CRS measurement (m² or m).")
    geodesic_value: float | None = Field(
        None, description="Same measurement on the WGS84 ellipsoid, as an accuracy cross-check."
    )
    difference_pct: float | None = None
    measurement_crs: str | None = Field(None, description="CRS the measurement was computed in.")
    method: str | None = None
    area_m2: float | None = None
    area_km2: float | None = None
    area_ha: float | None = None
    length_m: float | None = None
    length_km: float | None = None


class FeatureMeasurement(BaseModel):
    feature_index: int
    layer: str
    layer_feature_index: int
    geometry_type: str | None
    source_crs: str | None
    properties: dict[str, Any]
    measurement: MeasurementOut
    warnings: list[str] = []
    geometry: dict[str, Any] | None = None
    geometry_crs: str | None = None


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    crs: str | None
    feature_count: int
    summary: dict[str, float] | None
    total: int = Field(description="Number of features matching the filters.")
    limit: int
    offset: int
    items: list[FeatureMeasurement]


class ErrorResponse(BaseModel):
    error: str
    message: str
