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
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests
import trafilatura

# ---------- Настройки ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@newsfrom1AC")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
VK_TOKEN = os.environ.get("VK_TOKEN", "")                      # ключ сообщества ВКонтакте
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "243786644")       # группа vk.com/club243786644
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
GEMINI_MODELS = [GEMINI_MODEL, GEMINI_FALLBACK_MODEL, "gemini-flash-latest"]
MAX_FAILS = 3  # сколько раз пробовать статью, если Gemini недоступен

MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "36"))       # статьи старше не берём
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "8"))  # постов за один запуск
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "7"))           # сколько дней помнить темы (и для дайджеста)
POST_INTERVAL_SEC = int(os.environ.get("POST_INTERVAL_SEC", "300"))  # минимальная пауза между постами (5 минут)
SPREAD_WINDOW_SEC = int(os.environ.get("SPREAD_WINDOW_SEC", "9600"))  # растягиваем посты запуска на ~160 минут

SOURCES_FILE = "sources.txt"
STATE_FILE = "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

NOW = datetime.now(timezone.utc)


def log(*args):
    print(*args, flush=True)


PROBLEMS = []  # серьёзные проблемы запуска: если есть, запуск помечается неудачным и GitHub пишет на почту


def problem(text):
    PROBLEMS.append(text)
    log(f"[!!] ПРОБЛЕМА: {text}")


# ---------- Состояние ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen", {})    # url -> время, когда увидели
    state.setdefault("posted", [])  # [{title, text, source, ts}]
    state.setdefault("fails", {})   # url -> сколько раз не удалось обработать
    state.setdefault("health", {"feeds": {}, "gemini": 0})  # счётчики сбоев подряд
    return state


def save_state(state):
    # чистим старое, чтобы файл не рос бесконечно
    seen_cutoff = (NOW - timedelta(days=30)).isoformat()
    state["seen"] = {u: t for u, t in state["seen"].items() if t >= seen_cutoff}
    hist_cutoff = (NOW - timedelta(days=HISTORY_DAYS)).isoformat()
    state["posted"] = [p for p in state["posted"] if p["ts"] >= hist_cutoff]
    state["fails"] = {u: n for u, n in state.get("fails", {}).items() if u not in state["seen"]}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- Синхронизация памяти с GitHub ----------
# Память (state.json) сохраняется в репозиторий сразу после каждого поста,
# а перед публикацией сверяется с последней сохранённой версией.
# Так одна и та же новость не выйдет дважды, даже если запуск оборвётся.
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))


def git(*args):
    return subprocess.run(["git", "-c", "user.name=news-bot",
                           "-c", "user.email=news-bot@users.noreply.github.com", *args],
                          capture_output=True, text=True)


def merge_state(a, b):
    """Объединяет две версии памяти, ничего не теряя."""
    seen = dict(b.get("seen", {}))
    for u, t in a.get("seen", {}).items():
        seen[u] = max(t, seen.get(u, t))
    posted, keys = [], set()
    for p in b.get("posted", []) + a.get("posted", []):
        k = (p.get("orig") or p.get("title"), p.get("source"))
        if k not in keys:
            keys.add(k)
            posted.append(p)
    fails = dict(b.get("fails", {}))
    for u, n in a.get("fails", {}).items():
        fails[u] = max(n, fails.get(u, 0))
    return {"seen": seen, "posted": posted, "fails": fails,
            "health": a.get("health") or b.get("health") or {"feeds": {}, "gemini": 0},
            "digest_week": max(a.get("digest_week", ""), b.get("digest_week", "")),
            "last_poll_ts": max(a.get("last_poll_ts", 0), b.get("last_poll_ts", 0)),
            "discover_month": max(a.get("discover_month", ""), b.get("discover_month", "")),
            "link_domains": a.get("link_domains") or b.get("link_domains") or {},
            "last_post_ts": max(a.get("last_post_ts", 0), b.get("last_post_ts", 0))}


def remote_state():
    if git("fetch", "-q", "origin", "main").returncode != 0:
        return None
    r = git("show", "origin/main:" + STATE_FILE)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def sync_state(state):
    """Подтягивает свежую память из репозитория (её мог обновить другой запуск)."""
    if not IN_ACTIONS:
        return
    remote = remote_state()
    if remote:
        merged = merge_state(state, remote)
        state.clear()
        state.update(merged)


def push_state(state):
    """Сохраняет память локально и сразу отправляет её в репозиторий."""
    save_state(state)
    if not IN_ACTIONS:
        return
    for attempt in range(4):
        remote = remote_state()
        if remote is None:
            time.sleep(3)
            continue
        merged = merge_state(state, remote)
        state.clear()
        state.update(merged)
        git("reset", "-q", "--hard", "origin/main")
        save_state(state)
        git("add", STATE_FILE)
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        git("commit", "-q", "-m", "update state")
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return
        time.sleep(3)
    log("[!] Не удалось сохранить память в репозиторий")


