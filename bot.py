"""
bot.py — Bot O'KFC avec solde interne, mirror @chikenuhq_bot, et paiements

Fonctionnalités :
  - Solde interne (bot_balance) créé automatiquement au /start
  - Prix majorés de +1.50€
  - Mirror temps réel via Telethon (sauf bouton Valider la commande)
  - Dépôts PayPal (validation admin) et Crypto (auto)
  - Panneau admin : stats, créditer, débiter, voir solde
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    ADMIN_ID,
    BRIDGE_ENABLED,
    CANAL_URL,
    DEFAULT_IMAGE_URL,
    PRICE_INCREASE,
    TELEGRAM_TOKEN,
)
from database import (
    get_or_create_user,
    update_balance,
    credit_user,
    debit_user,
    create_deposit,
    update_deposit_status,
    get_deposit,
    get_user,
)
from bridge import bridge as telethon_bridge
from mirror import mirror, MirrorError, MirrorBusyError
from payments import (
    generer_message_paypal,
    notifier_admin_paypal,
    generer_boutons_admin,
    generer_adresse_crypto,
    verifier_paiements_crypto,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kfc_bot")

# Chargement du logo personnalisé
logo_path = "C:\\Users\\Shadow\\Desktop\\images0101.jpg"
if os.path.exists(logo_path):
    with open(logo_path, "rb") as f:
        DEFAULT_IMAGE_URL = f.read()
    logger.info("Logo chargé (%d octets)", len(DEFAULT_IMAGE_URL))
else:
    DEFAULT_IMAGE_URL = "https://1000logos.net/wp-content/uploads/2023/04/KFC-Logo-2018.png"

# ── État utilisateur ───────────────────────────────────────────────────
user_flows: Dict[int, Dict[str, Any]] = {}

def get_flow(user_id: int) -> Dict[str, Any]:
    if user_id not in user_flows:
        user_flows[user_id] = {
            "view": "main",
            "bridge_resto_id": None,
            "bridge_results": [],
            "bridge_result_page": 0,
            "last_msg_id": None,
        }
    return user_flows[user_id]

# ── Helpers ────────────────────────────────────────────────────────────

def _cb(action: str, **kw) -> str:
    return json.dumps({"a": action, **kw}, separators=(",", ":"))

def _parse_cb(data: str):
    obj = json.loads(data)
    return obj.pop("a"), obj

async def edit_media(update, context, photo_url, caption, reply_markup=None, parse_mode="Markdown"):
    chat_id = update.effective_chat.id
    flow = get_flow(chat_id)
    last_id = flow.get("last_msg_id")

    if update.callback_query:
        last_id = update.callback_query.message.message_id

    if last_id:
        try:
            await context.bot.edit_message_media(
                chat_id=chat_id, message_id=last_id,
                media=InputMediaPhoto(media=photo_url, caption=caption, parse_mode=parse_mode),
                reply_markup=reply_markup,
            )
            flow["last_msg_id"] = last_id
            return
        except Exception:
            try:
                await context.bot.edit_message_caption(
                    chat_id=chat_id, message_id=last_id,
                    caption=caption, reply_markup=reply_markup, parse_mode=parse_mode,
                )
                flow["last_msg_id"] = last_id
                return
            except Exception:
                pass

    sent = await context.bot.send_photo(
        chat_id=chat_id,
        photo=photo_url, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode,
    )
    flow["last_msg_id"] = sent.message_id

async def edit_caption(update, context, caption, reply_markup=None, parse_mode="Markdown"):
    chat_id = update.effective_chat.id
    flow = get_flow(chat_id)
    last_id = flow.get("last_msg_id")

    if update.callback_query:
        last_id = update.callback_query.message.message_id

    if last_id:
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id, message_id=last_id,
                caption=caption, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            flow["last_msg_id"] = last_id
            return
        except Exception:
            pass
    await edit_media(update, context, DEFAULT_IMAGE_URL, caption, reply_markup, parse_mode)

def format_prix(prix_original: float) -> float:
    """Applique la majoration de PRICE_INCREASE."""
    return prix_original + PRICE_INCREASE

def _is_bridge_home(texte: str, boutons_rows: list) -> bool:
    """Détecte si la réponse du bridge est son menu principal (pas un contenu utile)."""
    if not texte:
        return False
    bas = texte.lower()
    if "kfc france" in bas and "solde" in bas and "que veux-tu faire" in bas:
        return True
    return False

# ── Écran 1 : Menu Principal ───────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    user_id = update.effective_user.id
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass

    flow = get_flow(user_id)
    flow["view"] = "main"

    # Libérer le bridge si l'user l'utilisait, et retirer de la file
    mirror.release(user_id)
    mirror.remove_from_queue(user_id)

    # Crée ou récupère l'utilisateur avec solde
    user = get_or_create_user(user_id)
    solde = user["bot_balance"]

    caption = (
        f"🍗 **KFC France**\n\n"
        f"🆔 `{user_id}`\n"
        f"💰 Solde : **{solde:.2f}€**\n\n"
        f"Que veux-tu faire ?"
    )
    keyboard = [
        [InlineKeyboardButton("🛒 Commander", callback_data=_cb("start_search"))],
        [InlineKeyboardButton("💳 Déposer", callback_data=_cb("show_deposit"))],
        [InlineKeyboardButton("📢 Canal", url=CANAL_URL)],
    ]

    await edit_media(update, context, DEFAULT_IMAGE_URL, caption, InlineKeyboardMarkup(keyboard))

# ── Écran 2 : Recherche (via Mirror) ───────────────────────────────────

async def show_search(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    user_id = update.effective_user.id
    acquired, pos = mirror.acquire_or_queue(user_id)
    if not acquired:
        total = mirror.queue_length()
        await edit_media(
            update, context, DEFAULT_IMAGE_URL,
            f"⏳ *Une commande est déjà en cours.*\n\n"
            f"Position dans la file : **{pos}/{total}**\n\n"
            f"Tu seras notifié dès que ce sera ton tour 🔔",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]),
        )
        asyncio.create_task(_wait_bridge_turn(user_id, context))
        return
    flow = get_flow(user_id)
    flow["view"] = "search"
    await edit_media(
        update, context, DEFAULT_IMAGE_URL,
        "🔎 Envoie le **code postal**, la **ville** ou le **nom** du KFC.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]),
    )

async def _wait_bridge_turn(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attend le tour de l'user dans la file, puis notifie."""
    await mirror.wait_until_acquired(user_id)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="🎉 *C'est ton tour !*\n\n"
                 "Clique sur 🛒 Commander pour commencer ta commande.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Commander", callback_data=_cb("start_search"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]),
        )
    except Exception:
        logger.exception("Erreur notification tour user %s", user_id)


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    flow = get_flow(user_id)
    if flow["view"] not in ("search",):
        return
    try:
        mirror._check(user_id)
    except MirrorBusyError as e:
        await edit_media(update, context, DEFAULT_IMAGE_URL, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    query = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    flow["search_query"] = query
    flow["bridge_result_page"] = 0

    if not BRIDGE_ENABLED:
        await edit_media(
            update, context, DEFAULT_IMAGE_URL,
            "❌ Bridge désactivé.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]),
        )
        return

    await edit_media(update, context, DEFAULT_IMAGE_URL, "⏳ Recherche en cours...")

    try:
        await mirror.ensure_connected()
        resp = await mirror.search_restaurants(query)
    except Exception as e:
        logger.exception("Erreur mirror search")
        await edit_media(
            update, context, DEFAULT_IMAGE_URL,
            f"❌ Erreur de recherche : {e}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Réessayer", callback_data=_cb("start_search"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]),
        )
        return

    buttons_rows = resp.get("buttons_rows", [])
    all_buttons = [b for row in buttons_rows for b in row]
    if not resp or not all_buttons:
        await edit_media(
            update, context, DEFAULT_IMAGE_URL,
            f"❌ Aucun KFC trouvé pour « {query} »",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Nouvelle recherche", callback_data=_cb("start_search"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]),
        )
        return

    resto_buttons = [b for b in all_buttons if b.get("type") == "restaurant_select"]
    logger.info("DO_SEARCH %d résultats, %d boutons resto, msg_id=%s",
        len(all_buttons), len(resto_buttons), resp.get("id"))
    flow["bridge_results"] = resto_buttons
    flow["bridge_result_page"] = 0
    flow["last_bridge_msg_id"] = resp.get("id")

    await _show_bridge_results(update, context)

