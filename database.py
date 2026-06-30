"""
database.py — Gestion de la base SQLite pour O'KFC Bot

Tables :
  - users : utilisateurs Telegram avec solde interne
  - deposits : historique des dépôts
  - crypto_invoices : suivi des paiements crypto
"""

import sqlite3
import os
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(__file__), "kfc_bot.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Crée les tables si elles n'existent pas."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id  INTEGER PRIMARY KEY,
                bot_balance  REAL NOT NULL DEFAULT 0.00,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS deposits (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL,
                amount         REAL NOT NULL,
                method         TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                admin_msg_id   INTEGER,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS crypto_invoices (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                deposit_id     INTEGER NOT NULL,
                amount_eur     REAL NOT NULL,
                amount_crypto  REAL,
                currency       TEXT DEFAULT 'USDT',
                address        TEXT,
                txid           TEXT,
                status         TEXT DEFAULT 'pending',
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (deposit_id) REFERENCES deposits(id)
            );
        """)


# ── Users ──────────────────────────────────────────────────────────────────

def get_or_create_user(telegram_id: int) -> Dict[str, Any]:
    """Retourne l'utilisateur ou le crée avec solde 0."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO users (telegram_id, bot_balance) VALUES (?, 0.00)",
            (telegram_id,),
        )
        return {"telegram_id": telegram_id, "bot_balance": 0.00}


def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def update_balance(telegram_id: int, new_balance: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET bot_balance = ? WHERE telegram_id = ?",
            (round(new_balance, 2), telegram_id),
        )


def credit_user(telegram_id: int, amount: float) -> float:
    """Ajoute un montant au solde, retourne le nouveau solde."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT bot_balance FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (telegram_id, bot_balance) VALUES (?, ?)",
                (telegram_id, round(amount, 2)),
            )
            return round(amount, 2)
        new_bal = round(row["bot_balance"] + amount, 2)
        conn.execute(
            "UPDATE users SET bot_balance = ? WHERE telegram_id = ?",
            (new_bal, telegram_id),
        )
        return new_bal


def debit_user(telegram_id: int, amount: float) -> Optional[float]:
    """Débite un montant du solde si suffisant, retourne le nouveau solde ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT bot_balance FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not row or row["bot_balance"] < amount:
            return None
        new_bal = round(row["bot_balance"] - amount, 2)
        conn.execute(
            "UPDATE users SET bot_balance = ? WHERE telegram_id = ?",
            (new_bal, telegram_id),
        )
        return new_bal


# ── Dépôts ─────────────────────────────────────────────────────────────────

def create_deposit(telegram_id: int, amount: float, method: str) -> int:
    """Crée une demande de dépôt, retourne son ID."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO deposits (telegram_id, amount, method) VALUES (?, ?, ?)",
            (telegram_id, round(amount, 2), method),
        )
        return cur.lastrowid


def get_deposit(deposit_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM deposits WHERE id = ?", (deposit_id,)
        ).fetchone()
        return dict(row) if row else None


def update_deposit_status(deposit_id: int, status: str, admin_msg_id: int = None) -> None:
    with _connect() as conn:
        if admin_msg_id:
            conn.execute(
                "UPDATE deposits SET status = ?, admin_msg_id = ? WHERE id = ?",
                (status, admin_msg_id, deposit_id),
            )
        else:
            conn.execute(
                "UPDATE deposits SET status = ? WHERE id = ?",
                (status, deposit_id),
            )


def get_deposits_by_user(telegram_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM deposits WHERE telegram_id = ? ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_deposits() -> List[Dict[str, Any]]:
    """Retourne tous les dépôts en attente (pour l'admin)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM deposits WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Crypto invoices ────────────────────────────────────────────────────────

def create_crypto_invoice(deposit_id: int, amount_eur: float, address: str, currency: str = "USDT") -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO crypto_invoices (deposit_id, amount_eur, address, currency)
               VALUES (?, ?, ?, ?)""",
            (deposit_id, round(amount_eur, 2), address, currency),
        )
        return cur.lastrowid


def get_crypto_invoice(invoice_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        return dict(row) if row else None


def get_pending_crypto_invoices() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM crypto_invoices WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def confirm_crypto_invoice(invoice_id: int, txid: str, amount_crypto: float) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE crypto_invoices
               SET status = 'confirmed', txid = ?, amount_crypto = ?
               WHERE id = ?""",
            (txid, round(amount_crypto, 8), invoice_id),
        )


init_db()
