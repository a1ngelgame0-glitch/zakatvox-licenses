import json
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    summary TEXT,
    pending_context TEXT,
    last_report_month TEXT,
    last_checkin_week TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    category TEXT NOT NULL,
    note TEXT,
    spent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budgets (
    user_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    monthly_limit REAL NOT NULL,
    PRIMARY KEY (user_id, category)
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    target_amount REAL,
    saved_amount REAL NOT NULL DEFAULT 0,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    plan TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    repeat TEXT NOT NULL DEFAULT 'none',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def _all(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params=()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # --- пользователи ---

    def ensure_user(self, user_id: int) -> dict:
        self._exec("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        return self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def all_users(self) -> list[dict]:
        return self._all("SELECT * FROM users")

    def set_user_field(self, user_id: int, field: str, value) -> None:
        assert field in {"summary", "pending_context", "last_report_month", "last_checkin_week"}
        self._exec(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))

    def add_pending_context(self, user_id: int, text: str) -> None:
        """Заметка, которая подмешается в следующее сообщение пользователя для ИИ."""
        current = self.ensure_user(user_id)["pending_context"]
        self.set_user_field(user_id, "pending_context", f"{current}\n{text}" if current else text)

    def pop_pending_context(self, user_id: int) -> str | None:
        current = self.ensure_user(user_id)["pending_context"]
        if current:
            self.set_user_field(user_id, "pending_context", None)
        return current

    # --- история диалога (только дописываем в конец, ничего не редактируем) ---

    def load_history(self, user_id: int) -> list[dict]:
        rows = self._all("SELECT role, content FROM messages WHERE user_id = ? ORDER BY id", (user_id,))
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def append_history(self, user_id: int, messages: list[dict]) -> None:
        self.conn.executemany(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            [(user_id, m["role"], json.dumps(m["content"], ensure_ascii=False)) for m in messages],
        )
        self.conn.commit()

    def clear_history(self, user_id: int) -> None:
        self._exec("DELETE FROM messages WHERE user_id = ?", (user_id,))

    # --- траты ---

    def add_expense(self, user_id: int, amount: float, category: str, note: str | None, spent_at: str) -> int:
        cur = self._exec(
            "INSERT INTO expenses (user_id, amount, category, note, spent_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, category, note, spent_at),
        )
        return cur.lastrowid

    def delete_expense(self, user_id: int, expense_id: int) -> bool:
        cur = self._exec("DELETE FROM expenses WHERE user_id = ? AND id = ?", (user_id, expense_id))
        return cur.rowcount > 0

    def expenses(self, user_id: int, start: str, end: str, category: str | None = None) -> list[dict]:
        sql = "SELECT * FROM expenses WHERE user_id = ? AND spent_at BETWEEN ? AND ?"
        params: list = [user_id, start, end]
        if category:
            sql += " AND category = ?"
            params.append(category)
        return self._all(sql + " ORDER BY spent_at, id", params)

    def totals_by_category(self, user_id: int, start: str, end: str) -> dict[str, float]:
        rows = self._all(
            "SELECT category, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_at BETWEEN ? AND ? GROUP BY category ORDER BY total DESC",
            (user_id, start, end),
        )
        return {r["category"]: round(r["total"], 2) for r in rows}

    # --- бюджеты ---

    def set_budget(self, user_id: int, category: str, limit: float) -> None:
        if limit <= 0:
            self._exec("DELETE FROM budgets WHERE user_id = ? AND category = ?", (user_id, category))
        else:
            self._exec(
                "INSERT INTO budgets (user_id, category, monthly_limit) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
                (user_id, category, limit),
            )

    def budgets(self, user_id: int) -> dict[str, float]:
        rows = self._all("SELECT category, monthly_limit FROM budgets WHERE user_id = ?", (user_id,))
        return {r["category"]: r["monthly_limit"] for r in rows}

    # --- цели ---

    def add_goal(self, user_id: int, title: str, target: float | None, deadline: str | None) -> int:
        cur = self._exec(
            "INSERT INTO goals (user_id, title, target_amount, deadline, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, target, deadline, _now_iso()),
        )
        return cur.lastrowid

    def goals(self, user_id: int, only_active: bool = True) -> list[dict]:
        sql = "SELECT * FROM goals WHERE user_id = ?"
        if only_active:
            sql += " AND status = 'active'"
        return self._all(sql + " ORDER BY id", (user_id,))

    def goal(self, user_id: int, goal_id: int) -> dict | None:
        return self._one("SELECT * FROM goals WHERE user_id = ? AND id = ?", (user_id, goal_id))

    def update_goal(self, user_id: int, goal_id: int, **fields) -> bool:
        allowed = {"title", "target_amount", "saved_amount", "deadline", "status", "plan"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self._exec(
            f"UPDATE goals SET {sets} WHERE user_id = ? AND id = ?",
            (*fields.values(), user_id, goal_id),
        )
        return cur.rowcount > 0

    # --- напоминания ---

    def add_reminder(self, user_id: int, text: str, due_at_utc: str, repeat: str) -> int:
        cur = self._exec(
            "INSERT INTO reminders (user_id, text, due_at, repeat) VALUES (?, ?, ?, ?)",
            (user_id, text, due_at_utc, repeat),
        )
        return cur.lastrowid

    def reminders(self, user_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM reminders WHERE user_id = ? AND active = 1 ORDER BY due_at", (user_id,)
        )

    def due_reminders(self, now_utc: str) -> list[dict]:
        return self._all("SELECT * FROM reminders WHERE active = 1 AND due_at <= ?", (now_utc,))

    def reschedule_reminder(self, reminder_id: int, due_at_utc: str | None) -> None:
        if due_at_utc is None:
            self._exec("UPDATE reminders SET active = 0 WHERE id = ?", (reminder_id,))
        else:
            self._exec("UPDATE reminders SET due_at = ? WHERE id = ?", (due_at_utc, reminder_id))

    def cancel_reminder(self, user_id: int, reminder_id: int) -> bool:
        cur = self._exec(
            "UPDATE reminders SET active = 0 WHERE user_id = ? AND id = ? AND active = 1",
            (user_id, reminder_id),
        )
        return cur.rowcount > 0

    # --- заметки о пользователе («память») ---

    def add_note(self, user_id: int, text: str) -> int:
        cur = self._exec(
            "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)", (user_id, text, _now_iso())
        )
        return cur.lastrowid

    def notes(self, user_id: int) -> list[dict]:
        return self._all("SELECT id, text FROM notes WHERE user_id = ? ORDER BY id", (user_id,))

    def delete_note(self, user_id: int, note_id: int) -> bool:
        cur = self._exec("DELETE FROM notes WHERE user_id = ? AND id = ?", (user_id, note_id))
        return cur.rowcount > 0
