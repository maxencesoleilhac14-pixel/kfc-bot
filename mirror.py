"""
mirror.py — Miroir temps réel vers @chikenuhq_bot

Toutes les actions de l'user sont mirrorées sur le bot du collègue
via le bridge Telethon, SAUF le bouton "Valider la commande" qui
est intercepté par bot.py pour la vérification de solde.

⚠️ File d'attente : 1 seul user à la fois sur le bridge.
Les autres reçoivent "⏳ Occupé" jusqu'à libération.
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from config import BRIDGE_ENABLED, DEFAULT_IMAGE_URL

if BRIDGE_ENABLED:
    from bridge import bridge as telethon_bridge

logger = logging.getLogger("mirror")
BRIDGE_TIMEOUT = 300  # 5 min avant libération auto


class MirrorError(Exception):
    pass


class MirrorBusyError(MirrorError):
    """Bridge occupé par un autre user."""


class Mirror:
    """Interface de mirroring avec file d'attente (1 user à la fois)."""

    def __init__(self):
        self._current_user: Optional[int] = None
        self._last_activity: float = 0
        self._queue: List[int] = []
        self._queue_events: Dict[int, asyncio.Event] = {}

    # ── File d'attente ────────────────────────────────────────────────

    def acquire(self, user_id: int) -> bool:
        """Tente d'acquérir le bridge pour un user. Retourne True si OK."""
        if self._current_user == user_id:
            self._last_activity = time.time()
            return True
        if self._current_user is not None:
            if time.time() - self._last_activity > BRIDGE_TIMEOUT:
                logger.info("🔓 Bridge libéré (timeout %ss) pour %s",
                            BRIDGE_TIMEOUT, self._current_user)
                self._current_user = None
            else:
                return False
        self._current_user = user_id
        self._last_activity = time.time()
        logger.info("🔒 Bridge acquis par user %s", user_id)
        return True

    def release(self, user_id: int):
        """Libère le bridge."""
        if self._current_user == user_id:
            logger.info("🔓 Bridge libéré par user %s", user_id)
            self._current_user = None
            self._last_activity = 0
            self._notify_next()

    def _check(self, user_id: int):
        """Vérifie que l'user possède le bridge."""
        if self._current_user is not None and self._current_user != user_id:
            raise MirrorBusyError(
                "⏳ Un autre utilisateur commande actuellement.\n"
                "Réessaie dans quelques instants."
            )
        # Si pas d'owner ou timeout, on prend
        if self._current_user is None or time.time() - self._last_activity > BRIDGE_TIMEOUT:
            self.acquire(user_id)

    # ── Queue publique ────────────────────────────────────────────────

    def queue_length(self) -> int:
        return len(self._queue)

    def queue_position(self, user_id: int) -> int:
        try:
            return self._queue.index(user_id) + 1
        except ValueError:
            return 0

    def acquire_or_queue(self, user_id: int) -> Tuple[bool, int]:
        """Returns (acquired, queue_position). Si non acquis, ajoute à la file."""
        if self.acquire(user_id):
            return True, 0
        if user_id not in self._queue:
            self._queue.append(user_id)
            self._queue_events[user_id] = asyncio.Event()
        return False, self.queue_position(user_id)

    def remove_from_queue(self, user_id: int):
        if user_id in self._queue:
            self._queue.remove(user_id)
            self._queue_events.pop(user_id, None)
            logger.info("User %s retiré de la file d'attente", user_id)

    async def wait_until_acquired(self, user_id: int):
        """Bloque jusqu'à ce que l'user soit appelé (via son event)."""
        event = self._queue_events.get(user_id)
        if event:
            await event.wait()

    def _notify_next(self):
        """Signale au prochain dans la file que le bridge est libre."""
        if self._queue:
            next_uid = self._queue.pop(0)
            event = self._queue_events.pop(next_uid, None)
            if event:
                event.set()

    # ── Bridge ────────────────────────────────────────────────────────

    async def ensure_connected(self):
        if BRIDGE_ENABLED:
            await telethon_bridge.ensure_connected()

    async def _bridge_call(self, fn, *args, retries: int = 1, **kwargs):
        """Appelle une fonction du bridge avec reconnexion automatique si déconnecté."""
        for attempt in range(retries + 1):
            try:
                await self.ensure_connected()
                return await fn(*args, **kwargs)
            except (ConnectionError, OSError, RuntimeError) as e:
                if "disconnect" in str(e).lower() and attempt < retries:
                    logger.warning("Bridge déconnecté, reconnexion (tentative %d/%d)", attempt + 1, retries)
                    telethon_bridge._ready.clear()
                    await asyncio.sleep(1)
                    continue
                raise

    async def search_restaurants(self, query: str) -> Optional[Dict]:
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        return await self._bridge_call(telethon_bridge.search_restaurants, query)

    async def select_restaurant(self, resto_id: str, msg_id: Optional[int] = None) -> Optional[Dict]:
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        return await self._bridge_call(telethon_bridge.select_restaurant, resto_id, msg_id)

    async def start_order(self, resto_id: str, msg_id: Optional[int] = None) -> Optional[Dict]:
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        return await self._bridge_call(telethon_bridge.start_order_at_restaurant, resto_id, msg_id)

    async def click_callback(self, cb: str, msg_id: Optional[int] = None) -> None:
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        await self._bridge_call(telethon_bridge.click_callback, cb.encode(), msg_id)

    async def get_last_message(self) -> Optional[Dict]:
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        return await self._bridge_call(telethon_bridge.get_last_bot_message)

    async def forward_click(self, callback_data: str) -> Optional[Dict]:
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        await self._bridge_call(telethon_bridge.click_callback, callback_data.encode())
        resp = await self._bridge_call(telethon_bridge.wait_for_bot_response, timeout=3.0)
        if not resp:
            resp = await self._bridge_call(telethon_bridge.get_last_bot_message)
        return resp

    async def send_text(self, text: str) -> None:
        """Envoie un message texte au bot pote."""
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        await self._bridge_call(telethon_bridge.send_command, text)

    async def wait_for_response(self, timeout: float = 10.0) -> Optional[Dict]:
        """Attend un nouveau message du bot pote (polling)."""
        if not BRIDGE_ENABLED:
            raise MirrorError("Bridge désactivé")
        return await self._bridge_call(telethon_bridge.wait_for_bot_response, timeout=timeout)

    # ── Traitement réponse ────────────────────────────────────────────

    def process_bridge_response(self, resp: Dict) -> Dict:
        if not resp:
            return resp
        processed = dict(resp)
        if processed.get("text"):
            processed["text"] = self.detect_price_in_text(processed["text"])
        buttons_rows = processed.get("buttons_rows", [])
        all_btn_text = " ".join(
            b.get("text", "") for row in buttons_rows for b in row
        )
        garder = "Choisir mes options" in all_btn_text or "Ajouter" in all_btn_text
        if not garder:
            processed["photo_bytes"] = None
        return processed

    def detect_price_in_text(self, text: str) -> str:
        def _majorer(m):
            prix_str = m.group(1).replace(",", ".")
            try:
                prix = float(prix_str)
                from config import PRICE_INCREASE
                nouveau = prix + PRICE_INCREASE
                return f"{nouveau:.2f}€"
            except ValueError:
                return m.group(0)
        return re.sub(r'(\d+[.,]\d{2})\s*€', _majorer, text)


mirror = Mirror()