async def _show_bridge_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    flow = get_flow(user_id)
    resto_buttons = flow.get("bridge_results", [])
    page = flow.get("bridge_result_page", 0)
    query = flow.get("search_query", "")

    PER_PAGE = 8
    total_pages = max(1, (len(resto_buttons) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages - 1)
    flow["bridge_result_page"] = page
    start = page * PER_PAGE
    page_buttons = resto_buttons[start:start + PER_PAGE]

    caption = (
        f"🔍 **{len(resto_buttons)} KFC** pour « {query} »\n"
        f"__🟢 ouvert · 🔴 fermé__"
    )
    keyboard = []
    for b in page_buttons:
        keyboard.append([
            InlineKeyboardButton(
                b["text"],
                callback_data=_cb("bridge_select", id=b["resto_id"]),
            )
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=_cb("bridge_page", p=page - 1)))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb("noop")))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=_cb("bridge_page", p=page + 1)))
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🔍 Nouvelle recherche", callback_data=_cb("start_search"))])
    keyboard.append([InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))])

    await edit_media(update, context, DEFAULT_IMAGE_URL, caption, InlineKeyboardMarkup(keyboard))

async def bridge_page(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    flow = get_flow(update.effective_user.id)
    flow["bridge_result_page"] = params["p"]
    await _show_bridge_results(update, context)

# ── Écran 3 : Fiche restaurant ─────────────────────────────────────────

async def bridge_select_restaurant(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    user_id = update.effective_user.id
    try:
        mirror._check(user_id)
    except MirrorBusyError as e:
        await edit_caption(update, context, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return
    flow = get_flow(user_id)
    resto_id = params["id"]
    flow["bridge_resto_id"] = resto_id

    await edit_caption(update, context, "⏳ Chargement du restaurant...")

    try:
        msg_id = flow.get("last_bridge_msg_id")
        resp = await mirror.select_restaurant(resto_id, msg_id)
        resp = mirror.process_bridge_response(resp)
        logger.info("SELECT_RESTO resto_id=%s resp_id=%s text='%s' btns=%s",
            resto_id, resp.get("id") if resp else None,
            (resp.get("text") or "")[:120] if resp else None,
            [[b.get("text") for b in row] for row in (resp.get("buttons_rows") or [])] if resp else None)
    except Exception as e:
        logger.exception("Erreur mirror select")
        await edit_caption(update, context, f"❌ Erreur : {e}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Résultats", callback_data=_cb("bridge_back"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]))
        return

    if not resp:
        await edit_caption(update, context, "❌ Restaurant introuvable.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Résultats", callback_data=_cb("bridge_back"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]))
        return

    if _is_bridge_home(resp.get("text", ""), resp.get("buttons_rows", [])):
        logger.info("Bridge home détecté dans select_restaurant")
        mirror.release(user_id)
        await cmd_start(update, context)
        return

    flow["last_bridge_msg_id"] = resp.get("id")

    text = resp.get("text", "")
    m = re.search(r"\*\*(.+?)\*\*", text)
    resto_display_name = m.group(1) if m else "Restaurant"

    lines = [f"🏪 **{resto_display_name}**"]
    for line in text.split("\n"):
        if "Ouvert" in line or "Fermé" in line or "Aujourd" in line:
            lines.append(f"\n{line.strip()}")
    caption = "\n".join(lines) if len(lines) > 1 else text

    keyboard = [
        [InlineKeyboardButton("🛒 Faire mon panier", callback_data=_cb("bridge_order", resto_id=resto_id))],
        [InlineKeyboardButton("🔙 Résultats", callback_data=_cb("bridge_back")),
         InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
    ]

    photo_url = resp.get("photo_bytes") or DEFAULT_IMAGE_URL
    await edit_media(update, context, photo_url, caption, InlineKeyboardMarkup(keyboard))

# ── Écran 4 : Catalogue produits (mirror) ─────────────────────────────

async def bridge_start_order(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    user_id = update.effective_user.id
    try:
        mirror._check(user_id)
    except MirrorBusyError as e:
        await edit_caption(update, context, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return
    resto_id = params["resto_id"]

    await edit_caption(update, context, "⏳ Récupération du menu...")

    try:
        flow = get_flow(user_id)
        msg_id = flow.get("last_bridge_msg_id")
        resp = await mirror.start_order(resto_id, msg_id)
        resp = mirror.process_bridge_response(resp)
        logger.info("START_ORDER resto_id=%s resp_id=%s text='%s' btns=%s",
            resto_id, resp.get("id") if resp else None,
            (resp.get("text") or "")[:120] if resp else None,
            [[b.get("text") for b in row] for row in (resp.get("buttons_rows") or [])] if resp else None)
    except Exception as e:
        logger.exception("Erreur mirror order")
        await edit_caption(update, context, f"❌ Erreur : {e}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Résultats", callback_data=_cb("bridge_back"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]))
        return

    if not resp:
        await edit_caption(update, context, "❌ Pas de réponse du bot.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Résultats", callback_data=_cb("bridge_back"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]))
        return

    if _is_bridge_home(resp.get("text", ""), resp.get("buttons_rows", [])):
        logger.info("Bridge home détecté dans start_order")
        mirror.release(user_id)
        await cmd_start(update, context)
        return

    text = resp.get("text", "")

    if "n'accepte pas" in text.lower():
        m = re.search(r"\*\*(.+?)\*\*", text)
        name = m.group(1) if m else "Le restaurant"
        await edit_caption(update, context,
            f"❌ **{name}** n'accepte pas les commandes en ligne pour le moment.\n\nChoisis un autre KFC.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Résultats", callback_data=_cb("bridge_back"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]))
        return

    flow = get_flow(update.effective_user.id)
    flow["view"] = "products"
    flow["bridge_resto_id"] = resto_id
    flow["bridge_product_data"] = resp
    flow["last_bridge_msg_id"] = resp.get("id")

    await _show_products(update, context, resp)

async def _show_products(update, context, resp):
    """Affiche la réponse du bot pote à l'identique, avec majoration des prix."""
    text = resp.get("text", "")
    pb = resp.get("photo_bytes")
    buttons_rows = resp.get("buttons_rows", [])
    all_btn_text = " ".join(b.get("text", "") for row in buttons_rows for b in row)

    garder = "Choisir mes options" in all_btn_text or "Ajouter" in all_btn_text
    if not garder:
        pb = None
    photo_url = pb or DEFAULT_IMAGE_URL

    keyboard = []
    for row in buttons_rows:
        kbd_row = []
        for b in row:
            cb = b.get("callback_data", "")
            btn_type = b.get("type", "")
            btn_text = b.get("text", "")

            if btn_type == "home":
                kbd_row.append(InlineKeyboardButton(btn_text, callback_data=_cb("home")))
            elif cb:
                kbd_row.append(InlineKeyboardButton(btn_text, callback_data=_cb("bridge_prod_click", cb=cb)))
            elif b.get("url"):
                kbd_row.append(InlineKeyboardButton(btn_text, url=b["url"]))
        if kbd_row:
            keyboard.append(kbd_row)

    await edit_media(update, context, photo_url, text, InlineKeyboardMarkup(keyboard) if keyboard else None)

async def bridge_prod_click(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    """Transmet un clic au bot pote via le mirror."""
    user_id = update.effective_user.id
    try:
        mirror._check(user_id)
    except MirrorBusyError as e:
        await edit_caption(update, context, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return
    cb = params.get("cb", "")
    if not cb:
        return

    await edit_caption(update, context, "⏳...")

    try:
        resp = await mirror.forward_click(cb)
        if resp:
            follow_up = await mirror.wait_for_response(timeout=4.0)
            if follow_up:
                resp = follow_up
        resp = mirror.process_bridge_response(resp)
        logger.info("PROD_CLICK cb='%s' resp_id=%s text='%s' btns=%s",
            cb, resp.get("id") if resp else None,
            (resp.get("text") or "")[:120] if resp else None,
            [[b.get("text") for b in row] for row in (resp.get("buttons_rows") or [])] if resp else None)
    except Exception as e:
        logger.exception("Erreur mirror click")
        await edit_caption(update, context, f"❌ {e}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    if not resp:
        return

    # Si le bridge a renvoyé son menu principal → rediriger vers notre accueil
    if _is_bridge_home(resp.get("text", ""), resp.get("buttons_rows", [])):
        logger.info("Bridge home détecté, redirection accueil user %s", user_id)
        mirror.release(user_id)
        await cmd_start(update, context)
        return

    text3 = (resp.get("text") or "").lower()
    if "numéro de commande" in text3 or "commande envoyée" in text3:
        caption = resp.get("text", "")
        caption = re.sub(
            r'\n?Quand tu as reçu ta commande, n\'oublie pas de poster un vouch \(Mon compte → Vouch\) - tu recevras une récompense sur ton solde !\s*',
            '', caption)
        photo = resp.get("photo_bytes") or DEFAULT_IMAGE_URL
        keyboard = []
        for row in resp.get("buttons_rows", []):
            for b in row:
                if "vouch" in b.get("text", "").lower():
                    continue
                if b.get("url"):
                    keyboard.append([InlineKeyboardButton(b["text"], url=b["url"])])
        keyboard.append([InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))])
        await edit_media(update, context, photo, caption, InlineKeyboardMarkup(keyboard))
        return

    await _show_products(update, context, resp)

# ── Interception : Valider la commande ──────────────────────────────────

async def validate_order(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    """
    User clique "Valider la commande"
    → Forward direct au bridge
    → Si rejet du bridge → message d'erreur
    → Sinon affiche la suite (prénom, nom, etc.)
    """
    user_id = update.effective_user.id
    try:
        mirror._check(user_id)
    except MirrorBusyError as e:
        await edit_caption(update, context, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return
    cb = params.get("cb", "")

    await edit_caption(update, context, "⏳...")
    try:
        await mirror.click_callback(cb)
        resp = await mirror.wait_for_response(timeout=4.0)
        if not resp:
            resp = await mirror.get_last_message()
        resp = mirror.process_bridge_response(resp) if resp else None
    except Exception as e:
        logger.exception("Erreur forward validation")
        await edit_caption(update, context, f"❌ Erreur : {e}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    if not resp:
        await edit_caption(update, context, "❌ Pas de réponse du bot.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    logger.info("VALIDATE_ORDER resp id=%s text='%s' btns=%s",
        resp.get("id"), (resp.get("text") or "")[:150],
        [[b.get("text") for b in row] for row in (resp.get("buttons_rows") or [])])

    if _is_bridge_home(resp.get("text", ""), resp.get("buttons_rows", [])):
        logger.info("Bridge home détecté dans validate_order, redirection accueil")
        mirror.release(user_id)
        await cmd_start(update, context)
        return

    flow = get_flow(user_id)
    flow["bridge_product_data"] = resp
    flow["view"] = "products"

    text_resp = resp.get("text", "").lower()

    if "solde insuffisant" in text_resp or "fais un dépôt" in text_resp:
        mirror.release(user_id)
        await edit_caption(update, context,
            "❌ *Service de commande temporairement indisponible.*\n\n"
            "Le compte de commande doit être rechargé.\n"
            "Réessaie plus tard ou contacte le support.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
                [InlineKeyboardButton("💬 Support", url=f"tg://user?id={ADMIN_ID}")],
            ]))
        return

    if "n'accepte pas" in text_resp or "erreur" in text_resp:
        await _show_products(update, context, resp)
        return

    await _show_products(update, context, resp)


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    """
    Étape 2/2 : User clique "Confirmer" ou "✅ Payer X.XX€"
    → Vérifie solde → débite → forward au bridge
    → Si rejet à 2/2 → refund + indisponible
    """
    user_id = update.effective_user.id
    try:
        mirror._check(user_id)
    except MirrorBusyError as e:
        await edit_caption(update, context, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return
    cb = params.get("cb", "")
    prix_total = params.get("amount", 0)

    user = get_user(user_id)
    if not user:
        await edit_caption(update, context, "❌ Utilisateur introuvable.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    solde = user["bot_balance"]

    # Si pas de prix fourni, l'extraire du dernier message
    if prix_total <= 0:
        flow = get_flow(user_id)
        resp = flow.get("bridge_product_data", {})
        prix_total = _extraire_prix(resp.get("text", ""))

    if prix_total <= 0:
        await edit_caption(update, context, "❌ Impossible de déterminer le prix.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    if solde < prix_total:
        await edit_caption(update, context,
            f"❌ *Solde insuffisant*\n\n"
            f"💰 Ton solde : **{solde:.2f}€**\n"
            f"💵 Total : **{prix_total:.2f}€**\n\n"
            f"💳 Recharge ton solde avec Déposer pour continuer.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Déposer", callback_data=_cb("show_deposit"))],
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
            ]))
        return

    # Débiter
    nouveau_solde = debit_user(user_id, prix_total)
    if nouveau_solde is None:
        await edit_caption(update, context, "❌ Erreur de débit. Contacte le support.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    logger.info("Débit %.2f€ user %s, nouveau solde: %.2f€", prix_total, user_id, nouveau_solde)

    # Forward confirmation au bridge
    await edit_caption(update, context, "⏳...")
    try:
        await mirror.click_callback(cb)
        resp2 = await mirror.wait_for_response(timeout=4.0)
        if not resp2:
            resp2 = await mirror.get_last_message()
        resp2 = mirror.process_bridge_response(resp2) if resp2 else None
        logger.info("CONFIRM_PAYMENT resp_id=%s text='%s' btns=%s",
            resp2.get("id") if resp2 else None,
            (resp2.get("text") or "")[:120] if resp2 else None,
            [[b.get("text") for b in row] for row in (resp2.get("buttons_rows") or [])] if resp2 else None)
    except Exception as e:
        logger.exception("Erreur 2/2, remboursement")
        credit_user(user_id, prix_total)
        await edit_caption(update, context, f"❌ Erreur : {e}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    if not resp2:
        await edit_caption(update, context, "✅ *Commande confirmée !* 🍗",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    if _is_bridge_home(resp2.get("text", ""), resp2.get("buttons_rows", [])):
        logger.info("Bridge home dans confirm_payment, user %s", user_id)
        mirror.release(user_id)
        await cmd_start(update, context)
        return

    follow_up = await mirror.wait_for_response(timeout=2.0)
    if follow_up:
        resp2 = follow_up
        resp2 = mirror.process_bridge_response(resp2)

    text2 = resp2.get("text", "").lower()

    # Rejet à 2/2 → refund + indisponible
    if any(mot in text2 for mot in ("solde insuffisant", "fais un dépôt", "n'accepte pas", "erreur", "refus")):
        logger.warning("Rejet 2/2, remboursement: %.100s", text2)
        mirror.release(user_id)
        credit_user(user_id, prix_total)
        await edit_caption(update, context,
            f"❌ *Commande indisponible pour le moment.*\n\n"
            f"{resp2['text']}\n\n"
            f"💰 Ton solde a été remboursé automatiquement.\n"
            f"Réessaie plus tard ou contacte le support.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
                [InlineKeyboardButton("💬 Support", url=f"tg://user?id={ADMIN_ID}")],
            ]))
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🚨 *ALERTE ADMIN*\nCommande échouée (2/2) — user `{user_id}`\n"
                     f"Montant remboursé : **{prix_total:.2f}€**",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # Succès 2/2 → continuer le mirror (formulaire nom/prénom etc.)
    flow = get_flow(user_id)
    flow["bridge_product_data"] = resp2
    flow["view"] = "products"
    # Vérifier si la commande est terminée (numéro reçu)
    if "numéro de commande" in text2 or "commande envoyée" in text2:
        logger.info("Commande confirmée pour user %s, libération bridge", user_id)
        mirror.release(user_id)
        caption2 = resp2.get("text", "")
        caption2 = re.sub(
            r'\n?Quand tu as reçu ta commande, n\'oublie pas de poster un vouch \(Mon compte → Vouch\) - tu recevras une récompense sur ton solde !\s*',
            '', caption2)
        photo2 = resp2.get("photo_bytes") or DEFAULT_IMAGE_URL
        keyboard2 = []
        for row in resp2.get("buttons_rows", []):
            for b in row:
                if "vouch" in b.get("text", "").lower():
                    continue
                if b.get("url"):
                    keyboard2.append([InlineKeyboardButton(b["text"], url=b["url"])])
        keyboard2.append([InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))])
        await edit_media(update, context, photo2, caption2, InlineKeyboardMarkup(keyboard2))
        return
    if any(mot in text2 for mot in ("confirmé", "numéro", "merci", "en cuisine")):
        logger.info("Commande confirmée pour user %s, libération bridge", user_id)
        mirror.release(user_id)
    await _show_products(update, context, resp2)


def _extraire_prix(texte: str) -> float:
    """Extrait le prix total d'un texte. Retourne 0.0 si introuvable."""
    if not texte:
        return 0.0
    mots_prix = r"(?:total|montant|prix|somme|ttc|régler|à payer|payer|solde|coût|tarif)"
    m = re.search(
        mots_prix + r"[^0-9]*(\d+[.,]\d{2})\s*€",
        texte, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(
        r"(\d+[.,]\d{2})\s*€.*(?:total|à payer|confirmer)",
        texte, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))
    prix_trouves = re.findall(r'(\d+[.,]\d{2})\s*€', texte)
    if prix_trouves:
        return max(float(p.replace(",", ".")) for p in prix_trouves)
    m_pts = re.search(mots_prix + r"[^0-9]*(\d+)\s*pts?", texte, re.IGNORECASE)
    if m_pts:
        pts = float(m_pts.group(1))
        return round(pts / 1000 * 2.5 + 1.50, 2)
    return 0.0

async def bridge_back(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    await _show_bridge_results(update, context)

# ── Dépôt ──────────────────────────────────────────────────────────────

async def show_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    user_id = update.effective_user.id
    user = get_or_create_user(user_id)

    await edit_media(
        update, context, DEFAULT_IMAGE_URL,
        f"💳 *Recharge ton solde*\n\n"
        f"💰 Solde actuel : **{user['bot_balance']:.2f}€**\n\n"
        f"Choisis un montant :\n"
        f"__Minimum 5€__",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 5€", callback_data=_cb("deposit_menu", amount=5)),
             InlineKeyboardButton("💰 10€", callback_data=_cb("deposit_menu", amount=10))],
            [InlineKeyboardButton("💰 20€", callback_data=_cb("deposit_menu", amount=20)),
             InlineKeyboardButton("💰 50€", callback_data=_cb("deposit_menu", amount=50))],
            [InlineKeyboardButton("✏️ Montant personnalisé", callback_data=_cb("deposit_custom"))],
            [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
        ]),
    )

