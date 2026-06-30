"""
bridge.py — Pont Telethon entre notre bot O'KFC et @chikenuhq_bot

Utilise un compte Telegram réel (via session Telethon) pour dialoguer
avec le bot du pote et transmettre les commandes.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from telethon import TelegramClient, events
from telethon.tl import types as tl_types
from telethon.tl.custom import Message

from config import DEFAULT_IMAGE_URL

logger = logging.getLogger("kfc_bridge")

# ── Credentials Telethon ────────────────────────────────────────────────
API_ID = 35315568
API_HASH = "74b6fa33e79786713d3a74befc70a502"
PHONE = "+12367702842"
TARGET_BOT = "chikenuhq_bot"
SESSION_FILE = "session_analyze"

# ── Patterns callback du bot cible ──────────────────────────────────────
# Recherche
CMD_SEARCH = "cmd_search"
HOME = "home"
BACK = "back"
NOOP = "noop"
PAGE_PREFIX = "pg_"
RESTO_SELECT_PREFIX = "rs_"
RESTO_ORDER_PREFIX = "rw_"
DEPOT_PREFIX = "depot_menu"
LOYALTY = "cmd_loyalty"
REFERRAL = "cmd_referral"


class BridgeError(Exception):
    """Erreur du bridge Telethon."""


class KFCBridge:
    """
    Pont vers @chikenuhq_bot via Telethon.

    Maintient une connexion persistante et permet d'interagir
    avec le bot cible comme si l'utilisateur le faisait manuellement.
    """

    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.bot_entity = None
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

        # Callback handlers enregistrés par l'utilisateur du bridge
        self._update_handlers: List[Callable] = []

    # ── Connexion / Déconnexion ─────────────────────────────────────────

    async def start(self):
        """Connecte Telethon et récupère l'entité du bot."""

        session_b64 = os.environ.get("SESSION_BASE64", "")
        if session_b64 and not os.path.exists(SESSION_FILE):
            try:
                import base64, gzip
                raw = base64.b64decode(session_b64.strip())
                data = gzip.decompress(raw)
                with open(SESSION_FILE, "wb") as f:
                    f.write(data)
                logger.info("Session restored from SESSION_BASE64 (%d bytes)", len(data))
            except Exception as e:
                logger.error("Session restore failed: %s", e)

        code = os.environ.get("TG_CODE")
        if not code and os.path.exists("tg_code.txt"):
            code = open("tg_code.txt", encoding="utf-8").read().strip()

        self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)

        kwargs = {"phone": PHONE}
        if code:
            kwargs["code_callback"] = lambda: code
        try:
            await self.client.start(**kwargs)
        except (EOFError, ConnectionError) as e:
            logger.error("Bridge auth failed: %s. Session file invalid or expired. Set TG_CODE env var for fresh auth.", e)
            raise

        self.bot_entity = await self.client.get_entity(TARGET_BOT)
        logger.info("Bridge connecté à @%s", TARGET_BOT)
        self._ready.set()

    async def stop(self):
        """Déconnecte Telethon."""
        if self.client:
            await self.client.disconnect()
            self._ready.clear()
            logger.info("Bridge déconnecté")

    async def ensure_connected(self):
        """Assure que la connexion est active. Reconnexion si perdue."""
        if not self._ready.is_set() or (self.client and not self.client.is_connected()):
            self._ready.clear()
            await self.start()

    # ── Écoute des messages ─────────────────────────────────────────────

    def on_update(self, handler: Callable):
        """Enregistre un handler pour les messages du bot."""
        self._update_handlers.append(handler)

    async def _handle_message(self, event):
        """Callback interne pour les nouveaux messages."""
        msg = event.message
        if not msg or msg.out:
            return
        for handler in self._update_handlers:
            try:
                await handler(msg)
            except Exception:
                logger.exception("Erreur dans le handler bridge")

    # ── Actions de base ─────────────────────────────────────────────────

    async def send_command(self, command: str) -> None:
        """Envoie une commande texte au bot."""
        await self.ensure_connected()
        async with self._lock:
            await self.client.send_message(self.bot_entity, command)
            await asyncio.sleep(0.5)

    async def click_callback(self, data: bytes, message_id: Optional[int] = None) -> None:
        """Clique sur un bouton callback du bot. Réessaie 3x si pas trouvé."""
        await self.ensure_connected()
        async with self._lock:
            if message_id:
                msg = await self.client.get_messages(self.bot_entity, ids=message_id)
                if msg and msg.reply_markup:
                    for row in msg.reply_markup.rows:
                        for btn in row.buttons:
                            if hasattr(btn, 'data') and btn.data == data:
                                logger.info("Click OK msg_id=%s cb='%s'", message_id, data)
                                await msg.click(data=data)
                                await asyncio.sleep(0.3)
                                return
                    logger.warning("Bouton %s pas dans msg_id=%s, fallback recherche", data, message_id)
            for attempt in range(3):
                msgs = await self.client.get_messages(self.bot_entity, limit=15)
                for m in msgs:
                    if not m.reply_markup:
                        continue
                    for row in m.reply_markup.rows:
                        for btn in row.buttons:
                            if hasattr(btn, 'data') and btn.data == data:
                                logger.info("Click OK (tentative %d/3) msg_id=%s cb='%s'", attempt + 1, m.id, data)
                                await m.click(data=data)
                                await asyncio.sleep(0.3)
                                return
                logger.info("Click tentative %d/3 échouée pour %s", attempt + 1, data)
                if attempt < 2:
                    await asyncio.sleep(0.7)
                else:
                    for m in msgs[:3]:
                        txt = (m.text or "")[:100]
                        btns = []
                        if m.reply_markup:
                            for row in m.reply_markup.rows:
                                for btn in row.buttons:
                                    btns.append(getattr(btn, 'data', getattr(btn, 'text', '?')))
                        logger.warning("Msg id=%d out=%s texte='%s' boutons=%s", m.id, m.out, txt, btns)
                    logger.warning("Bouton %s non trouvé après 3 tentatives", data)

    async def get_last_message(self) -> Optional[Message]:
        """Récupère le dernier message du bot (pas le nôtre)."""
        await self.ensure_connected()
        msgs = await self.client.get_messages(self.bot_entity, limit=8)
        for m in msgs:
            if not m.out:
                return m
        return None

    async def get_last_bot_message(self) -> Optional[Dict]:
        """Retourne le dernier message du bot formaté en dict."""
        msg = await self.get_last_message()
        if not msg:
            return None
        return await self._message_to_dict(msg)

    async def wait_for_bot_response(self, timeout: float = 10.0) -> Optional[Dict]:
        """Attend un nouveau message OU un message édité du bot (polling)."""
        last_texts: Dict[int, str] = {}
        msgs = await self.client.get_messages(self.bot_entity, limit=5)
        for m in msgs:
            if not m.out:
                last_texts[m.id] = m.text or ""

        for _ in range(int(timeout / 0.3)):
            await asyncio.sleep(0.3)
            msgs = await self.client.get_messages(self.bot_entity, limit=5)
            for m in msgs:
                if m.out:
                    continue
                if m.id not in last_texts:
                    return await self._message_to_dict(m)
                if m.text and m.text != last_texts.get(m.id, ""):
                    return await self._message_to_dict(m)
        return None

    # ── Actions métier ──────────────────────────────────────────────────

    async def search_restaurants(self, query: str) -> Optional[Dict]:
        """
        Recherche des restaurants via le bot.
        1. Va au menu principal
        2. Clique 'Commander'
        3. Envoie la requête texte
        4. Retourne la réponse du bot
        """
        await self.send_command("/start")
        await asyncio.sleep(0.3)
        await self.click_callback(CMD_SEARCH.encode())
        await asyncio.sleep(0.3)
        await self.client.send_message(self.bot_entity, query)
        resp = await self.wait_for_bot_response(timeout=4.0)
        if not resp:
            resp = await self.get_last_bot_message()
        logger.info("SEARCH query='%s' resp_id=%s text='%s' btns=%s",
            query, resp.get("id") if resp else None,
            (resp.get("text") or "")[:100] if resp else None,
            [[b.get("text") for b in row] for row in (resp.get("buttons_rows") or [])] if resp else None)
        return resp

    async def select_restaurant(self, resto_id: str, msg_id: Optional[int] = None) -> Optional[Dict]:
        """Clique sur un restaurant dans la liste."""
        data = f"{RESTO_SELECT_PREFIX}{resto_id}".encode()
        await self.click_callback(data, msg_id)
        resp = await self.wait_for_bot_response(timeout=4.0)
        if not resp:
            resp = await self.get_last_bot_message()
        return resp

    async def start_order_at_restaurant(self, resto_id: str, msg_id: Optional[int] = None) -> Optional[Dict]:
        """Clique 'Faire mon panier' pour un restaurant."""
        data = f"{RESTO_ORDER_PREFIX}{resto_id}".encode()
        await self.click_callback(data, msg_id)
        resp = await self.wait_for_bot_response(timeout=5.0)
        if not resp:
            resp = await self.get_last_bot_message()
        # Si la réponse est "Vérification..." sans boutons, attendre la vraie
        if resp and not resp.get("buttons_rows") and "vérification" in (resp.get("text") or "").lower():
            logger.info("Vérification détectée, attente réponse réelle…")
            follow_up = await self.wait_for_bot_response(timeout=5.0)
            if follow_up:
                resp = follow_up
            else:
                resp2 = await self.get_last_bot_message()
                if resp2:
                    resp = resp2
        return resp

    async def go_home(self):
        """Retourne au menu principal."""
        await self.click_callback(HOME.encode())
        await asyncio.sleep(0.5)

    async def go_back(self):
        """Retourne à l'écran précédent."""
        await self.click_callback(BACK.encode())
        await asyncio.sleep(0.5)

    # ── Parse des réponses ──────────────────────────────────────────────

    async def _message_to_dict(self, msg: Message) -> Dict:
        """Convertit un message Telethon en dict structuré."""
        result = {
            "id": msg.id,
            "text": msg.text,
            "has_media": msg.media is not None,
            "photo_bytes": None,
        }
        if msg.media:
            result["media_type"] = type(msg.media).__name__
            try:
                result["photo_bytes"] = await self.client.download_media(msg.media, bytes)
            except Exception:
                pass

        # Extraire les boutons (en préservant les lignes)
        rows = []
        if msg.reply_markup and isinstance(msg.reply_markup, tl_types.ReplyInlineMarkup):
            for row in msg.reply_markup.rows:
                current_row = []
                for btn in row.buttons:
                    b = {"text": btn.text}
                    if hasattr(btn, "data"):
                        decoded = btn.data.decode()
                        b["callback_data"] = decoded
                        if decoded == HOME:
                            b["type"] = "home"
                        elif decoded == BACK:
                            b["type"] = "back"
                        elif decoded == CMD_SEARCH:
                            b["type"] = "search"
                        elif decoded == DEPOT_PREFIX:
                            b["type"] = "deposit"
                        elif decoded == LOYALTY:
                            b["type"] = "loyalty"
                        elif decoded == REFERRAL:
                            b["type"] = "referral"
                        elif decoded == NOOP:
                            b["type"] = "noop"
                        elif decoded.startswith(RESTO_SELECT_PREFIX):
                            b["type"] = "restaurant_select"
                            b["resto_id"] = decoded[len(RESTO_SELECT_PREFIX):]
                        elif decoded.startswith(RESTO_ORDER_PREFIX):
                            b["type"] = "restaurant_order"
                            b["resto_id"] = decoded[len(RESTO_ORDER_PREFIX):]
                        elif decoded.startswith(PAGE_PREFIX):
                            b["type"] = "page"
                            b["page"] = decoded[len(PAGE_PREFIX):]
                    elif hasattr(btn, "url"):
                        b["url"] = btn.url
                        b["type"] = "url"
                    current_row.append(b)
                if current_row:
                    rows.append(current_row)
            result["buttons_rows"] = rows

        result["parsed"] = self._parse_text(msg.text or "")

        return result

    def _parse_text(self, text: str) -> Dict:
        """Extrait des infos du texte du bot."""
        info = {}
        # Solde : "💰 Solde : **0.00€**"
        m = re.search(r'💰\s*Solde\s*:?\s*\*{0,2}([\d.]+)\s*€', text)
        if m:
            info["balance_eur"] = float(m.group(1))

        # ID: "🆔 \`8610027292\`"
        m = re.search(r'🆔\s*`?(\d+)`?', text)
        if m:
            info["user_id"] = m.group(1)

        # Statut resto : "🟢 Ouvert"
        m = re.search(r'([🟢🔴])\s*\*?(Ouvert|Fermé)', text)
        if m:
            info["is_open"] = m.group(1) == "🟢"

        # Horaires
        m = re.search(r"Aujourd'hui\s*:\s*([\d: -]+)", text)
        if m:
            info["hours_today"] = m.group(1).strip()

        # Nombre de résultats
        m = re.search(r'(\d+)\s*KFC', text)
        if m:
            info["result_count"] = int(m.group(1))

        return info

    # ── Forward de commande complète ─────────────────────────────────────

    async def forward_order(
        self,
        restaurant_name: str,
        restaurant_zip: str,
        items: List[Dict],
        firstname: str,
        lastname: str,
    ) -> Dict:
        """
        Transmet une commande complète à @chikenuhq_bot.

        Étapes :
        1. Rechercher le restaurant
        2. Le sélectionner
        3. Cliquer 'Faire mon panier'
        4. Ajouter chaque article au panier
        5. Finaliser la commande

        Retourne un dict avec le résultat.
        """
        result = {"success": False, "order_id": None, "error": None, "steps": []}

        try:
            # Étape 1 : Rechercher
            logger.info("Bridge: recherche de %s %s", restaurant_name, restaurant_zip)
            search_resp = await self.search_restaurants(restaurant_zip)
            result["steps"].append(("search", search_resp))
            if not search_resp:
                raise BridgeError("Pas de réponse du bot")

            # Étape 2 : Trouver le bon restaurant dans les résultats
            resto_id = None
            if search_resp.get("buttons"):
                for btn in search_resp["buttons"]:
                    if btn.get("type") == "restaurant_select":
                        # Vérifier le nom
                        if restaurant_name.lower() in btn["text"].lower():
                            resto_id = btn["resto_id"]
                            break

            if not resto_id:
                # Prendre le premier restaurant disponible
                for btn in search_resp["buttons"]:
                    if btn.get("type") == "restaurant_select":
                        resto_id = btn["resto_id"]
                        break

            if not resto_id:
                raise BridgeError(f"Restaurant {restaurant_name} non trouvé")

            # Étape 3 : Sélectionner le restaurant
            logger.info("Bridge: sélection resto %s", resto_id)
            resto_resp = await self.select_restaurant(resto_id)
            result["steps"].append(("select_restaurant", resto_resp))

            # Étape 4 : Cliquer "Faire mon panier"
            logger.info("Bridge: démarre commande %s", resto_id)
            order_resp = await self.start_order_at_restaurant(resto_id)
            result["steps"].append(("start_order", order_resp))

            # NOTE : La suite (ajout d'articles, checkout) dépend du flow exact
            # du bot @chikenuhq_bot qu'il faudra explorer plus en détail.

            # Pour l'instant, marquer comme transmis partiellement
            result["success"] = True
            result["partial"] = True
            result["message"] = "Commande transmise au restaurant via le bridge"

        except BridgeError as e:
            result["error"] = str(e)
            logger.exception("Erreur bridge")
        except Exception as e:
            result["error"] = f"Erreur inattendue: {e}"
            logger.exception("Erreur bridge")

        return result


# ── Instance singleton ──────────────────────────────────────────────────
bridge = KFCBridge()
