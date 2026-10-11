"""End-to-end processing of real files through GDAL, without a database."""

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, box

from app.core.config import Settings
from app.services.processor import process_dataset
from app.services.upload import FileType
from tests.factories import (
    kml_document,
    kml_line,
    kml_point,
    kml_polygon,
    write_shapefile_zip,
    zip_bytes,
)

SETTINGS = Settings()


def test_shapefile_polygons_in_wgs84():
    data = write_shapefile_zip(
        [box(77.0, 28.0, 77.01, 28.01), box(77.02, 28.0, 77.03, 28.01)],
        properties={"name": ["a", "b"], "pop": [10, 20]},
    )
    result = process_dataset("parcels.zip", data, FileType.SHAPEFILE, SETTINGS)

    assert result.crs == "EPSG:4326"
    assert len(result.features) == 2
    f = result.features[0]
    assert f["geometry_type"] == "Polygon"
    assert f["properties"] == {"name": "a", "pop": 10}
    assert f["geometry"]["type"] == "Polygon"
    assert f["measurement"]["status"] == "MEASURED"
    assert f["measurement"]["area_m2"] == pytest.approx(1.09e6, rel=0.01)
    assert result.summary["area_m2"] == pytest.approx(2 * f["measurement"]["area_m2"], rel=1e-3)
    assert "location" in f  # indexed for spatial queries


def test_shapefile_in_projected_crs_reports_source_and_wgs84_geometry():
    data = write_shapefile_zip(
        [box(712000, 3170000, 713000, 3171000)], crs="EPSG:32643", name="utm"
    )
    result = process_dataset("utm.zip", data, FileType.SHAPEFILE, SETTINGS)
    f = result.features[0]
    assert result.crs == "EPSG:32643"
    assert f["measurement"]["method"] == "SOURCE_PROJECTED"
    assert f["measurement"]["area_m2"] == pytest.approx(1e6)
    assert f["geometry_crs"] == "EPSG:4326"
    assert 77 < f["geometry"]["coordinates"][0][0][0] < 78


def test_shapefile_without_prj_assumes_wgs84_when_coordinates_fit():
    data = write_shapefile_zip([LineString([(0, 0), (1, 0)])], drop_parts=(".prj",))
    result = process_dataset("noprj.zip", data, FileType.SHAPEFILE, SETTINGS)
    assert result.layers[0]["crs_assumed"] is True
    assert any("assumed" in w for w in result.warnings)
    assert result.features[0]["measurement"]["length_m"] == pytest.approx(111_319, rel=1e-3)


def test_shapefile_without_prj_and_projected_coordinates_is_not_guessed():
    data = write_shapefile_zip([box(712000, 3170000, 713000, 3171000)], drop_parts=(".prj",))
    result = process_dataset("noprj.zip", data, FileType.SHAPEFILE, SETTINGS)
    assert result.crs == "UNKNOWN"
    assert result.features[0]["measurement"]["status"] == "UNKNOWN_CRS"


def test_zip_with_multiple_shapefiles_reads_all_layers():
    a = write_shapefile_zip([Point(1, 1)], name="points")
    b = write_shapefile_zip([LineString([(0, 0), (0, 1)])], name="roads")
    import io
    import zipfile

    merged = {}
    for blob in (a, b):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            merged.update({n: zf.read(n) for n in zf.namelist()})
    result = process_dataset("multi.zip", zip_bytes(merged), FileType.SHAPEFILE, SETTINGS)
    assert sorted(layer["name"] for layer in result.layers) == ["points", "roads"]
    assert result.measurement_status_counts == {"NOT_APPLICABLE": 1, "MEASURED": 1}
    assert [f["feature_index"] for f in result.features] == [0, 1]


def test_multipolygon_shapefile():
    mp = MultiPolygon([box(0, 0, 0.01, 0.01), box(0.02, 0, 0.03, 0.01)])
    result = process_dataset("mp.zip", write_shapefile_zip([mp]), FileType.SHAPEFILE, SETTINGS)
    assert result.features[0]["geometry_type"] == "MultiPolygon"
    assert result.features[0]["measurement"]["status"] == "MEASURED"


def test_kml_with_mixed_geometries_and_folders():
    data = kml_document(
        [("field", kml_polygon([(77, 28), (77.01, 28), (77.01, 28.01), (77, 28.01), (77, 28)]))],
        folders={
            "Roads": [("road", kml_line([(77, 28), (77.1, 28)]))],
            "Wells": [("well", kml_point(77.005, 28.005))],
        },
    )
    result = process_dataset("survey.kml", data, FileType.KML, SETTINGS)

    assert result.crs == "EPSG:4326"
    assert len(result.features) == 3
    by_type = {f["geometry_type"]: f for f in result.features}
    assert set(by_type) == {"Polygon", "LineString", "Point"}
    assert by_type["Polygon"]["measurement"]["area_m2"] == pytest.approx(1.09e6, rel=0.01)
    assert by_type["LineString"]["measurement"]["length_m"] == pytest.approx(9_800, rel=0.01)
    assert by_type["Point"]["measurement"]["status"] == "NOT_APPLICABLE"
    assert by_type["Polygon"]["properties"]["Name"] == "field"
    assert {layer["name"] for layer in result.layers} >= {"Roads", "Wells"}


def test_kmz_is_supported():
    kml = kml_document([("p", kml_point(1, 1))])
    result = process_dataset("a.kmz", zip_bytes({"doc.kml": kml}), FileType.KML, SETTINGS)
    assert len(result.features) == 1


def test_bad_feature_does_not_fail_the_file():
    bowtie = kml_polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    data = kml_document([("bad", bowtie), ("ok", kml_point(0, 0))])
    result = process_dataset("x.kml", data, FileType.KML, SETTINGS)
    statuses = [f["measurement"]["status"] for f in result.features]
    assert statuses == ["MEASURED", "NOT_APPLICABLE"]
    assert result.features[0]["warnings"]
