import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
from config import ALLOWED_USERS, CURRENCY, GEMINI_API_KEY, TELEGRAM_TOKEN
from db import DB
from timeutil import local_now, month_range, next_occurrence, prev_month, utc_iso_to_local
from tools import budget_warning, goal_view

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

db = DB()
dp = Dispatcher()
# Один ход разговора на пользователя за раз, чтобы история не перемешалась.
locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

HELP = f"""Привет! Я твой личный ИИ-помощник 🤖

Просто пиши мне как человеку:
💸 «кофе 250», «вчера такси 600» — запишу трату
📸 пришли фото чека — сам разберу покупки
📊 «сколько я потратил на кафе в октябре?»
🎯 «хочу накопить 80 000 на ноутбук к марту» — заведу цель
⏰ «напомни завтра в 9 позвонить маме» или «каждый понедельник в 8 — спортзал»
🚦 «поставь лимит на кафе 5000 в месяц» — предупрежу, когда подойдёшь к лимиту
🧠 «запомни, что зарплата 10 числа» — буду учитывать

Команды:
/report — умный отчёт о тратах за месяц
/goals — твои цели и планы к ним
/plan — пошаговый план по цели
/month — быстрая сводка трат (без ИИ)
/reminders — напоминания
/reset — начать разговор заново (траты и цели останутся)

Сам напишу тебе: напоминания, отчёт 1-го числа и чекин по целям в воскресенье вечером.
Валюта: {CURRENCY}"""


