import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.environ.get("ENV_FILE", ".env"), extra="ignore")
    tuya_access_id: str = ""
    tuya_access_secret: str = ""
    tuya_endpoint: str = "https://openapi.tuyaeu.com"
    # Palavra-passe (HTTP Basic): o acesso pela rede local. Vazia = desligada.
    web_user: str = "admin"
    web_password: str = ""
    # Autenticação Google OAuth2 (opcional; exige HTTPS externo). Sem client_id/secret, só a LAN confiável entra.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_allowed_emails: str = ""  # separados por vírgulas; vazio = ninguém
    auth_redirect_uri: str = ""  # por ex. https://jtx.ddns.net/power/auth/callback
    secret_key: str = ""  # cookie de sessão; vazio = gerado em <pasta de dados>/.session_secret
    web_prefix: str = ""  # por ex. /power (caminho do cookie de sessão)
    lan_trusted: bool = False  # True: pedidos da rede local em HTTP dispensam login
    lan_hosts: str = "lambdacore,lambdacore.lan,localhost,127.0.0.1"
    db_path: str = "data/tuya.db"
    sync_hour: int = 3
    sync_in_app: bool = True  # False se a sincronização for feita por cron
    sync_interval_minutes: int = 0  # >0: sincronizar de N em N minutos (por ex. 60) em vez de 1x por dia
    sync_initial_days: int = 7


settings = Settings()
