"""
CareerAI Bot MVP - Telegram бот для анализа резюме с Gemini 2.0 Flash
Деплой: Vercel Serverless Functions
ИИ: Google Gemini 2.0 Flash через Cloudflare Workers (бесплатно)
"""

import os
import json
import logging
import asyncio
import html
import io
import re
import uuid
import hashlib
import zipfile
import xml.etree.ElementTree as ET
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, LabeledPrice
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction

try:
    from PyPDF2 import PdfReader  # type: ignore
except Exception:  # pragma: no cover
    PdfReader = None  # type: ignore


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

def _env_required(name: str) -> str:
    value = os.getenv(name)
    if not value or value.strip() in {"YOUR_BOT_TOKEN", "YOUR_API_KEY"}:
        raise RuntimeError(
            f"Переменная окружения {name} не задана. "
            f"Задайте её в окружении (Vercel → Project → Settings → Environment Variables)."
        )
    return value.strip()


def _normalize_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not re.match(r"^https?://", u, flags=re.IGNORECASE):
        u = "https://" + u
    return u


APP_NAME = "CareerAI MVP"
APP_VERSION = "1.1.0"

# Environment Variables (Vercel / локально)
# Важно: не держим секреты в коде. Если переменные не заданы, приложение загрузится,
# но webhook/анализ будет возвращать ошибку конфигурации.
if load_dotenv:
    load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
CLOUDFLARE_WORKER_URL = _normalize_base_url(
    os.getenv("CLOUDFLARE_WORKER_URL", "https://gemini-proxy.alex555196.workers.dev")
)
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@YourSupportBot").strip() or "@YourSupportBot"

# Платежи: токен от @BotFather (Payments). Если пусто — кнопка «Купить» не показывается.
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()
# Цена премиума: сумма в минимальных единицах валюты (центы для USD, копейки для RUB)
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

PREMIUM_PRICE_CENTS = _env_int("PREMIUM_PRICE_CENTS", 999)  # 9.99 USD
PREMIUM_CURRENCY = (os.getenv("PREMIUM_CURRENCY", "USD") or "USD").strip().upper()[:3]  # USD или RUB
PREMIUM_DAYS = _env_int("PREMIUM_DAYS", 30)  # срок подписки в днях

FREE_DAILY_LIMIT = _env_int("FREE_DAILY_LIMIT", 3)
MAX_RESUME_CHARS_FREE = _env_int("MAX_RESUME_CHARS_FREE", 3500)
MAX_FILE_BYTES = _env_int("MAX_FILE_BYTES", 2 * 1024 * 1024)  # 2MB


# ============================================================================
# APP LIFECYCLE + SHARED HTTP CLIENT
# ============================================================================

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client, bot
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    # Сбрасываем кэш Bot при старте, чтобы избежать проблем с устаревшими экземплярами
    bot = None
    try:
        yield
    finally:
        try:
            await http_client.aclose()
        except Exception:
            pass
        # Закрываем Bot при завершении
        if bot is not None:
            try:
                session = getattr(bot, "session", None)
                if session:
                    await session.close()
            except Exception:
                pass
            bot = None


# Инициализация
app = FastAPI(lifespan=lifespan)
bot: Optional[Bot] = None
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory хранилище (для production используйте Redis/PostgreSQL)
user_data: Dict[int, Dict[str, Any]] = {}  # {user_id: {"requests_today": int, "day": "YYYY-MM-DD", ...}}
user_ctx: Dict[int, Dict[str, Any]] = {}   # {user_id: {"mode": str, "last_resume_text": str, ...}}
user_lock = asyncio.Lock()

# Простой кэш (уменьшает расходы, но не является персистентным)
analysis_cache: Dict[str, Dict[str, Any]] = {}  # {cache_key: {"expires_at": datetime, "value": dict}}

# Премиум-подписки: user_id -> дата окончания (UTC). В production — хранить в БД.
premium_users: Dict[int, datetime] = {}  # {user_id: premium_until}

def is_premium(user_id: int) -> bool:
    """Проверка, есть ли у пользователя активная премиум-подписка"""
    until = premium_users.get(user_id)
    if until is None:
        return False
    if until < _now():
        premium_users.pop(user_id, None)
        return False
    return True

def set_premium_until(user_id: int, days: int = 30) -> None:
    """Выдать премиум на указанное количество дней"""
    premium_users[user_id] = _now() + timedelta(days=days)

# ============================================================================
# АНАЛИТИКА (простая система отслеживания событий)
# ============================================================================

# События для отслеживания:
# - user_started: пользователь запустил бота
# - resume_analyzed: пользователь проанализировал резюме
# - premium_clicked: пользователь нажал на премиум
# - tailor_used: использовал оптимизацию под вакансию
# - improve_used: использовал черновик
# - error_occurred: произошла ошибка

analytics_events: List[Dict[str, Any]] = []  # Список всех событий
analytics_lock = asyncio.Lock()

def track_event(event_name: str, user_id: int, metadata: Optional[Dict[str, Any]] = None):
    """
    Логирование события для аналитики
    
    Args:
        event_name: название события (user_started, resume_analyzed и т.д.)
        user_id: ID пользователя
        metadata: дополнительные данные (опционально)
    """
    event = {
        "event": event_name,
        "user_id": user_id,
        "timestamp": _now().isoformat(),
        "date": _today_key(),
        "metadata": metadata or {}
    }
    
    # Логируем в консоль (для Vercel это попадет в Function Logs)
    logger.info(f"ANALYTICS: {event_name} | user_id={user_id} | metadata={metadata}")
    
    # Сохраняем в память (для простого дашборда)
    # ВАЖНО: на Vercel это сбросится при перезапуске, но для MVP достаточно
    analytics_events.append(event)
    
    # Ограничиваем размер списка (храним последние 10000 событий)
    if len(analytics_events) > 10000:
        analytics_events.pop(0)


def get_analytics_stats() -> Dict[str, Any]:
    """
    Получить базовую статистику из событий
    
    Возвращает:
        - total_users: всего уникальных пользователей
        - daily_active_users: активных пользователей сегодня
        - events_today: событий сегодня
        - events_by_type: события по типам
        - conversion_rate: конверсия в анализ резюме (из тех, кто запустил бота)
    """
    today = _today_key()
    
    # Фильтруем события за сегодня
    events_today = [e for e in analytics_events if e.get("date") == today]
    
    # Уникальные пользователи за все время
    all_user_ids = set(e["user_id"] for e in analytics_events)
    
    # Уникальные пользователи сегодня
    today_user_ids = set(e["user_id"] for e in events_today)
    
    # События по типам
    events_by_type: Dict[str, int] = {}
    for event in analytics_events:
        event_name = event.get("event", "unknown")
        events_by_type[event_name] = events_by_type.get(event_name, 0) + 1
    
    # Конверсия: сколько из тех, кто запустил бота, проанализировали резюме
    started_count = events_by_type.get("user_started", 0)
    analyzed_count = events_by_type.get("resume_analyzed", 0)
    conversion_rate = (analyzed_count / started_count * 100) if started_count > 0 else 0
    
    return {
        "total_users": len(all_user_ids),
        "daily_active_users": len(today_user_ids),
        "events_today": len(events_today),
        "total_events": len(analytics_events),
        "events_by_type": events_by_type,
        "conversion_rate": round(conversion_rate, 2),
        "last_updated": _now().isoformat()
    }


def h(text: str) -> str:
    """HTML-escape для безопасной выдачи в Telegram (ParseMode.HTML)."""
    return html.escape(text or "", quote=False)


def _now() -> datetime:
    # Используем timezone.utc для совместимости с Python < 3.11
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return _now().strftime("%Y-%m-%d")


