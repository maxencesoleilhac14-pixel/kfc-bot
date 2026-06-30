"""
kfc_api.py — Wrapper complet de l'API KFC Loyalty

Authentification :
  - B2B OAuth v3 (login + refresh token)
  - CC v1.6 (admin_kfc)

Tous les appels sont asynchrones (httpx.AsyncClient).
Le refresh du token B2B est automatique.
"""

import os
import logging
from typing import Any, Dict, List, Optional

import httpx

from database import get_token, save_token, invalidate_token

logger = logging.getLogger(__name__)

# ── Configuration depuis les variables d'environnement ─────────────────────
B2B_USERNAME = os.getenv("B2B_USERNAME", "b2b_api")
B2B_PASSWORD = os.getenv("B2B_PASSWORD", "hGJ7x7C8PKCGWZLk2HfSqHchAxE")
CC_USERNAME = os.getenv("CC_USERNAME", "admin_kfc")
CC_PASSWORD = os.getenv("CC_PASSWORD", "yyBzwmNEgrV8vyvddz5Ptj9xC94V...")
BASE_URL = os.getenv("KFC_BASE_URL", "https://b2b-lmckfcuat.eu12.loyalty-comarch.com")
CC_URL = os.getenv("KFC_CC_URL", "https://cc-lmc-kfc-uat.eu9.loyalty-comarch.com")
API_VERSION = os.getenv("KFC_API_VERSION", "v3")


class KFCAPIError(Exception):
    """Exception levée quand l'API KFC retourne une erreur."""

    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(f"[{status}] {detail}")


