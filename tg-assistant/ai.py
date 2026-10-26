"""Всё общение с GigaChat: чат с функциями, отчёты, планы и распознавание чеков."""

import json
import logging
import re
from datetime import date
from typing import Optional

import httpx
from gigachat import GigaChat
from gigachat.exceptions import GigaChatException, RateLimitError, AuthenticationError, ServerError
from gigachat.models import Chat, Function, Messages
from pydantic import BaseModel, ValidationError, field_validator

from config import (
    CATEGORIES,
    CHAT_MODEL,
    CURRENCY,
    GIGACHAT_CA_BUNDLE,
    GIGACHAT_CREDENTIALS,
    GIGACHAT_SCOPE,
    HISTORY_LIMIT,
    SMART_MODEL,
)
from db import DB
from timeutil import local_now, month_range, prev_month, stamp
from tools import TOOLS, ToolError, goal_view, run_tool

log = logging.getLogger(__name__)
# Серверы GigaChat используют сертификат Минцифры — его путь передаём явно (см. README).
client = GigaChat(
    credentials=GIGACHAT_CREDENTIALS,
    scope=GIGACHAT_SCOPE,
    ca_bundle_file=str(GIGACHAT_CA_BUNDLE) if GIGACHAT_CA_BUNDLE.exists() else None,
    timeout=120,
)

AIError = (GigaChatException, httpx.HTTPError)
MAX_TOOL_STEPS = 8

# Системный промпт не меняется между запросами.
# Всё, что меняется (дата, заметки, резюме), приходит внутри сообщений пользователя.
SYSTEM = f"""Ты — личный ИИ-помощник пользователя в Telegram. Общаешься по-русски, на «ты», \
дружелюбно и по делу, как умный друг, который помогает с деньгами, целями и делами.

Что ты умеешь (через инструменты):
- записывать траты и показывать, куда уходят деньги; следить за лимитами по категориям;
- вести цели и прогресс по ним;
- ставить напоминания (в том числе повторяющиеся);
- запоминать важные факты о пользователе.

Правила:
- Валюта по умолчанию — {CURRENCY}.
- Перед каждым сообщением пользователя в квадратных скобках стоит текущие дата и время. \
Считай «сегодня», «завтра», «в пятницу» от них.
- Если пользователь пишет трату («кофе 250», «такси 600 вчера»), сразу записывай её, не переспрашивая. \
Категорию выбирай сам. Если трат несколько — запиши каждую.
- Если инструмент вернул budget_warning, обязательно мягко скажи об этом.
- Отвечай коротко: обычно 1–4 предложения. Длинно — только если просят анализ или совет.
- Пиши обычным текстом с эмодзи по вкусу. Не используй Markdown (никаких **, #, таблиц).
- Для подробного отчёта за месяц предлагай команду /report, для пошагового плана по цели — /plan.
- Не выдумывай данные: если нужны цифры, сначала получи их инструментом."""

FUNCTIONS = [Function(name=t["name"], description=t["description"], parameters=t["input_schema"]) for t in TOOLS]


def error_text(error: Exception) -> str:
    if isinstance(error, RateLimitError):
        return "GigaChat просит притормозить ⏳ Подожди минутку и напиши ещё раз."
    if isinstance(error, AuthenticationError):
        return "Не могу войти в GigaChat: проверь GIGACHAT_CREDENTIALS в .env 🔑"
    if isinstance(error, ServerError):
        return "GigaChat сейчас барахлит, попробуй чуть позже 🛠"
    if isinstance(error, httpx.ConnectError) and "certificate" in str(error).lower():
        return "Не получилось проверить сертификат GigaChat — положи сертификат Минцифры рядом с ботом (см. README) 🔐"
    if isinstance(error, httpx.HTTPError):
        return "Нет связи с GigaChat, проверь интернет 🌐"
    if getattr(error, "status_code", None) == 402:
        return "Закончился бесплатный пакет токенов GigaChat 💳 Проверь остаток в личном кабинете developers.sber.ru."
    return "Что-то пошло не так 😵 Попробуй ещё раз."


def _system() -> Messages:
    return Messages(role="system", content=SYSTEM)


def _user(text: str) -> Messages:
    return Messages(role="user", content=text)


def _dump(message: Messages) -> dict:
    data = message.model_dump(mode="json", exclude_none=True, by_alias=True)
    return {"role": data["role"], "content": data}


def _load(history: list[dict]) -> list[Messages]:
    return [Messages.model_validate(m["content"]) for m in history]


async def _ask(model: str, messages: list[Messages], **kwargs) -> Messages | None:
    """Один запрос к GigaChat. None — если модель не дала ответа (например, сработал фильтр)."""
    response = await client.achat(Chat(model=model, messages=messages, **kwargs))
    if not response.choices:
        return None
    choice = response.choices[0]
    if choice.finish_reason == "blacklist":
        return None
    return choice.message