# ---------- Проверка дублей в коде ----------
GENERIC = set("""a an the and or for with to of in on at by from new now is are its it this that
your how why what all more than after as up out into about vs via
announced announces announcing announce introduced introduces introducing introduce
launched launches launching launch released releases releasing release unveiled unveils unveil
update updated updates version firmware camera cameras lens lenses review hands-on first look
video cinema plugin plug-in available free sale deal deals black friday now officially
""".split())
BRANDS = set("""nikon sony canon arri red blackmagic dji panasonic fujifilm fuji leica zeiss cooke
sigma tamron laowa aputure amaran nanlite nanlux godox tilta smallrig atomos teradek hollyland
rode deity sennheiser gopro insta360 apple adobe avid davinci resolve kino flo litepanels
""".split())


def key_tokens(text):
    toks = set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-+]*", text or ""):
        w = w.lower().strip(".-")
        if re.fullmatch(r"\d+(\.\d+)?", w):
            w = w.rstrip("0").rstrip(".") if "." in w else w
        if w in GENERIC or (len(w) < 2 and not w.isdigit()):
            continue
        toks.add(w)
    return toks


def is_duplicate(tokens, others):
    """Совпадают ли ключевые названия (бренд, модель, версия) с уже опубликованным."""
    for o in others:
        common = tokens & o
        # должно совпасть хотя бы одно «имя» (модель, продукт), а не только бренд и цифры
        names = [w for w in common if w not in BRANDS and not re.fullmatch(r"[\d.]+", w)]
        if names and len(common) >= 2 and len(common) >= 0.6 * min(len(tokens), len(o)):
            return True
    return False


def posted_tokens(state):
    return [key_tokens(p.get("orig", "")) | key_tokens(p.get("title", ""))
            for p in state["posted"]]


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
            n = state["health"]["feeds"].get(feed_url, 0) + 1
            state["health"]["feeds"][feed_url] = n
            if n == 3 or n % 16 == 0:  # 3 запуска подряд, дальше раз в сутки
                problem(f"источник не отвечает {n} запусков подряд: {feed_url} ({e})")
            continue
        state["health"]["feeds"].pop(feed_url, None)
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
            content, raw_html = "", entry.get("summary", "")
            if entry.get("content"):
                raw_html = entry.content[0].get("value", "") + raw_html
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
                "links": html_links(raw_html, link),
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
    for model in GEMINI_MODELS:
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
            if r.status_code == 429 and "PerDay" in r.text:
                last_error = f"{r.status_code} {r.text[:200]}"
                log(f"[..] Gemini {model}: дневной лимит исчерпан, беру другую модель")
                break
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"{r.status_code} {r.text[:200]}"
                if attempt >= 2:
                    log(f"[..] Gemini {model} не отвечает, переключаюсь на запасную модель")
                    break
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
   экшн-камеры и 360-камеры, съёмки фильмов и сериалов, программы и видеоплатформы
   для работы с видео. Новости производителей о другой продукции
   (бинокли, микроскопы, прицелы, принтеры, медицинская и промышленная техника) — убирай.
   Финансовые результаты и крупные сделки компаний кино- и видеоиндустрии — оставляй.
   Убирай мелкие локальные новости учреждений: какая-то школа, университет, студия
   или прокат получили грант, закупили или установили технику, открыли класс и т.п.
   Опыт и уроки операторов, режиссёров и съёмочных групп (как снимали фильм,
   какие приёмы использовали) — оставляй, это ценный формат.
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
SKIP_LINK_HOSTS = ("facebook.", "twitter.", "x.com", "instagram.", "linkedin.", "tiktok.",
                   "pinterest.", "t.me", "telegram.", "youtube.", "youtu.be", "reddit.",
                   "patreon.", "wikipedia.", "google.", "apple.com/podcasts", "spotify.")


