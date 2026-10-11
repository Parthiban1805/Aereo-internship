from __future__ import annotations

import logging

from gridfs import AsyncGridFSBucket
from pymongo import ASCENDING, GEOSPHERE, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger(__name__)

FILES = "files"
FEATURES = "features"
BLOB_BUCKET = "uploads"


class Mongo:
    """Owns the client for the application's lifetime."""

    def __init__(self, uri: str, db_name: str):
        self.client: AsyncMongoClient = AsyncMongoClient(
            uri, tz_aware=True, uuidRepresentation="standard"
        )
        self.db: AsyncDatabase = self.client[db_name]
        self.bucket = AsyncGridFSBucket(self.db, bucket_name=BLOB_BUCKET)

    async def init_indexes(self) -> None:
        await self.db[FILES].create_index([("created_at", ASCENDING)])
        await self.db[FILES].create_index([("status", ASCENDING)])
        await self.db[FEATURES].create_index(
            [("file_id", ASCENDING), ("feature_index", ASCENDING)], unique=True
        )
        await self.db[FEATURES].create_index([("file_id", ASCENDING), ("geometry_type", ASCENDING)])
        # 2dsphere indexes are sparse: features without a valid `location` are skipped.
        await self.db[FEATURES].create_index([("location", GEOSPHERE)])
        logger.info("MongoDB indexes ensured")

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.close()