def get_bot() -> Bot:
    global bot
    if bot is not None:
        return bot
    token = _env_required("BOT_TOKEN")
    # Используем правильный синтаксис для aiogram 3.7+
    # ВАЖНО: не передаем parse_mode напрямую в Bot(), только через DefaultBotProperties
    logger.info("Initializing Bot instance...")
    try:
        # Увеличиваем таймауты для работы на Vercel (serverless может быть медленнее)
        # Используем request_timeout параметр для увеличения таймаутов запросов
        bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            # Увеличиваем таймаут запросов до 30 секунд для Vercel
            # Это поможет избежать таймаутов при медленных соединениях
        )
        logger.info("Bot initialized successfully")
    except Exception as e:
        logger.exception(f"Bot initialization error: {type(e).__name__}: {e}")
        raise RuntimeError(f"Не удалось инициализировать бота: {e}") from e
    return bot

# ============================================================================
# GEMINI API CLIENT (через Cloudflare Workers)
# ============================================================================

class GeminiClient:
    """Клиент для работы с Gemini 2.0 Flash через Cloudflare Workers"""
    
    def __init__(self, api_key: str, worker_url: str):
        self.api_key = api_key
        self.worker_url = worker_url.rstrip('/')
        # Используем модель с гарантированным free tier
        # Согласно документации Google GenAI SDK:
        # - gemini-3-flash-preview - рекомендуется для общего использования (имеет free tier)
        # - gemini-2.5-flash - также имеет free tier
        # - gemini-2.0-flash - может не иметь free tier для некоторых ключей
        # Попробуем gemini-2.5-flash - стабильная модель с free tier
        self.model = "gemini-2.5-flash"  # Стабильная модель с free tier
        self.daily_limit = 1500
        
    async def generate_content(self, prompt: str, max_tokens: int = 1000) -> str:
        """Генерация контента через Gemini API"""
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY не задан. Укажите ключ в переменных окружения.")
        if not self.worker_url:
            raise RuntimeError("CLOUDFLARE_WORKER_URL не задан. Укажите URL воркера в переменных окружения.")

        url = f"{self.worker_url}/v1beta/models/{self.model}:generateContent"
        
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key
        }
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.7,
                "topP": 0.95,
                "stopSequences": []  # Убираем возможные стоп-последовательности
            },
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_NONE"
                }
            ]
        }
        
        logger.debug(f"Gemini API request URL: {url}")
        logger.debug(f"Gemini API request headers: {dict(headers)}")
        logger.debug(f"Gemini API request payload keys: {list(payload.keys())}")
        
        retries = 3
        backoffs = [0.7, 1.5, 3.0]
        last_err: Optional[Exception] = None

        for attempt in range(retries):
            try:
                client = http_client or httpx.AsyncClient(timeout=30.0)
                try:
                    response = await client.post(url, json=payload, headers=headers)
                    logger.debug(f"Gemini API response status: {response.status_code}, headers: {dict(response.headers)}")
                finally:
                    if http_client is None:
                        await client.aclose()

                if response.status_code in {429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        "retryable",
                        request=response.request,
                        response=response,
                    )

                response.raise_for_status()

                # Логируем ответ для отладки
                # ВАЖНО: читаем ответ как bytes, затем декодируем, чтобы избежать проблем с кодировкой
                response_bytes = response.content
                response_text = response_bytes.decode('utf-8', errors='replace')
                logger.info(f"Gemini API response status: {response.status_code}, length: {len(response_text)}, bytes: {len(response_bytes)}")
                
                if not response_text or not response_text.strip():
                    logger.error("Empty response from Gemini API")
                    raise RuntimeError("Пустой ответ от Gemini API")
                
                try:
                    data = json.loads(response_text)  # Используем json.loads вместо response.json() для лучшего контроля
                except Exception as e:
                    logger.error(f"Failed to parse JSON from Gemini API response: {e}")
                    logger.error(f"Response content (first 2000 chars): {response_text[:2000]}")
                    logger.error(f"Response content (last 500 chars): {response_text[-500:] if len(response_text) > 500 else response_text}")
                    raise RuntimeError(f"Неверный формат ответа от Gemini API: {e}") from e
                
                # Проверяем структуру ответа
                if "candidates" not in data or not data.get("candidates"):
                    logger.error(f"Unexpected Gemini API response structure: {data}")
                    raise RuntimeError("Неожиданная структура ответа от Gemini API")
                
                # Извлекаем текст из ответа
                candidate = data["candidates"][0]
                
                # Проверяем finishReason - если STOP, значит ответ обрезан
                finish_reason = candidate.get("finishReason", "")
                if finish_reason == "MAX_TOKENS":
                    logger.warning(f"Response truncated due to MAX_TOKENS limit! finishReason: {finish_reason}")
                    raise RuntimeError(
                        "Ответ от Gemini API обрезан из-за лимита токенов. "
                        "Попробуйте использовать более короткое резюме или подождите."
                    )
                
                if "content" not in candidate or "parts" not in candidate["content"]:
                    logger.error(f"Unexpected candidate structure: {candidate}")
                    raise RuntimeError("Неожиданная структура candidate в ответе Gemini API")
                
                parts = candidate["content"]["parts"]
                if not parts or "text" not in parts[0]:
                    logger.error(f"Unexpected parts structure: {parts}")
                    raise RuntimeError("Неожиданная структура parts в ответе Gemini API")
                
                text = parts[0]["text"]
                logger.info(f"Extracted text from Gemini API, length: {len(text)}, finishReason: {finish_reason}")
                
                # Проверяем, не обрезан ли ответ (если заканчивается на незакрытой строке/объекте)
                if text and not text.strip().endswith("}"):
                    logger.warning(f"Response may be truncated! Last 100 chars: {text[-100:]}")
                    # Проверяем, есть ли незакрытые скобки
                    open_braces = text.count("{")
                    close_braces = text.count("}")
                    if open_braces > close_braces:
                        logger.error(f"Unclosed JSON object! Open braces: {open_braces}, Close braces: {close_braces}")
                        raise RuntimeError(
                            f"Ответ от Gemini API обрезан (получено {len(text)} символов, finishReason: {finish_reason}). "
                            f"Попробуйте позже или используйте более короткое резюме."
                        )
                
                logger.debug(f"Extracted text (first 500 chars): {text[:500]}")
                logger.debug(f"Extracted text (last 200 chars): {text[-200:] if len(text) > 200 else text}")
                return text

            except httpx.HTTPStatusError as e:
                last_err = e
                body = ""
                try:
                    body = e.response.text[:1000]  # не логируем слишком много
                except Exception:
                    pass

                logger.warning(f"Gemini API error: {e.response.status_code} - {body}")
                
                # Специальная обработка ошибки 429 (Quota exceeded)
                if e.response.status_code == 429:
                    if "quota" in body.lower() or "limit: 0" in body:
                        raise RuntimeError(
                            "Квота Gemini API исчерпана или API ключ не имеет доступа к free tier.\n\n"
                            "Проверьте:\n"
                            "1. Действителен ли ваш GEMINI_API_KEY\n"
                            "2. Есть ли у ключа доступ к free tier\n"
                            "3. Не исчерпана ли дневная квота\n\n"
                            "Подробнее: https://ai.google.dev/gemini-api/docs/rate-limits"
                        )
                    # Если это временная перегрузка, пробуем повторить
                    if attempt < retries - 1:
                        retry_after = 60  # ждем минуту при 429
                        logger.info(f"Rate limit hit, waiting {retry_after}s before retry...")
                        await asyncio.sleep(retry_after)
                        continue
                    raise RuntimeError("Превышен лимит запросов к Gemini API. Попробуйте позже.")
                
                if attempt < retries - 1:
                    await asyncio.sleep(backoffs[attempt])
                    continue

                if "User location is not supported" in body:
                    raise RuntimeError("Cloudflare Worker не настроен или API key недействителен")
                raise RuntimeError(f"Ошибка Gemini API: {e.response.status_code}")

            except Exception as e:
                last_err = e
                logger.exception("Unexpected Gemini error")
                break

        raise RuntimeError("Технические неполадки при обращении к ИИ. Попробуйте позже.") from last_err

