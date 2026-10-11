from fastapi import Request

from app.core.config import Settings, get_settings
from app.db.mongo import FEATURES, FILES, Mongo
from app.repositories.features import FeaturesRepository
from app.repositories.files import FilesRepository


def get_mongo(request: Request) -> Mongo:
    return request.app.state.mongo


def get_files_repo(request: Request) -> FilesRepository:
    mongo = get_mongo(request)
    return FilesRepository(mongo.db[FILES], mongo.bucket)


def get_features_repo(request: Request) -> FeaturesRepository:
    return FeaturesRepository(get_mongo(request).db[FEATURES])


def settings_dep() -> Settings:
    return get_settings()
