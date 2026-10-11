import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.routes import files, health
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging
from app.db.mongo import FILES, Mongo
from app.repositories.files import FilesRepository

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    mongo = Mongo(settings.mongo_uri, settings.mongo_db)
    await mongo.init_indexes()
    interrupted = await FilesRepository(mongo.db[FILES], mongo.bucket).fail_interrupted()
    if interrupted:
        logger.warning("Marked %d interrupted job(s) as FAILED", interrupted)
    app.state.mongo = mongo
    yield
    await mongo.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "Upload a zipped Shapefile or a KML file; every feature is extracted and measured "
            "(polygon area, line length) in an appropriate projected CRS, with a geodesic "
            "cross-check."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.code, "message": exc.message}, status_code=exc.status_code
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    app.include_router(health.router)
    app.include_router(files.router)
    return app


app = create_app()
