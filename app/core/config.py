from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "OLeon Ask Python API V1"
    env: str = "dev"
    port: int = 8000
    cors_origins: str = "*"

settings = Settings()
