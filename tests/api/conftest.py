import asyncio
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from pymongo import MongoClient

from app.db.mongo import Mongo
from app.main import create_app

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")


def _mongo_available() -> bool:
    try:
        MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.integration

if not _mongo_available():
    pytest.skip(f"MongoDB not reachable at {MONGO_URI}", allow_module_level=True)


@pytest_asyncio.fixture(scope="session")
async def client():
    db_name = f"geo_measure_test_{uuid.uuid4().hex[:8]}"
    mongo = Mongo(MONGO_URI, db_name)
    await mongo.init_indexes()

    app = create_app()
    app.state.mongo = mongo  # httpx's ASGI transport does not run the lifespan
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await mongo.client.drop_database(db_name)
    await mongo.close()


@pytest_asyncio.fixture(scope="session")
async def upload(client):
    async def _upload(filename: str, data: bytes, *, wait: bool = True) -> dict:
        resp = await client.post("/api/files/", files={"file": (filename, data)})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        if not wait:
            return body
        for _ in range(100):
            info = (await client.get(f"/api/files/{body['id']}/")).json()
            if info["status"] in ("COMPLETED", "FAILED"):
                return info
            await asyncio.sleep(0.05)
        raise AssertionError("processing did not finish in time")

    return _upload
