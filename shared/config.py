from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://redis:6379/0"
    queue_name: str = "scrape_jobs"

    # minimum seconds between scrape requests made by the worker
    polite_delay_seconds: float = 3.0

    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
