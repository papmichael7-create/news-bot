"""
Новостной бот для Telegram-канала @newsfrom1AC.

Что делает за один запуск:
1. Читает RSS-ленты из sources.txt.
2. Берёт свежие статьи (не старше MAX_AGE_HOURS), которых ещё не видел.
3. Одним запросом к Gemini убирает дубли: одна и та же новость из разных
   источников публикуется один раз, а уже опубликованные темы не повторяются.
4. Для каждой выбранной статьи берёт полный текст, просит Gemini написать
   короткий пост на русском (заголовок + до 4 предложений).
5. Публикует в канал (с картинкой из статьи, если она есть).
6. Сохраняет историю в state.json, чтобы не повторяться в следующих запусках.

Настройки берутся из переменных окружения (в GitHub — из Secrets).
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
import trafilatura

# ---------- Настройки ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@newsfrom1AC")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")

MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "36"))       # статьи старше не берём
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))  # постов за один запуск
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "5"))           # сколько дней помнить темы
POST_INTERVAL_SEC = int(os.environ.get("POST_INTERVAL_SEC", "300"))  # пауза между постами (5 минут)

SOURCES_FILE = "sources.txt"
STATE_FILE = "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

NOW = datetime.now(timezone.utc)


def log(*args):
    print(*args, flush=True)


# ---------- Состояние ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen", {})    # url -> время, когда увидели
    state.setdefault("posted", [])  # [{title, text, source, ts}]
    return state


def save_state(state):
    # чистим старое, чтобы файл не рос бесконечно
    seen_cutoff = (NOW - timedelta(days=30)).isoformat()
    state["seen"] = {u: t for u, t in state["seen"].items() if t >= seen_cutoff}
    hist_cutoff = (NOW - timedelta(days=HISTORY_DAYS)).isoformat()
    state["posted"] = [p for p in state["posted"] if p["ts"] >= hist_cutoff]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- Утилиты ----------
def normalize_url(url):
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def strip_html(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def entry_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]
    return None


def http_get(url, timeout=30):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r


# ---------- Шаг 1–2: сбор статей ----------
def read_sources():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return [line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")]


def collect_candidates(state):
    candidates = []
    cutoff = NOW - timedelta(hours=MAX_AGE_HOURS)
    for feed_url in read_sources():
        try:
            raw = http_get(feed_url).content
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                raw = raw.decode("cp1251").encode("utf-8")  # русские сайты в старой кодировке
            feed = feedparser.parse(raw)
        except Exception as e:
            log(f"[!] Лента недоступна: {feed_url} — {e}")
            continue
        source = feed.feed.get("title") or urlsplit(feed_url).netloc
        count = 0
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            url = normalize_url(link)
            if url in state["seen"]:
                continue
            published = entry_time(entry)
            if published and published < cutoff:
                continue
            content = ""
            if entry.get("content"):
                content = strip_html(entry.content[0].get("value", ""))
            summary = strip_html(entry.get("summary", ""))
            candidates.append({
                "url": url,
                "link": link,
                "source": source,
                "title": strip_html(entry.get("title", "")),
                "summary": summary[:400] or content[:400],
                "rss_text": content if len(content) > len(summary) else summary,
                "image": entry_image(entry),
                "published": (published or NOW).isoformat(),
            })
            count += 1
        log(f"[ok] {source}: новых статей {count}")
    candidates.sort(key=lambda c: c["published"])  # старые первыми
    return candidates


# ---------- Gemini ----------
def gemini_json(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }
    last_error = None
    for model in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(4):
            try:
                r = requests.post(url, json=body, timeout=60,
                                  headers={"x-goog-api-key": GEMINI_KEY})
            except requests.RequestException as e:
                last_error = e
                if attempt >= 1:
                    log(f"[..] Gemini {model} не отвечает, переключаюсь на запасную модель")
                    break
                time.sleep(5)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"{r.status_code} {r.text[:200]}"
                if r.status_code >= 500 and attempt >= 1:
                    log(f"[..] Gemini {model} перегружен, переключаюсь на запасную модель")
                    break
                wait = 15 * (attempt + 1)
                log(f"[..] Gemini {model}: {r.status_code}, жду {wait} c")
                time.sleep(wait)
                continue
            if r.status_code in (400, 403, 404) and "API key" not in r.text:
                last_error = f"{r.status_code} {r.text[:200]}"
                log(f"[!] Модель {model} недоступна, пробую запасную")
                break
            if r.status_code != 200:
                raise RuntimeError(f"Gemini: {r.status_code} {r.text[:300]}")
            data = r.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
            except (KeyError, IndexError):
                last_error = f"пустой ответ: {str(data)[:200]}"
                time.sleep(5)
                continue
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                last_error = f"не JSON: {text[:200]}"
                time.sleep(3)
                continue
    raise RuntimeError(f"Gemini не ответил: {last_error}")


# ---------- Шаг 3: отбор и удаление дублей ----------
SELECT_PROMPT = """Ты редактор Telegram-канала новостей о кинотехнике и кинопроизводстве
для профессионалов камерного цеха (камеры, объективы, свет, грип, постпродакшн,
новости индустрии кино и видео).