async def deposit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    amount = params["amount"]
    flow = get_flow(update.effective_user.id)
    flow["deposit_amount"] = amount

    await edit_caption(
        update, context,
        f"💳 *Dépôt de {amount:.2f}€*\n\n"
        f"Choisis ta méthode de paiement :",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 PayPal", callback_data=_cb("deposit_paypal"))],
            [InlineKeyboardButton("₿ Crypto", callback_data=_cb("deposit_crypto"))],
            [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
        ]),
    )

async def deposit_custom(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    flow = get_flow(update.effective_user.id)
    flow["view"] = "deposit_custom"
    await edit_caption(
        update, context,
        "✏️ *Montant personnalisé*\n\n"
        "Envoie le montant (minimum 5€).\n\n"
        "Exemple : `15`",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Déposer", callback_data=_cb("show_deposit"))],
            [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
        ]),
    )

async def deposit_paypal(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    user_id = update.effective_user.id
    flow = get_flow(user_id)
    amount = flow.get("deposit_amount", 10)
    flow["view"] = "deposit_paypal"

    message = generer_message_paypal(amount)

    await edit_caption(
        update, context, message,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
        ]),
    )

async def deposit_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    user_id = update.effective_user.id
    flow = get_flow(user_id)
    amount = flow.get("deposit_amount", 10)

    # Créer le dépôt en DB
    deposit_id = create_deposit(user_id, amount, "crypto")

    # Générer une adresse
    adresse = generer_adresse_crypto(amount)
    if not adresse:
        await edit_caption(update, context, "❌ Erreur de génération d'adresse. Réessaie plus tard.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    # Créer l'invoice crypto
    from database import create_crypto_invoice
    create_crypto_invoice(deposit_id, amount, adresse)

    await edit_caption(
        update, context,
        f"₿ *Paiement Crypto*\n\n"
        f"Envoie l'équivalent de **{amount:.2f}€** à cette adresse :\n\n"
        f"`{adresse}`\n\n"
        f"En attente de confirmation...",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
        ]),
    )

