"""Build real Shapefile / KML fixtures in memory so tests don't depend on binary blobs."""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import shapely
from pyogrio.raw import write as ogr_write
from shapely.geometry.base import BaseGeometry


def write_shapefile_zip(
    geometries: list[BaseGeometry],
    *,
    crs: str | None = "EPSG:4326",
    name: str = "layer",
    properties: dict[str, list] | None = None,
    drop_parts: tuple[str, ...] = (),
    folder: str = "",
) -> bytes:
    properties = properties or {"name": [f"f{i}" for i in range(len(geometries))]}
    geom_type = geometries[0].geom_type
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.shp"
        ogr_write(
            path,
            geometry=np.array(shapely.to_wkb(geometries), dtype=object),
            field_data=[np.array(v) for v in properties.values()],
            fields=list(properties),
            crs=crs,
            geometry_type=geom_type,
            driver="ESRI Shapefile",
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for part in Path(tmp).iterdir():
                if part.suffix.lower() in drop_parts:
                    continue
                zf.write(part, arcname=f"{folder}{part.name}")
        return buf.getvalue()


def kml_document(
    placemarks: list[tuple[str, str]],
    folders: dict[str, list] | None = None,
    *,
    name: str = "test",
) -> bytes:
    """placemarks: (name, geometry-kml-snippet). folders: {folder name: placemarks}."""

    def pm(name: str, geom: str) -> str:
        desc = f"<description>{name} desc</description>"
        return f"<Placemark><name>{name}</name>{desc}{geom}</Placemark>"

    body = "".join(pm(n, g) for n, g in placemarks)
    for folder, items in (folders or {}).items():
        body += f"<Folder><name>{folder}</name>{''.join(pm(n, g) for n, g in items)}</Folder>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{name}</name>'
        f"{body}</Document></kml>"
    ).encode()


def kml_polygon(coords: list[tuple[float, float]]) -> str:
    ring = " ".join(f"{x},{y},0" for x, y in coords)
    return (
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        f"{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
    )


def kml_line(coords: list[tuple[float, float]]) -> str:
    points = " ".join(f"{x},{y}" for x, y in coords)
    return f"<LineString><coordinates>{points}</coordinates></LineString>"


def kml_point(x: float, y: float) -> str:
    return f"<Point><coordinates>{x},{y}</coordinates></Point>"


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()
