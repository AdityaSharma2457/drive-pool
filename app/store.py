import hashlib
import json
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY,
                credentials BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY,
                verifier BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _cipher() -> Fernet:
    key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    return Fernet(key.encode("ascii"))


def save_oauth_state(database_path: Path, state: str, verifier: str) -> None:
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    encrypted_verifier = _cipher().encrypt(verifier.encode("utf-8"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM oauth_states WHERE created_at < datetime('now', '-10 minutes')"
        )
        connection.execute(
            "INSERT INTO oauth_states (state_hash, verifier) VALUES (?, ?) "
            "ON CONFLICT(state_hash) DO UPDATE SET verifier = excluded.verifier, "
            "created_at = CURRENT_TIMESTAMP",
            (state_hash, encrypted_verifier),
        )


def consume_oauth_state(database_path: Path, state: str) -> str | None:
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT verifier FROM oauth_states "
            "WHERE state_hash = ? AND created_at >= datetime('now', '-10 minutes')",
            (state_hash,),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
    return _cipher().decrypt(row[0]).decode("utf-8")


def save_account(database_path: Path, email: str, credentials: str) -> None:
    encrypted = _cipher().encrypt(credentials.encode("utf-8"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO accounts (email, credentials) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET credentials = excluded.credentials",
            (email, encrypted),
        )


def list_accounts(database_path: Path) -> list[dict[str, str]]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT email, created_at FROM accounts ORDER BY created_at, email"
        ).fetchall()
    return [dict(row) for row in rows]


def get_credentials(database_path: Path, email: str) -> dict:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT credentials FROM accounts WHERE email = ?", (email,)
        ).fetchone()
    if row is None:
        raise KeyError(email)
    decrypted = _cipher().decrypt(row[0]).decode("utf-8")
    return json.loads(decrypted)


def delete_account(database_path: Path, email: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM accounts WHERE email = ?", (email,))