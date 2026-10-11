from __future__ import annotations

import logging
from typing import Any

from pymongo import ASCENDING
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import BulkWriteError

logger = logging.getLogger(__name__)

# MongoDB error code for "Can't extract geo keys" (geometry rejected by the 2dsphere index)
GEO_KEY_ERROR = 16755


class FeaturesRepository:
    def __init__(self, collection: AsyncCollection):
        self.col = collection

    async def replace_for_file(
        self, file_id: str, features: list[dict[str, Any]], batch_size: int
    ) -> None:
        await self.col.delete_many({"file_id": file_id})
        for start in range(0, len(features), batch_size):
            batch = [{**f, "file_id": file_id} for f in features[start : start + batch_size]]
            await self._insert_batch(batch)

    async def _insert_batch(self, batch: list[dict[str, Any]]) -> None:
        try:
            await self.col.insert_many(batch, ordered=False)
        except BulkWriteError as exc:
            # MongoDB's spherical geometry rules are stricter than OGC validity (e.g. it
            # rejects some degenerate edges). Keep the feature, just drop it from the index.
            failed = [e for e in exc.details.get("writeErrors", []) if e["code"] == GEO_KEY_ERROR]
            other = [e for e in exc.details.get("writeErrors", []) if e["code"] != GEO_KEY_ERROR]
            if other:
                raise
            retry = []
            for err in failed:
                doc = dict(batch[err["index"]])
                doc.pop("location", None)
                doc.pop("_id", None)
                doc["warnings"] = [
                    *doc.get("warnings", []),
                    "Geometry not accepted by MongoDB's spherical index; excluded from "
                    "spatial queries.",
                ]
                retry.append(doc)
            logger.info("Re-inserting %d features without spatial index key", len(retry))
            await self.col.insert_many(retry, ordered=False)

    async def list_for_file(
        self,
        file_id: str,
        *,
        geometry_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        projection: dict[str, int] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        query: dict[str, Any] = {"file_id": file_id}
        if geometry_type:
            query["geometry_type"] = geometry_type
        if status:
            query["measurement.status"] = status
        total = await self.col.count_documents(query)
        cursor = (
            self.col.find(query, projection)
            .sort("feature_index", ASCENDING)
            .skip(offset)
            .limit(limit)
        )
        return await cursor.to_list(length=limit), total

    async def intersecting(
        self,
        file_id: str,
        bbox: tuple[float, float, float, float],
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        minx, miny, maxx, maxy = bbox
        box = {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        }
        cursor = (
            self.col.find(
                {"file_id": file_id, "location": {"$geoIntersects": {"$geometry": box}}},
                {"_id": 0, "location": 0},
            )
            .sort("feature_index", ASCENDING)
            .skip(offset)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def delete_for_file(self, file_id: str) -> None:
        await self.col.delete_many({"file_id": file_id})