def html_links(raw, base):
    """Ссылки из HTML: [(текст, адрес)]."""
    out = []
    for href, label in re.findall(r'<a\s[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                  raw or "", flags=re.S | re.I):
        out.append((strip_html(label), urljoin(base, html.unescape(href))))
    return out


def clean_links(pairs, article_url):
    """Оставляем только внешние ссылки, похожие на сайты производителей и продуктов."""
    own = urlsplit(article_url).netloc.lower().removeprefix("www.")
    seen, result = set(), []
    for label, url in pairs:
        url = url.strip()
        parts = urlsplit(url)
        host = parts.netloc.lower().removeprefix("www.")
        if parts.scheme not in ("http", "https") or not host or host == own:
            continue
        if any(x in host + parts.path for x in SKIP_LINK_HOSTS):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append((label[:80], url))
    return result[:40]


def fetch_article(c):
    """Полный текст статьи, картинка и ссылки из неё. Если сайт не пускает — берём текст из RSS."""
    text, image, pairs = "", c["image"], list(c.get("links", []))
    try:
        page_bytes = http_get(c["link"]).content
        text = trafilatura.extract(page_bytes, include_comments=False, include_tables=False) or ""
        try:
            md = trafilatura.extract(page_bytes, output_format="markdown", include_links=True,
                                     include_comments=False, include_tables=False) or ""
            for label, href in re.findall(r"\[([^\]]{1,120})\]\((https?://[^)\s]+|/[^)\s]*)\)", md):
                pairs.append((label, urljoin(c["link"], href)))
        except Exception:
            pass
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
    return text[:12000], image, clean_links(pairs, c["link"])


POST_PROMPT = """Ты редактор русскоязычного Telegram-канала новостей о кинотехнике
и кинопроизводстве для профессионалов камерного цеха.

Напиши по статье ниже короткий новостной пост на русском языке.

Требования:
- "title": короткий цепкий заголовок, до 90 символов, без кавычек и точки в конце.
- "text": от 2 до 4 предложений. Только суть: что произошло или что анонсировано,
  ключевые характеристики, цена и дата выхода, если они есть в статье.
- Названия брендов, моделей и технологий оставляй в оригинале (ARRI Alexa 35, DaVinci Resolve).
- Нейтральный информационный тон, без восклицаний, эмодзи, хэштегов и адресов сайтов в тексте.
- Ничего не выдумывай: только факты из статьи.
- Это новость, а не реклама: никаких оценок вроде «лучший», «потрясающий», «must have»,
  никаких призывов купить, заказать или успеть, никаких магазинов и промокодов.
- "tag": ОДНА основная тема новости, строго одно слово из списка:
  камеры, оптика, свет, ПО, звук, мониторы, грип, стабилизаторы, дроны,
  аккумуляторы, накопители, аксессуары, сумки, выставки, опыт, индустрия.
  (ПО — программы, плагины, прошивки, видеоплатформы и сервисы;
  опыт — операторы и съёмочные группы делятся опытом, как снимали фильмы и сериалы;
  индустрия — бизнес-новости компаний, сделки, финансовые результаты.)
- "tag2": о чём конкретно, одно слово или название без пробелов:
  если новость о продукте или компании — бренд/компания (Sekonic, ARRI, Sony, Blackmagic, DJI);
  если это опыт съёмок — тип проекта: фильм, сериал, реклама, клип, документалка;
  если выставка — название выставки (NAB, IBC, CineGear, CPS);
  иначе — самое точное ключевое слово новости.
- "tag3": сфера, строго одно слово из списка: кино, видео, фото, звук, съемка.
  (съемка — если вещь универсальная и нужна сразу нескольким: фотографам, видеографам
  и кинооператорам, например экспонометр, штатив, свет, карты памяти.)

- "link": если новость о конкретном продукте (камера, объектив, свет, программа,
  обновление, прошивка, аксессуар и т.п.) — ссылка на страницу этого продукта
  на официальном сайте производителя или разработчика. Бери адрес ТОЛЬКО из списка
  ссылок ниже. Не магазины (B&H, Amazon, Adorama и т.п.), не соцсети, не другие новости.
  Если подходящей ссылки в списке нет или новость не о продукте — пустая строка.
- "link_text": название продукта, на которое вешается ссылка, — ДОСЛОВНО
  так, как оно написано в твоём "text" (например "DaVinci Resolve 20" или "Aputure STORM 1200x").
  Если "link" пустая — тоже пустая строка.

Верни JSON: {{"title": "...", "text": "...", "tag": "...", "tag2": "...", "tag3": "...", "link": "...", "link_text": "..."}}

Источник: {source}
Адрес статьи: {url}
Заголовок оригинала: {title}

Ссылки из статьи (текст ссылки — адрес):
{links}

Текст статьи:
{text}
"""


VERIFY_PROMPT = """Ты фактчекер новостного канала о кинотехнике.
Сверь пост с текстом статьи. Проверь КАЖДУЮ цифру, характеристику, название модели,
цену, валюту, дату, разрешение, вес, размер, имя и название компании.
Всё, что не подтверждается статьёй или искажено, исправь по статье, а если исправить
нельзя — убери из поста. Не добавляй новых фактов, не меняй стиль и длину без нужды.
{extra}
Верни JSON: {{"title": "...", "text": "...", "fixed": ["что исправлено", ...]}}

Пост:
Заголовок: {title}
Текст: {text}

Статья (заголовок оригинала: {orig}):
{article}
"""


class FactCheckError(RuntimeError):
    """Цифры в посте не подтверждаются статьёй — такой пост не публикуем."""


def numbers(s):
    """Все числа из текста в нескольких вариантах записи (24 000 / 24,000 / 2,7 / 2.7)."""
    out = []
    for m in re.finditer(r"\d(?:[\d.,]|[ \u00a0\u202f](?=\d{3}\b))*\d|\d", s):
        t = re.sub(r"[ \u00a0\u202f]", "", m.group())
        cands = set()
        for v in (t.replace(",", ""), t.replace(".", "").replace(",", "."), t.replace(",", "").replace(".", "")):
            try:
                f = float(v)
                cands.add(int(f) if f == int(f) else round(f, 4))
            except ValueError:
                pass
        if cands:
            out.append((m.group(), cands))
    return out


NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                "ten", "eleven", "twelve"]
NUMBER_WORDS_RU = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь",
                   "девять", "десять", "одиннадцать", "двенадцать"]


