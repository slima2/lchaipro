"""Case Service configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    service_name: str = "case-service"
    version: str = "0.1.0"
    log_level: str = "INFO"

    postgres_dsn: str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)
    postgres_dsn_sync: str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)
    rabbitmq_url: str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)
    otel_exporter_otlp_endpoint: str | None = None

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