# ── Réception screenshot PayPal ───────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Réception d'une photo (screenshot PayPal)."""
    user_id = update.effective_user.id
    flow = get_flow(user_id)

    if flow.get("view") != "deposit_paypal":
        await update.message.reply_text("ℹ️ Envoie une photo seulement quand tu es sur l'écran Déposer.")
        return

    amount = flow.get("deposit_amount", 10)

    # Créer le dépôt en DB
    deposit_id = create_deposit(user_id, amount, "paypal")

    # Récupérer la photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    # Notifier l'admin
    try:
        admin_msg_id = await notifier_admin_paypal(context, user_id, amount, deposit_id, bytes(photo_bytes))
        update_deposit_status(deposit_id, "pending", admin_msg_id)
    except Exception as e:
        logger.exception("Erreur notification admin")
        await update.message.reply_text("❌ Erreur lors de l'envoi de ta demande. Contacte le support.")
        return

    # Éditer le message original + boutons admin sur le message admin
    # Ajouter les boutons Valider/Refuser
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=ADMIN_ID,
            message_id=admin_msg_id,
            reply_markup=generer_boutons_admin(deposit_id),
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ *Demande envoyée !*\n\n"
        f"💰 Montant : **{amount:.2f}€**\n"
        f"⏳ En attente de validation...\n\n"
        f"Tu seras notifié dès que c'est confirmé.",
        parse_mode="Markdown",
    )

    # Nettoyer la view
    try:
        await update.message.delete()
    except Exception:
        pass