def _context_header(db: DB, user_id: int, fresh: bool) -> str:
    """Шапка для сообщения пользователя: время, а в начале разговора — память и резюме."""
    parts = [f"[{stamp()}]"]
    if fresh:
        user = db.ensure_user(user_id)
        notes = db.notes(user_id)
        if notes:
            facts = "\n".join(f"- (id {n['id']}) {n['text']}" for n in notes)
            parts.append(f"[Что ты помнишь о пользователе:\n{facts}]")
        if user["summary"]:
            parts.append(f"[Краткое содержание прошлых разговоров:\n{user['summary']}]")
    if pending := db.pop_pending_context(user_id):
        parts.append(f"[Пока вы не общались:\n{pending}]")
    return "\n".join(parts)


async def _compact_if_needed(db: DB, user_id: int, history: list[dict]) -> list[dict]:
    """Когда история разрастается, сжимаем её в резюме и начинаем разговор заново."""
    if len(history) < HISTORY_LIMIT:
        return history
    old_summary = db.ensure_user(user_id)["summary"]
    request = (
        "Сожми наш разговор в краткое резюме (до 15 пунктов) для себя на будущее: "
        "о чём договорились, что важно пользователю, незавершённые дела. "
        "Цифры трат не перечисляй — они хранятся в базе."
    )
    if old_summary:
        request += f"\nВключи важное из прошлого резюме:\n{old_summary}"
    try:
        answer = await _ask(CHAT_MODEL, [_system(), *_load(history), _user(request)])
    except AIError:
        log.exception("Не удалось сжать историю, оставляю как есть")
        return history
    summary = (answer.content or "").strip() if answer else ""
    if not summary:
        return history
    db.set_user_field(user_id, "summary", summary)
    db.clear_history(user_id)
    return []


async def chat(db: DB, user_id: int, text: str) -> str:
    history = await _compact_if_needed(db, user_id, db.load_history(user_id))
    header = _context_header(db, user_id, fresh=not history)
    previous = _load(history)
    new_messages: list[Messages] = [_user(f"{header}\n{text}")]
    answer = None

    # GigaChat вызывает максимум одну функцию за ответ, поэтому шагов может быть несколько.
    for step in range(MAX_TOOL_STEPS + 1):
        answer = await _ask(
            CHAT_MODEL,
            [_system(), *previous, *new_messages],
            functions=FUNCTIONS,
            function_call="none" if step == MAX_TOOL_STEPS else "auto",
        )
        if answer is None:
            # Ход не сохраняем, чтобы в истории не осталось оборванного запроса.
            return "Извини, с этим я помочь не могу 🙏"

        new_messages.append(answer)
        call = answer.function_call
        if not call:
            break

        try:
            result = run_tool(db, user_id, call.name, dict(call.arguments or {}))
        except (ToolError, KeyError, ValueError, TypeError) as e:
            result = json.dumps({"error": str(e)}, ensure_ascii=False)
        new_messages.append(Messages(role="function", name=call.name, content=result))

    db.append_history(user_id, [_dump(m) for m in new_messages])
    return (answer.content or "").strip() or "👌"


# --- «Умные» задачи: отчёт, план, чекин ---


COACH = (
    "Ты — внимательный личный финансовый и лайф-коуч. Пишешь по-русски, на «ты», тепло, "
    "но конкретно: цифры, шаги, сроки. Текст для Telegram: без Markdown, "
    "используй эмодзи и пустые строки между блоками. До 2500 символов."
)


async def _smart(prompt: str) -> str:
    answer = await _ask(SMART_MODEL, [Messages(role="system", content=COACH), _user(prompt)])
    text = (answer.content or "").strip() if answer else ""
    return text or "Не получилось подготовить ответ 🙏 Попробуй ещё раз позже."


def _avg_monthly_spending(db: DB, user_id: int, months: int = 3) -> dict:
    today = local_now()
    year, month = today.year, today.month
    totals: dict[str, float] = {}
    for _ in range(months):
        year, month = prev_month(year, month)
        for category, amount in db.totals_by_category(user_id, *month_range(year, month)).items():
            totals[category] = totals.get(category, 0) + amount
    return {k: round(v / months) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}


def _notes_text(db: DB, user_id: int) -> str:
    return "\n".join(f"- {n['text']}" for n in db.notes(user_id)) or "пока ничего"


async def monthly_report(db: DB, user_id: int, year: int, month: int) -> str:
    start, end = month_range(year, month)
    py, pm = prev_month(year, month)
    rows = db.expenses(user_id, start, end)
    if not rows:
        return f"За {month:02d}.{year} трат не записано 🤷 Пиши мне траты, например «кофе 250», и в конце месяца будет отчёт."
    data = {
        "период": f"{start} — {end}",
        "всего": round(sum(r["amount"] for r in rows), 2),
        "по_категориям": db.totals_by_category(user_id, start, end),
        "прошлый_месяц_по_категориям": db.totals_by_category(user_id, *month_range(py, pm)),
        "лимиты": db.budgets(user_id),
        "крупные_траты": sorted(
            ({"сумма": r["amount"], "категория": r["category"], "что": r["note"], "дата": r["spent_at"]} for r in rows),
            key=lambda r: -r["сумма"],
        )[:10],
        "число_трат": len(rows),
        "цели": [goal_view(g) for g in db.goals(user_id)],
    }
    prompt = (
        f"Сделай отчёт о тратах за месяц. Валюта {CURRENCY}.\n\n"
        f"Данные:\n{json.dumps(data, ensure_ascii=False, indent=1)}\n\n"
        f"Что известно о человеке:\n{_notes_text(db, user_id)}\n\n"
        "Структура: 1) итог месяца одной фразой; 2) топ категорий с суммами и долями; "
        "3) что изменилось против прошлого месяца; 4) где лимиты превышены; "
        "5) 2–3 конкретных совета, сколько можно сэкономить и как это приблизит цели; "
        "6) одна мотивирующая фраза. Если прошлый месяц пустой — пропусти сравнение."
    )
    return await _smart(prompt)


