"""Per-feature measurement.

Every feature produces a `Measurement`; nothing here raises for bad geometry. Unsupported
or broken input becomes a status plus warnings so one bad feature never fails a file.

For measurable geometries two independent numbers are produced:
- `value`: planar measurement in a projected CRS chosen by `crs.choose_projection`
  (this is the primary figure, as the brief requires projected measurement), and
- `geodesic_value`: measurement on the WGS84 ellipsoid (pyproj.Geod / Karney's algorithm),
  used as a cross-check. `difference_pct` exposes any projection distortion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import shapely
from pyproj import CRS, Geod
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from app.services.crs import WGS84, choose_projection, get_transformer

GEOD = Geod(ellps="WGS84")
DENSIFY_DEG = 0.01  # ~1.1 km; only segments longer than this gain vertices

AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString", "LinearRing"}
POINT_TYPES = {"Point", "MultiPoint"}


class MeasurementStatus(str, Enum):
    MEASURED = "MEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # Points: no measurement required
    UNSUPPORTED = "UNSUPPORTED"  # e.g. GeometryCollection
    EMPTY = "EMPTY"  # missing / empty geometry
    UNKNOWN_CRS = "UNKNOWN_CRS"  # cannot measure safely without knowing the CRS
    ERROR = "ERROR"  # unexpected failure while measuring this feature


@dataclass
class Measurement:
    status: MeasurementStatus
    kind: str | None = None  # "area" | "length"
    value: float | None = None  # m² for area, m for length
    unit: str | None = None
    geodesic_value: float | None = None
    difference_pct: float | None = None
    measurement_crs: str | None = None
    method: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {
            "status": self.status.value,
            "kind": self.kind,
            "unit": self.unit,
            "value": self.value,
            "geodesic_value": self.geodesic_value,
            "difference_pct": self.difference_pct,
            "measurement_crs": self.measurement_crs,
            "method": self.method,
        }
        if self.kind == "area" and self.value is not None:
            d["area_m2"] = self.value
            d["area_km2"] = round(self.value / 1e6, 6)
            d["area_ha"] = round(self.value / 1e4, 4)
        elif self.kind == "length" and self.value is not None:
            d["length_m"] = self.value
            d["length_km"] = round(self.value / 1e3, 6)
        return d


def reproject(geom: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    """Vectorised reprojection of all coordinates of a shapely geometry."""
    if src == dst:
        return geom
    transformer = get_transformer(src, dst)

    def _apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, _apply)


def _polygonal_part(geom: BaseGeometry) -> BaseGeometry:
    """Keep only polygonal pieces (make_valid may return a GeometryCollection)."""
    if geom.geom_type in AREA_TYPES:
        return geom
    polys: list[Polygon] = []
    for part in getattr(geom, "geoms", []):
        if isinstance(part, Polygon):
            polys.append(part)
        elif isinstance(part, MultiPolygon):
            polys.extend(part.geoms)
    return MultiPolygon(polys) if polys else GeometryCollection()


def _geodesic_area(lonlat: BaseGeometry) -> float:
    # Orient shells CCW / holes CW so pyproj subtracts holes correctly.
    if isinstance(lonlat, Polygon):
        lonlat = orient(lonlat, sign=1.0)
    elif isinstance(lonlat, MultiPolygon):
        lonlat = MultiPolygon([orient(p, sign=1.0) for p in lonlat.geoms])
    area, _ = GEOD.geometry_area_perimeter(lonlat)
    return abs(area)


def _unwrap_antimeridian(lonlat: BaseGeometry, warnings: list[str]) -> BaseGeometry:
    """Shift western longitudes by +360° for features that cross the antimeridian.

    A feature spanning 179.9°E..179.9°W otherwise looks ~360° wide and is measured as if it
    wrapped the globe. Longitudes beyond 180 are fine for PROJ and Geod.
    """
    minx, _, maxx, _ = lonlat.bounds
    if maxx - minx <= 180.0:
        return lonlat

    def _shift(coords: np.ndarray) -> np.ndarray:
        coords = coords.copy()
        coords[:, 0] = np.where(coords[:, 0] < 0, coords[:, 0] + 360.0, coords[:, 0])
        return coords

    warnings.append("Feature crosses the antimeridian; longitudes unwrapped for measurement.")
    return shapely.transform(lonlat, _shift)


def _pct(a: float, b: float) -> float | None:
    return round(abs(a - b) / b * 100.0, 6) if b else None


def measure_geometry(
    geom: BaseGeometry | None,
    source_crs: CRS | None,
    *,
    max_span_deg: float = 6.0,
) -> tuple[Measurement, BaseGeometry | None]:
    """Measure one feature.

    Returns the measurement and the geometry reprojected to EPSG:4326 (or None when that is
    not possible), which callers use for storage / GeoJSON output.
    """
    if geom is None or geom.is_empty:
        return Measurement(MeasurementStatus.EMPTY, warnings=["Feature has no geometry."]), None

    warnings: list[str] = []
    if shapely.has_z(geom):
        z = shapely.get_coordinates(geom, include_z=True)[:, 2]
        if np.any(np.nan_to_num(z) != 0):
            warnings.append("Z values ignored; measurement is 2D (planimetric).")
        geom = shapely.force_2d(geom)

    if source_crs is None:
        return (
            Measurement(
                MeasurementStatus.UNKNOWN_CRS,
                warnings=[*warnings, "Source CRS unknown; measurement skipped."],
            ),
            None,
        )

    try:
        lonlat = reproject(geom, source_crs, WGS84)
    except Exception as exc:
        return Measurement(
            MeasurementStatus.ERROR, warnings=[*warnings, f"Reprojection failed: {exc}"]
        ), None

    gtype = geom.geom_type
    if gtype in POINT_TYPES:
        return Measurement(MeasurementStatus.NOT_APPLICABLE, warnings=warnings), lonlat
    if gtype not in AREA_TYPES | LENGTH_TYPES:
        return Measurement(
            MeasurementStatus.UNSUPPORTED,
            warnings=[*warnings, f"Measurement not supported for geometry type '{gtype}'."],
        ), lonlat

    kind = "area" if gtype in AREA_TYPES else "length"
    if not geom.is_valid:
        reason = shapely.is_valid_reason(geom)
        geom = shapely.make_valid(geom)
        if kind == "area":
            geom = _polygonal_part(geom)
        warnings.append(f"Invalid geometry repaired before measuring ({reason}).")
        if geom.is_empty:
            return Measurement(
                MeasurementStatus.ERROR,
                kind=kind,
                warnings=[*warnings, "Geometry could not be repaired into a measurable shape."],
            ), lonlat
        lonlat_measure = reproject(geom, source_crs, WGS84)
    else:
        lonlat_measure = lonlat

    measure_geom, measure_crs = geom, source_crs
    if source_crs.is_geographic:
        lonlat_measure = _unwrap_antimeridian(lonlat_measure, warnings)
        # Edges of lon/lat data are straight in lon/lat space. Densify long edges so both the
        # projected and the geodesic calculation follow the same edge path; otherwise the
        # cross-check would compare edge interpretations rather than projection accuracy.
        lonlat_measure = shapely.segmentize(lonlat_measure, DENSIFY_DEG)
        measure_geom, measure_crs = lonlat_measure, WGS84

    try:
        choice = choose_projection(
            source_crs, lonlat_measure.bounds, kind, max_span_deg=max_span_deg
        )
        projected = reproject(measure_geom, measure_crs, choice.crs)
        if kind == "area":
            value, geodesic, unit = projected.area, _geodesic_area(lonlat_measure), "m2"
        else:
            value, geodesic, unit = projected.length, GEOD.geometry_length(lonlat_measure), "m"
    except Exception as exc:  # pragma: no cover - defensive; PROJ edge cases
        return Measurement(
            MeasurementStatus.ERROR, kind=kind, warnings=[*warnings, f"Measurement failed: {exc}"]
        ), lonlat

    if not np.isfinite(value):
        return Measurement(
            MeasurementStatus.ERROR,
            kind=kind,
            warnings=[*warnings, "Projection produced non-finite coordinates."],
        ), lonlat

    diff = _pct(value, geodesic)
    if diff is not None and diff > 1.0:
        warnings.append(
            f"Projected and geodesic results differ by {diff:.2f}%; "
            "consider the geodesic value for this feature."
        )

    return Measurement(
        MeasurementStatus.MEASURED,
        kind=kind,
        value=round(value, 4),
        unit=unit,
        geodesic_value=round(geodesic, 4),
        difference_pct=diff,
        measurement_crs=choice.label,
        method=choice.method,
        warnings=warnings,
    ), lonlat