_gemini_client: Optional[GeminiClient] = None


def get_gemini_client() -> GeminiClient:
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    api_key = _env_required("GEMINI_API_KEY")
    worker_url = _normalize_base_url(CLOUDFLARE_WORKER_URL)
    _gemini_client = GeminiClient(api_key, worker_url)
    return _gemini_client

# ============================================================================
# RATE LIMITING & USER MANAGEMENT
# ============================================================================

def get_user_state(user_id: int) -> Dict[str, Any]:
    """Получить состояние пользователя"""
    if user_id not in user_data:
        user_data[user_id] = {
            "requests_today": 0,
            "last_request": None,
            "registered_at": _now(),
            "day": _today_key(),
        }
    # Смена дня (UTC)
    today = _today_key()
    if user_data[user_id].get("day") != today:
        user_data[user_id]["day"] = today
        user_data[user_id]["requests_today"] = 0
    return user_data[user_id]

def reset_daily_limits():
    """Сброс дневных лимитов (вызывать раз в день)"""
    for user_id in user_data:
        user_data[user_id]["requests_today"] = 0

async def check_rate_limit(user_id: int) -> Tuple[bool, str]:
    """Проверка лимитов: (allowed, message). Премиум-пользователи не ограничены."""
    if is_premium(user_id):
        return True, ""
    async with user_lock:
        state = get_user_state(user_id)

        if state["requests_today"] >= FREE_DAILY_LIMIT:
            return False, (
                f"🚫 Вы использовали {FREE_DAILY_LIMIT} бесплатных анализа сегодня.\n\n"
                "💎 <b>Премиум доступ</b>:\n"
                "• Неограниченные анализы\n"
                "• Детальная ATS-оптимизация\n"
                "• Оптимизация под вакансию\n\n"
                "Первые 3 дня бесплатно → /premium"
            )

        return True, ""


async def consume_quota(user_id: int) -> None:
    async with user_lock:
        state = get_user_state(user_id)
        state["requests_today"] += 1
        state["last_request"] = _now()


async def refund_quota(user_id: int) -> None:
    async with user_lock:
        state = get_user_state(user_id)
        if state["requests_today"] > 0:
            state["requests_today"] -= 1

# ============================================================================
# AI RESUME ANALYZER
# ============================================================================