def unconfirmed_numbers(post, article):
    known = set()
    for _, c in numbers(article):
        known |= c
    low = article.lower()
    for n, (en, ru) in enumerate(zip(NUMBER_WORDS, NUMBER_WORDS_RU)):  # числа словами
        if re.search(rf"\b{en}\b", low) or ru in low:
            known.add(n)
    return [raw for raw, c in numbers(post) if not (c & known)]


def fact_check(title, body, article, orig):
    """Gemini сверяет пост со статьёй, затем код проверяет, что все цифры есть в статье."""
    source = f"{orig}\n{article}"
    extra = ""
    for attempt in range(2):
        try:
            r = gemini_json(VERIFY_PROMPT.format(title=title, text=body, orig=orig,
                                                 article=article, extra=extra))
            t2, b2 = (r.get("title") or "").strip(), (r.get("text") or "").strip()
            if t2 and b2:
                if r.get("fixed"):
                    log(f"[✓] Фактчек исправил: {r['fixed']}")
                title, body = t2, b2
        except Exception as e:
            log(f"[~] Фактчек Gemini не сработал ({e}), проверяю только цифры")
        bad = unconfirmed_numbers(f"{title}\n{body}", source)
        if not bad:
            return title, body
        log(f"[~] В статье нет цифр: {bad}")
        extra = ("ВНИМАНИЕ: этих чисел нет в статье, исправь или убери их: "
                 + ", ".join(bad) + ". Пиши числа так же, как в статье.")
    raise FactCheckError(f"цифры не подтверждаются статьёй: {bad}")


SPHERES = {"кино", "видео", "фото", "звук", "съемка"}

TAGS = {t.lower(): t for t in """камеры оптика свет ПО звук мониторы грип стабилизаторы дроны
аккумуляторы накопители аксессуары сумки выставки опыт индустрия""".split()}


def make_post(c, text, links):
    """Возвращает заголовок, текст с хэштегом и ссылку на продукт (или None)."""
    link_lines = "\n".join(f"- {label or '(без текста)'} — {url}" for label, url in links) or "(нет)"
    # сайт производителя тоже годится: сама статья может быть страницей продукта
    allowed = {url for _, url in links} | {c["link"]}
    result = gemini_json(POST_PROMPT.format(source=c["source"], url=c["link"], title=c["title"],
                                            links=link_lines, text=text))
    title = (result.get("title") or "").strip()
    body = (result.get("text") or "").strip()
    if not title or not body:
        raise RuntimeError("Gemini вернул пустой пост")
    title, body = fact_check(title, body, text, c["title"])
    tag = re.sub(r"[^\wА-Яа-яЁё]", "", (result.get("tag") or "").strip().lstrip("#"))
    if tag.lower() not in TAGS:
        tag = "индустрия"
    tag = TAGS[tag.lower()]
    tag2 = re.sub(r"[^\wА-Яа-яЁё]", "", (result.get("tag2") or "").strip().lstrip("#"))[:30]
    tag3 = re.sub(r"[^\wА-Яа-яЁё]", "", (result.get("tag3") or "").strip().lstrip("#")).lower().replace("ё", "е")
    if tag3 not in SPHERES:
        tag3 = "кино"
    tags = []
    for t in (tag, tag2, tag3):  # три хэштега без повторов
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    link = None
    url = (result.get("link") or "").strip()
    anchor = (result.get("link_text") or "").strip()
    if url in allowed and anchor and anchor in body:
        link = (anchor, url)
    elif url or anchor:
        log(f"[~] Ссылку не вставляю (нет в статье или в тексте): {anchor!r} {url}")
    return title, body + "\n\n" + " ".join(f"#{t}" for t in tags), link


# ---------- Шаг 5: Telegram ----------
def tg(method, payload):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data


