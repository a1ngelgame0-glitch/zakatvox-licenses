"""Инструменты, которыми ИИ управляет тратами, целями, напоминаниями и памятью."""

import json
from datetime import date

from config import CATEGORIES, CURRENCY
from db import DB
from timeutil import local_now, local_to_utc_iso, month_range, utc_iso_to_local

DATE = {"type": "string", "description": "Дата в формате YYYY-MM-DD"}

TOOLS = [
    {
        "name": "add_expense",
        "description": "Записать трату. Вызывай, когда пользователь пишет что-то вроде «кофе 250» или «вчера такси 600».",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": f"Сумма в {CURRENCY}"},
                "category": {"type": "string", "enum": CATEGORIES},
                "note": {"type": "string", "description": "Что именно купили, коротко"},
                "date": {**DATE, "description": "Дата траты YYYY-MM-DD; по умолчанию сегодня"},
            },
            "required": ["amount", "category"],
        },
    },
    {
        "name": "list_expenses",
        "description": "Получить траты за период: итоги по категориям, бюджеты и последние записи (с id).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": DATE,
                "end_date": DATE,
                "category": {"type": "string", "enum": CATEGORIES},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "delete_expense",
        "description": "Удалить трату по id (например, если записали по ошибке).",
        "input_schema": {
            "type": "object",
            "properties": {"expense_id": {"type": "integer"}},
            "required": ["expense_id"],
        },
    },
    {
        "name": "set_budget",
        "description": "Задать месячный лимит на категорию. 0 — убрать лимит.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "monthly_limit": {"type": "number"},
            },
            "required": ["category", "monthly_limit"],
        },
    },
    {
        "name": "add_goal",
        "description": "Создать цель (например «накопить на ноутбук 80000 к марту» или «бегать 3 раза в неделю»).",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "target_amount": {"type": "number", "description": "Сколько нужно накопить, если цель денежная"},
                "deadline": DATE,
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_goals",
        "description": "Показать цели пользователя с прогрессом.",
        "input_schema": {
            "type": "object",
            "properties": {"include_done": {"type": "boolean"}},
        },
    },
    {
        "name": "update_goal",
        "description": "Обновить цель: добавить отложенные деньги, переименовать, сменить срок или отметить выполненной.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
                "add_saved": {"type": "number", "description": "Сколько пользователь отложил сейчас (прибавится)"},
                "title": {"type": "string"},
                "target_amount": {"type": "number"},
                "deadline": DATE,
                "status": {"type": "string", "enum": ["active", "done", "dropped"]},
            },
            "required": ["goal_id"],
        },
    },
    {
        "name": "add_reminder",
        "description": "Поставить напоминание. Время — местное время пользователя.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Что напомнить, от второго лица: «Позвонить маме»"},
                "when": {"type": "string", "description": "Местное время в формате YYYY-MM-DD HH:MM"},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly"]},
            },
            "required": ["text", "when"],
        },
    },
    {
        "name": "list_reminders",
        "description": "Показать активные напоминания (с id).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "Отменить напоминание по id.",
        "input_schema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Запомнить важный факт о пользователе надолго: имя, зарплату, день зарплаты, привычки, "
            "предпочтения, важные даты. Не записывай мелочи из разговора."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string"}},
            "required": ["fact"],
        },
    },
    {
        "name": "list_notes",
        "description": "Показать всё, что ты помнишь о пользователе (с id).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "forget",
        "description": "Забыть факт о пользователе по id.",
        "input_schema": {
            "type": "object",
            "properties": {"note_id": {"type": "integer"}},
            "required": ["note_id"],
        },
    },
]


class ToolError(Exception):
    pass


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _check_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ToolError(f"Неверная дата {value!r}, нужен формат YYYY-MM-DD")


def _check_category(value: str) -> str:
    if value not in CATEGORIES:
        raise ToolError(f"Неизвестная категория {value!r}. Доступны: {', '.join(CATEGORIES)}")
    return value


def budget_warning(db: DB, user_id: int, category: str) -> str | None:
    limit = db.budgets(user_id).get(category)
    if not limit:
        return None
    today = local_now()
    start, end = month_range(today.year, today.month)
    spent = db.totals_by_category(user_id, start, end).get(category, 0)
    share = spent / limit
    if share >= 1:
        return f"Лимит по «{category}» превышен: {spent:.0f} из {limit:.0f} {CURRENCY}"
    if share >= 0.8:
        return f"По «{category}» потрачено {share:.0%} лимита ({spent:.0f} из {limit:.0f} {CURRENCY})"
    return None


