import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv()

MODEL_NAME = os.getenv("MODEL_NAME", "claude-haiku-5-5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "1500"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "500"))
DAILY_TOKEN_LIMIT_PER_USER = int(os.getenv("DAILY_TOKEN_LIMIT_PER_USER", "60000"))

# Haiku-class pricing (USD per million tokens) used only for the admin cost estimate.
PRICE_IN_PER_M = float(os.getenv("PRICE_IN_PER_M", "1.0"))
PRICE_OUT_PER_M = float(os.getenv("PRICE_OUT_PER_M", "5.0"))

STORE_NAME = os.getenv("STORE_NAME", "Bloom & Brew")


def raw_database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return bool(raw_database_url())


def sqlalchemy_url() -> str:
    url = raw_database_url()
    if not url:
        return f"sqlite:///{ROOT / 'shopbot.db'}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def psycopg_url() -> str:
    url = raw_database_url()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url