def publish(title, body, image, link=None, source=None):
    main_text, _, tag = body.rpartition("\n\n#")
    if not main_text:
        main_text, tag = body, ""
    plain = f"{title}\n\n{main_text}\n\nИсточник\n\n#{tag}"
    text = html.escape(main_text)
    if link:
        anchor = html.escape(link[0])
        a = f'<a href="{html.escape(link[1], quote=True)}">{anchor}</a>'
        text = text.replace(anchor, a, 1)
    if source:  # слово «Источник» — ссылка на исходную статью
        text += f'\n\n<a href="{html.escape(source, quote=True)}">Источник</a>'
    if tag:
        text += f"\n\n#{html.escape(tag)}"
    message = f"<b>{html.escape(title)}</b>\n\n{text}"
    if image and len(plain) <= 1024:
        try:
            r = tg("sendPhoto", {"chat_id": CHANNEL, "photo": image,
                                 "caption": message, "parse_mode": "HTML"})
            return r["result"]["message_id"]
        except Exception as e:
            log(f"[!] Картинка не отправилась ({e}), публикую текстом")
    r = tg("sendMessage", {"chat_id": CHANNEL, "text": message, "parse_mode": "HTML",
                           "link_preview_options": {"is_disabled": True}})
    return r["result"]["message_id"]


# ---------- Шаг 6: копия поста во ВКонтакте ----------
def vk_api(method, **params):
    params.update(access_token=VK_TOKEN, v="5.199")
    data = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=60).json()
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"VK {method}: {e.get('error_code')} {e.get('error_msg')}")
    return data["response"]


def vk_photo(image):
    """Загружает картинку в группу, возвращает вложение photo... или None."""
    if not image:
        return None
    try:
        content = http_get(image).content
        # ключ сообщества не может загружать фото на стену напрямую,
        # поэтому грузим через сервер сообщений — такое фото тоже можно приложить к посту
        server = vk_api("photos.getMessagesUploadServer", peer_id=0)
        up = requests.post(server["upload_url"],
                           files={"photo": ("image.jpg", content, "image/jpeg")}, timeout=60).json()
        saved = vk_api("photos.saveMessagesPhoto", photo=up["photo"],
                       server=up["server"], hash=up["hash"])[0]
        att = f"photo{saved['owner_id']}_{saved['id']}"
        if saved.get("access_key"):
            att += f"_{saved['access_key']}"
        return att
    except Exception as e:
        log(f"[!] VK: картинка не загрузилась ({e})")
        return None


def publish_vk(title, body, image, link=None, article=None):
    """Дублирует пост в группу ВКонтакте. Ошибки ВК не мешают Telegram."""
    if not VK_TOKEN:
        return
    try:
        text, _, tag = body.rpartition("\n\n#")
        message = f"{title}\n\n{text or body}"
        # фото ключом сообщества не загрузить, а ссылку-вложение ВК без фото не принимает,
        # поэтому ставим ссылку в текст — ВК сам делает из неё превью с картинкой
        if link:
            message += f"\n\n{link[0]}: {link[1]}"
        if article:
            message += f"\n\nИсточник: {article}"
        if text:
            message += f"\n\n#{tag}"
        vk_api("wall.post", owner_id=f"-{VK_GROUP_ID}", from_group=1, message=message)
        log("[+] VK: опубликовано")
    except Exception as e:
        problem(f"ВКонтакте не принял пост «{title}»: {e}")


