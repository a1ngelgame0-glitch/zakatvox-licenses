import calendar
from datetime import date, datetime, timedelta, timezone

from config import TZ

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def local_now() -> datetime:
    return datetime.now(TZ)


def stamp() -> str:
    """Метка времени, которую видит ИИ перед каждым сообщением."""
    now = local_now()
    return f"{now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}"


def month_range(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last).isoformat()


def prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def local_to_utc_iso(value: str) -> str:
    """'2026-10-27 09:00' в местном времени -> ISO-строка в UTC."""
    local = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_iso_to_local(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ)


def add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year, month = dt.year + month_index // 12, month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_occurrence(due_utc_iso: str, repeat: str, now: datetime) -> str | None:
    """Следующее срабатывание повторяющегося напоминания (в будущем), либо None."""
    if repeat == "none":
        return None
    due = utc_iso_to_local(due_utc_iso)
    while due <= now:
        if repeat == "daily":
            due += timedelta(days=1)
        elif repeat == "weekly":
            due += timedelta(weeks=1)
        elif repeat == "monthly":
            due = add_months(due, 1)
        else:
            return None
    return due.astimezone(timezone.utc).isoformat(timespec="seconds")
