"""EHR Service configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    service_name: str = "ehr-service"
    version: str = "0.1.0"
    log_level: str = "INFO"

    postgres_dsn: (
        str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)
    )
    rabbitmq_url: (
        str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)
    )
    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = (
        ""  # la entrega el Secret; vacia = credenciales del entorno (IAM)
    )
    s3_secret_key: str = (
        ""  # la entrega el Secret; vacia = credenciales del entorno (IAM)
    )
    s3_bucket: str = "oncology-xai"
    fuseki_url: str = "http://fuseki:3030"
    fuseki_dataset: str = "oncology"
    llm_provider: str = "mock"
    otel_exporter_otlp_endpoint: str | None = None

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