# ---------- Запуск ----------
def main():
    if not BOT_TOKEN or not GEMINI_KEY:
        log("Не заданы TELEGRAM_BOT_TOKEN и/или GEMINI_API_KEY")
        sys.exit(1)

    state = load_state()
    sync_state(state)
    candidates = collect_candidates(state)
    if not candidates:
        log("Новых статей нет")
        push_state(state)
        return

    try:
        selected = select_unique(candidates, state)
    except Exception as e:
        log(f"[!] Gemini недоступен, попробую в следующий запуск: {e}")
        state["health"]["gemini"] = state["health"].get("gemini", 0) + 1
        if state["health"]["gemini"] >= 3:
            problem(f"Gemini не отвечает {state['health']['gemini']} запусков подряд: {e}")
        push_state(state)
        return
    state["health"]["gemini"] = 0

    # вторая проверка дублей — в коде, по ключевым названиям
    known = posted_tokens(state)
    unique = []
    for i in selected:
        t = key_tokens(candidates[i]["title"])
        if is_duplicate(t, known):
            log(f"[=] Дубль, пропускаю: {candidates[i]['title']}")
            continue
        unique.append(i)
        known.append(t)
    selected = unique
    log(f"Новых статей: {len(candidates)}, после отбора и удаления дублей: {len(selected)}")

    to_post = selected[:MAX_POSTS_PER_RUN]
    postponed = set(selected[MAX_POSTS_PER_RUN:])  # опубликуем в следующих запусках

    # всё, что не выбрано (дубли, реклама) и не отложено, отмечаем как просмотренное
    for i, c in enumerate(candidates):
        if i not in to_post and i not in postponed:
            state["seen"][c["url"]] = NOW.isoformat()
    push_state(state)

    # распределяем посты по интервалу до следующего запуска (3 часа): каждому посту
    # своя часть интервала и случайное время внутри неё, чтобы посты выходили не ровно по часам
    start = time.time()
    slot = SPREAD_WINDOW_SEC / max(1, len(to_post))
    targets = [start + k * slot + random.uniform(0, slot * 0.7) for k in range(len(to_post))]
    log(f"Постов в этом запуске: {len(to_post)}, время выхода: "
        + ", ".join(datetime.fromtimestamp(t, timezone(timedelta(hours=3))).strftime("%H:%M")
                    for t in targets) + " (МСК)")

    for n, i in enumerate(to_post):
        c = candidates[i]
        try:
            text, image, links = fetch_article(c)
            title, body, link = make_post(c, text, links)
            # ждём своё случайное время, но не меньше 5 минут после предыдущего поста
            wait_left = max(targets[n], state.get("last_post_ts", 0) + POST_INTERVAL_SEC) - time.time()
            if wait_left > 0:
                log(f"[..] Жду {int(wait_left)} c до следующего поста")
                time.sleep(wait_left)
            # перед публикацией сверяемся со свежей памятью
            sync_state(state)
            t = key_tokens(c["title"])
            if c["url"] in state["seen"] or is_duplicate(t, posted_tokens(state)):
                log(f"[=] Уже опубликовано, пропускаю: {c['title']}")
                state["seen"][c["url"]] = NOW.isoformat()
                push_state(state)
                continue
            tg_id = publish(title, body, image, link, c["link"])
            publish_vk(title, body, image, link, c["link"])
            state["last_post_ts"] = time.time()
            log(f"[+] Опубликовано: {title}  ({c['source']})")
            state["posted"].append({"title": title, "text": body, "orig": c["title"], "image": image,
                                    "source": c["source"], "tg_id": tg_id,
                                    "ts": datetime.now(timezone.utc).isoformat()})
            state["seen"][c["url"]] = NOW.isoformat()
            note_domains(state, links, c["source"])
        except FactCheckError as e:
            log(f"[x] Не публикую, факты не сошлись: {c['title']} — {e}")
            state["seen"][c["url"]] = NOW.isoformat()
        except Exception as e:
            fails = state["fails"].get(c["url"], 0) + 1
            state["fails"][c["url"]] = fails
            log(f"[!] Ошибка со статьёй {c['link']} (попытка {fails} из {MAX_FAILS}): {e}")
            if fails >= MAX_FAILS:
                state["seen"][c["url"]] = NOW.isoformat()  # больше не пробуем
                problem(f"не удалось опубликовать статью после {MAX_FAILS} попыток: {c['title']} ({e})")
        push_state(state)


# ---------- Дайджест недели и опросы ----------
MSK = timezone(timedelta(hours=3))
POLL_EVERY_SEC = 44 * 3600  # примерно раз в два дня

DIGEST_PROMPT = """Ты редактор канала новостей о кинотехнике для камерного цеха.
Ниже новости за неделю. Выбери до 7 самых важных и интересных для операторов,
механиков и гафферов (новые камеры, оптика, свет, крупные обновления ПО, выставки).
Не бери почти одинаковые новости дважды. Верни JSON: {{"ids": [номера по важности]}}

{items}
"""

POLL_PROMPT = """Ты редактор канала новостей о кинотехнике для камерного цеха.
Ниже свежие новости. Выбери ОДНУ, по которой профессионалам интересно проголосовать
(новая камера, объектив, свет, обновление, тренд), и придумай короткий опрос на русском:
вопрос до 200 символов и 2–4 варианта ответа до 60 символов. Нейтрально, без рекламы
и призывов купить. Если ни одна новость не подходит — верни {{"skip": true}}.
Верни JSON: {{"id": номер, "question": "...", "options": ["...", "..."]}}

{items}
"""


def channel_link(msg_id):
    return f"https://t.me/{CHANNEL.lstrip('@')}/{msg_id}" if msg_id else None


def maybe_digest(state):
    """По воскресеньям вечером — «Главное за неделю» в Telegram и ВКонтакте."""
    now = datetime.now(MSK)
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if now.weekday() != 6 or now.hour < 18 or state.get("digest_week") == week:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    posts = [p for p in state["posted"] if p["ts"] >= since]
    if len(posts) < 3:
        return
    items = "\n".join(f"{i}. {p['title']}" for i, p in enumerate(posts))
    try:
        ids = [int(i) for i in gemini_json(DIGEST_PROMPT.format(items=items)).get("ids", [])]
        chosen = [posts[i] for i in dict.fromkeys(ids) if 0 <= i < len(posts)][:7]
    except Exception as e:
        log(f"[~] Дайджест: Gemini не ответил ({e}), беру последние новости")
        chosen = []
    chosen = chosen or posts[-7:]
    lines_tg, lines_vk = [], []
    for p in chosen:
        url = channel_link(p.get("tg_id"))
        t = html.escape(p["title"])
        lines_tg.append(f"• <a href=\"{url}\">{t}</a>" if url else f"• {t}")
        lines_vk.append(f"• {p['title']}")
    tg("sendMessage", {"chat_id": CHANNEL, "parse_mode": "HTML",
                       "text": "<b>Главное за неделю</b>\n\n" + "\n".join(lines_tg) + "\n\n#дайджест",
                       "link_preview_options": {"is_disabled": True}})
    if VK_TOKEN:
        try:
            vk_api("wall.post", owner_id=f"-{VK_GROUP_ID}", from_group=1,
                   message="Главное за неделю\n\n" + "\n".join(lines_vk) + "\n\n#дайджест")
        except Exception as e:
            problem(f"ВКонтакте не принял дайджест: {e}")
    state["digest_week"] = week
    log(f"[+] Дайджест недели опубликован ({len(chosen)} новостей)")