# ── Admin ──────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Panneau admin."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Accès réservé à l'admin.")
        return

    from database import get_pending_deposits
    en_attente = len(get_pending_deposits())

    await update.message.reply_text(
        f"👑 *Panneau Admin*\n\n"
        f"Commandes :\n"
        f"• `/crediter {update.effective_user.id} 10` — ajouter du solde\n"
        f"• `/debit {update.effective_user.id} 10` — retirer du solde\n"
        f"• `/solde {update.effective_user.id}` — voir le solde\n\n"
        f"📋 Dépôts en attente : **{en_attente}**\n\n"
        f"Accès rapide :",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Voir dépôts en attente", callback_data=_cb("admin_pending"))],
        ]),
    )

async def cmd_crediter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Crédite le solde d'un utilisateur."""
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage : /crediter {telegram_id} {montant}")
        return
    try:
        target_id = int(context.args[0])
        montant = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Format invalide.")
        return

    nouveau = credit_user(target_id, montant)
    await update.message.reply_text(
        f"✅ {montant:.2f}€ crédité à `{target_id}`\n"
        f"💰 Nouveau solde : **{nouveau:.2f}€**",
        parse_mode="Markdown",
    )

async def cmd_debit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Débite le solde d'un utilisateur."""
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage : /debit {telegram_id} {montant}")
        return
    try:
        target_id = int(context.args[0])
        montant = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Format invalide.")
        return

    nouveau = debit_user(target_id, montant)
    if nouveau is None:
        await update.message.reply_text("❌ Solde insuffisant.")
    else:
        await update.message.reply_text(
            f"✅ {montant:.2f}€ débité de `{target_id}`\n"
            f"💰 Nouveau solde : **{nouveau:.2f}€**",
            parse_mode="Markdown",
        )