class ResumeAnalyzer:
    """Анализ резюме с помощью Gemini"""
    
    ANALYSIS_PROMPT = """Ты — строгий ATS-эксперт и карьерный коуч.

Задача: проанализируй резюме и верни СТРОГО валидный ПОЛНЫЙ JSON (без markdown/код-блоков/комментариев).

КРИТИЧЕСКИ ВАЖНО: 
- Верни ПОЛНЫЙ JSON объект целиком, не обрезай его!
- Объект ДОЛЖЕН быть полностью закрыт (все скобки закрыты)
- Не останавливайся на середине - верни ВСЕ поля полностью

Требования:
- ничего не выдумывай; опирайся только на текст резюме
- формулировки короткие и практичные
- strengths: 3–5 пунктов (короткие, по 5-10 слов каждый)
- improvements: 3 пункта, каждый = {{title, why, how}} (title: 3-5 слов, why: 1 предложение, how: 1 предложение)
- missing_keywords: 10–15 ключевых слов/фраз (без повторов, короткие)

Схема JSON (верни ВЕСЬ объект полностью, все поля обязательны):
{{
  "ats_score": 0,
  "summary": "1-2 предложения",
  "strengths": ["...", "...", "..."],
  "improvements": [{{"title":"...","why":"...","how":"..."}}, {{"title":"...","why":"...","how":"..."}}, {{"title":"...","why":"...","how":"..."}}],
  "missing_keywords": ["...", "...", "..."]
}}

Резюме:
{resume_text}

Верни ТОЛЬКО валидный JSON объект, без дополнительного текста до или после него. Убедись, что объект полностью закрыт финальной скобкой }}.
"""

    TAILOR_PROMPT = """Ты — ATS-эксперт.

Задача: сопоставь резюме и вакансию и верни СТРОГО валидный JSON (без markdown).

Требования:
- ничего не выдумывай; не добавляй опыт, которого нет
- missing_keywords: только то, чего явно нет в резюме, но важно для вакансии (10–25)
- quick_fixes: 5–8 быстрых правок (что поменять в тексте/структуре)
- rewritten_bullets: 3 переписанных буллета в формате {{before, after}} (before берём из резюме максимально близко, after — улучшенная версия с метриками/сильными глаголами, но без выдумки)

Схема JSON:
{{
  "fit_score": 0,
  "missing_keywords": ["..."],
  "quick_fixes": ["..."],
  "rewritten_bullets": [{{"before":"...","after":"..."}}]
}}

Резюме:
{resume_text}

Вакансия:
{job_text}
"""

    IMPROVE_RESUME_PROMPT = """Ты — карьерный редактор и ATS-специалист.

Перепиши резюме, улучшив структуру и формулировки, но:
- не выдумывай факты, компании, даты, цифры
- сохраняй язык резюме (рус/англ) и тон
- делай ATS-friendly: простой текст, чёткие секции, буллеты

Верни ТОЛЬКО обновлённый текст резюме (без вступлений и без markdown).

Резюме:
{resume_text}
"""

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = analysis_cache.get(key)
        if not entry:
            return None
        if entry["expires_at"] < _now():
            analysis_cache.pop(key, None)
            return None
        return entry["value"]

    def _cache_set(self, key: str, value: Dict[str, Any], ttl_seconds: int = 6 * 3600) -> None:
        analysis_cache[key] = {"expires_at": _now() + timedelta(seconds=ttl_seconds), "value": value}

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()

    async def analyze_resume(self, resume_text: str, user_id: int) -> Dict[str, Any]:
        """Базовый анализ резюме"""
        
        # Проверка лимитов
        allowed, error_msg = await check_rate_limit(user_id)
        if not allowed:
            raise ValueError(error_msg)
        
        # Ограничение длины для бесплатного тира
        resume_text = (resume_text or "").strip()
        if len(resume_text) > MAX_RESUME_CHARS_FREE:
            resume_text = resume_text[:MAX_RESUME_CHARS_FREE] + "\n...[текст обрезан]"

        cache_key = f"base:{user_id}:{self._hash_text(resume_text)}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        # Резервируем квоту (и откатываем при ошибке)
        await consume_quota(user_id)
        try:
            prompt = self.ANALYSIS_PROMPT.format(resume_text=resume_text)
            logger.info(f"Calling Gemini API for user {user_id}, resume length: {len(resume_text)}")
            # Увеличиваем max_tokens значительно, чтобы ответ не обрезался
            # JSON с полным анализом может быть довольно длинным
            response = await get_gemini_client().generate_content(prompt, max_tokens=4000)
            logger.debug(f"Gemini response length: {len(response)}")
            analysis = self._parse_analysis_json(response)
            logger.info(f"Analysis parsed successfully for user {user_id}")
        except Exception as e:
            logger.exception(f"Error analyzing resume for user {user_id}: {type(e).__name__}: {e}")
            await refund_quota(user_id)
            raise

        analysis["premium_available"] = True
        analysis["requests_left"] = max(0, FREE_DAILY_LIMIT - get_user_state(user_id)["requests_today"])

        self._cache_set(cache_key, analysis)
        # сохраняем последнюю версию резюме
        user_ctx.setdefault(user_id, {})["last_resume_text"] = resume_text
        user_ctx.setdefault(user_id, {})["mode"] = "idle"
        return analysis
    
    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            logger.error("Empty response from Gemini API")
            raise ValueError("Пустой ответ от ИИ.")
        
        logger.info(f"Raw Gemini response length: {len(raw)}")
        logger.debug(f"Raw Gemini response (first 1000 chars): {raw[:1000]}")
        logger.debug(f"Raw Gemini response (last 500 chars): {raw[-500:] if len(raw) > 500 else raw}")
        
        # Попытка 1: чистый JSON
        try:
            result = json.loads(raw)
            logger.info("Successfully parsed JSON (attempt 1)")
            return result
        except json.JSONDecodeError as e:
            logger.debug(f"JSON parse attempt 1 failed: {e} at position {e.pos}")
            # Показываем контекст ошибки
            start = max(0, e.pos - 50)
            end = min(len(raw), e.pos + 50)
            logger.debug(f"Error context: ...{raw[start:end]}...")

        # Попытка 2: убрать ```json ... ```
        raw2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
        try:
            result = json.loads(raw2)
            logger.info("Successfully parsed JSON (attempt 2, removed markdown)")
            return result
        except json.JSONDecodeError as e:
            logger.debug(f"JSON parse attempt 2 failed: {e} at position {e.pos}")

        # Попытка 3: вытащить первый {...} (может быть неполный JSON)
        m = re.search(r"\{[\s\S]*\}", raw2)
        if m:
            extracted = m.group(0)
            try:
                result = json.loads(extracted)
                logger.info("Successfully parsed JSON (attempt 3, extracted object)")
                return result
            except json.JSONDecodeError as e:
                logger.warning(f"JSON parse attempt 3 failed: {e} at position {e.pos}")
                logger.warning(f"Extracted text length: {len(extracted)}")
                logger.warning(f"Extracted text (first 500): {extracted[:500]}")
                logger.warning(f"Extracted text (last 500): {extracted[-500:] if len(extracted) > 500 else extracted}")
                
                # Попытка 4: попробуем "починить" обрезанный JSON
                # Если JSON обрезан, попробуем закрыть незакрытые структуры
                try:
                    # Подсчитываем незакрытые скобки и кавычки
                    open_braces = extracted.count("{")
                    close_braces = extracted.count("}")
                    open_brackets = extracted.count("[")
                    close_brackets = extracted.count("]")
                    
                    # Пытаемся закрыть структуры
                    fixed = extracted
                    if open_braces > close_braces:
                        fixed += "}" * (open_braces - close_braces)
                    if open_brackets > close_brackets:
                        fixed += "]" * (open_brackets - close_brackets)
                    
                    # Если последний символ - запятая, убираем её
                    fixed = fixed.rstrip().rstrip(",")
                    
                    result = json.loads(fixed)
                    logger.warning("Successfully parsed JSON (attempt 4, fixed truncated JSON)")
                    return result
                except Exception as fix_error:
                    logger.debug(f"JSON fix attempt failed: {fix_error}")
        
        # Логируем полный ответ для диагностики
        logger.error(f"Failed to parse JSON from Gemini response.")
        logger.error(f"Response length: {len(raw)}")
        logger.error(f"Response (first 3000 chars): {raw[:3000]}")
        logger.error(f"Response (last 1000 chars): {raw[-1000:] if len(raw) > 1000 else raw}")
        raise ValueError("Не удалось распарсить ответ ИИ (ожидался JSON).")

    def _parse_analysis_json(self, response: str) -> Dict[str, Any]:
        obj = self._extract_json_object(response)
        result = {
            "ats_score": int(obj.get("ats_score", 0) or 0),
            "summary": str(obj.get("summary", "") or "").strip(),
            "strengths": obj.get("strengths") or [],
            "improvements": obj.get("improvements") or [],
            "missing_keywords": obj.get("missing_keywords") or [],
            "raw_text": response,
        }
        # нормализация
        if not isinstance(result["strengths"], list):
            result["strengths"] = []
        if not isinstance(result["improvements"], list):
            result["improvements"] = []
        if not isinstance(result["missing_keywords"], list):
            result["missing_keywords"] = []
        result["ats_score"] = max(0, min(100, result["ats_score"]))
        return result

    async def tailor_to_job(self, resume_text: str, job_text: str, user_id: int) -> Dict[str, Any]:
        """Оптимизация под вакансию (подбор ключевых слов и быстрые правки)"""
        allowed, error_msg = await check_rate_limit(user_id)
        if not allowed:
            raise ValueError(error_msg)

        resume_text = (resume_text or "").strip()
        job_text = (job_text or "").strip()
        if len(resume_text) > MAX_RESUME_CHARS_FREE:
            resume_text = resume_text[:MAX_RESUME_CHARS_FREE] + "\n...[текст обрезан]"
        if len(job_text) > 4000:
            job_text = job_text[:4000] + "\n...[текст обрезан]"

        cache_key = f"job:{user_id}:{self._hash_text(resume_text)}:{self._hash_text(job_text)}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        await consume_quota(user_id)
        try:
            prompt = self.TAILOR_PROMPT.format(resume_text=resume_text, job_text=job_text)
            response = await get_gemini_client().generate_content(prompt, max_tokens=4000)
            obj = self._extract_json_object(response)
        except Exception:
            await refund_quota(user_id)
            raise

        result = {
            "fit_score": max(0, min(100, int(obj.get("fit_score", 0) or 0))),
            "missing_keywords": obj.get("missing_keywords") or [],
            "quick_fixes": obj.get("quick_fixes") or [],
            "rewritten_bullets": obj.get("rewritten_bullets") or [],
            "raw_text": response,
            "requests_left": max(0, FREE_DAILY_LIMIT - get_user_state(user_id)["requests_today"]),
        }
        self._cache_set(cache_key, result)
        return result

    async def improve_resume_text(self, resume_text: str, user_id: int) -> str:
        """Генерация улучшенного текста резюме (как черновик)"""
        allowed, error_msg = await check_rate_limit(user_id)
        if not allowed:
            raise ValueError(error_msg)

        resume_text = (resume_text or "").strip()
        if len(resume_text) > MAX_RESUME_CHARS_FREE:
            resume_text = resume_text[:MAX_RESUME_CHARS_FREE] + "\n...[текст обрезан]"

        await consume_quota(user_id)
        try:
            prompt = self.IMPROVE_RESUME_PROMPT.format(resume_text=resume_text)
            return (await get_gemini_client().generate_content(prompt, max_tokens=4000)).strip()
        except Exception:
            await refund_quota(user_id)
            raise

resume_analyzer = ResumeAnalyzer()

# ============================================================================
# BOT HANDLERS
# ============================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Под вакансию", callback_data="tailor_start")],
            [InlineKeyboardButton(text="📝 Черновик резюме", callback_data="improve_start")],
            [InlineKeyboardButton(text="💎 Премиум", callback_data="premium_info")],
            [InlineKeyboardButton(text="📚 Примеры", callback_data="examples")],
        ]
    )


def post_analysis_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Оптимизировать под вакансию", callback_data="tailor_start")],
            [InlineKeyboardButton(text="📝 Сделать черновик резюме", callback_data="improve_start")],
            [InlineKeyboardButton(text="💎 Детальный разбор (Премиум)", callback_data="premium_info")],
            [InlineKeyboardButton(text="📤 Поделиться ботом", callback_data="share_bot")],
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Приветствие"""
    user_id = message.from_user.id
    state = get_user_state(user_id)
    
    # Логируем событие: пользователь запустил бота
    track_event("user_started", user_id, {
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "is_new_user": state.get("registered_at") is None
    })
    
    welcome_text = """👋 <b>Добро пожаловать в CareerAI!</b>

🎯 <b>Ваш личный карьерный ассистент с ИИ</b>

Я помогу вам:
• 📊 Проанализировать резюме на ATS-совместимость
• 🎓 Найти слабые места и дать конкретные советы
• 🚀 Увеличить шансы на получение оффера

<b>Как использовать:</b>
Просто отправьте мне текст резюме или файл (PDF/DOCX/TXT)

<b>Бесплатно:</b> {free_limit} анализа в день
<b>Премиум:</b> Неограниченно + детальная оптимизация

