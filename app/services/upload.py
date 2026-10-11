"""Upload validation and safe archive extraction.

Validation happens synchronously at upload time so clients get an immediate 4xx for
structurally broken files; actual geospatial parsing happens later in the background.
"""

from __future__ import annotations

import io
import zipfile
from enum import Enum
from pathlib import Path, PurePosixPath

from app.core.exceptions import FileTooLarge, InvalidUpload, UnsupportedFileType

SHAPEFILE_REQUIRED_PARTS = (".shp", ".shx", ".dbf")


class FileType(str, Enum):
    SHAPEFILE = "SHAPEFILE"
    KML = "KML"


ALLOWED_EXTENSIONS = {".zip": FileType.SHAPEFILE, ".kml": FileType.KML, ".kmz": FileType.KML}


def detect_file_type(filename: str | None) -> FileType:
    suffix = Path(filename or "").suffix.lower()
    try:
        return ALLOWED_EXTENSIONS[suffix]
    except KeyError:
        raise UnsupportedFileType(
            f"Unsupported file extension '{suffix or '(none)'}'. "
            "Upload a .zip containing a Shapefile, or a .kml/.kmz file."
        ) from None


def _is_unsafe_member(name: str) -> bool:
    """Reject absolute paths, drive letters and parent-directory traversal (zip-slip)."""
    path = PurePosixPath(name.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts or (len(name) > 1 and name[1] == ":")


def _is_junk(name: str) -> bool:
    """macOS archive metadata (__MACOSX/, ._foo.shp) is not real data."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return "__MACOSX" in parts or (bool(parts) and parts[-1].startswith("._"))


def inspect_zip(data: bytes, *, max_unzipped_bytes: int, max_entries: int) -> list[str]:
    """Validate a zip archive without extracting it; return the shapefile (.shp) members.

    Raises InvalidUpload if the archive is corrupt, unsafe, a likely zip bomb, or does not
    contain at least one complete shapefile (.shp + .shx + .dbf).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InvalidUpload("The uploaded file is not a valid zip archive.") from None

    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir() and not _is_junk(i.filename)]
        if len(infos) > max_entries:
            raise InvalidUpload(f"Archive has too many entries ({len(infos)} > {max_entries}).")

        unsafe = [i.filename for i in infos if _is_unsafe_member(i.filename)]
        if unsafe:
            raise InvalidUpload(f"Archive contains unsafe paths: {unsafe[:5]}")

        total = sum(i.file_size for i in infos)
        if total > max_unzipped_bytes:
            raise InvalidUpload(
                f"Archive expands to {total / 1e6:.1f} MB, above the "
                f"{max_unzipped_bytes / 1e6:.0f} MB limit."
            )

        names = {i.filename.lower(): i.filename for i in infos}
        shp_members = [orig for low, orig in names.items() if low.endswith(".shp")]
        if not shp_members:
            raise InvalidUpload("No .shp file found in the zip archive.")

        for shp in shp_members:
            stem = shp[:-4].lower()
            missing = [ext for ext in SHAPEFILE_REQUIRED_PARTS if f"{stem}{ext}" not in names]
            if missing:
                raise InvalidUpload(
                    f"Shapefile '{shp}' is incomplete; missing required part(s): "
                    f"{', '.join(missing)}."
                )
        return sorted(shp_members)


def inspect_kmz(data: bytes, *, max_unzipped_bytes: int, max_entries: int) -> str:
    """Validate a KMZ (zipped KML) and return the name of its main .kml document."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InvalidUpload("The uploaded .kmz is not a valid zip archive.") from None
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > max_entries or sum(i.file_size for i in infos) > max_unzipped_bytes:
            raise InvalidUpload("KMZ archive exceeds size or entry limits.")
        kmls = [i.filename for i in infos if i.filename.lower().endswith(".kml")]
        if not kmls:
            raise InvalidUpload("No .kml document found inside the .kmz archive.")
        if any(_is_unsafe_member(k) for k in kmls):
            raise InvalidUpload("KMZ archive contains unsafe paths.")
        # By convention the root document is doc.kml; otherwise take the first one.
        return next((k for k in kmls if k.lower() == "doc.kml"), kmls[0])


def validate_upload(filename: str, data: bytes, *, settings) -> FileType:
    """Run all cheap, synchronous checks on an upload. Returns the detected file type."""
    file_type = detect_file_type(filename)
    if len(data) > settings.max_upload_bytes:
        raise FileTooLarge(f"File exceeds the {settings.max_upload_mb} MB upload limit.")
    if not data:
        raise InvalidUpload("The uploaded file is empty.")

    limits = {
        "max_unzipped_bytes": settings.max_unzipped_bytes,
        "max_entries": settings.max_zip_entries,
    }
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        inspect_zip(data, **limits)
    elif suffix == ".kmz":
        inspect_kmz(data, **limits)
    elif b"<kml" not in data[:4096].lower():
        raise InvalidUpload("File does not look like KML (no <kml> root element found).")
    return file_type


def extract_zip_safely(data: bytes, dest: Path) -> None:
    """Extract an archive that has already passed inspect_zip/inspect_kmz into dest."""
    dest = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or _is_junk(info.filename):
                continue
            target = (dest / info.filename).resolve()
            # Defence in depth against zip-slip, even though inspect_* already checked.
            if dest not in target.parents:
                raise InvalidUpload(f"Unsafe path in archive: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                out.write(src.read())


def prepare_source(filename: str, data: bytes, workdir: Path) -> list[Path]:
    """Materialise an upload on disk and return the dataset paths GDAL should open.

    - .zip -> one path per shapefile found inside
    - .kml -> the file itself
    - .kmz -> the main KML document inside it
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".kml":
        path = workdir / "upload.kml"
        path.write_bytes(data)
        return [path]

    extract_zip_safely(data, workdir)
    if suffix == ".kmz":
        kmls = sorted(workdir.rglob("*.kml"), key=lambda p: (p.name.lower() != "doc.kml", str(p)))
        return kmls[:1]
    return sorted(
        p for p in workdir.rglob("*") if p.suffix.lower() == ".shp" and not _is_junk(p.name)
    )
