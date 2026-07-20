"""User store — SQLite, zero-config.

One ``users`` table: username (pk), password hash, roles, project_ids. Roles and
project ids are stored as comma-separated text (small, append-rarely lists; a
join table would be overkill here). The store never sees plaintext — callers
hash via ``security.hash_password`` before ``create``.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass
class User:
    username: str
    roles: list[str]
    project_ids: list[str]
    password_hash: str = ""

    def public(self) -> dict:
        return {"username": self.username, "roles": self.roles,
                "project_ids": self.project_ids}


def _split(s: str | None) -> list[str]:
    return [p for p in (s or "").split(",") if p]


class UserStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("AUTH_DB", "/data/auth.db")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   username      TEXT PRIMARY KEY,
                   password_hash TEXT NOT NULL,
                   roles         TEXT NOT NULL DEFAULT '',
                   project_ids   TEXT NOT NULL DEFAULT ''
               )"""
        )
        self._conn.commit()

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            username=row["username"],
            roles=_split(row["roles"]),
            project_ids=_split(row["project_ids"]),
            password_hash=row["password_hash"],
        )

    def get(self, username: str) -> User | None:
        cur = self._conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        return self._row_to_user(row) if row else None

    def list(self) -> list[User]:
        cur = self._conn.execute("SELECT * FROM users ORDER BY username")
        return [self._row_to_user(r) for r in cur.fetchall()]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create(
        self, username: str, password_hash: str,
        roles: list[str], project_ids: list[str],
    ) -> User:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, roles, project_ids)"
                    " VALUES (?, ?, ?, ?)",
                    (username, password_hash, ",".join(roles), ",".join(project_ids)),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"user '{username}' already exists") from exc
        return User(username, roles, project_ids, password_hash)

    def update(
        self, username: str, roles: list[str], project_ids: list[str],
        password_hash: str | None = None,
    ) -> User | None:
        """Update a user's roles/project scope (and optionally password).

        Returns the updated user, or ``None`` if no such user. A blank
        ``password_hash`` leaves the existing password untouched.
        """
        with self._lock:
            existing = self.get(username)
            if existing is None:
                return None
            new_hash = password_hash or existing.password_hash
            self._conn.execute(
                "UPDATE users SET roles = ?, project_ids = ?, password_hash = ?"
                " WHERE username = ?",
                (",".join(roles), ",".join(project_ids), new_hash, username),
            )
            self._conn.commit()
        return User(username, roles, project_ids, new_hash)

    def admin_count(self) -> int:
        """How many users currently hold the ``admin`` role (lockout guard)."""
        return sum(1 for u in self.list() if "admin" in u.roles)

    def delete(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM users WHERE username = ?", (username,))
            self._conn.commit()
        return cur.rowcount > 0
