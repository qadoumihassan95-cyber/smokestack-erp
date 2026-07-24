"""PFS Control Center — configuration.

Its own settings, independent of any ERP. The Control Plane never shares a database,
secret, or auth realm with a Data-Plane ERP (ADR-021).
"""
import os


class Settings:
    environment = os.environ.get("ENVIRONMENT", "development")
    database_url = os.environ.get("DATABASE_URL", "sqlite:///./control_center.db")
    jwt_secret = os.environ.get("JWT_SECRET", "dev-control-center-secret-change-me")
    jwt_alg = "HS256"
    jwt_expire_minutes = int(os.environ.get("JWT_EXPIRE_MINUTES", "720"))
    seed_on_start = os.environ.get("SEED_ON_START", "true").lower() == "true"
    seed_email = os.environ.get("SEED_EMAIL", "owner@pfs.local")
    seed_password = os.environ.get("SEED_PASSWORD", "changeme-owner")
    # Public base URL of this Control Center (used for absolute links; never for ERP auth).
    control_center_base_url = os.environ.get("CONTROL_CENTER_BASE_URL", "")
    # Default support-session lifetime (minutes): short-lived by design (ADR-025).
    support_session_minutes = int(os.environ.get("SUPPORT_SESSION_MINUTES", "30"))


settings = Settings()