async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voir le solde d'un utilisateur."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage : /solde {telegram_id}")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    user = get_user(target_id)
    if not user:
        await update.message.reply_text("❌ Utilisateur introuvable.")
    else:
        await update.message.reply_text(
            f"👤 User : `{target_id}`\n"
            f"💰 Solde : **{user['bot_balance']:.2f}€**",
            parse_mode="Markdown",
        )

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE, _=None) -> None:
    """Affiche les dépôts en attente."""
    from database import get_pending_deposits
    pending = get_pending_deposits()
    if not pending:
        await edit_caption(update, context, "📋 Aucun dépôt en attente.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    text = "📋 *Dépôts en attente :*\n\n"
    for d in pending[:10]:
        text += f"#{d['id']} — User `{d['telegram_id']}` — **{d['amount']:.2f}€** ({d['method']})\n"

    await edit_caption(update, context, text,
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))

async def admin_confirm_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    """Admin valide un dépôt PayPal."""
    if update.effective_user.id != ADMIN_ID:
        return

    deposit_id = params["d"]
    deposit = get_deposit(deposit_id)
    if not deposit or deposit["status"] != "pending":
        await update.callback_query.answer("Dépôt déjà traité.")
        return

    # Créditer l'utilisateur
    nouveau = credit_user(deposit["telegram_id"], deposit["amount"])

    # Mettre à jour le statut
    update_deposit_status(deposit_id, "confirmed")

    # Notifier l'utilisateur
    try:
        await context.bot.send_message(
            chat_id=deposit["telegram_id"],
            text=(
                f"✅ *Dépôt confirmé !*\n\n"
                f"{deposit['amount']:.2f}€ ajouté à ton solde.\n"
                f"💰 Nouveau solde : **{nouveau:.2f}€**"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Erreur notification user dépôt confirmé")

    # Éditer le message admin
    await update.callback_query.edit_message_caption(
        caption=(
            f"✅ *DÉPÔT CONFIRMÉ*\n\n"
            f"👤 User : `{deposit['telegram_id']}`\n"
            f"💰 Montant : **{deposit['amount']:.2f}€**\n"
            f"📝 Dépôt #{deposit_id}\n"
            f"✅ Traité par l'admin."
        ),
        parse_mode="Markdown",
    )
    await update.callback_query.answer("✅ Dépôt confirmé !")

async def admin_refuse_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict) -> None:
    """Admin refuse un dépôt PayPal."""
    if update.effective_user.id != ADMIN_ID:
        return

    deposit_id = params["d"]
    deposit = get_deposit(deposit_id)
    if not deposit or deposit["status"] != "pending":
        await update.callback_query.answer("Dépôt déjà traité.")
        return

    update_deposit_status(deposit_id, "refused")

    # Notifier l'utilisateur
    try:
        await context.bot.send_message(
            chat_id=deposit["telegram_id"],
            text=(
                f"❌ *Dépôt refusé.*\n\n"
                f"Le paiement n'a pas pu être vérifié.\n"
                f"Contacte le support si tu penses que c'est une erreur."
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await update.callback_query.edit_message_caption(
        caption=(
            f"❌ *DÉPÔT REFUSÉ*\n\n"
            f"👤 User : `{deposit['telegram_id']}`\n"
            f"💰 Montant : **{deposit['amount']:.2f}€**\n"
            f"📝 Dépôt #{deposit_id}\n"
            f"❌ Refusé par l'admin."
        ),
        parse_mode="Markdown",
    )
    await update.callback_query.answer("❌ Dépôt refusé.")

# ── Forward texte au bridge (formulaire nom/prénom) ──────────────────

async def forward_text_to_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transmet un message texte de l'user au bot pote via le bridge."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        mirror._check(user_id)
        await mirror.send_text(text)
        resp = await mirror.wait_for_response(timeout=3.0)
        if not resp:
            resp = await mirror.get_last_message()
        resp = mirror.process_bridge_response(resp) if resp else None
        logger.info("FORWARD_TEXT text='%s' resp_id=%s text='%s' btns=%s",
            text, resp.get("id") if resp else None,
            (resp.get("text") or "")[:100] if resp else None,
            [[b.get("text") for b in row] for row in (resp.get("buttons_rows") or [])] if resp else None)
        if resp and _is_bridge_home(resp.get("text", ""), resp.get("buttons_rows", [])):
            logger.info("Bridge home dans forward_text, fin de commande user %s", user_id)
            mirror.release(user_id)
            await cmd_start(update, context)
            return
    except MirrorBusyError as e:
        await edit_caption(update, context, str(e),
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return
    except Exception as e:
        logger.exception("Erreur forward texte bridge")
        await edit_caption(update, context, f"❌ Erreur : {e}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))
        return

    if resp:
        flow = get_flow(user_id)
        flow["bridge_product_data"] = resp
        text_r = (resp.get("text") or "").lower()
        if "numéro de commande" in text_r or "commande envoyée" in text_r:
            caption_r = resp.get("text", "")
            caption_r = re.sub(
                r'\n?Quand tu as reçu ta commande, n\'oublie pas de poster un vouch \(Mon compte → Vouch\) - tu recevras une récompense sur ton solde !\s*',
                '', caption_r)
            photo_r = resp.get("photo_bytes") or DEFAULT_IMAGE_URL
            keyboard_r = []
            for row in resp.get("buttons_rows", []):
                for b in row:
                    if "vouch" in b.get("text", "").lower():
                        continue
                    if b.get("url"):
                        keyboard_r.append([InlineKeyboardButton(b["text"], url=b["url"])])
            keyboard_r.append([InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))])
            await edit_media(update, context, photo_r, caption_r, InlineKeyboardMarkup(keyboard_r))
            mirror.release(user_id)
            return
        await _show_products(update, context, resp)

# ── Montant personnalisé ──────────────────────────────────────────────

async def handle_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    flow = get_flow(user_id)
    try:
        montant = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Montant invalide. Envoie un nombre (ex: `15`).", parse_mode="Markdown")
        return

    if montant < 5:
        await update.message.reply_text("❌ Minimum 5€. Envoie un montant valide.", parse_mode="Markdown")
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    flow["deposit_amount"] = montant
    await edit_caption(
        update, context,
        f"💳 *Dépôt de {montant:.2f}€*\n\n"
        f"Choisis ta méthode de paiement :",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 PayPal", callback_data=_cb("deposit_paypal"))],
            [InlineKeyboardButton("₿ Crypto", callback_data=_cb("deposit_crypto"))],
            [InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))],
        ]),
    )