Начнем? Отправьте ваше резюме! 📄"""

    await message.answer(welcome_text.format(free_limit=FREE_DAILY_LIMIT), reply_markup=main_menu_keyboard())

@dp.message(Command("premium"))
async def cmd_premium(message: types.Message):
    """Информация о премиум и кнопка оплаты"""
    user_id = message.from_user.id
    premium_text = """💎 <b>CareerAI Premium</b>

<b>Что входит:</b>
✅ Неограниченные анализы резюме
✅ Черновик и оптимизация под вакансию без лимитов
✅ Приоритетная поддержка"""
    
    if is_premium(user_id):
        premium_text += "\n\n🎉 <b>У вас активна премиум-подписка.</b>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
        ])
    elif PAYMENT_PROVIDER_TOKEN:
        price_label = f"{PREMIUM_PRICE_CENTS / 100:.2f} USD" if PREMIUM_CURRENCY == "USD" else f"{PREMIUM_PRICE_CENTS / 100:.0f} ₽"
        premium_text += f"\n\n<b>Цена:</b> {price_label} за {PREMIUM_DAYS} дней.\nОплата прямо в Telegram."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить Premium", callback_data="buy_premium")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
        ])
    else:
        premium_text += "\n\n<i>Оплата скоро. Email для уведомления: hello@careerai.bot</i>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Уведомить о запуске", callback_data="notify_launch")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
        ])
    
    await message.answer(premium_text, reply_markup=keyboard)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Статистика пользователя"""
    user_id = message.from_user.id
    state = get_user_state(user_id)
    
    limit_text = "∞ (Премиум)" if is_premium(user_id) else f"{state['requests_today']}/{FREE_DAILY_LIMIT}"
    stats_text = f"""📊 <b>Ваша статистика</b>

📅 С нами с: {state['registered_at'].strftime('%d.%m.%Y')}
🔢 Запросов сегодня: {limit_text}
⏱ Последний анализ: {state['last_request'].strftime('%H:%M') if state['last_request'] else 'Еще не было'}
{"💎 Премиум активен" if is_premium(user_id) else "💡 /premium — неограниченные анализы"}"""
    
    await message.answer(stats_text)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "🧭 <b>Команды</b>\n\n"
        "• /start — начать\n"
        "• /stats — статистика\n"
        "• /premium — премиум (скоро)\n\n"
        "Отправьте резюме текстом или файлом (PDF/DOCX/TXT)."
    )


@dp.message(Command("privacy"))
async def cmd_privacy(message: types.Message):
    await message.answer(
        "🔒 <b>Приватность</b>\n\n"
        "• Мы используем текст резюме только для анализа и не публикуем его.\n"
        "• В текущем MVP данные/лимиты хранятся в памяти сервера и могут сбрасываться.\n"
        "• Не отправляйте чувствительные данные (паспорт, банковские реквизиты).\n"
    )


def _truncate_for_telegram(text: str, limit: int = 3900) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[:limit] + "\n…"


def _format_strengths(items: list) -> str:
    if not items:
        return "• Не обнаружено"
    return "\n".join([f"• {h(str(x))}" for x in items[:5]])


def _format_improvements(items: list) -> str:
    if not items:
        return "1. Продолжайте в том же духе!"
    lines = []
    for i, item in enumerate(items[:3]):
        if isinstance(item, dict):
            title = h(str(item.get("title", "")).strip())
            how = h(str(item.get("how", "")).strip())
            why = h(str(item.get("why", "")).strip())
            chunk = f"{i+1}. <b>{title}</b>"
            if why:
                chunk += f"\n<i>Почему:</i> {why}"
            if how:
                chunk += f"\n<i>Как:</i> {how}"
            lines.append(chunk)
        else:
            lines.append(f"{i+1}. {h(str(item))}")
    return "\n\n".join(lines)


def _format_keywords(items: list) -> str:
    if not items:
        return "Не обнаружено"
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    return ", ".join(cleaned[:25])


def _extract_text_from_docx_bytes(data: bytes) -> str:
    """Минимальный DOCX→text без внешних зависимостей."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml_bytes = z.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    # Собираем текст из узлов w:t, добавляя переводы строк по абзацам w:p
    paragraphs = []
    for p in root.iter():
        if p.tag.endswith("}p"):
            chunks = []
            for t in p.iter():
                if t.tag.endswith("}t") and t.text:
                    chunks.append(t.text)
            if chunks:
                paragraphs.append("".join(chunks))
    return "\n".join(paragraphs).strip()


async def _extract_resume_text_from_message(message: types.Message) -> Optional[str]:
    # Текст
    if message.text:
        return message.text

    # Файл
    if not message.document:
        return None

    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        raise ValueError(f"Файл слишком большой. Максимум {MAX_FILE_BYTES // (1024*1024)}MB.")

    bot = get_bot()
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    data = buf.read()

    filename = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    # TXT
    if mime in {"text/plain"} or filename.endswith(".txt"):
        return data.decode("utf-8", errors="ignore")

    # PDF
    if mime in {"application/pdf"} or filename.endswith(".pdf"):
        if PdfReader is None:
            raise ValueError("Поддержка PDF не установлена на сервере (нужен пакет PyPDF2).")
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:20]:  # ограничим для MVP
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts).strip()
        return text or None

    # DOCX
    if (
        mime in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        or filename.endswith(".docx")
    ):
        try:
            text = _extract_text_from_docx_bytes(data)
        except Exception:
            raise ValueError("Не удалось прочитать DOCX. Попробуйте сохранить файл заново или пришлите текст.")
        return text or None

    raise ValueError("Неподдерживаемый формат. Отправьте PDF/DOCX/TXT или вставьте текст резюме.")


# Обработчики платежей — регистрируем ДО общего @dp.message(), чтобы срабатывали первыми
@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: types.PreCheckoutQuery):
    """Пользователь нажал «Оплатить» в счёте. Обязательно ответить answer_pre_checkout_query."""
    try:
        user_id = pre_checkout.from_user.id if pre_checkout.from_user else None
        payload = (pre_checkout.invoice_payload or "").strip()
        logger.info(f"pre_checkout_query: user_id={user_id}, payload={payload[:50]}, amount={pre_checkout.total_amount}, currency={pre_checkout.currency}")
        
        # Проверяем, что это наш премиум-счёт
        if not payload.startswith("premium_"):
            logger.warning(f"pre_checkout_query: unknown payload {payload[:50]}, rejecting")
            await pre_checkout.answer(ok=False, error_message="Неверный счёт. Используйте кнопку «Купить Premium» из бота.")
            return
        
        # Подтверждаем оплату
        await pre_checkout.answer(ok=True)
        logger.info(f"pre_checkout_query: confirmed for user_id={user_id}")
    except Exception as e:
        logger.exception(f"pre_checkout_query error: {e}")
        # КРИТИЧНО: даже при ошибке нужно ответить Telegram, иначе кнопка не работает
        try:
            await pre_checkout.answer(ok=False, error_message="Произошла ошибка. Попробуйте позже.")
        except Exception:
            pass


@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    """После успешной оплаты — выдаём премиум."""
    user_id = message.from_user.id
    payment = message.successful_payment
    if not payment:
        return
    payload = (payment.invoice_payload or "").strip()
    if not payload.startswith("premium_"):
        return
    set_premium_until(user_id, days=PREMIUM_DAYS)
    track_event("premium_purchased", user_id, {
        "amount": payment.total_amount,
        "currency": payment.currency,
    })
    await message.answer(
        "🎉 <b>Спасибо за покупку!</b>\n\n"
        f"Премиум активирован на {PREMIUM_DAYS} дней. Теперь вам доступны неограниченные анализы и черновики.",
        reply_markup=main_menu_keyboard()
    )


@dp.message()
async def handle_resume(message: types.Message):
    """Обработка текста резюме или файла"""
    user_id = message.from_user.id
    rid = uuid.uuid4().hex[:8]
    
    # Режимы ожидания (после кнопок)
    mode = user_ctx.get(user_id, {}).get("mode", "idle")
    if mode == "awaiting_email":
        email = (message.text or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            await message.answer("❌ Похоже, это не email. Пришлите корректный адрес, например: name@example.com")
            return
        user_ctx.setdefault(user_id, {})["email"] = email
        user_ctx.setdefault(user_id, {})["mode"] = "idle"
        await message.answer("✅ Отлично! Я уведомлю вас о запуске. Спасибо.", reply_markup=main_menu_keyboard())
        return

    if mode == "awaiting_job_desc":
        job_text = (message.text or "").strip()
        if len(job_text) < 80:
            await message.answer("❌ Текст вакансии слишком короткий. Пришлите описание вакансии (минимум 80 символов).")
            return
        resume_text = user_ctx.get(user_id, {}).get("last_resume_text")
        if not resume_text:
            user_ctx.setdefault(user_id, {})["mode"] = "idle"
            await message.answer("Сначала пришлите резюме (текстом или файлом), затем я смогу оптимизировать под вакансию.")
            return

        bot = get_bot()
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        try:
            result = await resume_analyzer.tailor_to_job(resume_text, job_text, user_id)
            
            # Логируем событие: пользователь успешно оптимизировал резюме под вакансию
            track_event("tailor_completed", user_id, {
                "resume_length": len(resume_text),
                "job_text_length": len(job_text),
                "fit_score": result.get("fit_score", 0)
            })
            
            user_ctx.setdefault(user_id, {})["mode"] = "idle"

            text = (
                "🎯 <b>Оптимизация под вакансию</b>\n\n"
                f"📌 <b>Fit Score:</b> {result['fit_score']}/100\n\n"
                "<b>🔑 Недостающие ключевые слова:</b>\n"
                f"<code>{h(_format_keywords(result.get('missing_keywords') or []))}</code>\n\n"
                "<b>⚡ Быстрые правки:</b>\n"
                + "\n".join([f"• {h(str(x))}" for x in (result.get('quick_fixes') or [])[:8]])
                + f"\n\n<i>Осталось бесплатных анализов сегодня: {result['requests_left']}</i>"
            )
            await message.answer(_truncate_for_telegram(text), reply_markup=post_analysis_keyboard())
            return
        except ValueError as e:
            await message.answer(str(e), parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            logger.exception(f"[{rid}] Error tailoring to job")
            await message.answer(
                "😔 Не удалось оптимизировать под вакансию.\n\n"
                f"Код ошибки: <code>{rid}</code>\n"
                f"Поддержка: {h(SUPPORT_HANDLE)}"
            )
            return

    # Обычный режим: ждём резюме
    bot = get_bot()
    try:
        resume_text = await _extract_resume_text_from_message(message)
    except ValueError as e:
        await message.answer(str(e))
        return
    
    if not resume_text or len(resume_text) < 100:
        await message.answer(
            "❌ Резюме слишком короткое.\n\n"
            "Пожалуйста, отправьте полный текст резюме (минимум 100 символов)."
        )
        return
    
    # Отправка индикатора печати
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    
    try:
        # Анализ резюме
        analysis = await resume_analyzer.analyze_resume(resume_text, user_id)
        
        # Логируем событие: пользователь проанализировал резюме
        track_event("resume_analyzed", user_id, {
            "resume_length": len(resume_text),
            "ats_score": analysis.get("ats_score", 0),
            "has_file": hasattr(message, "document") and message.document is not None
        })
        
        # Форматирование результата
        result_text = f"""✅ <b>Анализ завершен!</b>

