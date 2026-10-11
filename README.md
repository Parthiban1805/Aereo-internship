# Geospatial File Measurement API

A FastAPI + MongoDB service that accepts a **zipped Shapefile** or a **KML/KMZ** file, extracts every feature (index, geometry type, geometry, CRS, properties), and measures it:

| Geometry | Measurement |
|---|---|
| Polygon / MultiPolygon | Area in m², km², ha |
| LineString / MultiLineString | Length in m, km |
| Point / MultiPoint | None (`NOT_APPLICABLE`) |
| Anything else (e.g. GeometryCollection) | Handled without crashing (`UNSUPPORTED`) |

Measurements are **never computed in degrees**. Each feature is projected into a suitable metric CRS (normally its local UTM zone) and measured there. The service also computes the same quantity **geodesically on the WGS84 ellipsoid** and reports the difference, so every number comes with its own accuracy check.

```
POST /api/files/                       → 202 {id, status: PENDING, links}
GET  /api/files/{id}/                  → status, feature count, CRS, layers, totals
GET  /api/files/{id}/measurements/     → per-feature area/length (+ filters, pagination)
GET  /api/files/{id}/features/         → GeoJSON FeatureCollection (opens in QGIS / geojson.io)
```

---

## Contents
- [Setup](#setup)
- [API](#api)
- [Architecture](#architecture)
- [CRS handling](#crs-handling)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Assumptions and limitations](#assumptions-and-limitations)
- [Learnings](#learnings)
- [Future scope](#future-scope)

---

## Setup

### Option A: Docker (recommended)

```bash
git clone <this-repo> && cd <this-repo>
docker compose up --build
```

- API: http://localhost:8000. Interactive docs: http://localhost:8000/docs
- MongoDB: `localhost:27017`, with data kept in the `mongo-data` volume

Run the full test suite inside the container (Mongo must be running):

```bash
docker compose run --rm api pytest -v
```

### Option B: Local Python (3.10+)

GDAL, GEOS and PROJ ship inside the `pyogrio`, `shapely` and `pyproj` wheels, so **no system GDAL install is needed**. This works on Windows, macOS and Linux.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

docker compose up -d mongo         # or point MONGO_URI at any MongoDB 6+ (e.g. Atlas)
cp .env.example .env               # optional; defaults work out of the box
uvicorn app.main:app --reload
```

### Configuration

All settings are environment variables (see [.env.example](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `geo_measure` | Database name |
| `MAX_UPLOAD_MB` | `50` | Maximum upload size |
| `MAX_UNZIPPED_MB` | `500` | Maximum total uncompressed archive size (zip-bomb guard) |
| `MAX_ZIP_ENTRIES` | `1000` | Maximum number of files in an archive |
| `UTM_MAX_SPAN_DEG` | `6.0` | Features wider than this (degrees of longitude) use a feature-centred projection instead of UTM |
| `INSERT_BATCH_SIZE` | `500` | Features per MongoDB bulk insert |
| `LOG_LEVEL` | `INFO` | Log level |

### Sample data

[sample_data/](sample_data/) contains files **built with known sizes**, so you can check the results by eye. Each feature's `expected` attribute states the right answer.

| File | Contents |
|---|---|
| `parcels_wgs84.zip` | Shapefile in EPSG:4326: 1 km² square, 2 ha plot, a plot with a hole (12 ha), a 2-part MultiPolygon (18 ha) |
| `parcels_utm43n.zip` | Shapefile already in a projected CRS (EPSG:32643), measured as-is |
| `roads.zip` | 5 km road, and 1° along the equator (111.32 km) |
| `survey.kml` | KML with folders: polygon (25 ha), 2 km line, a point, and a **self-intersecting** polygon |

Regenerate them with `python -m scripts.make_sample_data`.

---

## API

Full OpenAPI docs are at `/docs` (Swagger) and `/redoc`. All examples below are **real responses** from the sample data.

### `POST /api/files/`: upload

Multipart form field `file`. Accepts `.zip` (Shapefile), `.kml` or `.kmz`.

```bash
curl -F "file=@sample_data/survey.kml" http://localhost:8000/api/files/
```

`202 Accepted`. Processing continues in the background:

```json
{
  "id": "0fe9c12390d6470cb632dd8423122782",
  "filename": "survey.kml",
  "status": "PENDING",
  "links": {
    "self": "http://localhost:8000/api/files/0fe9c12390d6470cb632dd8423122782/",
    "measurements": "http://localhost:8000/api/files/0fe9c12390d6470cb632dd8423122782/measurements/",
    "features": "http://localhost:8000/api/files/0fe9c12390d6470cb632dd8423122782/features/"
  }
}
```

The upload is checked **before** it is accepted, so a structurally broken file is rejected right away:

| Status | `error` | When |
|---|---|---|
| 413 | `FILE_TOO_LARGE` | Over `MAX_UPLOAD_MB` (read in chunks; aborts as soon as the limit is crossed) |
| 415 | `UNSUPPORTED_FILE_TYPE` | Extension is not `.zip`, `.kml` or `.kmz` |
| 422 | `INVALID_UPLOAD` | Corrupt zip, no `.shp`, missing `.shx`/`.dbf`, unsafe paths (zip-slip), zip bomb, empty file, not KML |

```json
{"error": "INVALID_UPLOAD", "message": "Shapefile 'parcels.shp' is incomplete; missing required part(s): .dbf."}
```

### `GET /api/files/{id}/`: file information

```bash
curl http://localhost:8000/api/files/0fe9c12390d6470cb632dd8423122782/
```

```json
{
  "id": "0fe9c12390d6470cb632dd8423122782",
  "filename": "survey.kml",
  "file_type": "KML",
  "size_bytes": 1329,
  "status": "COMPLETED",
  "error": null,
  "feature_count": 4,
  "crs": "EPSG:4326",
  "layers": [
    {"name": "Site survey",  "source": "upload.kml", "crs": "EPSG:4326", "crs_assumed": false, "feature_count": 1},
    {"name": "Access roads", "source": "upload.kml", "crs": "EPSG:4326", "crs_assumed": false, "feature_count": 1},
    {"name": "Wells",        "source": "upload.kml", "crs": "EPSG:4326", "crs_assumed": false, "feature_count": 1},
    {"name": "Needs review", "source": "upload.kml", "crs": "EPSG:4326", "crs_assumed": false, "feature_count": 1}
  ],
  "geometry_type_counts": {"Polygon": 2, "LineString": 1, "Point": 1},
  "measurement_status_counts": {"MEASURED": 3, "NOT_APPLICABLE": 1},
  "summary": {
    "area_m2": 791732.6678, "geodesic_area_m2": 791410.1586, "area_km2": 0.791733,
    "length_m": 2000.0, "geodesic_length_m": 1999.6907, "length_km": 2.0
  },
  "warnings": [],
  "created_at": "2026-10-11T08:02:28.942000Z",
  "processed_at": "2026-10-11T08:02:29.058000Z",
  "processing_ms": 113.8
}
```

`status` moves through `PENDING → PROCESSING → COMPLETED | FAILED`. On `FAILED`, `error` explains why (for example, a corrupt KML that GDAL cannot parse). `crs` is the source CRS, `MIXED` when layers differ, or `UNKNOWN` when it cannot be determined.

### `GET /api/files/{id}/measurements/`: measurements

| Query param | Example | Notes |
|---|---|---|
| `geometry_type` | `Polygon` | Filter by geometry type |
| `status` | `MEASURED` | Filter by measurement status |
| `include_geometry` | `false` | Leave out GeoJSON geometry for a lighter payload (default `true`) |
| `limit`, `offset` | `100`, `0` | Pagination (`limit` ≤ 1000) |

```bash
curl "http://localhost:8000/api/files/<id>/measurements/?limit=2"
```

```json
{
  "file_id": "0fe9c12390d6470cb632dd8423122782",
  "filename": "survey.kml",
  "crs": "EPSG:4326",
  "feature_count": 4,
  "summary": {"area_m2": 791732.6678, "length_m": 2000.0, "...": "..."},
  "total": 4,
  "limit": 2,
  "offset": 0,
  "items": [
    {
      "feature_index": 0,
      "layer": "Site survey",
      "layer_feature_index": 0,
      "geometry_type": "Polygon",
      "source_crs": "EPSG:4326",
      "properties": {"Name": "Survey boundary (25 ha)", "description": "Survey boundary (25 ha) desc"},
      "measurement": {
        "status": "MEASURED",
        "kind": "area",
        "unit": "m2",
        "value": 250000.0,
        "geodesic_value": 249922.0206,
        "difference_pct": 0.031202,
        "measurement_crs": "EPSG:32643",
        "method": "UTM",
        "area_m2": 250000.0,
        "area_km2": 0.25,
        "area_ha": 25.0
      },
      "warnings": [],
      "geometry": {"type": "Polygon", "coordinates": [[[77.1739, 28.6397], "..."]]},
      "geometry_crs": "EPSG:4326"
    },
    {
      "feature_index": 1,
      "layer": "Access roads",
      "geometry_type": "LineString",
      "measurement": {
        "status": "MEASURED", "kind": "length", "unit": "m",
        "value": 2000.0, "geodesic_value": 1999.6907, "difference_pct": 0.015469,
        "measurement_crs": "EPSG:32643", "method": "UTM",
        "length_m": 2000.0, "length_km": 2.0
      },
      "...": "..."
    }
  ]
}
```

Features that **can't be measured are still returned**, with a status that says why, instead of failing the request:

```json
{"geometry_type": "Point", "measurement": {"status": "NOT_APPLICABLE"}, "warnings": []}
```

```json
{
  "geometry_type": "Polygon",
  "properties": {"Name": "Self-intersecting plot"},
  "measurement": {"status": "MEASURED", "area_m2": 541732.6678, "measurement_crs": "EPSG:32643", "...": "..."},
  "warnings": ["Invalid geometry repaired before measuring (Self-intersection[77.305 28.705])."]
}
```

| `measurement.status` | Meaning |
|---|---|
| `MEASURED` | Area or length computed |
| `NOT_APPLICABLE` | Point geometry; no measurement required |
| `UNSUPPORTED` | Geometry type with no single meaningful measurement (e.g. GeometryCollection) |
| `EMPTY` | Feature has no geometry |
| `UNKNOWN_CRS` | No `.prj` and coordinates are not lon/lat, so measuring would be guesswork |
| `ERROR` | Unexpected failure for this feature only (e.g. a geometry that can't be repaired) |

The file isn't ready yet, or failed → `409 FILE_NOT_READY`. Unknown id → `404 FILE_NOT_FOUND`.

### `GET /api/files/{id}/features/`: GeoJSON

Returns an RFC 7946 FeatureCollection (`application/geo+json`, EPSG:4326). Measurements are flattened into properties (`_area_m2`, `_length_m`, `_measurement_status`, ...), so you can paste the output into [geojson.io](https://geojson.io) or drag it into QGIS to see what was measured.

The optional `bbox=minLon,minLat,maxLon,maxLat` returns only features that intersect the box. The query is served by MongoDB's `2dsphere` index:

```bash
curl "http://localhost:8000/api/files/<id>/features/?bbox=76.8,28.3,77.6,28.9"
```

### Other endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/files/?status=COMPLETED&limit=20&offset=0` | List uploads, newest first |
| `DELETE` | `/api/files/{id}/` | Delete the file record, its features and the stored upload (`204`) |
| `GET` | `/health` | `200 {"status":"ok","mongo":"up"}`, or `503` if MongoDB is unreachable |

---

## Architecture

### Application structure

```
app/
├── main.py                 App factory, lifespan (Mongo connect, indexes, recover stuck jobs), error handler
├── core/
│   ├── config.py           pydantic-settings configuration
│   ├── exceptions.py       Domain errors carrying an HTTP status (413/415/422/404/409)
│   └── logging.py
├── api/
│   ├── deps.py             Dependency injection (repositories, settings)
│   └── routes/             files.py (all /api/files endpoints), health.py
├── schemas/files.py        Pydantic response models (drive the OpenAPI docs)
├── db/mongo.py             AsyncMongoClient, GridFS bucket, index definitions
├── repositories/           Data access: files.py (metadata + GridFS blob), features.py
└── services/               Domain logic, no FastAPI or Mongo imports
    ├── upload.py           Type detection, zip/kmz inspection, safe extraction
    ├── readers.py          GDAL (pyogrio) → RawFeature per layer; CRS resolution
    ├── crs.py              Projection selection strategy
    ├── measurement.py      Per-feature area/length + geodesic cross-check
    └── processor.py        Pipeline orchestration and the background job
```

The layers depend in one direction only: **routes → repositories / services**. Services know nothing about HTTP or MongoDB, so the whole measurement pipeline is unit-tested without a database or web server.

### File-processing flow

```
Client                API (request)                       Background job (after 202)
  │  POST /api/files/   │                                        │
  ├────────────────────►│ 1. read upload in chunks (size cap)    │
  │                     │ 2. validate: extension, zip integrity, │
  │                     │    zip-slip, zip-bomb, .shp/.shx/.dbf  │
  │                     │ 3. store original in GridFS            │
  │                     │ 4. insert files doc  status=PENDING    │
  │◄────────────────────┤ 5. 202 + id + links ──── schedule ────►│
  │                     │                                        │ 6. status=PROCESSING
  │                     │                                        │ 7. load blob, extract to temp dir
  │                     │                                        │ 8. in a worker thread (GDAL is blocking):
  │                     │                                        │    for each dataset → each layer → each feature:
  │                     │                                        │      resolve CRS → measure → to GeoJSON (4326)
  │                     │                                        │ 9. bulk insert features (batches of 500)
  │  GET /{id}/ (poll)  │                                        │ 10. status=COMPLETED + summary
  ├────────────────────►│◄─────────── reads Mongo ───────────────┤     (or FAILED + error)
```

- **Shapefile zip**: every `.shp` in the archive (including subfolders) becomes a layer. `__MACOSX/` junk is ignored.
- **KML**: every folder becomes a layer (via `pyogrio.list_layers`). A **KMZ** is unzipped and its `doc.kml` is read. LIBKML's unset display fields (`visibility`, `drawOrder`, ...) are dropped so `properties` shows real attributes.
- **Failure isolation**: a broken feature gets its own status and warnings, and never fails the file. Only file-level problems (for example, GDAL can't open the dataset) mark the file `FAILED`.
- **Restart safety**: on startup, jobs left `PENDING` or `PROCESSING` by a crashed process are marked `FAILED`, so they don't sit in limbo forever.

### Measurement calculation flow

For each feature ([services/measurement.py](app/services/measurement.py)):

1. **Empty or missing geometry** → `EMPTY`.
2. **Drop Z** (2D, planimetric measurement). A warning is added only if any Z value is non-zero, since KML's `,0` altitudes are common and not interesting.
3. **Source CRS unknown** → `UNKNOWN_CRS`.
4. **Reproject to EPSG:4326** (vectorised `shapely.transform` + cached `pyproj.Transformer`). This copy is used for storage, GeoJSON output, the spatial index and the geodesic reference.
5. **Dispatch on geometry type**: Point → `NOT_APPLICABLE`; anything that isn't polygonal or linear → `UNSUPPORTED`.
6. **Invalid geometry** (for example, a self-intersecting polygon) → `shapely.make_valid`, keep only the polygonal parts, then measure, with a warning that names the reason and location.
7. **Densify** lon/lat edges longer than 0.01° (≈1.1 km). See [CRS handling](#crs-handling) for why.
8. **Choose a projection** ([services/crs.py](app/services/crs.py)), reproject, and take the planar `area` or `length`. This is the **primary value**.
9. **Geodesic cross-check**: `pyproj.Geod(ellps="WGS84")`, using Karney's algorithm. Polygons are oriented first (shell counter-clockwise, holes clockwise) so holes are subtracted correctly.
10. Report `value`, `geodesic_value`, `difference_pct`, `measurement_crs` and `method`. If the two values differ by more than 1%, a warning is added.

---

## CRS handling

The brief requires that area and distance are **not** computed from lat/lon degrees. Every feature goes through this decision:

```
                 source CRS projected with metre units?
                    │yes                         │no (geographic, or feet, etc.)
                    ▼                            ▼
        measure in the source CRS       feature fits in one UTM zone (≤ 6° wide)
        method = SOURCE_PROJECTED       and |lat| ≤ 84°?
                                           │yes                  │no
                                           ▼                     ▼
                                  WGS84 UTM zone of the     projection centred on the feature
                                  feature's centroid        area   → Lambert Azimuthal Equal-Area
                                  method = UTM              length → Azimuthal Equidistant
                                  e.g. EPSG:32643           method = LOCAL_EQUAL_AREA / LOCAL_EQUIDISTANT
```

**Why this strategy**
- **Respect a metric projected source.** When the data producer chose a projected CRS (for example, a national grid), that is the CRS their numbers are defined in. Re-projecting would only add distortion. Projected CRSs in **feet** are not trusted as-is; they go through the UTM path instead.
- **UTM per feature, not per file.** A file can hold features from several continents. Choosing the zone from each feature's own centroid keeps distortion small: at most ~0.1% for length and ~0.2% for area at a zone's edge, and much less near the central meridian (the sample data shows 0.015–0.03%). Zones are computed arithmetically (`zone = ⌊(lon+180)/6⌋+1`, north 326xx / south 327xx), which is deterministic and needs no database lookup.
- **Fallbacks where UTM breaks down.** UTM is undefined beyond 84° latitude and distorts quickly outside its 6° zone. For those features, a projection centred on the feature itself gives an equal-area result for areas, and true distances from the centre for lengths.
- **Densify before measuring.** In lon/lat data, edges are straight lines *in lon/lat space*. A 20°-wide box's top edge follows a parallel, not a great circle. Without densification, the projected and geodesic calculations interpret long edges differently: I measured a **0.9%** gap on a 20° × 10° box that was entirely due to edge interpretation. After densifying to 0.01°, the equal-area result matches the geodesic one to **< 0.001%**. The cross-check then measures projection error alone.
- **Missing `.prj`**: if every coordinate fits lon/lat ranges, EPSG:4326 is assumed and `crs_assumed: true` plus a warning are reported. Otherwise the CRS is `UNKNOWN` and measurements are skipped instead of guessed.
- **KML** is always WGS84 by specification.

**Output geometries** are always returned in EPSG:4326, as GeoJSON (RFC 7946) requires. Each feature reports `source_crs` (what the file used), `geometry_crs` (what the returned geometry is in) and `measurement.measurement_crs` (what the number was computed in).

---

## Design decisions

| Decision | Chosen | Alternatives considered | Why |
|---|---|---|---|
| Framework | **FastAPI** | Django + DRF | Async I/O fits upload + background processing; Pydantic models give validation and accurate OpenAPI docs for free; there's no ORM or admin we'd need from Django. |
| Database | **MongoDB** (PyMongo async) | PostgreSQL + PostGIS | Feature properties vary from file to file, and a document store holds them without schema migrations. MongoDB also has native GeoJSON + `2dsphere` indexing, which powers the `bbox` query. PostGIS would be the choice for heavy spatial SQL (see Future scope). PyMongo's native async client is used because Motor is deprecated. |
| Feature storage | **Separate `features` collection** | Embed features in the file document | MongoDB documents are capped at 16 MB, and a large shapefile would hit that. A separate collection also gives indexed filtering and pagination by `(file_id, feature_index)` and a spatial index. |
| Upload storage | **GridFS** | Local disk / S3 | Keeps MongoDB as the only dependency and lets the background job (or a future separate worker) reload the original file. S3 is the production upgrade. |
| Processing | **FastAPI `BackgroundTasks` + worker thread** | Synchronous; Celery/RQ + Redis | Returning 202 immediately with a `status` field matches the spec's `status` field and keeps large files from tying up the request. GDAL work runs in `asyncio.to_thread` so the event loop stays responsive. Celery would add Redis and a worker process, which is too much for this scope; the processor is a plain function, so moving it to a queue is straightforward. |
| GIS stack | **pyogrio + shapely 2 + pyproj** | geopandas, fiona, osgeo.gdal | pyogrio's raw API reads WKB and numpy arrays from GDAL without the pandas stack. All three ship wheels with GDAL/GEOS/PROJ bundled, so there's no painful system install. shapely 2 is vectorised. |
| Measurement | **Projected (primary) + geodesic (cross-check)** | Geodesic only; one global equal-area CRS (e.g. EPSG:6933) | The brief asks for projected measurement. A single global CRS distorts lengths and shapes in many places. Reporting both values makes accuracy visible instead of something you have to take on trust. |
| Bad data | **Per-feature status + warnings** | Fail the whole file | Real-world files often contain a few broken geometries. Users still get every measurable result, and the problems are clearly flagged. |
| Validation timing | **Before 202** (cheap structural checks) | Everything in the background | A missing `.dbf` or a zip-slip attempt should be an immediate 4xx, not a `FAILED` status the client has to poll for. |

---

## Testing

```bash
pytest -v                                   # all tests (API tests need MongoDB; skipped if absent)
pytest tests/unit -v                        # no database needed
ruff check . && ruff format --check .
```

68 tests. Fixtures are **generated in code** with pyogrio (real Shapefiles and KML), so there are no opaque binary fixtures.

- **Known-answer measurement tests**: a 1 km² square built in UTM is measured as 1,000,000 m² from lon/lat input; 1° along the equator is 111,319.49 m geodesic; a polygon with a hole equals 75% of the full polygon; MultiPolygon totals.
- **CRS selection**: correct UTM zone for Delhi, Sydney (southern hemisphere), London, New York, and the antimeridian; projected metric source kept; feet-based CRS not trusted; polar and wide fallbacks.
- **Edge cases**: antimeridian crossing, self-intersecting polygon, empty geometry, GeometryCollection, Z coordinates, missing `.prj` (lon/lat vs projected coordinates), multiple shapefiles in one zip, KML folders, KMZ.
- **Upload safety**: zip-slip (`../`, absolute, drive-letter paths), zip bomb (small on the wire, 6 MB+ unpacked), missing `.shx`/`.dbf`, wrong extension, fake KML, oversize file.
- **API integration**: upload → poll → measurements; filters and pagination; GeoJSON + `bbox` spatial query; 404/409/415/422 errors; list and delete. Each run uses a throwaway database.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, the format check and the full suite against a MongoDB service container, then builds the Docker image.

---

## Assumptions and limitations

- Measurements are **2D**: Z is ignored, so slopes aren't accounted for (planimetric area and length, which is standard for cadastral and survey work).
- **Antimeridian-crossing** features are *measured* correctly: longitudes are unwrapped (+360°) before measuring, which otherwise would treat a 0.2° box as a band around the globe and overstate its area ~1,600×. The *returned GeoJSON* is not split at ±180° as RFC 7946 §3.1.9 recommends.
- Each feature's measurement comes from a single projection; very large features (country-sized) are best judged by `geodesic_value`. The API warns when the two differ by more than 1%.
- `BackgroundTasks` runs in the API process. A crash mid-job leaves the file `FAILED` on restart (it isn't retried), and processing doesn't scale beyond one process.
- The whole file is held in memory during processing; this is fine within the 50 MB default limit.
- No authentication or rate limiting (out of scope for the brief).

---

## Learnings

- **"Projected" isn't a single answer.** The choice of projection is the real design decision. UTM per feature is accurate for most data, but it breaks down for wide or polar features, and knowing *where* each method fails mattered more than picking one.
- **Edge interpretation is a hidden source of error.** My first cross-check showed a 0.9% gap on a large polygon. The cause wasn't projection distortion: straight lon/lat edges and great-circle edges simply enclose different areas. Densifying fixed it and made the cross-check meaningful.
- **The antimeridian is a real trap.** A 246 km² box straddling 180° first measured as ~406,000 km², because in lon/lat it looks 359.8° wide. Unwrapping longitudes before measuring fixed it; a test now locks this in.
- **Hole orientation matters for geodesic area.** `pyproj.Geod` sums signed ring areas, so polygons must be oriented (shell counter-clockwise, holes clockwise) or holes get *added* instead of subtracted.
- **MongoDB's spherical validity is stricter than OGC validity.** A geometry shapely accepts can still be rejected by the `2dsphere` index. The repository catches the geo-key error (code 16755) and re-inserts the feature without its index key, so one odd geometry can't fail a bulk insert.
- **KML through LIBKML** adds a dozen presentation fields to every placemark, and coordinates usually carry `,0` altitude. Both needed filtering so the output reflects the user's data, not driver details.
- **Zip uploads are an attack surface**: zip-slip and decompression bombs are easy to miss and cheap to guard against.

---

## Future scope

- **Job queue** (Celery / Arq / RQ with Redis) for retries, horizontal scaling and progress reporting (`processed_features / total`).
- **Streaming processing** for very large files: read and insert in chunks instead of holding all features in memory.
- **Object storage** (S3 / MinIO) for uploads instead of GridFS, with pre-signed uploads for large files.
- **More formats**: GeoJSON, GeoPackage, FlatGeobuf, GPX. Most of the work is a change in `readers.py`, since GDAL already supports them.
- **Spatial query API** built on the existing `2dsphere` index: `near`, `within` a polygon, cross-file search.
- **Antimeridian output**: split GeoJSON geometries at ±180° (measurement already handles it).
- **Perimeter for polygons**, 3D surface area or length using Z, and user-selectable units or target CRS (`?crs=EPSG:27700`).
- **Auth, rate limiting, and per-user ownership** of files; idempotent uploads by content hash.
- **Observability**: Prometheus metrics (processing time, features per second, failure rate) and OpenTelemetry traces.
- **A small Leaflet viewer** that renders `/features/` with measurement popups.
- **PostGIS backend option** for workloads that need spatial joins or analytics in SQL.
