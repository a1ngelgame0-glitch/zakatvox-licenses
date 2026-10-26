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

# Ключ авторизации из личного кабинета developers.sber.ru (строка base64).
GIGACHAT_CREDENTIALS = os.environ.get("GIGACHAT_CREDENTIALS", "")
# GIGACHAT_API_PERS — для физлиц (бесплатный пакет токенов).
GIGACHAT_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
# Корневой сертификат Минцифры: без него не проверить защищённое соединение с GigaChat.
GIGACHAT_CA_BUNDLE = Path(os.environ.get("GIGACHAT_CA_BUNDLE", BASE_DIR / "russian_trusted_root_ca.pem"))
# Модель для повседневного общения и модель для отчётов, планов и чеков (умеет читать фото).
CHAT_MODEL = os.environ.get("CHAT_MODEL", "GigaChat")
SMART_MODEL = os.environ.get("SMART_MODEL", "GigaChat-Max")

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
