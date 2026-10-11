import pytest
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.services.crs import WGS84
from app.services.measurement import GEOD, MeasurementStatus, measure_geometry, reproject

UTM_43N = CRS.from_epsg(32643)  # covers Delhi


def utm_square_in_lonlat(size_m: float = 1000.0) -> Polygon:
    """A size x size metre square built in UTM 43N, expressed in lon/lat."""
    x, y = 712000, 3170000  # near Delhi
    return reproject(box(x, y, x + size_m, y + size_m), UTM_43N, WGS84)


def test_polygon_area_from_geographic_coordinates_is_in_square_metres():
    m, _ = measure_geometry(utm_square_in_lonlat(), WGS84)
    assert m.status is MeasurementStatus.MEASURED
    assert m.kind == "area" and m.unit == "m2"
    assert m.measurement_crs == "EPSG:32643"
    # Built as exactly 1 km² in UTM, so the UTM measurement must recover it.
    assert m.value == pytest.approx(1_000_000, rel=1e-6)
    # And it must agree with the ellipsoidal (geodesic) truth within UTM's distortion.
    assert m.difference_pct < 0.1


def test_degrees_are_never_used_as_area():
    m, _ = measure_geometry(box(77.0, 28.0, 77.01, 28.01), WGS84)
    assert m.value > 1_000_000  # ~1.09 km², whereas the "degree area" would be 0.0001


def test_one_degree_along_equator_matches_known_length():
    m, _ = measure_geometry(LineString([(0, 0), (1, 0)]), WGS84)
    assert m.kind == "length" and m.unit == "m"
    assert m.geodesic_value == pytest.approx(111_319.49, abs=0.01)
    assert m.value == pytest.approx(111_319.49, rel=1e-3)


def test_projected_metric_source_is_measured_in_place():
    m, lonlat = measure_geometry(box(712000, 3170000, 713000, 3170500), UTM_43N)
    assert m.method == "SOURCE_PROJECTED"
    assert m.value == pytest.approx(500_000)
    assert lonlat.bounds[0] == pytest.approx(77.2, abs=0.1)  # reprojected for storage


def test_polygon_with_hole_subtracts_hole():
    outer = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]
    hole = [(0.0025, 0.0025), (0.0075, 0.0025), (0.0075, 0.0075), (0.0025, 0.0075)]
    full, _ = measure_geometry(Polygon(outer), WGS84)
    holed, _ = measure_geometry(Polygon(outer, [hole]), WGS84)
    assert holed.value == pytest.approx(full.value * 0.75, rel=1e-3)
    assert holed.geodesic_value == pytest.approx(full.geodesic_value * 0.75, rel=1e-3)


def test_multipolygon_and_multilinestring():
    mp = MultiPolygon([box(10, 10, 10.01, 10.01), box(10.02, 10, 10.03, 10.01)])
    single, _ = measure_geometry(box(10, 10, 10.01, 10.01), WGS84)
    multi, _ = measure_geometry(mp, WGS84)
    assert multi.value == pytest.approx(2 * single.value, rel=1e-3)

    ml, _ = measure_geometry(MultiLineString([[(0, 0), (0.1, 0)], [(0, 1), (0.1, 1)]]), WGS84)
    assert ml.kind == "length" and ml.value == pytest.approx(2 * 11_132, rel=0.01)


@pytest.mark.parametrize(
    "geom, expected",
    [
        (Point(77, 28), MeasurementStatus.NOT_APPLICABLE),
        (
            GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])]),
            MeasurementStatus.UNSUPPORTED,
        ),
        (Polygon(), MeasurementStatus.EMPTY),
        (None, MeasurementStatus.EMPTY),
    ],
)
def test_non_measurable_geometries_are_handled_gracefully(geom, expected):
    m, _ = measure_geometry(geom, WGS84)
    assert m.status is expected
    assert m.value is None


def test_invalid_bowtie_polygon_is_repaired_and_measured():
    bowtie = Polygon([(0, 0), (0.01, 0.01), (0.01, 0), (0, 0.01)])
    m, _ = measure_geometry(bowtie, WGS84)
    assert m.status is MeasurementStatus.MEASURED
    assert any("repaired" in w for w in m.warnings)
    assert m.value > 0


def test_unknown_crs_skips_measurement():
    m, lonlat = measure_geometry(box(0, 0, 10, 10), None)
    assert m.status is MeasurementStatus.UNKNOWN_CRS and lonlat is None


def test_z_values_are_dropped_with_warning():
    m, _ = measure_geometry(LineString([(0, 0, 100), (0.1, 0, 500)]), WGS84)
    assert m.status is MeasurementStatus.MEASURED
    assert any("Z values" in w for w in m.warnings)


def test_wide_feature_uses_local_equal_area_and_matches_geodesic():
    m, _ = measure_geometry(box(-100, 10, -80, 20), WGS84)
    assert m.method == "LOCAL_EQUAL_AREA"
    assert m.difference_pct < 0.01


def test_polar_feature_falls_back_from_utm():
    m, _ = measure_geometry(box(0, 85, 10, 86), WGS84)
    assert m.method == "LOCAL_EQUAL_AREA"
    assert m.difference_pct < 0.01


def test_geodesic_reference_is_independent():
    """Sanity check of the reference itself: 1°x1° cell at the equator ≈ 12,308 km²."""
    area, _ = GEOD.geometry_area_perimeter(Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))
    assert abs(area) / 1e6 == pytest.approx(12_308, rel=1e-3)


def test_antimeridian_crossing_feature_is_measured_locally():
    # 0.2° x 0.1° box straddling 180°: ~246 km², not a band wrapping the globe.
    m, _ = measure_geometry(Polygon([(179.9, 0), (-179.9, 0), (-179.9, 0.1), (179.9, 0.1)]), WGS84)
    assert m.method == "UTM"
    assert m.geodesic_value / 1e6 == pytest.approx(246.2, rel=0.01)
    assert m.difference_pct < 0.25  # sits on a UTM zone edge: worst case for UTM scale
    assert any("antimeridian" in w for w in m.warnings)


def test_unoriented_hole_would_be_added_without_orient():
    """Guards the orient() call: pyproj sums signed ring areas."""
    outer = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]
    hole = [(0.0025, 0.0025), (0.0075, 0.0025), (0.0075, 0.0075), (0.0025, 0.0075)]
    raw, _ = GEOD.geometry_area_perimeter(Polygon(outer, [hole]))  # same orientation as shell
    full, _ = GEOD.geometry_area_perimeter(Polygon(outer))
    assert abs(raw) > abs(full)  # the naive call adds the hole
    m, _ = measure_geometry(Polygon(outer, [hole]), WGS84)
    assert m.geodesic_value == pytest.approx(abs(full) * 0.75, rel=1e-3)
