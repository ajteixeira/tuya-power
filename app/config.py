import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.environ.get("ENV_FILE", ".env"), extra="ignore")
    tuya_access_id: str = ""
    tuya_access_secret: str = ""
    tuya_endpoint: str = "https://openapi.tuyaeu.com"
    web_user: str = "admin"
    web_password: str = ""
    db_path: str = "data/tuya.db"
    sync_hour: int = 3
    sync_in_app: bool = True  # False se a sincronização for feita por cron
    sync_interval_minutes: int = 0  # >0: sincronizar de N em N minutos (por ex. 60) em vez de 1x por dia
    sync_initial_days: int = 7


settings = Settings()