def maybe_poll(state):
    """Примерно раз в два дня — опрос в Telegram по свежей новости (ответом на пост)."""
    if time.time() - state.get("last_poll_ts", 0) < POLL_EVERY_SEC:
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    posts = [p for p in state["posted"] if p["ts"] >= since and p.get("tg_id")]
    if not posts:
        return
    items = "\n".join(f"{i}. {p['title']} — {p['text'][:300]}" for i, p in enumerate(posts))
    r = gemini_json(POLL_PROMPT.format(items=items))
    if r.get("skip"):
        log("[~] Опрос: подходящей новости нет")
        return
    i = int(r.get("id", -1))
    q = (r.get("question") or "").strip()[:300]
    opts = [str(o).strip()[:100] for o in r.get("options", []) if str(o).strip()][:4]
    if not (0 <= i < len(posts)) or not q or len(opts) < 2:
        log(f"[~] Опрос: Gemini вернул неподходящий ответ {r}")
        return
    tg("sendPoll", {"chat_id": CHANNEL, "question": q, "is_anonymous": True,
                    "options": [{"text": o} for o in opts],
                    "reply_parameters": {"message_id": posts[i]["tg_id"]}})
    state["last_poll_ts"] = time.time()
    log(f"[+] Опрос опубликован: {q}")


# ---------- Поиск новых источников (раз в месяц) ----------
SHOP_HOSTS = ("bhphotovideo.", "amazon.", "adorama.", "ebay.", "aliexpress.", "walmart.",
              "bestbuy.", "kickstarter.", "indiegogo.", "vimeo.", "apple.com", "github.")

DISCOVER_PROMPT = """Ты редактор канала новостей о кинотехнике для камерного цеха
(камеры, оптика, свет, грип, звук, мониторы, аксессуары, ПО для видео, выставки).
Ниже сайты-кандидаты в источники новостей: адрес, название ленты и свежие заголовки.
Для каждого оцени: подходит ли тематика и насколько источник достоверный
(официальный производитель, известное отраслевое издание, выставка — достоверно;
перепечатки, агрегаторы, магазины, личные блоги без экспертизы — нет).
Верни JSON: {{"items": [{{"domain": "...", "relevant": true/false,
"kind": "производитель/издание/выставка/блог/магазин/другое", "reliable": true/false,
"note": "одна фраза по-русски"}}]}}

{items}
"""


def domain_of(url):
    return urlsplit(url).netloc.lower().removeprefix("www.")


def note_domains(state, links, source):
    """Запоминаем, на какие сайты ссылаются статьи наших источников."""
    d = state.setdefault("link_domains", {})
    for _, url in links:
        host = domain_of(url)
        if not host or any(x in host for x in SHOP_HOSTS):
            continue
        rec = d.setdefault(host, {"n": 0, "src": []})
        rec["n"] += 1
        if source not in rec["src"]:
            rec["src"].append(source)
    if len(d) > 300:  # не даём файлу памяти разрастаться
        keep = sorted(d, key=lambda h: d[h]["n"], reverse=True)[:200]
        state["link_domains"] = {h: d[h] for h in keep}