# ── Texte ──────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = get_flow(update.effective_user.id)
    if flow["view"] == "search":
        await do_search(update, context)
    elif flow["view"] == "deposit_custom":
        await handle_custom_amount(update, context)
    elif flow["view"] == "deposit_paypal":
        await update.message.reply_text(
            "📸 Envoie la **capture d'écran** de ton paiement PayPal.",
            parse_mode="Markdown",
        )
    elif flow["view"] == "products" and flow.get("bridge_resto_id"):
        # L'user est en train de commander → forwarder le texte au bot pote
        await forward_text_to_bridge(update, context)
    else:
        await edit_media(update, context, DEFAULT_IMAGE_URL,
            "ℹ️ Utilise les **boutons du menu** ci-dessous pour naviguer.\n\n"
            "🔎 Si tu veux chercher un KFC, clique sur **🛒 Commander**.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Accueil", callback_data=_cb("home"))]]))

# ── Router ─────────────────────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    raw = query.data
    try:
        action, params = _parse_cb(raw)
    except (json.JSONDecodeError, KeyError):
        await query.answer("Action invalide")
        return

    ROUTES = {
        "home": cmd_start,
        "start_search": show_search,
        "bridge_select": bridge_select_restaurant,
        "bridge_back": bridge_back,
        "bridge_page": bridge_page,
        "bridge_order": bridge_start_order,
        "bridge_prod_click": bridge_prod_click,
        "show_deposit": show_deposit,
        "deposit_menu": deposit_menu,
        "deposit_custom": deposit_custom,
        "deposit_paypal": deposit_paypal,
        "deposit_crypto": deposit_crypto,
        "admin_pending": admin_pending,
        "admin_confirm_deposit": admin_confirm_deposit,
        "admin_refuse_deposit": admin_refuse_deposit,
        "noop": lambda u, c, p: query.answer(),
    }

    handler = ROUTES.get(action)
    if handler:
        await handler(update, context, params)
    else:
        await query.answer(f"Action inconnue : {action}")

# ── Tâche de fond : vérification crypto ─────────────────────────────────

async def crypto_polling_task(app):
    """Vérifie les paiements crypto toutes les 30 secondes."""
    while True:
        try:
            await verifier_paiements_crypto(app)
        except Exception:
            logger.exception("Erreur crypto polling")
        await asyncio.sleep(30)

# ── Main ───────────────────────────────────────────────────────────────

def main() -> None:
    if TELEGRAM_TOKEN == "VOTRE_TOKEN_ICI":
        logger.error("Token Telegram non configuré !")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("crediter", cmd_crediter))
    app.add_handler(CommandHandler("debit", cmd_debit))
    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callback_router))

    # Démarrage du bridge Telethon
    async def bridge_on_startup(app_obj):
        if BRIDGE_ENABLED:
            try:
                await mirror.ensure_connected()
                logger.info("Bridge Telethon connecté")
            except Exception as e:
                logger.warning("Bridge non démarré: %s", e)
        # Lancer le polling crypto en arrière-plan
        asyncio.create_task(crypto_polling_task(app_obj))

    app.post_init = bridge_on_startup

    logger.info("🍗 Bot O'KFC démarré !")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
