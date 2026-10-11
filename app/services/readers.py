"""Read features out of a geospatial dataset with GDAL (via pyogrio).

pyogrio's raw API is used directly (WKB + numpy arrays) so we don't need the full
geopandas/pandas stack just to iterate over features.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyogrio
import shapely
from pyogrio.raw import read as ogr_read
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.services.crs import WGS84, parse_crs

logger = logging.getLogger(__name__)


@dataclass
class RawFeature:
    layer: str
    layer_index: int  # index of the feature within its layer
    geometry: BaseGeometry | None
    properties: dict[str, Any]


@dataclass
class LayerInfo:
    name: str
    source: str  # file name the layer came from
    crs: CRS | None
    crs_assumed: bool
    warnings: list[str]


def _to_python(value: Any) -> Any:
    """Convert numpy / GDAL field values into JSON + BSON friendly Python values."""
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, np.datetime64):
        value = value.astype("datetime64[ms]").item()
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return [_to_python(v) for v in value.tolist()]
    return value


# Presentation fields GDAL's LIBKML driver adds to every placemark. They are dropped when
# unset (None, or GDAL's -1 "not specified" sentinel) so properties show real attributes.
_KML_DISPLAY_FIELDS = {
    "id", "timestamp", "begin", "end", "altitudeMode", "tessellate", "extrude",
    "visibility", "drawOrder", "icon",
}  # fmt: skip


def _clean_kml_properties(props: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in props.items() if not (k in _KML_DISPLAY_FIELDS and v in (None, -1, 0, ""))
    }


def _looks_geographic(bounds: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bounds
    return -180.0 <= minx <= maxx <= 180.0 and -90.0 <= miny <= maxy <= 90.0


def _resolve_crs(
    meta_crs: str | None, geometries: list[BaseGeometry | None], is_kml: bool
) -> tuple[CRS | None, bool, list[str]]:
    """Return (crs, assumed, warnings) for a layer."""
    if is_kml:
        # The KML spec mandates WGS84 lon/lat; GDAL reports it but be explicit.
        return parse_crs(meta_crs) or WGS84, False, []
    crs = parse_crs(meta_crs)
    if crs is not None:
        return crs, False, []

    valid = [g for g in geometries if g is not None and not g.is_empty]
    if valid and _looks_geographic(shapely.total_bounds(valid)):
        return (
            WGS84,
            True,
            ["No .prj file; coordinates fall within lon/lat range so EPSG:4326 was assumed."],
        )
    return (
        None,
        False,
        ["No .prj file and coordinates are not lon/lat; CRS unknown, measurements skipped."],
    )


def read_layers(paths: list[Path], *, is_kml: bool) -> Iterator[tuple[LayerInfo, list[RawFeature]]]:
    """Yield (layer info, features) for every layer of every dataset path."""
    for path in paths:
        layer_names = [str(row[0]) for row in pyogrio.list_layers(path)]
        for layer_name in layer_names:
            meta, _fids, wkb, field_data = ogr_read(path, layer=layer_name)
            geometries: list[BaseGeometry | None] = (
                list(shapely.from_wkb(wkb, on_invalid="ignore")) if wkb is not None else []
            )
            crs, assumed, warnings = _resolve_crs(meta.get("crs"), geometries, is_kml)
            fields = [str(f) for f in meta.get("fields", [])]
            label = layer_name if is_kml else path.stem

            features = []
            for i, geom in enumerate(geometries):
                props = {name: _to_python(field_data[j][i]) for j, name in enumerate(fields)}
                if is_kml:
                    props = _clean_kml_properties(props)
                features.append(RawFeature(label, i, geom, props))

            logger.info("Read layer %s (%d features) from %s", label, len(features), path.name)
            yield LayerInfo(label, path.name, crs, assumed, warnings), features