Ниже список НОВЫХ статей из разных источников (английских и русских) и список тем,
которые канал УЖЕ опубликовал за последние дни.

Задача:
1. Найди среди новых статей те, что рассказывают об одном и том же событии или
   анонсе (один продукт, одно обновление, одна новость). Из каждой такой группы
   оставь только ОДНУ статью — самую информативную.
2. Убери статьи, чья новость уже есть среди опубликованных тем (даже если другими словами).
3. Убери чистую рекламу, скидки и распродажи, подборки «лучших сделок»,
   анонсы эпизодов подкастов и материалы, не связанные с кино/видео.
4. Оставляй только то, что связано со съёмкой: камеры, объективы и оптика для
   съёмки, свет, грип, стабилизаторы, мониторы, звук для съёмок, постпродакшн,
   операторские краны и телескопические краны, тележки и рельсы, аккумуляторы
   и питание для съёмочной техники, сумки и кейсы для техники, фильтры,
   экшн-камеры и 360-камеры, съёмки фильмов и сериалов. Новости производителей о другой продукции
   (бинокли, микроскопы, прицелы, принтеры, медицинская и промышленная техника,
   финансовые отчёты компаний) — убирай.
5. Новости отраслевых выставок по всему миру (NAB, IBC, Cine Gear, BSC Expo,
   Camerimage, Inter BEE, BIRTV, CP+, Prolight + Sound, а также российские CPS,
   CSTB.PRO.MEDIA, NATEXPO, ProIntegration Tech и т.п.): анонсы, премьеры
   техники, итоги — оставляй, если они про кино, видео, фото, свет или звук.
Обзоры, тесты, интервью и разборы по теме канала оставляй.

Верни JSON: {{"selected": [id, ...]}} — id выбранных статей, от самой важной к менее важной.

Уже опубликовано:
{published}

Новые статьи:
{candidates}
"""


def select_unique(candidates, state):
    published = "\n".join(f"- {p['title']}: {p['text'][:200]}" for p in state["posted"]) or "(пока ничего)"
    lines = []
    for i, c in enumerate(candidates):
        lines.append(f"id={i} | {c['source']} | {c['title']} | {c['summary'][:300]}")
    result = gemini_json(SELECT_PROMPT.format(published=published, candidates="\n".join(lines)))
    ids = []
    for x in result.get("selected", []):
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(candidates) and i not in ids:
            ids.append(i)
    return ids


# ---------- Шаг 4: текст статьи и пост ----------
def fetch_article(c):
    """Полный текст статьи и картинка. Если сайт не пускает — берём текст из RSS."""
    text, image = "", c["image"]
    try:
        page_bytes = http_get(c["link"]).content
        text = trafilatura.extract(page_bytes, include_comments=False, include_tables=False) or ""
        page = page_bytes.decode("utf-8", errors="ignore")
        if not image:
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page) \
                or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', page)
            if m:
                image = html.unescape(m.group(1))
    except Exception as e:
        log(f"[!] Не удалось открыть статью ({e}), использую текст из RSS")
    if len(text) < len(c["rss_text"]):
        text = c["rss_text"]
    return text[:12000], image


POST_PROMPT = """Ты редактор русскоязычного Telegram-канала новостей о кинотехнике
и кинопроизводстве для профессионалов камерного цеха.