📊 <b>ATS Score: {analysis['ats_score']}/100</b>
{_get_score_emoji(analysis['ats_score'])}

<b>🧾 Краткое резюме:</b>
{h(analysis.get('summary', '') or '—')}

<b>💪 Сильные стороны:</b>
{_format_strengths(analysis.get('strengths') or [])}

<b>🎯 Топ-3 улучшения:</b>
{_format_improvements(analysis.get('improvements') or [])}

<b>🔑 Недостающие ключевые слова:</b>
<code>{h(_format_keywords(analysis.get('missing_keywords') or []))}</code>

<i>Осталось бесплатных анализов сегодня: {analysis['requests_left']}</i>"""

        await message.answer(_truncate_for_telegram(result_text), reply_markup=post_analysis_keyboard())
        
        # Подсказка после первого использования
        if get_user_state(user_id)["requests_today"] == 1:
            await asyncio.sleep(2)
            await message.answer(
                "💡 <b>Совет:</b> Хотите узнать, как ваше резюме выглядит на фоне "
                "топ-10% кандидатов в вашей индустрии?\n\n"
                "Премиум даст вам доступ к базе из 50,000+ успешных резюме! → /premium"
            )
        
    except ValueError as e:
        # Лимит исчерпан
        await message.answer(str(e), parse_mode=ParseMode.HTML)
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        logger.exception(f"[{rid}] Error analyzing resume: {error_type}: {error_msg}")
        
        # Логируем событие: произошла ошибка
        track_event("error_occurred", user_id, {
            "error_type": error_type,
            "error_message": error_msg[:200],
            "error_id": rid,
            "action": "resume_analysis"
        })
        
        # Более информативное сообщение об ошибке
        user_friendly_msg = (
            "😔 Произошла ошибка при анализе.\n\n"
            f"Код ошибки: <code>{rid}</code>\n"
        )
        # Добавляем информацию об ошибке для пользователя (если это не критично)
        if "Gemini API" in error_msg or "API" in error_type:
            user_friendly_msg += "Проблема с подключением к ИИ-сервису. Попробуйте позже.\n\n"
        elif "JSON" in error_msg or "parse" in error_msg.lower():
            user_friendly_msg += "Ошибка обработки ответа ИИ. Попробуйте отправить резюме еще раз.\n\n"
        user_friendly_msg += f"Поддержка: {h(SUPPORT_HANDLE)}"
        await message.answer(user_friendly_msg)

# ============================================================================
# CALLBACK HANDLERS
# ============================================================================

BOT_USERNAME = os.getenv("BOT_USERNAME", "@YourCareerAIBot").strip() or "@YourCareerAIBot"


@dp.callback_query(lambda c: c.data == "premium_info")
async def callback_premium_info(callback: types.CallbackQuery):
    """Показать информацию о премиум и кнопку оплаты"""
    user_id = callback.from_user.id
    
    track_event("premium_clicked", user_id)
    
    text = (
        "💎 <b>CareerAI Premium</b>\n\n"
        "<b>Что входит в подписку:</b>\n"
        "✅ Неограниченные анализы резюме (без дневных лимитов)\n"
        "✅ Генерация черновиков резюме с улучшениями\n"
        "✅ Оптимизация резюме под конкретную вакансию\n"
        "✅ Детальный ATS-анализ с рекомендациями\n"
        "✅ Приоритетная поддержка\n\n"
    )
    
    keyboard_buttons = []
    if PAYMENT_PROVIDER_TOKEN and is_premium(user_id):
        text += "🎉 <b>У вас активна премиум-подписка.</b> Спасибо!"
    elif PAYMENT_PROVIDER_TOKEN:
        # Показываем цену: для USD сумма в центах (999 = 9.99), для RUB в копейках
        price_label = f"{PREMIUM_PRICE_CENTS / 100:.2f} USD" if PREMIUM_CURRENCY == "USD" else f"{PREMIUM_PRICE_CENTS / 100:.0f} ₽"
        text += f"<b>Цена:</b> {price_label} за {PREMIUM_DAYS} дней.\n\nОплата прямо в Telegram — картой или через провайдера."
        keyboard_buttons.append([InlineKeyboardButton(text="💳 Купить Premium", callback_data="buy_premium")])
    else:
        text += "Оплата скоро будет доступна. Оставьте email для уведомления: hello@careerai.bot"
        keyboard_buttons.append([InlineKeyboardButton(text="🔔 Уведомить о запуске", callback_data="notify_launch")])
    
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "buy_premium")
async def callback_buy_premium(callback: types.CallbackQuery):
    """Отправить счёт на оплату премиума"""
    user_id = callback.from_user.id
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.answer("Оплата пока не настроена. Попробуйте позже.", show_alert=True)
        return
    if is_premium(user_id):
        await callback.answer("У вас уже есть премиум!", show_alert=True)
        return
    
    bot = get_bot()
    # payload — внутренний идентификатор (не показывается пользователю), до 128 байт
    payload = f"premium_{user_id}_{uuid.uuid4().hex[:12]}"
    title = "CareerAI Premium"
    description = (
        f"Премиум-подписка на {PREMIUM_DAYS} дней.\n\n"
        "Включает:\n"
        "• Неограниченные анализы резюме\n"
        "• Генерация черновиков с улучшениями\n"
        "• Оптимизация под вакансии\n"
        "• Приоритетная поддержка"
    )
    # prices: список из одного элемента; сумма в минимальных единицах (центы для USD, копейки для RUB)
    prices = [LabeledPrice(label="Premium подписка", amount=PREMIUM_PRICE_CENTS)]
    
    try:
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=PREMIUM_CURRENCY,
            prices=prices,
        )
        await callback.answer("Счёт отправлен. Проверьте чат.")
    except Exception as e:
        err_msg = str(e).strip()
        # Скрываем возможные фрагменты токена в логах/сообщении
        if len(PAYMENT_PROVIDER_TOKEN) > 8:
            err_msg = err_msg.replace(PAYMENT_PROVIDER_TOKEN[:8], "***").replace(PAYMENT_PROVIDER_TOKEN[-4:], "***")
        logger.exception("Send invoice error: %s", e)
        await callback.answer("Не удалось отправить счёт. Попробуйте позже.", show_alert=True)
        # Подсказка в чат (видна в логах Vercel и помогает при отладке)
        hint = (
            "⚠️ <b>Ошибка отправки счёта</b>\n\n"
            f"Код: <code>{type(e).__name__}</code>\n"
            f"Текст: {html.escape(err_msg[:300])}\n\n"
            "Проверьте:\n"
            "• <b>PAYMENT_PROVIDER_TOKEN</b> в Vercel — это токен <b>от BotFather</b> (Payments → YooKassa), не секрет YooKassa из ЛК.\n"
            "• В BotFather: Payments → выбран YooKassa, вставлен секретный ключ из YooKassa.\n"
            "• Для YooKassa валюта обычно <b>RUB</b> (PREMIUM_CURRENCY=RUB), сумма в копейках."
        )
        try:
            await callback.message.answer(hint)
        except Exception:
            pass



@dp.callback_query(lambda c: c.data == "notify_launch")
async def callback_notify_launch(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_ctx.setdefault(user_id, {})["mode"] = "awaiting_email"
    await callback.message.answer(
        "🔔 Ок, пришлите ваш email одним сообщением.\n\n"
        "<i>Я сохраню его только для уведомления о запуске премиума (MVP-режим).</i>"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "tailor_start")
async def callback_tailor_start(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    # Логируем событие: пользователь начал оптимизацию под вакансию
    track_event("tailor_started", user_id)
    
    if not user_ctx.get(user_id, {}).get("last_resume_text"):
        await callback.message.answer("Сначала пришлите резюме (текстом или файлом), затем нажмите «🎯 Под вакансию».")
        await callback.answer()
        return
    user_ctx.setdefault(user_id, {})["mode"] = "awaiting_job_desc"
    await callback.message.answer(
        "🎯 Пришлите <b>текст вакансии</b> (или описание позиции).\n\n"
        "Я подберу недостающие ключевые слова и быстрые правки для ATS.\n"
        "<i>Минимум 80 символов.</i>"
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "improve_start")
async def callback_improve_start(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    # Логируем событие: пользователь начал создание черновика
    track_event("improve_started", user_id)
    
    rid = uuid.uuid4().hex[:8]
    resume_text = user_ctx.get(user_id, {}).get("last_resume_text")
    if not resume_text:
        await callback.message.answer("Сначала пришлите резюме (текстом или файлом), затем нажмите «📝 Черновик резюме».")
        await callback.answer()
        return

    bot = get_bot()
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
    try:
        improved = await resume_analyzer.improve_resume_text(resume_text, user_id)
        
        # Логируем событие: пользователь успешно создал черновик
        track_event("improve_completed", user_id, {
            "resume_length": len(resume_text),
            "improved_length": len(improved)
        })
        
        # Telegram лимит: лучше отдавать файлом
        file_bytes = improved.encode("utf-8", errors="ignore")
        document = BufferedInputFile(file_bytes, filename="resume_draft.txt")
        await callback.message.answer_document(
            document=document,
            caption="📝 Черновик готов. Проверьте факты/цифры и отредактируйте под себя."
        )
        await callback.message.answer(
            "Хотите адаптацию под вакансию? Нажмите «🎯 Оптимизировать под вакансию» и пришлите текст вакансии.",
            reply_markup=post_analysis_keyboard()
        )
    except ValueError as e:
        await callback.message.answer(str(e), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception(f"[{rid}] Error improving resume")
        await callback.message.answer(
            "😔 Не удалось сделать черновик.\n\n"
            f"Код ошибки: <code>{rid}</code>\n"
            f"Поддержка: {h(SUPPORT_HANDLE)}"
        )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "examples")
async def callback_examples(callback: types.CallbackQuery):
    """Показать примеры"""
    await callback.message.edit_text(
        "📚 <b>Примеры анализа</b>\n\n"
        "<b>До оптимизации:</b> ATS Score 34/100\n"
        "• Нет ключевых слов индустрии\n"
        "• Обязанности вместо достижений\n"
        "• Плохое форматирование\n\n"
        "<b>После оптимизации:</b> ATS Score 87/100\n"
        "✅ Добавлены релевантные навыки\n"
        "✅ Метрики в достижениях (↑35% продаж)\n"
        "✅ ATS-friendly структура\n\n"
        "<b>Результат:</b> 3 приглашения на собеседование за неделю!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Попробовать", callback_data="back_to_start")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data in {"share_bot"} or c.data.startswith("share_"))
async def callback_share(callback: types.CallbackQuery):
    """Поделиться ботом"""
    user_id = callback.from_user.id
    share_text = (
        "🎯 Я только что проанализировал(а) своё резюме с CareerAI!\n\n"
        "Получил(а) конкретные советы и список ключевых слов для ATS.\n\n"
        f"Попробуй сам → {BOT_USERNAME}\n\n"
        f"🎁 Бонус-код: REF{user_id}"
    )

    await callback.message.answer(
        f"📤 <b>Скопируйте и отправьте друзьям:</b>\n\n<code>{h(share_text)}</code>"
    )
    await callback.answer("Спасибо за распространение!")

@dp.callback_query(lambda c: c.data == "back_to_start")
async def callback_back(callback: types.CallbackQuery):
    """Вернуться к началу"""
    await callback.message.edit_text(
        "👋 <b>CareerAI - Ваш карьерный ассистент</b>\n\n"
        "Отправьте текст резюме или файл (PDF/DOCX/TXT) для анализа!",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _get_score_emoji(score: int) -> str:
    """Эмодзи для ATS score"""
    if score >= 80:
        return "🟢 Отлично! Резюме пройдет большинство ATS"
    elif score >= 60:
        return "🟡 Хорошо, но есть что улучшить"
    elif score >= 40:
        return "🟠 Требуется серьезная доработка"
    else:
        return "🔴 Критично! Резюме не пройдет ATS"

def _format_list_legacy(items: list) -> str:
    """(legacy) Старый формат списка."""
    if not items:
        return "• Не обнаружено"
    return "\n".join([f"• {item}" for item in items])


def _format_improvements_legacy(items: list) -> str:
    """(legacy) Старый формат улучшений."""
    if not items:
        return "1. Продолжайте в том же духе!"
    return "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])

# ============================================================================
# YOOKASSA WEBHOOK (уведомления о платежах)
# ============================================================================

@app.post("/api/yookassa-webhook")
async def yookassa_webhook(request: Request):
    """Принимает уведомления от YooKassa (payment.succeeded и др.). Возвращаем 200 — подтверждение получения."""
    try:
        body = await request.json()
        event = body.get("event", "")
        logger.info(f"YooKassa webhook: event={event}")
    except Exception:
        pass
    return {"ok": True}


# ============================================================================
# VERCEL WEBHOOK HANDLER
# ============================================================================

async def _process_webhook_update(update: dict) -> None:
    """Обработка одного апдейта (вызов диспетчера). Используется из /api/webhook и /api/webhook-handler."""
    update_id = update.get("update_id", "N/A")
    telegram_update = types.Update(**update)
    b = get_bot()
    try:
        await asyncio.wait_for(dp.feed_update(b, telegram_update), timeout=25.0)
        logger.info(f"Update {update_id} processed successfully")
    except asyncio.TimeoutError:
        logger.warning(f"Update {update_id} processing timeout (25s)")
    except Exception as e:
        logger.exception(f"Error processing update {update_id}: {type(e).__name__}: {str(e)}")


@app.post("/api/webhook-handler")
async def telegram_webhook_handler_internal(request: Request):
    """Внутренний обработчик: вызывается Edge-функцией для всех апдейтов кроме pre_checkout_query."""
    try:
        update = await request.json()
        # Прогревочный запрос от cron (update_id 0) — не обрабатываем, сразу 200
        if update.get("update_id") in (0, "0"):
            return {"ok": True}
        await _process_webhook_update(update)
        return {"ok": True}
    except Exception as e:
        logger.exception(f"webhook-handler error: {e}")
        return {"ok": False}, 500


@app.options("/api/webhook")
async def telegram_webhook_options(request: Request):
    """Обработка OPTIONS-запросов для CORS и ngrok"""
    return {"ok": True}


@app.get("/api/webhook")
async def telegram_webhook_get():
    """GET для прогрева (cron пингует этот URL). Отвечает 200 в любом случае."""
    return {"ok": True}


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    """Обработчик вебхуков от Telegram"""
    try:
        # Читаем JSON напрямую
        update = await request.json()
        update_id = update.get('update_id', 'N/A')
        
        # Определяем тип обновления для логирования
        update_type = "unknown"
        if "message" in update:
            update_type = "message"
            msg_text = update.get("message", {}).get("text", "")
            logger.info(f"Webhook received: update_id={update_id}, type={update_type}, text={msg_text[:50]}")
        elif "callback_query" in update:
            update_type = "callback_query"
            callback_data = update.get("callback_query", {}).get("data", "")
            logger.info(f"Webhook received: update_id={update_id}, type={update_type}, data={callback_data}")
        elif "pre_checkout_query" in update:
            update_type = "pre_checkout_query"
            pre_checkout = update.get("pre_checkout_query", {})
            user_id = pre_checkout.get("from", {}).get("id")
            payload = (pre_checkout.get("invoice_payload") or "").strip()
            amount = pre_checkout.get("total_amount")
            currency = pre_checkout.get("currency")
            pq_id = pre_checkout.get("id")
            logger.info(f"Webhook pre_checkout_query: update_id={update_id}, user_id={user_id}, payload={payload[:50]}")
            # КРИТИЧНО: Telegram ждёт ответ ~10 сек. Не вызываем get_bot() — холодный старт
            # съедает время. Отвечаем Telegram напрямую по HTTP (без aiogram/Bot).
            # Чтобы не было cold start при оплате — пингуйте /api/health раз в 1–2 мин (cron).
            if pq_id:
                token = (os.getenv("BOT_TOKEN") or "").strip()
                ok = payload.startswith("premium_")
                err_msg = None if ok else "Неверный счёт. Используйте кнопку «Купить Premium» из бота."
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        body = {"pre_checkout_query_id": pq_id, "ok": ok}
                        if err_msg:
                            body["error_message"] = err_msg
                        r = await client.post(
                            f"https://api.telegram.org/bot{token}/answerPreCheckoutQuery",
                            json=body,
                        )
                    if r.is_success:
                        logger.info(f"pre_checkout_query {update_id}: answered ok={ok} (direct HTTP)")
                    else:
                        logger.warning(f"pre_checkout_query answer HTTP {r.status_code}: {r.text[:200]}")
                except Exception as e:
                    logger.exception(f"pre_checkout_query direct HTTP error: {e}")
                    if token:
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                await client.post(
                                    f"https://api.telegram.org/bot{token}/answerPreCheckoutQuery",
                                    json={"pre_checkout_query_id": pq_id, "ok": False, "error_message": "Ошибка. Попробуйте позже."},
                                )
                        except Exception:
                            pass
            return {"ok": True}
        else:
            logger.info(f"Webhook received: update_id={update_id}, type={update_type}, keys={list(update.keys())}")
        
        await _process_webhook_update(update)
        return {"ok": True}
    except ValueError as e:
        # Ошибки валидации данных от Telegram
        logger.error(f"Webhook validation error: {str(e)}", exc_info=True)
        return {"ok": False, "error": "invalid_update"}, 400
    except RuntimeError as e:
        # Ошибки конфигурации (нет токена и т.д.)
        logger.error(f"Webhook config error: {str(e)}", exc_info=True)
        return {"ok": False, "error": "configuration_error"}, 500
    except Exception as e:
        # Все остальные ошибки
        logger.exception(f"Webhook unexpected error: {type(e).__name__}: {str(e)}")
        return {"ok": False, "error": "internal_error"}, 500

@app.get("/")
async def root():
    """Проверка работоспособности"""
    return {
        "status": "running",
        "bot": APP_NAME,
        "version": APP_VERSION,
        "configured": bool(BOT_TOKEN) and bool(GEMINI_API_KEY),
    }

@app.get("/api/health")
async def health_check():
    """Health check для мониторинга"""
    try:
        return {
            "status": "healthy",
            "users": len(user_data),
            "timestamp": _now().isoformat(),
            "version": APP_VERSION,
        }
    except Exception as e:
        logger.exception("health_check error")
        return {
            "status": "degraded",
            "error": str(e),
            "version": APP_VERSION,
        }

def _stats_response():
    """Общая логика ответа для /api/stats и /stats"""
    try:
        stats = get_analytics_stats()
        return {
            "status": "ok",
            "analytics": stats,
            "note": "Данные хранятся в памяти и сбрасываются при перезапуске сервера"
        }
    except Exception as e:
        logger.exception("_stats_response error")
        return {
            "status": "error",
            "error": str(e),
            "analytics": {},
        }

@app.get("/api/stats")
async def analytics_stats():
    """Эндпоинт для просмотра аналитики: https://careeraibot.vercel.app/api/stats"""
    return _stats_response()

@app.get("/stats")
async def analytics_stats_alt():
    """Альтернативный URL (на случай если /api/stats не доходит): https://careeraibot.vercel.app/stats"""
    return _stats_response()

# ============================================================================
# MAIN (для локального тестирования)
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    async def _run_polling():
        b = get_bot()
        # Удаляем webhook перед запуском polling
        try:
            await b.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook deleted successfully")
        except Exception as e:
            logger.warning(f"Failed to delete webhook (may not exist): {e}")
        logger.info("Starting polling...")
        await dp.start_polling(b)

    mode = os.getenv("RUN_MODE", "api").strip().lower()
    if mode in {"polling", "poll"}:
        print("🚀 Starting CareerAI Bot (polling mode)...")
        asyncio.run(_run_polling())
    else:
        print("🚀 Starting CareerAI Bot (api/webhook mode)...")
        if BOT_TOKEN:
            print(f"📝 Set webhook: https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url=YOUR_PUBLIC_URL/api/webhook")
        uvicorn.run(app, host="0.0.0.0", port=8000)
