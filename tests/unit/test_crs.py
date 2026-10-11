import pytest
from pyproj import CRS

from app.services.crs import WGS84, choose_projection, crs_label, is_metric_projected, utm_epsg


@pytest.mark.parametrize(
    "lon, lat, epsg",
    [
        (77.2090, 28.6139, 32643),  # New Delhi
        (151.2093, -33.8688, 32756),  # Sydney (southern hemisphere)
        (-0.1276, 51.5072, 32630),  # London
        (-74.0060, 40.7128, 32618),  # New York
        (180.0, 0.0, 32601),  # antimeridian wraps to zone 1
    ],
)
def test_utm_zone_selection(lon, lat, epsg):
    assert utm_epsg(lon, lat) == epsg


def test_geographic_source_gets_utm():
    choice = choose_projection(WGS84, (77.1, 28.5, 77.3, 28.7), "area")
    assert choice.method == "UTM" and choice.label == "EPSG:32643"


def test_metric_projected_source_is_kept():
    choice = choose_projection(CRS.from_epsg(27700), (-1, 51, 0, 52), "area")  # British Grid
    assert choice.method == "SOURCE_PROJECTED" and choice.label == "EPSG:27700"


def test_projected_in_feet_is_not_trusted_as_metric():
    feet = CRS.from_epsg(2263)  # NY Long Island, US survey feet
    assert not is_metric_projected(feet)
    assert choose_projection(feet, (-74.1, 40.6, -73.9, 40.8), "length").method == "UTM"


def test_wide_and_polar_features_use_local_projection():
    assert choose_projection(WGS84, (-100, 10, -80, 20), "area").method == "LOCAL_EQUAL_AREA"
    assert choose_projection(WGS84, (-100, 10, -80, 20), "length").method == "LOCAL_EQUIDISTANT"
    assert choose_projection(WGS84, (10, 84.5, 11, 85), "area").method == "LOCAL_EQUAL_AREA"


def test_crs_label():
    assert crs_label(WGS84) == "EPSG:4326"
    assert crs_label(None) is None