Напиши по статье ниже короткий новостной пост на русском языке.

Требования:
- "title": короткий цепкий заголовок, до 90 символов, без кавычек и точки в конце.
- "text": от 2 до 4 предложений. Только суть: что произошло или что анонсировано,
  ключевые характеристики, цена и дата выхода, если они есть в статье.
- Названия брендов, моделей и технологий оставляй в оригинале (ARRI Alexa 35, DaVinci Resolve).
- Нейтральный информационный тон, без восклицаний, эмодзи, хэштегов и ссылок.
- Ничего не выдумывай: только факты из статьи.

Верни JSON: {{"title": "...", "text": "..."}}

Источник: {source}
Заголовок оригинала: {title}

Текст статьи:
{text}
"""


def make_post(c, text):
    result = gemini_json(POST_PROMPT.format(source=c["source"], title=c["title"], text=text))
    title = (result.get("title") or "").strip()
    body = (result.get("text") or "").strip()
    if not title or not body:
        raise RuntimeError("Gemini вернул пустой пост")
    return title, body


# ---------- Шаг 5: Telegram ----------
def tg(method, payload):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data


def publish(title, body, image):
    message = f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
    if image and len(message) <= 1024:
        try:
            tg("sendPhoto", {"chat_id": CHANNEL, "photo": image,
                             "caption": message, "parse_mode": "HTML"})
            return
        except Exception as e:
            log(f"[!] Картинка не отправилась ({e}), публикую текстом")
    tg("sendMessage", {"chat_id": CHANNEL, "text": message, "parse_mode": "HTML",
                       "link_preview_options": {"is_disabled": True}})


# ---------- Запуск ----------
def main():
    if not BOT_TOKEN or not GEMINI_KEY:
        log("Не заданы TELEGRAM_BOT_TOKEN и/или GEMINI_API_KEY")
        sys.exit(1)

    state = load_state()
    candidates = collect_candidates(state)
    if not candidates:
        log("Новых статей нет")
        save_state(state)
        return

    selected = select_unique(candidates, state)
    log(f"Новых статей: {len(candidates)}, после отбора и удаления дублей: {len(selected)}")

    to_post = selected[:MAX_POSTS_PER_RUN]
    postponed = set(selected[MAX_POSTS_PER_RUN:])  # опубликуем в следующих запусках

    # всё, что не выбрано (дубли, реклама) и не отложено, отмечаем как просмотренное
    for i, c in enumerate(candidates):
        if i not in to_post and i not in postponed:
            state["seen"][c["url"]] = NOW.isoformat()
    save_state(state)

    for n, i in enumerate(to_post):
        c = candidates[i]
        try:
            text, image = fetch_article(c)
            title, body = make_post(c, text)
            # пауза между постами (в том числе между запусками)
            wait_left = POST_INTERVAL_SEC - (time.time() - state.get("last_post_ts", 0))
            if wait_left > 0:
                log(f"[..] Жду {int(wait_left)} c до следующего поста")
                time.sleep(wait_left)
            publish(title, body, image)
            state["last_post_ts"] = time.time()
            log(f"[+] Опубликовано: {title}  ({c['source']})")
            state["posted"].append({"title": title, "text": body,
                                    "source": c["source"], "ts": NOW.isoformat()})
        except Exception as e:
            log(f"[!] Ошибка со статьёй {c['link']}: {e}")
        # отмечаем в любом случае, чтобы проблемная статья не зациклилась
        state["seen"][c["url"]] = NOW.isoformat()
        save_state(state)


if __name__ == "__main__":
    main()
