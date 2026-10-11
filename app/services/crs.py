"""CRS detection and selection of a projected CRS suitable for measuring a feature.

Strategy (per feature):
1. If the source CRS is already projected with metre units, measure in it directly - the
   data producer chose it deliberately and re-projecting would only add distortion.
2. Otherwise compute the feature's lon/lat bounds and pick the WGS84 UTM zone containing
   its centroid. UTM (transverse Mercator) keeps scale error within ~0.1% (~0.2% for
   area) even at zone edges, and far less near the central meridian.
3. If the feature is too wide for one UTM zone, or lies beyond UTM's latitude range
   (|lat| > 84), fall back to a projection centred on the feature itself:
   Lambert Azimuthal Equal-Area for areas, Azimuthal Equidistant for lengths.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pyproj import CRS, Transformer

WGS84 = CRS.from_epsg(4326)
UTM_MAX_LAT = 84.0


def crs_label(crs: CRS | None) -> str | None:
    """Human-friendly identifier, e.g. 'EPSG:4326', falling back to the CRS name."""
    if crs is None:
        return None
    auth = crs.to_authority(min_confidence=70)
    return f"{auth[0]}:{auth[1]}" if auth else crs.name


def parse_crs(value: str | None) -> CRS | None:
    if not value:
        return None
    try:
        return CRS.from_user_input(value)
    except Exception:
        return None


def is_metric_projected(crs: CRS) -> bool:
    if not crs.is_projected:
        return False
    units = {(axis.unit_name or "").lower() for axis in crs.axis_info}
    return units <= {"metre", "meter", "m"}


@lru_cache(maxsize=256)
def _transformer(src_wkt: str, dst_wkt: str) -> Transformer:
    # always_xy keeps (lon, lat) / (x, y) ordering regardless of the CRS' axis order.
    return Transformer.from_crs(CRS.from_wkt(src_wkt), CRS.from_wkt(dst_wkt), always_xy=True)


@lru_cache(maxsize=64)
def _wkt(crs: CRS) -> str:
    return crs.to_wkt()


def get_transformer(src: CRS, dst: CRS) -> Transformer:
    return _transformer(_wkt(src), _wkt(dst))


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the WGS84 UTM zone containing (lon, lat)."""
    zone = int((lon + 180.0) // 6.0) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


@dataclass(frozen=True)
class ProjectionChoice:
    crs: CRS
    label: str  # e.g. "EPSG:32643" or "LAEA(lat_0=..., lon_0=...)"
    method: str  # SOURCE_PROJECTED | UTM | LOCAL_EQUAL_AREA | LOCAL_EQUIDISTANT


@lru_cache(maxsize=1024)
def _utm_crs(epsg: int) -> CRS:
    return CRS.from_epsg(epsg)


def _local_crs(kind: str, lon: float, lat: float) -> ProjectionChoice:
    lon, lat = round(lon, 4), round(lat, 4)  # rounding keeps the transformer cache useful
    if kind == "area":
        proj, method = "laea", "LOCAL_EQUAL_AREA"
    else:
        proj, method = "aeqd", "LOCAL_EQUIDISTANT"
    crs = CRS.from_proj4(f"+proj={proj} +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
    return ProjectionChoice(crs, f"{proj.upper()}(lat_0={lat}, lon_0={lon})", method)


def choose_projection(
    source_crs: CRS,
    lonlat_bounds: tuple[float, float, float, float],
    kind: str,
    *,
    max_span_deg: float = 6.0,
) -> ProjectionChoice:
    """Pick the CRS to measure a feature in.

    `lonlat_bounds` are the feature's (minx, miny, maxx, maxy) in EPSG:4326 and `kind` is
    "area" or "length".
    """
    if is_metric_projected(source_crs):
        return ProjectionChoice(source_crs, crs_label(source_crs) or "source", "SOURCE_PROJECTED")

    minx, miny, maxx, maxy = lonlat_bounds
    lon, lat = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    too_wide = (maxx - minx) > max_span_deg
    too_polar = max(abs(miny), abs(maxy)) > UTM_MAX_LAT
    if too_wide or too_polar:
        return _local_crs(kind, lon, lat)

    epsg = utm_epsg(lon, lat)
    return ProjectionChoice(_utm_crs(epsg), f"EPSG:{epsg}", "UTM")
