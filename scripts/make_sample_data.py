"""Generate the files in sample_data/ (run: python -m scripts.make_sample_data).

Shapes are built with known sizes so measured results can be checked by eye.
"""

from __future__ import annotations

from pathlib import Path

from pyproj import CRS
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from app.services.crs import WGS84
from app.services.measurement import reproject
from tests.factories import (
    kml_document,
    kml_line,
    kml_point,
    kml_polygon,
    write_shapefile_zip,
)

OUT = Path(__file__).resolve().parent.parent / "sample_data"
UTM_43N = CRS.from_epsg(32643)
X0, Y0 = 712000, 3170000  # near New Delhi, UTM 43N


def utm_box(dx: float, dy: float, w: float, h: float) -> Polygon:
    return box(X0 + dx, Y0 + dy, X0 + dx + w, Y0 + dy + h)


def to_ll(geom):
    return reproject(geom, UTM_43N, WGS84)


def main() -> None:
    OUT.mkdir(exist_ok=True)

    # 1. Polygons in WGS84 (EPSG:4326): 1 km², 2 ha, a plot with a courtyard, a 2-part farm.
    parcels = [
        to_ll(utm_box(0, 0, 1000, 1000)),
        to_ll(utm_box(1500, 0, 200, 100)),
        to_ll(
            Polygon(utm_box(0, 1500, 400, 400).exterior, [utm_box(100, 1600, 200, 200).exterior])
        ),
        to_ll(MultiPolygon([utm_box(2000, 1500, 300, 300), utm_box(2500, 1500, 300, 300)])),
    ]
    (OUT / "parcels_wgs84.zip").write_bytes(
        write_shapefile_zip(
            parcels,
            name="parcels",
            properties={
                "parcel_id": ["P-001", "P-002", "P-003", "P-004"],
                "owner": ["Asha", "Ravi", "Meera", "Kiran"],
                "expected": ["1 km2", "2 ha", "16 ha - 4 ha hole = 12 ha", "2 x 9 ha = 18 ha"],
            },
        )
    )

    # 2. Same idea, but stored in a projected CRS (UTM 43N) -> measured in place.
    (OUT / "parcels_utm43n.zip").write_bytes(
        write_shapefile_zip(
            [utm_box(0, 0, 1000, 1000), utm_box(1500, 0, 500, 200)],
            crs="EPSG:32643",
            name="parcels_utm",
            properties={"parcel_id": ["U-001", "U-002"], "expected": ["1 km2", "10 ha"]},
        )
    )

    # 3. Lines: a 5 km road and a 1-degree line along the equator (~111.32 km).
    (OUT / "roads.zip").write_bytes(
        write_shapefile_zip(
            [to_ll(LineString([(X0, Y0), (X0 + 3000, Y0 + 4000)])), LineString([(0, 0), (1, 0)])],
            name="roads",
            properties={
                "name": ["Ring Rd segment", "Equator 1deg"],
                "expected": ["5 km", "111.32 km"],
            },
        )
    )

    # 4. KML survey: polygon + line + point + an invalid (self-intersecting) polygon, in folders.
    field = list(to_ll(utm_box(0, 0, 500, 500)).exterior.coords)
    bowtie = [(77.30, 28.70), (77.31, 28.71), (77.31, 28.70), (77.30, 28.71), (77.30, 28.70)]
    road = list(to_ll(LineString([(X0, Y0), (X0 + 0, Y0 + 2000)])).coords)
    well = to_ll(Point(X0 + 250, Y0 + 250))
    (OUT / "survey.kml").write_bytes(
        kml_document(
            [("Survey boundary (25 ha)", kml_polygon(field))],
            folders={
                "Access roads": [("North access (2 km)", kml_line(road))],
                "Wells": [("Well W-1", kml_point(well.x, well.y))],
                "Needs review": [("Self-intersecting plot", kml_polygon(bowtie))],
            },
            name="Site survey",
        )
    )
    print(f"Wrote sample files to {OUT}")


if __name__ == "__main__":
    main()