async def goal_plan(db: DB, user_id: int, goal_id: int) -> str:
    goal = db.goal(user_id, goal_id)
    if not goal:
        return "Не нашёл такую цель 🤔"
    today = local_now().date()
    month_start, _ = month_range(today.year, today.month)
    data = {
        "сегодня": today.isoformat(),
        "цель": goal_view(goal),
        "средние_траты_в_месяц_за_3_мес": _avg_monthly_spending(db, user_id),
        "траты_в_этом_месяце": db.totals_by_category(user_id, month_start, today.isoformat()),
        "лимиты": db.budgets(user_id),
        "другие_цели": [goal_view(g) for g in db.goals(user_id) if g["id"] != goal_id],
    }
    prompt = (
        f"Составь реалистичный план достижения цели. Валюта {CURRENCY}.\n\n"
        f"Данные:\n{json.dumps(data, ensure_ascii=False, indent=1)}\n\n"
        f"Что известно о человеке:\n{_notes_text(db, user_id)}\n\n"
        "Если цель денежная: посчитай, сколько откладывать в месяц и в неделю до дедлайна, "
        "из каких категорий трат реально высвободить деньги (с суммами), и честно скажи, если срок нереален. "
        "Если цель не денежная: разбей на этапы с неделями и маленькими ежедневными действиями. "
        "В конце: первый шаг на сегодня и предложи, какие напоминания поставить "
        "(пользователь может попросить меня их создать)."
    )
    plan = await _smart(prompt)
    db.update_goal(user_id, goal_id, plan=plan)
    db.add_pending_context(user_id, f"Ты составил план для цели «{goal['title']}» (id {goal_id}):\n{plan}")
    return plan


async def weekly_checkin(db: DB, user_id: int) -> str | None:
    goals = db.goals(user_id)
    if not goals:
        return None
    today = local_now().date()
    week_start = date.fromordinal(today.toordinal() - 6).isoformat()
    prompt = (
        f"[{stamp()}] Сейчас вечер воскресенья. Напиши мне короткое (3–5 строк) "
        "дружеское сообщение-чекин по моим целям: напомни о них, отметь прогресс, "
        "задай один вопрос, как прошла неделя.\n"
        f"Цели: {json.dumps([goal_view(g) for g in goals], ensure_ascii=False)}\n"
        f"Траты за неделю: {json.dumps(db.totals_by_category(user_id, week_start, today.isoformat()), ensure_ascii=False)}"
    )
    answer = await _ask(CHAT_MODEL, [_system(), _user(prompt)])
    return ((answer.content or "").strip() if answer else "") or None


# --- Чеки по фото ---


class ReceiptItem(BaseModel):
    amount: float
    category: str
    note: str = ""

    @field_validator("category")
    @classmethod
    def known_category(cls, value: str) -> str:
        value = value.strip().lower()
        return value if value in CATEGORIES else "другое"


class Receipt(BaseModel):
    is_receipt: bool
    store: Optional[str] = None
    date: Optional[str] = None
    items: list[ReceiptItem]


RECEIPT_PROMPT = (
    "Это фото чека или скриншот оплаты? Если да — извлеки покупки. "
    "Объединяй похожие позиции одной категории в одну строку "
    "(например, все продукты — одна трата «продукты»), чтобы было 1–5 трат. "
    "Ответь ТОЛЬКО JSON без пояснений, в таком виде:\n"
    '{"is_receipt": true, "store": "Пятёрочка", "date": "2026-10-26", '
    '"items": [{"amount": 512.5, "category": "еда", "note": "продукты"}]}\n'
    f"category — одно из: {', '.join(CATEGORIES)}. amount — итоговая сумма с учётом скидок. "
    "date — дата чека YYYY-MM-DD или null. Если это не чек — "
    '{"is_receipt": false, "store": null, "date": null, "items": []}'
)


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


async def parse_receipt(image: bytes, media_type: str = "image/jpeg") -> Receipt | None:
    uploaded = await client.aupload_file(("receipt.jpg", image, media_type))
    answer = await _ask(
        SMART_MODEL,
        [Messages(role="user", content=RECEIPT_PROMPT, attachments=[uploaded.id_])],
    )
    data = _extract_json(answer.content or "") if answer else None
    if not data:
        return None
    try:
        return Receipt.model_validate(data)
    except ValidationError:
        log.warning("Не удалось разобрать чек: %s", data)
        return None
