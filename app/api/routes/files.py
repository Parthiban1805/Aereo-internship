from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response

from app.api.deps import get_features_repo, get_files_repo, settings_dep
from app.core.config import Settings
from app.core.exceptions import (
    FileNotFound,
    FileNotReady,
    FileTooLarge,
    InvalidQuery,
    InvalidUpload,
)
from app.repositories.features import FeaturesRepository
from app.repositories.files import FilesRepository
from app.schemas.files import (
    ErrorResponse,
    FeatureMeasurement,
    FileList,
    FileOut,
    FileUploadResponse,
    MeasurementsResponse,
)
from app.services.processor import FileStatus, run_processing
from app.services.upload import validate_upload

router = APIRouter(prefix="/api/files", tags=["files"])

FilesRepo = Annotated[FilesRepository, Depends(get_files_repo)]
FeaturesRepo = Annotated[FeaturesRepository, Depends(get_features_repo)]
AppSettings = Annotated[Settings, Depends(settings_dep)]

ERRORS = {
    404: {"model": ErrorResponse, "description": "File not found"},
    409: {"model": ErrorResponse, "description": "File not yet processed (or failed)"},
}

MeasurementStatusQ = Literal[
    "MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "EMPTY", "UNKNOWN_CRS", "ERROR"
]


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Read the upload in chunks, aborting as soon as it exceeds the limit."""
    chunks, size = [], 0
    while chunk := await upload.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise FileTooLarge(f"File exceeds the {limit // (1024 * 1024)} MB upload limit.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _get_or_404(repo: FilesRepository, file_id: str) -> dict[str, Any]:
    doc = await repo.get(file_id)
    if doc is None:
        raise FileNotFound(f"No file with id '{file_id}'.")
    return doc


def _require_completed(doc: dict[str, Any]) -> None:
    if doc["status"] == FileStatus.FAILED:
        raise FileNotReady(f"File processing failed: {doc.get('error')}")
    if doc["status"] != FileStatus.COMPLETED:
        raise FileNotReady(f"File is {doc['status']}; measurements are not available yet.")


@router.post(
    "/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=FileUploadResponse,
    responses={
        413: {"model": ErrorResponse, "description": "File too large"},
        415: {"model": ErrorResponse, "description": "Unsupported file type"},
        422: {"model": ErrorResponse, "description": "Invalid or incomplete upload"},
    },
    summary="Upload a Shapefile (.zip) or KML (.kml/.kmz) for processing",
)
async def upload_file(
    request: Request,
    background: BackgroundTasks,
    files_repo: FilesRepo,
    features_repo: FeaturesRepo,
    settings: AppSettings,
    file: UploadFile = File(..., description="A .zip with a Shapefile, or a .kml/.kmz file"),
) -> FileUploadResponse:
    if not file.filename:
        raise InvalidUpload("Uploaded file must have a filename.")
    data = await _read_limited(file, settings.max_upload_bytes)
    file_type = validate_upload(file.filename, data, settings=settings)

    doc = await files_repo.create(file.filename, file_type.value, data)
    background.add_task(run_processing, doc["_id"], files_repo, features_repo, settings)

    base = str(request.url_for("get_file", file_id=doc["_id"]))
    return FileUploadResponse(
        id=doc["_id"],
        filename=doc["filename"],
        status=doc["status"],
        links={
            "self": base,
            "measurements": f"{base}measurements/",
            "features": f"{base}features/",
        },
    )


@router.get("/", response_model=FileList, summary="List uploaded files")
async def list_files(
    files_repo: FilesRepo,
    status_: Annotated[
        Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"] | None, Query(alias="status")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileList:
    docs, total = await files_repo.list(status=status_, limit=limit, offset=offset)
    return FileList(
        items=[FileOut.from_doc(d) for d in docs], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{file_id}/",
    response_model=FileOut,
    responses={404: ERRORS[404]},
    summary="File information and processing status",
)
async def get_file(file_id: str, files_repo: FilesRepo) -> FileOut:
    return FileOut.from_doc(await _get_or_404(files_repo, file_id))


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    response_model_exclude_none=True,
    responses=ERRORS,
    summary="Per-feature measurements (area for polygons, length for lines)",
)
async def get_measurements(
    file_id: str,
    files_repo: FilesRepo,
    features_repo: FeaturesRepo,
    geometry_type: Annotated[
        str | None, Query(description="Filter, e.g. Polygon, LineString, MultiPolygon")
    ] = None,
    measurement_status: Annotated[MeasurementStatusQ | None, Query(alias="status")] = None,
    include_geometry: Annotated[
        bool, Query(description="Include each feature's GeoJSON geometry")
    ] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MeasurementsResponse:
    doc = await _get_or_404(files_repo, file_id)
    _require_completed(doc)

    projection = {"_id": 0, "location": 0, "file_id": 0}
    if not include_geometry:
        projection["geometry"] = 0
    features, total = await features_repo.list_for_file(
        file_id,
        geometry_type=geometry_type,
        status=measurement_status,
        limit=limit,
        offset=offset,
        projection=projection,
    )
    return MeasurementsResponse(
        file_id=file_id,
        filename=doc["filename"],
        crs=doc.get("crs"),
        feature_count=doc.get("feature_count") or 0,
        summary=doc.get("summary"),
        total=total,
        limit=limit,
        offset=offset,
        items=[FeatureMeasurement(**f) for f in features],
    )


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    except ValueError:
        raise InvalidQuery("bbox must be 'minLon,minLat,maxLon,maxLat'.") from None
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise InvalidQuery("bbox must be a valid lon/lat box with min < max.")
    return minx, miny, maxx, maxy


@router.get(
    "/{file_id}/features/",
    responses=ERRORS,
    summary="Features as a GeoJSON FeatureCollection (EPSG:4326) with measurements",
    response_class=JSONResponse,
)
async def get_features_geojson(
    file_id: str,
    files_repo: FilesRepo,
    features_repo: FeaturesRepo,
    bbox: Annotated[
        str | None,
        Query(
            description="Only features intersecting minLon,minLat,maxLon,maxLat "
            "(served by MongoDB's 2dsphere index)",
            examples=["77.0,28.4,77.4,28.8"],
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    doc = await _get_or_404(files_repo, file_id)
    _require_completed(doc)
    box = _parse_bbox(bbox)

    if box is None:
        features, _ = await features_repo.list_for_file(
            file_id, limit=limit, offset=offset, projection={"_id": 0, "location": 0}
        )
    else:
        features = await features_repo.intersecting(file_id, box, limit=limit, offset=offset)

    collection = {
        "type": "FeatureCollection",
        "name": doc["filename"],
        "features": [
            {
                "type": "Feature",
                "id": f["feature_index"],
                "geometry": f["geometry"] if f.get("geometry_crs") == "EPSG:4326" else None,
                "properties": {
                    **f["properties"],
                    "_layer": f["layer"],
                    "_geometry_type": f["geometry_type"],
                    "_measurement_status": f["measurement"]["status"],
                    "_area_m2": f["measurement"].get("area_m2"),
                    "_length_m": f["measurement"].get("length_m"),
                    "_measurement_crs": f["measurement"].get("measurement_crs"),
                },
            }
            for f in features
        ],
    }
    return JSONResponse(collection, media_type="application/geo+json")


@router.delete(
    "/{file_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: ERRORS[404]},
    summary="Delete a file, its features and the stored upload",
)
async def delete_file(file_id: str, files_repo: FilesRepo, features_repo: FeaturesRepo) -> Response:
    if not await files_repo.delete(file_id):
        raise FileNotFound(f"No file with id '{file_id}'.")
    await features_repo.delete_for_file(file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
