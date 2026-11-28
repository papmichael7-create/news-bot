"""Сайт-архив новостей для GitHub Pages (папка docs/).

Бот вызывает build_site(posted) перед каждым сохранением памяти: новые посты
добавляются в docs/archive.json, а страницы пересобираются заново. Страницы —
обычный HTML без скриптов, чтобы их индексировали Яндекс и Google.
"""
import hashlib
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

SITE_URL = os.environ.get("SITE_URL", "https://papmichael7-create.github.io/news-bot").rstrip("/")
DOCS = "docs"
ARCHIVE = os.path.join(DOCS, "archive.json")
MAX_ITEMS = 3000
SITE_VERSION = "3"  # при изменении дизайна сайт пересоберётся сам
INDEXNOW_KEY = "1ac7f3b9e2d84c6a9f0b5e1d3c2a4f60"  # открытый ключ IndexNow (лежит на сайте файлом)
MSK = timezone(timedelta(hours=3))
TG = "https://t.me/newsfrom1AC"
VK = "https://vk.com/newsfrom1ac"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

CSS = """
:root{--bg:#F1F0EB;--fg:#141416;--muted:#55554f;--meta:#77766f;--line:#d8d6cd;--tape:#141416;--tapefg:#F4F3EE;--ph:#e4e2da}
[data-theme=dark]{--bg:#141416;--fg:#ECEBE6;--muted:#b3b2aa;--meta:#8f8e86;--line:#2e2e33;--tape:#F1F0EB;--tapefg:#141416;--ph:#232327}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141416;--fg:#ECEBE6;--muted:#b3b2aa;
--meta:#8f8e86;--line:#2e2e33;--tape:#F1F0EB;--tapefg:#141416;--ph:#232327}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);transition:background .2s,color .2s;
font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
a{color:var(--fg)}.wrap{max-width:760px;margin:0 auto;padding:0 18px}
header{padding:34px 0 18px;position:relative}.tape{display:inline-flex;align-items:center;gap:16px;background:var(--tape);
color:var(--tapefg);padding:14px 30px 14px 24px;transform:rotate(-2deg);text-decoration:none;
clip-path:polygon(0 0,100% 0,98.5% 20%,100% 40%,98.5% 60%,100% 80%,98.5% 100%,0 100%,1.5% 80%,0 60%,1.5% 40%,0 20%)}
.dot{width:22px;height:22px;border-radius:50%;background:#E5392B;flex:none}
.tape small{display:block;font:700 12px "Courier New",monospace;letter-spacing:5px}
.tape b{display:block;font:900 40px/1 "Arial Black",Arial,sans-serif}
.theme{position:absolute;top:34px;right:0;border:1px solid var(--line);background:none;color:var(--fg);
border-radius:20px;padding:6px 12px;font-size:16px;cursor:pointer}
.sub{margin:22px 0 0;color:var(--muted);font-size:15px}.nav{margin:10px 0 0;font-size:15px}
.nav a{margin-right:14px}h1.page{font-size:22px;margin:26px 0 6px}
article{border-top:1px solid var(--line);padding:22px 0}article img{width:100%;max-height:420px;
object-fit:cover;border-radius:6px;background:var(--ph)}article h2{font-size:21px;line-height:1.3;margin:12px 0 8px}
.meta{font:13px "Courier New",monospace;color:var(--meta)}.tags a{font:14px "Courier New",monospace;
margin-right:10px;text-decoration:none;color:var(--muted)}.tags a:hover{color:#E5392B}
article h2 a{text-decoration:none}article h2 a:hover{text-decoration:underline}
.src{font-size:14px}footer{border-top:1px solid var(--line);margin:30px 0;padding:18px 0;color:var(--meta);font-size:13px}
"""


def _load():
    try:
        with open(ARCHIVE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _key(p):
    return f"{p.get('orig') or p.get('title')}|{p.get('source')}"


def _split(text):
    """Текст поста и список хэштегов (они в последней строке)."""
    body, _, tags = (text or "").rpartition("\n\n#")
    if not body:
        return text or "", []
    return body, [t for t in ("#" + tags).replace("#", " ").split() if t]


def _tag_url(tag, depth=""):
    return f"{depth}tag/{quote(tag.lower())}.html"


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                     "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _slug(p):
    """Постоянный адрес новости: дата + заголовок латиницей."""
    t = "".join(TRANSLIT.get(ch, ch) for ch in p["title"].lower())
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:70].rstrip("-")
    h = hashlib.md5(_key(p).encode()).hexdigest()[:5]
    return f"{p['ts'][:10]}-{t}-{h}"


def _news_url(p, depth=""):
    return f"{depth}n/{p['slug']}.html"


def _date(ts):
    d = datetime.fromisoformat(ts).astimezone(MSK)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}, {d:%H:%M}"


