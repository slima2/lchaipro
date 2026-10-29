"""Active Learning Service configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    service_name: str = "active-learning-service"
    version: str = "0.1.0"
    log_level: str = "INFO"

    postgres_dsn: str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)
    postgres_dsn_sync: str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)

    s3_endpoint: str = "http://minio:9000"
    s3_access_key: str = ""  # la entrega el Secret; vacia = credenciales del entorno (IAM)
    s3_secret_key: str = ""  # la entrega el Secret; vacia = credenciales del entorno (IAM)
    s3_bucket: str = "oncology-xai"

    inference_service_url: str = "http://inference-service:8003"
    rabbitmq_url: str  # obligatoria: la entrega el Secret (sin clave por defecto en el codigo)

    delta_train_epochs: int = 10
    delta_train_lr: float = 1e-5
    delta_train_buffer_ratio: float = 0.3

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
