"""
config.py — Configuration centralisée du bot O'KFC

Tous les paramètres sensibles sont chargés depuis .env (via python-dotenv).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_ICI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ── Bridge Telethon ───────────────────────────────────────────────────────
BRIDGE_ENABLED = os.getenv("BRIDGE_ENABLED", "true").lower() == "true"

# ── Prix ──────────────────────────────────────────────────────────────────
# Majoration fixe appliquée à tous les prix affichés
PRICE_INCREASE = float(os.getenv("PRICE_INCREASE", "1.50"))

# ── Paiements ─────────────────────────────────────────────────────────────
PAYPAL_LINK = os.getenv("PAYPAL_LINK", "paypal.me/tonlien")
CRYPTO_API_KEY = os.getenv("CRYPTO_API_KEY", "")

# ── Image par défaut (logo personnalisé) ──────────────────────────────────
DEFAULT_IMAGE_URL = None  # chargé au runtime depuis le fichier local

# ── Liens ─────────────────────────────────────────────────────────────────
CANAL_URL = "https://t.me/ton_canal"

# ── API KFC (pour compatibilité, non utilisée directement) ────────────────
B2B_USERNAME = os.getenv("B2B_USERNAME", "")
B2B_PASSWORD = os.getenv("B2B_PASSWORD", "")
B2B_BASE_URL = os.getenv("KFC_BASE_URL", "")
CC_USERNAME = os.getenv("CC_USERNAME", "")
CC_PASSWORD = os.getenv("CC_PASSWORD", "")
CC_BASE_URL = os.getenv("KFC_CC_URL", "")
API_VERSION = os.getenv("KFC_API_VERSION", "v3")
POINTS_PER_EURO = 400  # non utilisé dans la nouvelle version