def _article(p, depth, full=False):
    body, tags = _split(p.get("text"))
    text = html.escape(body).replace("\n", "<br>")
    pl = p.get("plink")
    if pl and pl[0] and html.escape(pl[0]) in text:
        a = html.escape(pl[0])
        text = text.replace(a, f'<a href="{html.escape(pl[1], quote=True)}" rel="nofollow noopener">{a}</a>', 1)
    img = (f'<img src="{html.escape(p["image"], quote=True)}" alt="{html.escape(p["title"], quote=True)}" '
           f'loading="lazy" referrerpolicy="no-referrer">') if p.get("image") else ""
    src = (f' · <a class="src" href="{html.escape(p["url"], quote=True)}" rel="nofollow noopener">Источник: '
           f'{html.escape(p.get("source", ""))}</a>') if p.get("url") else ""
    tg = f' · <a class="src" href="{TG}/{p["tg_id"]}">в Telegram</a>' if p.get("tg_id") else ""
    tag_links = " ".join(f'<a href="{_tag_url(t, depth)}">#{html.escape(t)}</a>' for t in tags)
    head = (f'<h1>{html.escape(p["title"])}</h1>' if full else
            f'<h2><a href="{_news_url(p, depth)}">{html.escape(p["title"])}</a></h2>')
    return (f'<article>{img}{head}<p>{text}</p>'
            f'<div class="meta">{_date(p["ts"])}{src}{tg}</div><div class="tags">{tag_links}</div></article>')


