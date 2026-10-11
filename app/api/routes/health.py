from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_mongo
from app.db.mongo import Mongo

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness and MongoDB connectivity")
async def health(mongo: Mongo = Depends(get_mongo)) -> JSONResponse:
    ok = await mongo.ping()
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "mongo": "up" if ok else "down"},
        status_code=200 if ok else 503,
    )
