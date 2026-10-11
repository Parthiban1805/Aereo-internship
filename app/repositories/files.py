from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timezone
from typing import Any

from gridfs import AsyncGridFSBucket
from pymongo import DESCENDING, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection


class FilesRepository:
    def __init__(self, collection: AsyncCollection, bucket: AsyncGridFSBucket):
        self.col = collection
        self.bucket = bucket

    async def create(self, filename: str, file_type: str, data: bytes) -> dict[str, Any]:
        file_id = uuid.uuid4().hex
        blob_id = await self.bucket.upload_from_stream(
            filename, data, metadata={"file_id": file_id}
        )
        doc = {
            "_id": file_id,
            "filename": filename,
            "file_type": file_type,
            "size_bytes": len(data),
            "blob_id": blob_id,
            "status": "PENDING",
            "error": None,
            "feature_count": None,
            "crs": None,
            "created_at": datetime.now(timezone.utc),
            "processed_at": None,
        }
        await self.col.insert_one(doc)
        return doc

    async def get(self, file_id: str) -> dict[str, Any] | None:
        return await self.col.find_one({"_id": file_id})

    async def list(
        self, *, status: str | None, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        query = {"status": status} if status else {}
        total = await self.col.count_documents(query)
        cursor = self.col.find(query).sort("created_at", DESCENDING).skip(offset).limit(limit)
        return await cursor.to_list(length=limit), total

    async def read_blob(self, blob_id) -> bytes:
        stream = await self.bucket.open_download_stream(blob_id)
        return await stream.read()

    async def mark_processing(self, file_id: str) -> dict[str, Any] | None:
        return await self.col.find_one_and_update(
            {"_id": file_id},
            {"$set": {"status": "PROCESSING", "started_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def mark_completed(self, file_id: str, fields: dict[str, Any]) -> None:
        await self.col.update_one(
            {"_id": file_id}, {"$set": {**fields, "status": "COMPLETED", "error": None}}
        )

    async def mark_failed(self, file_id: str, error: str) -> None:
        await self.col.update_one(
            {"_id": file_id},
            {
                "$set": {
                    "status": "FAILED",
                    "error": error,
                    "processed_at": datetime.now(timezone.utc),
                }
            },
        )

    async def fail_interrupted(self) -> int:
        """On startup, jobs left PENDING/PROCESSING by a previous process can never finish."""
        result = await self.col.update_many(
            {"status": {"$in": ["PENDING", "PROCESSING"]}},
            {"$set": {"status": "FAILED", "error": "Processing interrupted by a server restart."}},
        )
        return result.modified_count

    async def delete(self, file_id: str) -> bool:
        doc = await self.col.find_one_and_delete({"_id": file_id})
        if doc is None:
            return False
        if doc.get("blob_id") is not None:
            # A missing blob is fine; deleting the record is what matters.
            with contextlib.suppress(Exception):
                await self.bucket.delete(doc["blob_id"])
        return True
