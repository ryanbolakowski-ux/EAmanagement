from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    APP_NAME: str = "Theta Algos"
    APP_ENV: str = "development"
    SECRET_KEY: str = "changeme-at-least-32-characters-long!!"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str = "postgresql+asyncpg://edge_user:edge_pass@localhost:5432/edge_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # Tradovate
    TRADOVATE_USERNAME: str = ""
    TRADOVATE_PASSWORD: str = ""
    TRADOVATE_APP_ID: str = ""
    TRADOVATE_APP_VERSION: str = "1.0"
    TRADOVATE_CID: str = ""
    TRADOVATE_SEC: str = ""
    TRADOVATE_DEMO: bool = True

    # Market data
    POLYGON_API_KEY: str = ""
    IPQS_API_KEY: str = ""
    EMAIL_KILL_SWITCH: str = "0"
    GEO_BLOCK_ENABLED: str = "1"
    IPQS_FRAUD_THRESHOLD: int = 85

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # Email (Resend)
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "onboarding@resend.dev"
    FRONTEND_URL: str = "https://thetaalgos.com"

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