class KFCAPI:
    """Client HTTP asynchrone pour l'API KFC Loyalty."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    # ── Gestion du client HTTP ──────────────────────────────────────────

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0, verify=False)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Authentification B2B ────────────────────────────────────────────

    async def _get_b2b_token(self) -> str:
        """
        Retourne un token B2B valide, en le refresher / re-créant si nécessaire.
        """
        now = int(__import__("time").time())
        stored = get_token("b2b")

        # Token encore valide (avec 60s de marge)
        if stored and stored["expires_at"] > now + 60:
            return stored["access_token"]

        # Tentative de refresh si on a un refresh_token
        if stored and stored.get("refresh_token"):
            try:
                return await self._refresh_b2b(stored["refresh_token"])
            except Exception as exc:
                logger.warning("Refresh token invalide, nouveau login : %s", exc)

        # Login complet
        return await self._login_b2b()

    async def _login_b2b(self) -> str:
        """POST /b2b/oauth/v3/login — authentification B2B."""
        data = {"username": B2B_USERNAME, "password": B2B_PASSWORD}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.client.post(
            f"{BASE_URL}/b2b/oauth/v3/login",
            data=data,
            headers=headers,
        )
        if resp.status_code != 200:
            raise KFCAPIError(resp.status_code, resp.text)
        body = resp.json()
        access = body["access_token"]
        refresh = body.get("refresh_token")
        expires = body.get("expires_in", 3600)
        save_token("b2b", access, refresh, expires)
        logger.info("Nouveau token B2B obtenu (expire dans %ss)", expires)
        return access

    async def _refresh_b2b(self, old_refresh: str) -> str:
        """POST /b2b/oauth/v2/refresh-token — rafraîchit le token B2B."""
        data = {"refresh_token": old_refresh}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.client.post(
            f"{BASE_URL}/b2b/oauth/v2/refresh-token",
            data=data,
            headers=headers,
        )
        if resp.status_code != 200:
            raise KFCAPIError(resp.status_code, resp.text)
        body = resp.json()
        access = body["access_token"]
        refresh = body.get("refresh_token", old_refresh)
        expires = body.get("expires_in", 3600)
        save_token("b2b", access, refresh, expires)
        logger.info("Token B2B rafraîchi (expire dans %ss)", expires)
        return access

    # ── Authentification CC ─────────────────────────────────────────────

    async def _get_cc_token(self) -> str:
        """Retourne un token CC valide."""
        now = int(__import__("time").time())
        stored = get_token("cc")
        if stored and stored["expires_at"] > now + 60:
            return stored["access_token"]
        return await self._login_cc()

    async def _login_cc(self) -> str:
        """POST /cc/v1.6/login — authentification CC (admin)."""
        data = {"username": CC_USERNAME, "password": CC_PASSWORD}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.client.post(
            f"{CC_URL}/cc/v1.6/login",
            data=data,
            headers=headers,
        )
        if resp.status_code != 200:
            raise KFCAPIError(resp.status_code, resp.text)
        body = resp.json()
        access = body["access_token"]
        expires = body.get("expires_in", 3600)
        save_token("cc", access, None, expires)
        logger.info("Nouveau token CC obtenu")
        return access

    # ── Requête authentifiée générique ──────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        use_cc: bool = False,
        **kwargs,
    ) -> Any:
        """
        Exécute une requête HTTP authentifiée vers l'API KFC.

        Args :
            method : HTTP method (GET / POST / PATCH / DELETE)
            path   : chemin relatif (ex: /b2b/profile/v3/customers/...)
            use_cc : utiliser le token CC au lieu du B2B
        """
        token = await self._get_cc_token() if use_cc else await self._get_b2b_token()
        base = CC_URL if use_cc else BASE_URL
        url = f"{base}{path}"

        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {token}")
        headers.setdefault("Content-Type", "application/json")

        resp = await self.client.request(method, url, headers=headers, **kwargs)

        # Tentative de rejeu si 401 (token expiré entre temps)
        if resp.status_code == 401:
            logger.info("Token expiré, re-authentification…")
            invalidate_token("cc" if use_cc else "b2b")
            token = await self._get_cc_token() if use_cc else await self._get_b2b_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = await self.client.request(method, url, headers=headers, **kwargs)

        if resp.status_code >= 400:
            detail = resp.text[:500] if resp.text else "erreur inconnue"
            raise KFCAPIError(resp.status_code, detail)

        if resp.text and resp.text.strip():
            try:
                return resp.json()
            except Exception:
                return resp.text
        return {}

    # ── Endpoints Profil ────────────────────────────────────────────────

    async def get_profile(self, identifier_no: str) -> Dict[str, Any]:
        """
        GET /b2b/profile/v3/customers/identifierNo={id}
        Retourne le profil complet d'un membre.
        """
        return await self._request(
            "GET",
            f"/b2b/profile/v3/customers/identifierNo={identifier_no}",
        )

    async def get_balance(self, identifier_no: str) -> Dict[str, Any]:
        """
        POST /b2b/profile/{version}/balance-inquiries?identifierNo={id}
        Retourne le solde en points.
        Body : {"partner": "KFC"}
        """
        return await self._request(
            "POST",
            f"/b2b/profile/{API_VERSION}/balance-inquiries?identifierNo={identifier_no}",
            json={"partner": "KFC"},
        )

    async def get_member_attributes(
        self, identifier_no: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        GET /b2b/profile/{version}/customers/member-attributes
        Retourne la liste des attributs d'un membre.
        """
        params = f"?identifierNo={identifier_no}" if identifier_no else ""
        result = await self._request(
            "GET",
            f"/b2b/profile/{API_VERSION}/customers/member-attributes{params}",
        )
        return result if isinstance(result, list) else result.get("attributes", [])

    async def update_profile(
        self, customer_id: str, attributes: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """
        PATCH /b2b/profile/v3/customers/{id}
        Met à jour le profil. Attributes = [{"code": "...", "value": "..."}]
        """
        return await self._request(
            "PATCH",
            f"/b2b/profile/v3/customers/{customer_id}",
            json={"predefinedAttributes": attributes},
        )

    async def enroll_member(
        self, attributes: List[Dict[str, str]], simulate: bool = False
    ) -> Dict[str, Any]:
        """
        POST /b2b/profile/v3/enrollment?simulate=false
        Crée un nouveau compte membre.
        """
        return await self._request(
            "POST",
            f"/b2b/profile/v3/enrollment?simulate={str(simulate).lower()}",
            json={"predefinedAttributes": attributes},
        )

    async def close_account(self, customer_id: str) -> Dict[str, Any]:
        """
        POST /b2b/profile/{version}/customers/{id}/close
        Ferme un compte.
        """
        return await self._request(
            "POST",
            f"/b2b/profile/{API_VERSION}/customers/{customer_id}/close",
        )

    async def generic_event(
        self, identifier_no: str, event_type: str, simulate: bool = False
    ) -> Dict[str, Any]:
        """
        POST /b2b/trnprocessor/{version}/generic-events?identifierNo={id}&simulate=false
        Body : {"partner": "KFC", "type": "UO"}
        Utilisé pour la mise à jour des anciens membres.
        """
        return await self._request(
            "POST",
            f"/b2b/trnprocessor/{API_VERSION}/generic-events"
            f"?identifierNo={identifier_no}&simulate={str(simulate).lower()}",
            json={"partner": "KFC", "type": event_type},
        )

    # ── Endpoints Récompenses ───────────────────────────────────────────

    async def get_rewards(
        self, api_version: Optional[str] = None, filters: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        """
        GET /b2b/redemption/{version}/rewards
        Retourne toutes les récompenses (avec filtre optionnel).
        """
        ver = api_version or API_VERSION
        url = f"/b2b/redemption/{ver}/rewards"
        if filters:
            # Exemple : points min / max en query params
            params = "&".join(f"{k}={v}" for k, v in filters.items())
            url += f"?{params}"
        result = await self._request("GET", url)
        return result if isinstance(result, list) else result.get("rewards", result.get("data", []))

    async def get_reward_detail(
        self, reward_id: int, api_version: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        GET /b2b/redemption/{version}/rewards/{rewardId}
        Détail d'une récompense.
        """
        ver = api_version or API_VERSION
        return await self._request("GET", f"/b2b/redemption/{ver}/rewards/{reward_id}")

    async def get_reward_categories(
        self, api_version: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        GET /b2b/redemption/{version}/reward-categories
        Catégories de récompenses.
        """
        ver = api_version or API_VERSION
        result = await self._request("GET", f"/b2b/redemption/{ver}/reward-categories")
        return result if isinstance(result, list) else result.get("categories", [])

    async def redeem_reward(
        self,
        identifier_no: str,
        price_plan_code: str,
        quantity: int = 1,
        simulate: bool = False,
    ) -> Dict[str, Any]:
        """
        POST /b2b/trnprocessor/{version}/redemptions?identifierNo={id}&simulate=false
        Body : {"rewards": [{"pricePlanCode": "...", "quantity": 2}]}
        Utilise/échange des points contre une récompense.
        """
        return await self._request(
            "POST",
            f"/b2b/trnprocessor/{API_VERSION}/redemptions"
            f"?identifierNo={identifier_no}&simulate={str(simulate).lower()}",
            json={
                "rewards": [{"pricePlanCode": price_plan_code, "quantity": quantity}]
            },
        )

    async def get_reward_order(
        self, identifier_no: str, order_id: str, api_version: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        GET /b2b/redemption/{version}/rewards-orders/identifierNo={id}/orderId={orderId}
        Détail d'une commande de récompense.
        """
        ver = api_version or API_VERSION
        return await self._request(
            "GET",
            f"/b2b/redemption/{ver}/rewards-orders"
            f"/identifierNo={identifier_no}/orderId={order_id}",
        )

    async def cancel_reward_order(
        self, trn_id: str, api_version: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        POST /b2b/redemption/{version}/rewards-orders/trnId={trnId}/cancel
        Annule une récompense par transaction ID.
        """
        ver = api_version or API_VERSION
        return await self._request(
            "POST",
            f"/b2b/redemption/{ver}/rewards-orders/trnId={trn_id}/cancel",
        )

    # ── Endpoints Transactions / Ventes ─────────────────────────────────

    async def record_sale(
        self,
        identifier_no: str,
        products: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        POST /b2b/trnprocessor/{version}/sales?identifierNo={id}
        Enregistre une vente (gagne des points).
        Body : {"partner": "KFC", "products": [{"code": 28545, "quantity": 1, "value": 9.15}]}
        """
        return await self._request(
            "POST",
            f"/b2b/trnprocessor/{API_VERSION}/sales?identifierNo={identifier_no}",
            json={"partner": "KFC", "products": products},
        )

    async def reverse_transaction(self, trn_id: str) -> Dict[str, Any]:
        """
        POST /b2b/trnprocessor/{version}/transactions/trnId={id}/reverse
        Annule une transaction.
        """
        return await self._request(
            "POST",
            f"/b2b/trnprocessor/{API_VERSION}/transactions/trnId={trn_id}/reverse",
        )

    async def return_products(self, trn_id: str) -> Dict[str, Any]:
        """
        POST /b2b/trnprocessor/{version}/transactions/{id}/return
        Retourne des produits d'une transaction.
        """
        return await self._request(
            "POST",
            f"/b2b/trnprocessor/{API_VERSION}/transactions/{trn_id}/return",
        )

    async def get_points_expiration(self, trn_id: str) -> Dict[str, Any]:
        """
        GET /b2b/transaction/v2/transactions/{id}/points-expiration-forecast
        Prévisions d'expiration des points.
        """
        return await self._request(
            "GET",
            f"/b2b/transaction/v2/transactions/{trn_id}/points-expiration-forecast",
        )

    # ── Dictionnaires ───────────────────────────────────────────────────

    async def get_dictionary(self, dic_name: str) -> List[Dict[str, Any]]:
        """
        GET /b2b/dictionary/{version}/dictionaries?dic={name}
        Retourne les entrées d'un dictionnaire (codes, libellés…).
        """
        result = await self._request(
            "GET",
            f"/b2b/dictionary/{API_VERSION}/dictionaries?dic={dic_name}",
        )
        return result if isinstance(result, list) else result.get("entries", [])
