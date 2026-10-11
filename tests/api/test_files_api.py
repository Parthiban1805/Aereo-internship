import pytest
from shapely.geometry import Point, box

from tests.factories import kml_document, kml_line, kml_point, kml_polygon, write_shapefile_zip

SURVEY_KML = kml_document(
    [("field", kml_polygon([(77, 28), (77.01, 28), (77.01, 28.01), (77, 28.01), (77, 28)]))],
    folders={
        "Roads": [("road", kml_line([(77, 28), (77.1, 28)]))],
        "Wells": [("well", kml_point(77.005, 28.005))],
    },
)


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200 and resp.json()["mongo"] == "up"


async def test_upload_returns_202_with_links(upload):
    body = await upload("survey.kml", SURVEY_KML, wait=False)
    assert body["status"] == "PENDING"
    assert body["links"]["measurements"].endswith(f"/api/files/{body['id']}/measurements/")


async def test_kml_end_to_end(client, upload):
    info = await upload("survey.kml", SURVEY_KML)
    assert info["status"] == "COMPLETED"
    assert info["filename"] == "survey.kml"
    assert info["feature_count"] == 3
    assert info["crs"] == "EPSG:4326"
    assert info["geometry_type_counts"] == {"Polygon": 1, "LineString": 1, "Point": 1}

    resp = await client.get(f"/api/files/{info['id']}/measurements/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    items = {i["geometry_type"]: i for i in body["items"]}
    assert items["Polygon"]["measurement"]["area_m2"] == pytest.approx(1.09e6, rel=0.01)
    assert items["Polygon"]["measurement"]["measurement_crs"] == "EPSG:32643"
    assert items["LineString"]["measurement"]["length_m"] == pytest.approx(9_800, rel=0.01)
    assert items["Point"]["measurement"]["status"] == "NOT_APPLICABLE"
    assert items["Polygon"]["geometry"]["type"] == "Polygon"
    assert items["Polygon"]["properties"]["Name"] == "field"
    assert body["summary"]["length_m"] == pytest.approx(9_800, rel=0.01)


async def test_measurement_filters_and_pagination(client, upload):
    info = await upload("survey.kml", SURVEY_KML)
    url = f"/api/files/{info['id']}/measurements/"

    polys = (await client.get(url, params={"geometry_type": "Polygon"})).json()
    assert [i["geometry_type"] for i in polys["items"]] == ["Polygon"]

    na = (await client.get(url, params={"status": "NOT_APPLICABLE"})).json()
    assert na["total"] == 1

    page = (
        await client.get(url, params={"limit": 1, "offset": 1, "include_geometry": False})
    ).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert "geometry" not in page["items"][0]


async def test_shapefile_end_to_end(client, upload):
    data = write_shapefile_zip(
        [box(712000, 3170000, 713000, 3171000), box(714000, 3170000, 716000, 3171000)],
        crs="EPSG:32643",
        properties={"parcel": ["A", "B"]},
    )
    info = await upload("parcels.zip", data)
    assert info["status"] == "COMPLETED"
    assert info["file_type"] == "SHAPEFILE"
    assert info["crs"] == "EPSG:32643"

    body = (await client.get(f"/api/files/{info['id']}/measurements/")).json()
    areas = [i["measurement"]["area_m2"] for i in body["items"]]
    assert areas == [pytest.approx(1e6), pytest.approx(2e6)]
    assert body["summary"]["area_m2"] == pytest.approx(3e6)


async def test_geojson_features_and_bbox_query(client, upload):
    data = kml_document(
        [
            ("Delhi", kml_point(77.2, 28.6)),
            ("Mumbai", kml_point(72.8, 19.0)),
            ("Delhi road", kml_line([(77.1, 28.5), (77.3, 28.7)])),
        ]
    )
    info = await upload("cities.kml", data)
    url = f"/api/files/{info['id']}/features/"

    resp = await client.get(url)
    assert resp.headers["content-type"].startswith("application/geo+json")
    fc = resp.json()
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 3

    delhi = (await client.get(url, params={"bbox": "76.8,28.3,77.6,28.9"})).json()
    assert {f["properties"]["Name"] for f in delhi["features"]} == {"Delhi", "Delhi road"}

    bad = await client.get(url, params={"bbox": "1,2,3"})
    assert bad.status_code == 422


@pytest.mark.parametrize(
    "filename, data, status, code",
    [
        ("a.geojson", b"{}", 415, "UNSUPPORTED_FILE_TYPE"),
        ("a.zip", b"not a zip", 422, "INVALID_UPLOAD"),
        ("a.zip", write_shapefile_zip([Point(0, 0)], drop_parts=(".dbf",)), 422, "INVALID_UPLOAD"),
        ("a.kml", b"", 422, "INVALID_UPLOAD"),
    ],
)
async def test_upload_validation_errors(client, filename, data, status, code):
    resp = await client.post("/api/files/", files={"file": (filename, data)})
    assert resp.status_code == status
    assert resp.json()["error"] == code


async def test_corrupt_kml_fails_gracefully(client, upload):
    info = await upload("broken.kml", b"<kml><Document><Placemark><Polygon>oops")
    assert info["status"] == "FAILED"
    assert info["error"]
    resp = await client.get(f"/api/files/{info['id']}/measurements/")
    assert resp.status_code == 409


async def test_unknown_file_returns_404(client):
    resp = await client.get("/api/files/doesnotexist/")
    assert resp.status_code == 404 and resp.json()["error"] == "FILE_NOT_FOUND"


async def test_list_and_delete(client, upload):
    info = await upload("survey.kml", SURVEY_KML)
    listing = (await client.get("/api/files/", params={"status": "COMPLETED"})).json()
    assert info["id"] in {f["id"] for f in listing["items"]}

    assert (await client.delete(f"/api/files/{info['id']}/")).status_code == 204
    assert (await client.get(f"/api/files/{info['id']}/")).status_code == 404
    assert (await client.delete(f"/api/files/{info['id']}/")).status_code == 404