def find_feed(host):
    """Ищем RSS/Atom-ленту сайта. Возвращаем (адрес, лента) или (None, None)."""
    cands = []
    for base in (f"https://{host}", f"https://www.{host}"):
        try:
            page = http_get(base, timeout=20).text
            for m in re.finditer(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', page):
                h = re.search(r'href=["\']([^"\']+)', m.group())
                if h:
                    cands.append(urljoin(base, html.unescape(h.group(1))))
            cands += [base + "/feed/", base + "/rss", base + "/feed.xml", base + "/news/feed/"]
            break
        except Exception:
            continue
    for u in dict.fromkeys(cands):
        try:
            feed = feedparser.parse(http_get(u, timeout=20).content)
            if feed.entries:
                return u, feed
        except Exception:
            continue
    return None, None


def maybe_discover(state):
    """Раз в месяц ищем новые источники и присылаем список на почту (через задачу на GitHub)."""
    month = datetime.now(MSK).strftime("%Y-%m")
    if state.get("discover_month") == month:
        return
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    have = {domain_of(u) for u in read_sources()}
    stats = state.get("link_domains", {})
    hosts = [h for h, r in sorted(stats.items(), key=lambda kv: kv[1]["n"], reverse=True)
             if h not in have and (len(r["src"]) >= 2 or r["n"] >= 3)][:15]
    state["discover_month"] = month
    if not hosts:
        log("[~] Поиск источников: кандидатов пока нет")
        return
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    found = []
    for h in hosts:
        url, feed = find_feed(h)
        if not url:
            continue
        recent = sum(1 for e in feed.entries if (entry_time(e) or month_ago) > month_ago)
        titles = [strip_html(e.get("title", "")) for e in feed.entries[:5]]
        found.append({"domain": h, "feed": url, "title": feed.feed.get("title", h),
                      "recent": recent, "titles": titles, "refs": stats[h]})
    if not found:
        log("[~] Поиск источников: у кандидатов нет RSS-лент")
        return
    items = "\n\n".join(f"{f['domain']} — {f['title']}\n" + "\n".join(f"  - {t}" for t in f["titles"])
                         for f in found)
    try:
        verdict = {x.get("domain"): x for x in gemini_json(DISCOVER_PROMPT.format(items=items)).get("items", [])}
    except Exception as e:
        log(f"[~] Поиск источников: Gemini не ответил ({e})")
        verdict = {}
    owner = repo.split("/")[0]
    good, other = [], []
    for f in found:
        v = verdict.get(f["domain"], {})
        line = (f"- **{f['domain']}** — {f['title']}\n  лента: {f['feed']}\n"
                f"  постов за 30 дней: {f['recent']}; на сайт ссылались {f['refs']['n']} раз(а) "
                f"из: {', '.join(f['refs']['src'][:4])}\n"
                f"  оценка: {v.get('kind', '?')}, {'достоверный' if v.get('reliable') else 'достоверность под вопросом'}"
                f" — {v.get('note', '')}")
        (good if v.get("relevant") and v.get("reliable") else other).append(line)
    body = (f"@{owner}, бот нашёл кандидатов в новые источники за {month}.\n"
            "Ничего не добавлено автоматически. Напишите Claude, какие подключить.\n\n"
            "### Подходят по теме и выглядят достоверными\n" + ("\n".join(good) or "— нет") +
            "\n\n### Остальные (сомнительные или не по теме)\n" + ("\n".join(other) or "— нет"))
    r = requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"title": f"Кандидаты в источники — {month}", "body": body})
    if r.status_code >= 300:
        raise RuntimeError(f"не удалось создать задачу на GitHub: {r.status_code} {r.text[:200]}")
    log(f"[+] Поиск источников: отправлен список из {len(found)} кандидатов")


def extras():
    """Дайджест и опросы: запускаются после основной работы бота."""
    state = load_state()
    sync_state(state)
    for job in (maybe_digest, maybe_poll, maybe_discover):
        try:
            job(state)
        except Exception as e:
            problem(f"{job.__name__}: {e}")
    push_state(state)


def find_article(orig):
    """Ищет статью в лентах по оригинальному заголовку."""
    for feed_url in read_sources():
        try:
            feed = feedparser.parse(http_get(feed_url).content)
        except Exception:
            continue
        for entry in feed.entries:
            if strip_html(entry.get("title", "")) != orig or not entry.get("link"):
                continue
            raw_html = (entry.content[0].get("value", "") if entry.get("content") else "") \
                + entry.get("summary", "")
            return {"link": entry.link, "title": orig, "image": entry_image(entry),
                    "source": feed.feed.get("title") or urlsplit(feed_url).netloc,
                    "rss_text": strip_html(raw_html), "links": html_links(raw_html, entry.link)}
    return None


def vk_test():
    """Проверка ВКонтакте: заново собирает последний пост канала и публикует его в группе."""
    state = load_state()
    last = state["posted"][-1] if state["posted"] else {"title": "Проверка", "text": "Тестовый пост."}
    c = find_article(last.get("orig", ""))
    if not c:
        log(f"VK-тест: статья не найдена в лентах, публикую как есть: {last['title']}")
        publish_vk(last["title"], last["text"], None)
        return
    text, image, links = fetch_article(c)
    title, body, link = make_post(c, text, links)
    log(f"VK-тест: {title} | ссылка на продукт: {link}")
    publish_vk(title, body, image, link, c["link"])


if __name__ == "__main__":
    if "--vk-test" in sys.argv:
        vk_test()
    else:
        main()
        extras()
    if PROBLEMS:
        log("\n===== ИТОГ: есть проблемы =====")
        for x in PROBLEMS:
            log(" - " + x)
        sys.exit(1)  # GitHub пометит запуск как неудачный и пришлёт письмо