class AccessMiddleware(BaseMiddleware):
    """Пускаем только своих, чтобы чужие не съедали твой лимит запросов."""

    async def __call__(self, handler, event, data):
        user = event.from_user
        if user and user.id in ALLOWED_USERS:
            db.ensure_user(user.id)
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer(
                f"Это личный бот. Твой Telegram ID: {user.id}\n"
                "Если это твой бот — впиши этот ID в ALLOWED_USERS в файле .env и перезапусти бота."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа")


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


async def send_long(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Telegram не принимает сообщения длиннее 4096 символов — режем по абзацам."""
    chunks, current = [], ""
    for paragraph in text.split("\n"):
        while len(paragraph) > 4000:
            chunks.append(paragraph[:4000])
            paragraph = paragraph[4000:]
        if len(current) + len(paragraph) + 1 > 4000:
            chunks.append(current)
            current = ""
        current += paragraph + "\n"
    if current.strip():
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        await bot.send_message(chat_id, chunk, **(kwargs if i == len(chunks) - 1 else {}))


@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(HELP)


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    async with locks[message.from_user.id]:
        db.clear_history(message.from_user.id)
        db.set_user_field(message.from_user.id, "summary", None)
    await message.answer("Начали с чистого листа 🧹 Траты, цели, напоминания и то, что я о тебе помню, на месте.")


@dp.message(Command("month"))
async def cmd_month(message: Message):
    user_id = message.from_user.id
    today = local_now()
    start, end = month_range(today.year, today.month)
    totals = db.totals_by_category(user_id, start, end)
    if not totals:
        await message.answer("В этом месяце трат пока нет. Напиши, например, «обед 450» 🍜")
        return
    budgets = db.budgets(user_id)
    lines = [f"📊 Траты за {today:%m.%Y}: {sum(totals.values()):,.0f} {CURRENCY}".replace(",", " "), ""]
    for category, total in totals.items():
        line = f"• {category}: {total:,.0f}".replace(",", " ")
        if limit := budgets.get(category):
            line += f" из {limit:,.0f} ({total / limit:.0%})".replace(",", " ")
        lines.append(line)
    await message.answer("\n".join(lines))


@dp.message(Command("reminders"))
async def cmd_reminders(message: Message):
    reminders = db.reminders(message.from_user.id)
    if not reminders:
        await message.answer("Напоминаний нет. Скажи, например: «напомни в пятницу в 18:00 купить подарок» ⏰")
        return
    repeat = {"none": "", "daily": " 🔁 каждый день", "weekly": " 🔁 каждую неделю", "monthly": " 🔁 каждый месяц"}
    lines = ["⏰ Напоминания:", ""]
    for r in reminders:
        lines.append(f"• {utc_iso_to_local(r['due_at']):%d.%m %H:%M} — {r['text']}{repeat.get(r['repeat'], '')}")
    lines += ["", "Чтобы отменить, просто напиши мне, какое."]
    await message.answer("\n".join(lines))


def goals_keyboard(goals: list[dict]):
    kb = InlineKeyboardBuilder()
    for g in goals:
        kb.button(text=f"🧭 План: {g['title'][:40]}", callback_data=f"plan:{g['id']}")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(Command("goals"))
async def cmd_goals(message: Message):
    goals = db.goals(message.from_user.id)
    if not goals:
        await message.answer("Целей пока нет. Напиши, например: «хочу накопить 50 000 на отпуск к июлю» 🎯")
        return
    lines = ["🎯 Твои цели:", ""]
    for g in goals:
        view = goal_view(g)
        line = f"• {g['title']}"
        if g["target_amount"]:
            line += f" — {g['saved_amount']:,.0f} из {g['target_amount']:,.0f} {CURRENCY} ({view['progress']})".replace(",", " ")
        if g["deadline"]:
            line += f", до {g['deadline']}"
        if g["plan"]:
            line += " 📋"
        lines.append(line)
    lines += ["", "Нажми на цель, чтобы получить (или обновить) пошаговый план 👇"]
    await message.answer("\n".join(lines), reply_markup=goals_keyboard(goals))


@dp.message(Command("plan"))
async def cmd_plan(message: Message):
    goals = db.goals(message.from_user.id)
    if not goals:
        await message.answer("Сначала заведи цель — просто напиши, чего хочешь достичь 🎯")
        return
    await message.answer("Для какой цели составить план?", reply_markup=goals_keyboard(goals))


@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(callback: CallbackQuery, bot: Bot):
    await callback.answer("Думаю над планом… 🧠")
    user_id, goal_id = callback.from_user.id, int(callback.data.split(":", 1)[1])
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=callback.message.chat.id):
            plan = await ai.goal_plan(db, user_id, goal_id)
    except ai.AIError as e:
        log.exception("plan failed")
        plan = ai.error_text(e)
    await send_long(bot, callback.message.chat.id, plan)


@dp.message(Command("report"))
async def cmd_report(message: Message, bot: Bot):
    today = local_now()
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            report = await ai.monthly_report(db, message.from_user.id, today.year, today.month)
    except ai.AIError as e:
        log.exception("report failed")
        report = ai.error_text(e)
    await send_long(bot, message.chat.id, report)


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    user_id = message.from_user.id
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            image = await bot.download(message.photo[-1].file_id)
            receipt = await ai.parse_receipt(image.read())
    except ai.AIError as e:
        log.exception("receipt failed")
        await message.answer(ai.error_text(e))
        return
    if not receipt or not receipt.is_receipt or not receipt.items:
        await message.answer("Не похоже на чек 🤔 Пришли фото чека или скриншот оплаты — я запишу траты.")
        return

    spent_at = local_now().date().isoformat()
    if receipt.date:
        try:
            spent_at = datetime.fromisoformat(receipt.date).date().isoformat()
        except ValueError:
            pass
    lines = [f"🧾 Записал из чека{' «' + receipt.store + '»' if receipt.store else ''}:", ""]
    warnings = set()
    for item in receipt.items:
        if item.amount <= 0:
            continue
        db.add_expense(user_id, item.amount, item.category, item.note, spent_at)
        lines.append(f"• {item.note} — {item.amount:,.0f} {CURRENCY} ({item.category})".replace(",", " "))
        if warning := budget_warning(db, user_id, item.category):
            warnings.add(warning)
    total = sum(i.amount for i in receipt.items if i.amount > 0)
    lines += ["", f"Итого: {total:,.0f} {CURRENCY}".replace(",", " ")]
    lines += [f"⚠️ {w}" for w in sorted(warnings)]
    lines += ["", "Если что-то не так — напиши, поправлю."]
    await message.answer("\n".join(lines))
    db.add_pending_context(user_id, "Пользователь прислал фото чека, записаны траты:\n" + "\n".join(lines[2:-2]))


@dp.message(F.voice | F.video_note)
async def on_voice(message: Message):
    await message.answer("Голосовые пока не понимаю 🙉 Напиши текстом, пожалуйста.")


@dp.message(F.text)
async def on_text(message: Message, bot: Bot):
    user_id = message.from_user.id
    try:
        async with locks[user_id], ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            answer = await ai.chat(db, user_id, message.text)
    except ai.AIError as e:
        log.exception("chat failed")
        answer = ai.error_text(e)
    await send_long(bot, message.chat.id, answer)


@dp.message()
async def on_other(message: Message):
    await message.answer("Я понимаю текст и фото чеков 🙂")


# --- Фоновые задачи: напоминания, месячный отчёт, воскресный чекин ---


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)


async def tick(bot: Bot) -> None:
    now = local_now()
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for r in db.due_reminders(now_utc):
        if r["user_id"] not in ALLOWED_USERS:
            continue
        await bot.send_message(r["user_id"], f"⏰ Напоминаю: {r['text']}")
        db.reschedule_reminder(r["id"], next_occurrence(r["due_at"], r["repeat"], now))
        db.add_pending_context(r["user_id"], f"В {now:%H:%M} ты напомнил: «{r['text']}»")

    for user in db.all_users():
        user_id = user["user_id"]
        if user_id not in ALLOWED_USERS:
            continue

        # 1-го числа после 10:00 — отчёт за прошлый месяц.
        year, month = prev_month(now.year, now.month)
        month_key = f"{year}-{month:02d}"
        if now.day == 1 and now.hour >= 10 and user["last_report_month"] != month_key:
            db.set_user_field(user_id, "last_report_month", month_key)
            if db.expenses(user_id, *month_range(year, month)):
                report = await ai.monthly_report(db, user_id, year, month)
                await send_long(bot, user_id, f"📅 Итоги месяца\n\n{report}")
                db.add_pending_context(user_id, f"Ты прислал отчёт за {month_key}:\n{report}")

        # Воскресенье после 19:00 — чекин по целям.
        week_key = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if now.weekday() == 6 and now.hour >= 19 and user["last_checkin_week"] != week_key:
            db.set_user_field(user_id, "last_checkin_week", week_key)
            if text := await ai.weekly_checkin(db, user_id):
                await bot.send_message(user_id, text)
                db.add_pending_context(user_id, f"Ты прислал воскресный чекин по целям:\n{text}")


async def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN — смотри README.md")
    if not GEMINI_API_KEY:
        raise SystemExit("Не задан GEMINI_API_KEY — смотри README.md")
    bot = Bot(TELEGRAM_TOKEN)
    await bot.set_my_commands(
        [
            BotCommand(command="report", description="Отчёт о тратах за месяц"),
            BotCommand(command="goals", description="Мои цели"),
            BotCommand(command="plan", description="План по цели"),
            BotCommand(command="month", description="Быстрая сводка трат"),
            BotCommand(command="reminders", description="Напоминания"),
            BotCommand(command="reset", description="Начать разговор заново"),
            BotCommand(command="help", description="Что ты умеешь?"),
        ]
    )
    if not ALLOWED_USERS:
        log.warning("ALLOWED_USERS пуст — напиши боту, он пришлёт твой ID, и добавь его в .env")
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
