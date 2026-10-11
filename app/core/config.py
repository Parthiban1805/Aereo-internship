from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, overridable via environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Geospatial File Measurement API"
    log_level: str = "INFO"

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "geo_measure"

    # Upload limits
    max_upload_mb: int = 50
    max_unzipped_mb: int = 500
    max_zip_entries: int = 1000

    # Processing
    insert_batch_size: int = 500
    # Features spanning more longitude than this are measured with a feature-centred
    # equal-area / equidistant projection instead of a single UTM zone.
    utm_max_span_deg: float = 6.0

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_unzipped_bytes(self) -> int:
        return self.max_unzipped_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
