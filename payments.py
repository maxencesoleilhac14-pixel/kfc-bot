"""
payments.py — Gestion des paiements PayPal et Crypto

PayPal : l'user envoie un screenshot → l'admin valide ou refuse.
Crypto : génération d'adresse via API, confirmation automatique par polling.
"""

import asyncio
import logging
import os
import random
import string
from typing import Optional, Dict, Any

from config import PAYPAL_LINK, CRYPTO_API_KEY, ADMIN_ID
from database import (
    create_deposit,
    update_deposit_status,
    credit_user,
    create_crypto_invoice,
    get_crypto_invoice,
    get_pending_crypto_invoices,
    confirm_crypto_invoice,
)

logger = logging.getLogger("payments")


# ── PayPal ─────────────────────────────────────────────────────────────────

def generer_message_paypal(amount: float) -> str:
    """Génère le message à afficher pour un dépôt PayPal."""
    return (
        f"💸 *Paiement PayPal*\n\n"
        f"Envoie **{amount:.2f}€** en paiement\n"
        f"👥 *Ami proche* — *SANS NOTE* →\n"
        f"[Clique ici pour payer]({PAYPAL_LINK})\n\n"
        f"📌 *Obligatoire :*\n"
        f"✅ Paiement Ami proche uniquement\n"
        f"✅ Aucune note dans le paiement\n"
        f"✅ Envoie le reçu juste après\n\n"
        f"Une fois payé, envoie la 📸 *capture d'écran* ici."
    )


async def notifier_admin_paypal(context, telegram_id: int, amount: float,
                                deposit_id: int, photo_bytes: bytes) -> int:
    """Envoie la demande de dépôt à l'admin, retourne l'ID du message."""
    caption = (
        f"🟡 *DEMANDE DE DÉPÔT PAYPAL*\n\n"
        f"👤 User : `{telegram_id}`\n"
        f"💰 Montant : **{amount:.2f}€**\n"
        f"📝 Dépôt #{deposit_id}"
    )
    msg = await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo_bytes,
        caption=caption,
        parse_mode="Markdown",
    )
    return msg.message_id


def generer_boutons_admin(deposit_id: int):
    """Boutons de validation/refus pour l'admin."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    import json
    keyboard = [
        [
            InlineKeyboardButton("✅ Valider", callback_data=json.dumps(
                {"a": "admin_confirm_deposit", "d": deposit_id}, separators=(",", ":"))),
            InlineKeyboardButton("❌ Refuser", callback_data=json.dumps(
                {"a": "admin_refuse_deposit", "d": deposit_id}, separators=(",", ":"))),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ── Crypto ─────────────────────────────────────────────────────────────────

def generer_adresse_crypto(montant_eur: float, currency: str = "USDT") -> Optional[str]:
    """
    Génère une adresse de paiement via l'API crypto.
    Retourne l'adresse ou None si échec.
    À adapter selon l'API utilisée (NowPayments, Coinbase, etc.).
    """
    if not CRYPTO_API_KEY:
        logger.warning("CRYPTO_API_KEY non configurée, utilisation d'une adresse de démonstration")
        # Adresse de démo USDT TRC20
        return "TXYZ123456789DemoAddressForTesting"
    try:
        # Simulation d'appel API — à remplacer par l'API réelle
        chars = string.ascii_letters + string.digits
        adresse = "T" + "".join(random.choices(chars, k=32))
        logger.info("Adresse crypto générée pour %.2f %s: %s", montant_eur, currency, adresse)
        return adresse
    except Exception as e:
        logger.exception("Erreur génération adresse crypto: %s", e)
        return None


async def verifier_paiements_crypto(context) -> None:
    """
    Vérifie les paiements crypto en attente.
    À appeler périodiquement (ex: toutes les 30s).
    """
    invoices = get_pending_crypto_invoices()
    if not invoices:
        return

    for inv in invoices:
        try:
            confirmee = await _check_single_payment(inv)
            if confirmee:
                deposit = get_deposit(inv["deposit_id"])
                if deposit:
                    nouveau_solde = credit_user(deposit["telegram_id"], deposit["amount"])
                    update_deposit_status(deposit["id"], "confirmed")
                    try:
                        await context.bot.send_message(
                            chat_id=deposit["telegram_id"],
                            text=(
                                f"✅ *Paiement crypto confirmé !*\n\n"
                                f"{deposit['amount']:.2f}€ ajouté à ton solde.\n"
                                f"💰 Nouveau solde : **{nouveau_solde:.2f}€**"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.exception("Erreur notification user crypto")
        except Exception as e:
            logger.exception("Erreur vérification crypto invoice %s: %s", inv["id"], e)


async def _check_single_payment(invoice: Dict[str, Any]) -> bool:
    """
    Vérifie si un paiement a été reçu pour une facture donnée.
    À remplacer par l'appel API réel.
    Retourne True si confirmé.
    """
    # Simulation : pas de vérification réelle sans API
    # Dans la vraie vie : appeler l'API crypto (NowPayments, Coinbase, etc.)
    await asyncio.sleep(1)
    return False