def _page(title, desc, items, depth="", heading="", url="", extra_head="", full=False):
    arts = "\n".join(_article(p, depth, full) for p in items) or "<p>Пока пусто.</p>"
    canon = f'<link rel="canonical" href="{html.escape(url, quote=True)}">\n' if url else ""
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(desc, quote=True)}">
<meta property="og:title" content="{html.escape(title, quote=True)}">
<meta property="og:description" content="{html.escape(desc, quote=True)}">
<meta property="og:type" content="{'article' if full else 'website'}">
{canon}{extra_head}<link rel="alternate" type="application/rss+xml" title="News from 1AC" href="{SITE_URL}/feed.xml">
<style>{CSS}</style>
<script>try{{var t=localStorage.getItem("theme");if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script>
</head><body><div class="wrap">
<header><button class="theme" type="button" aria-label="Дневной или ночной режим" onclick="toggleTheme()">☾ / ☀</button>
<a class="tape" href="{depth}index.html"><span class="dot"></span><span><small>NEWS FROM</small><b>1AC</b></span></a>
<p class="sub">Новости кинотехники для камерного цеха: камеры, оптика, свет, звук, грип, выставки и опыт операторов.</p>
<p class="nav"><a href="{TG}">Telegram</a><a href="{VK}">ВКонтакте</a><a href="{depth}tags.html">Все теги</a></p></header>
{f'<h1 class="page">{html.escape(heading)}</h1>' if heading else ''}
{arts}
<footer>Новости собираются и переводятся автоматически — сверяйте детали с источником.
Права на изображения принадлежат их владельцам; по вопросам удаления пишите в <a href="{TG}">Telegram-канал</a>.</footer>
</div>
<script>function toggleTheme(){{var d=document.documentElement,cur=d.dataset.theme||
(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"),n=cur==="dark"?"light":"dark";
d.dataset.theme=n;try{{localStorage.setItem("theme",n)}}catch(e){{}}}}</script>
</body></html>"""


def _write(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _news_page(p):
    body = _split(p.get("text"))[0]
    desc = re.sub(r"\s+", " ", body)[:200]
    url = f"{SITE_URL}/{_news_url(p)}"
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": p["title"][:110],
          "description": desc, "datePublished": p["ts"], "dateModified": p["ts"], "inLanguage": "ru",
          "mainEntityOfPage": url, "author": {"@type": "Organization", "name": "News from 1AC", "url": SITE_URL},
          "publisher": {"@type": "Organization", "name": "News from 1AC", "url": SITE_URL}}
    if p.get("image"):
        ld["image"] = [p["image"]]
    extra = f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>\n'
    if p.get("image"):
        extra += (f'<meta property="og:image" content="{html.escape(p["image"], quote=True)}">\n'
                  '<meta name="twitter:card" content="summary_large_image">\n')
    extra += f'<meta property="article:published_time" content="{p["ts"]}">\n'
    page = _page(f"{p['title']} | News from 1AC", desc, [p], depth="../", url=url, extra_head=extra, full=True)
    more = f'<p><a href="../index.html">← Все новости</a></p>'
    return page.replace("<footer>", more + "\n<footer>", 1)


def indexnow(urls):
    """Сообщаем Яндексу и Bing о новых страницах (IndexNow)."""
    if not urls:
        return
    host = SITE_URL.split("/")[2]
    try:
        r = requests.post("https://yandex.com/indexnow", timeout=30,
                          json={"host": host, "key": INDEXNOW_KEY,
                                "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt", "urlList": urls[:100]})
        r2 = requests.post("https://api.indexnow.org/indexnow", timeout=30,
                           json={"host": host, "key": INDEXNOW_KEY,
                                 "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt", "urlList": urls[:100]})
        print(f"[+] IndexNow: {len(urls)} страниц, Яндекс {r.status_code}, Bing {r2.status_code}", flush=True)
    except Exception as e:
        print(f"[~] IndexNow не ответил: {e}", flush=True)


def build_site(posted):
    """Добавляет новые посты в архив и пересобирает сайт.
    Возвращает список адресов новых страниц (или [SITE_URL], если сайт пересобран без новых постов),
    пустой список — если ничего не изменилось."""
    archive = _load()
    known = {_key(p) for p in archive}
    added = 0
    for p in posted:
        if p.get("title") and p.get("ts") and _key(p) not in known:
            archive.append({k: p.get(k) for k in
                            ("title", "text", "orig", "source", "ts", "image", "url", "plink", "tg_id")})
            known.add(_key(p))
            added += 1
    new_urls = []
    for p in archive:
        if not p.get("slug"):
            p["slug"] = _slug(p)
            new_urls.append(f"{SITE_URL}/{_news_url(p)}")
    ver_file = os.path.join(DOCS, ".version")
    try:
        same_ver = open(ver_file, encoding="utf-8").read().strip() == SITE_VERSION
    except Exception:
        same_ver = False
    if not added and same_ver and os.path.exists(os.path.join(DOCS, "index.html")):
        return []
    archive.sort(key=lambda p: p["ts"], reverse=True)
    archive = archive[:MAX_ITEMS]
    _write(ARCHIVE, json.dumps(archive, ensure_ascii=False, indent=1))

    desc = "Свежие новости кинотехники на русском: камеры, объективы, свет, звук, выставки NAB, IBC, CPS."
    _write(os.path.join(DOCS, "index.html"),
           _page("News from 1AC — новости кинотехники", desc, archive[:60], url=f"{SITE_URL}/"))
    for p in archive:  # отдельная страница для каждой новости
        _write(os.path.join(DOCS, "n", f"{p['slug']}.html"), _news_page(p))

    by_tag = {}
    for p in archive:
        for t in _split(p.get("text"))[1]:
            by_tag.setdefault(t.lower(), (t, []))[1].append(p)
    for low, (t, items) in by_tag.items():
        _write(os.path.join(DOCS, "tag", f"{low}.html"),
               _page(f"#{t} — новости кинотехники | News from 1AC",
                     f"Все новости по теме #{t}: камеры, оптика, свет и другое оборудование для съёмок.",
                     items[:200], depth="../", heading=f"#{t}", url=f"{SITE_URL}/{_tag_url(t)}"))
    tag_list = " ".join(f'<a href="{_tag_url(t)}">#{html.escape(t)}</a>&nbsp;({len(items)})'
                        for t, items in sorted(by_tag.values(), key=lambda x: -len(x[1])))
    _write(os.path.join(DOCS, "tags.html"),
           _page("Все теги | News from 1AC", "Темы, бренды и сферы новостей кинотехники.", [],
                 heading="Все теги").replace("<p>Пока пусто.</p>", f'<p class="tags">{tag_list}</p>'))

    # RSS, карта сайта и служебные файлы
    rss_items = "".join(
        f"<item><title>{html.escape(p['title'])}</title><link>{SITE_URL}/{_news_url(p)}</link>"
        f"<guid isPermaLink=\"false\">{html.escape(_key(p))}</guid>"
        f"<pubDate>{datetime.fromisoformat(p['ts']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{html.escape(_split(p.get('text'))[0])}</description></item>"
        for p in archive[:50])
    _write(os.path.join(DOCS, "feed.xml"),
           f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>News from 1AC</title>'
           f"<link>{SITE_URL}/</link><description>{desc}</description><language>ru</language>{rss_items}</channel></rss>")
    urls = [(f"{SITE_URL}/", archive[0]["ts"][:10] if archive else ""), (f"{SITE_URL}/tags.html", "")]
    urls += [(f"{SITE_URL}/{_tag_url(t)}", items[0]["ts"][:10]) for t, items in by_tag.values()]
    urls += [(f"{SITE_URL}/{_news_url(p)}", p["ts"][:10]) for p in archive]
    _write(os.path.join(DOCS, "sitemap.xml"),
           '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f"<url><loc>{u}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>"
                     for u, d in urls) + "</urlset>")
    _write(os.path.join(DOCS, f"{INDEXNOW_KEY}.txt"), INDEXNOW_KEY)
    _write(os.path.join(DOCS, "robots.txt"), f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
    _write(os.path.join(DOCS, ".nojekyll"), "")
    _write(ver_file, SITE_VERSION)
    return new_urls or [f"{SITE_URL}/"]
