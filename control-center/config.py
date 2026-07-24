"""PFS Control Center — configuration.

Its own settings, independent of any ERP. The Control Plane never shares a database,
secret, or auth realm with a Data-Plane ERP (ADR-021).
"""
import os


class Settings:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///./control_center.db")
    jwt_secret = os.environ.get("JWT_SECRET", "dev-control-center-secret-change-me")
    jwt_alg = "HS256"
    jwt_expire_minutes = int(os.environ.get("JWT_EXPIRE_MINUTES", "720"))
    seed_on_start = os.environ.get("SEED_ON_START", "true").lower() == "true"
    seed_password = os.environ.get("SEED_PASSWORD", "changeme-owner")


settings = Settings()
