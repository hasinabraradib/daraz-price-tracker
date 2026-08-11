from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://redis:6379/0"
    queue_name: str = "scrape_jobs"

    # minimum seconds between scrape requests made by the worker
    polite_delay_seconds: float = 3.0

    # retry/backoff for failed scrape jobs — see shared/retry.py
    retry_base_delay_seconds: float = 5.0
    retry_backoff_factor: float = 2.0
    retry_max_delay_seconds: float = 600.0
    retry_max_attempts: int = 5

    # SMTP for AlertRule(channel="email") — defaults point at the local
    # Mailhog service (docker-compose.yml) so no real email is needed for
    # dev/tests. For real delivery (e.g. Gmail), set smtp_username/password
    # and smtp_use_tls=true.
    smtp_host: str = "mailhog"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = False
    alert_from_email: str = "alerts@daraz-price-tracker.local"

    # Convenience slot for your real Discord webhook URL (paste it into
    # .env yourself — never commit a real value). Not read by the notifier
    # itself: an AlertRule's own `destination` is what's actually POSTed
    # to; this is just a config-level place to keep the secret for
    # scripts/tests that need to create a rule pointing at it.
    discord_webhook_url: str | None = None

    # alert dedup — see shared/alerts.py's module docstring for the full
    # strategy this backs
    alert_material_change_pct: float = 5.0
    alert_cooldown_minutes: int = 60

    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