def goal_view(goal: dict) -> dict:
    view = {k: goal[k] for k in ("id", "title", "target_amount", "saved_amount", "deadline", "status")}
    if goal["target_amount"]:
        view["progress"] = f"{goal['saved_amount'] / goal['target_amount']:.0%}"
    view["has_plan"] = bool(goal["plan"])
    return view


def run_tool(db: DB, user_id: int, name: str, args: dict) -> str:
    if name == "add_expense":
        amount = float(args["amount"])
        if amount <= 0:
            raise ToolError("Сумма должна быть больше нуля")
        category = _check_category(args["category"])
        spent_at = _check_date(args.get("date")) or local_now().date().isoformat()
        expense_id = db.add_expense(user_id, amount, category, args.get("note"), spent_at)
        result = {"ok": True, "expense_id": expense_id, "date": spent_at}
        if warning := budget_warning(db, user_id, category):
            result["budget_warning"] = warning
        return _dump(result)

    if name == "list_expenses":
        start, end = _check_date(args["start_date"]), _check_date(args["end_date"])
        category = args.get("category")
        rows = db.expenses(user_id, start, end, category)
        return _dump(
            {
                "total": round(sum(r["amount"] for r in rows), 2),
                "by_category": db.totals_by_category(user_id, start, end),
                "budgets": db.budgets(user_id),
                "count": len(rows),
                "last_items": [
                    {k: r[k] for k in ("id", "amount", "category", "note", "spent_at")} for r in rows[-30:]
                ],
            }
        )

    if name == "delete_expense":
        if not db.delete_expense(user_id, int(args["expense_id"])):
            raise ToolError("Трата с таким id не найдена")
        return _dump({"ok": True})

    if name == "set_budget":
        db.set_budget(user_id, _check_category(args["category"]), float(args["monthly_limit"]))
        return _dump({"ok": True, "budgets": db.budgets(user_id)})

    if name == "add_goal":
        target = args.get("target_amount")
        goal_id = db.add_goal(
            user_id, args["title"], float(target) if target else None, _check_date(args.get("deadline"))
        )
        return _dump(
            {"ok": True, "goal_id": goal_id, "hint": "Предложи пользователю составить план: команда /plan"}
        )

    if name == "list_goals":
        goals = db.goals(user_id, only_active=not args.get("include_done"))
        return _dump([goal_view(g) for g in goals])

    if name == "update_goal":
        goal_id = int(args["goal_id"])
        goal = db.goal(user_id, goal_id)
        if not goal:
            raise ToolError("Цель с таким id не найдена")
        fields = {}
        if args.get("add_saved"):
            fields["saved_amount"] = goal["saved_amount"] + float(args["add_saved"])
        for key in ("title", "target_amount", "status"):
            if args.get(key) is not None:
                fields[key] = args[key]
        if args.get("deadline"):
            fields["deadline"] = _check_date(args["deadline"])
        db.update_goal(user_id, goal_id, **fields)
        return _dump({"ok": True, "goal": goal_view(db.goal(user_id, goal_id))})

    if name == "add_reminder":
        try:
            due = local_to_utc_iso(args["when"])
        except ValueError:
            raise ToolError("Неверное время, нужен формат YYYY-MM-DD HH:MM")
        if utc_iso_to_local(due) <= local_now():
            raise ToolError("Это время уже прошло")
        repeat = args.get("repeat") or "none"
        reminder_id = db.add_reminder(user_id, args["text"], due, repeat)
        return _dump({"ok": True, "reminder_id": reminder_id})

    if name == "list_reminders":
        return _dump(
            [
                {
                    "id": r["id"],
                    "text": r["text"],
                    "when": f"{utc_iso_to_local(r['due_at']):%Y-%m-%d %H:%M}",
                    "repeat": r["repeat"],
                }
                for r in db.reminders(user_id)
            ]
        )

    if name == "cancel_reminder":
        if not db.cancel_reminder(user_id, int(args["reminder_id"])):
            raise ToolError("Активное напоминание с таким id не найдено")
        return _dump({"ok": True})

    if name == "remember":
        return _dump({"ok": True, "note_id": db.add_note(user_id, args["fact"])})

    if name == "list_notes":
        return _dump(db.notes(user_id))

    if name == "forget":
        if not db.delete_note(user_id, int(args["note_id"])):
            raise ToolError("Факт с таким id не найден")
        return _dump({"ok": True})

    raise ToolError(f"Неизвестный инструмент {name}")
