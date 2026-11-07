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
    # Login OIDC (Auth0, Google...): opcional. Sem issuer/cliente fica desligado.
    oidc_issuer: str = ""  # por ex. https://O-SEU-TENANT.eu.auth0.com  (tem de ser https)
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    admin_emails: str = ""  # emails que entram logo como Admin ativos (separados por vírgulas); os outros ficam pendentes
    oidc_allowed_emails: str = ""  # DESCONTINUADO: tratado como ADMIN_EMAILS (para não perder o acesso ao migrar)
    oidc_name: str = "SSO"  # texto do botão de login (por ex. Auth0)
    oidc_scopes: str = "openid email profile"
    oidc_require_verified_email: bool = True  # com inscrições abertas, exigir sempre o email verificado
    auth_redirect_uri: str = ""  # vazio = deduzido do Host (ex.: http://lambdacore/power/auth/callback)
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
