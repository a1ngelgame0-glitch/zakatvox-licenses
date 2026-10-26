import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env, чтобы не тащить лишнюю зависимость."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Кому бот отвечает. Пусто — бот только подскажет твой ID и больше ничего не сделает.
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x
}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
CURRENCY = os.environ.get("CURRENCY", "₽")

# Дешёвая модель для повседневного общения и мощная — для отчётов и планов.
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-5-5")
SMART_MODEL = os.environ.get("SMART_MODEL", "claude-opus-5-5")

DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "assistant.db"))

# После скольких сообщений в истории старый разговор сжимается в краткое резюме.
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "60"))

CATEGORIES = [
    "еда",
    "кафе",
    "транспорт",
    "дом",
    "связь",
    "подписки",
    "развлечения",
    "одежда",
    "здоровье",
    "красота",
    "подарки",
    "образование",
    "путешествия",
    "другое",
]
