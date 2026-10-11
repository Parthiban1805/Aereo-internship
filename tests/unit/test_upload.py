import pytest
from shapely.geometry import Point

from app.core.config import Settings
from app.core.exceptions import FileTooLarge, InvalidUpload, UnsupportedFileType
from app.services.upload import FileType, inspect_zip, validate_upload
from tests.factories import kml_document, kml_point, write_shapefile_zip, zip_bytes

SETTINGS = Settings(max_upload_mb=1, max_unzipped_mb=5, max_zip_entries=50)
LIMITS = {"max_unzipped_bytes": SETTINGS.max_unzipped_bytes, "max_entries": 50}


def test_valid_shapefile_zip():
    data = write_shapefile_zip([Point(0, 0)])
    assert validate_upload("pts.zip", data, settings=SETTINGS) is FileType.SHAPEFILE


def test_shapefile_in_subfolder_is_found():
    data = write_shapefile_zip([Point(0, 0)], folder="nested/dir/")
    assert inspect_zip(data, **LIMITS) == ["nested/dir/layer.shp"]


def test_valid_kml():
    data = kml_document([("p", kml_point(1, 1))])
    assert validate_upload("a.KML", data, settings=SETTINGS) is FileType.KML


@pytest.mark.parametrize("name", ["data.geojson", "data.shp", "noext", "x.tar.gz"])
def test_unsupported_extension(name):
    with pytest.raises(UnsupportedFileType):
        validate_upload(name, b"whatever", settings=SETTINGS)


def test_too_large():
    with pytest.raises(FileTooLarge):
        validate_upload("a.kml", b"<kml>" + b" " * (2 * 1024 * 1024), settings=SETTINGS)


def test_not_a_zip():
    with pytest.raises(InvalidUpload, match="not a valid zip"):
        validate_upload("a.zip", b"definitely not a zip", settings=SETTINGS)


def test_zip_without_shapefile():
    with pytest.raises(InvalidUpload, match=r"No \.shp"):
        validate_upload("a.zip", zip_bytes({"readme.txt": b"hi"}), settings=SETTINGS)


@pytest.mark.parametrize("missing", [".dbf", ".shx"])
def test_incomplete_shapefile(missing):
    data = write_shapefile_zip([Point(0, 0)], drop_parts=(missing,))
    with pytest.raises(InvalidUpload, match=f"missing required part.*\\{missing}"):
        validate_upload("a.zip", data, settings=SETTINGS)


@pytest.mark.parametrize("evil", ["../../etc/passwd", "/abs/path.shp", "C:/win.shp"])
def test_zip_slip_is_rejected(evil):
    data = zip_bytes({evil: b"x", "a.shp": b"", "a.shx": b"", "a.dbf": b""})
    with pytest.raises(InvalidUpload, match="unsafe"):
        inspect_zip(data, **LIMITS)


def test_zip_bomb_is_rejected():
    data = zip_bytes({"a.shp": b"\0" * (6 * 1024 * 1024), "a.shx": b"", "a.dbf": b""})
    assert len(data) < 1024 * 1024  # small on the wire...
    with pytest.raises(InvalidUpload, match="expands to"):
        validate_upload("a.zip", data, settings=SETTINGS)  # ...but huge once extracted


def test_macos_metadata_is_ignored():
    data = zip_bytes({"__MACOSX/._a.shp": b"junk", "a.shp": b"", "a.shx": b"", "a.dbf": b""})
    assert inspect_zip(data, **LIMITS) == ["a.shp"]


def test_fake_kml_is_rejected():
    with pytest.raises(InvalidUpload, match="KML"):
        validate_upload("a.kml", b'{"type": "FeatureCollection"}', settings=SETTINGS)
