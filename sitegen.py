"""Сайт-архив новостей для GitHub Pages (папка docs/).

Бот вызывает build_site(posted) перед каждым сохранением памяти: новые посты
добавляются в docs/archive.json, а страницы пересобираются заново. Страницы —
обычный HTML без скриптов, чтобы их индексировали Яндекс и Google.
"""
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

SITE_URL = os.environ.get("SITE_URL", "https://papmichael7-create.github.io/news-bot").rstrip("/")
DOCS = "docs"
ARCHIVE = os.path.join(DOCS, "archive.json")
MAX_ITEMS = 3000
MSK = timezone(timedelta(hours=3))
TG = "https://t.me/newsfrom1AC"
VK = "https://vk.com/newsfrom1ac"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

CSS = """
*{box-sizing:border-box}body{margin:0;background:#F1F0EB;color:#141416;
font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
a{color:#141416}.wrap{max-width:760px;margin:0 auto;padding:0 18px}
header{padding:34px 0 18px}.tape{display:inline-flex;align-items:center;gap:16px;background:#141416;
color:#F4F3EE;padding:14px 30px 14px 24px;transform:rotate(-2deg);text-decoration:none;
clip-path:polygon(0 0,100% 0,98.5% 20%,100% 40%,98.5% 60%,100% 80%,98.5% 100%,0 100%,1.5% 80%,0 60%,1.5% 40%,0 20%)}
.dot{width:22px;height:22px;border-radius:50%;background:#E5392B;flex:none}
.tape small{display:block;font:700 12px "Courier New",monospace;letter-spacing:5px}
.tape b{display:block;font:900 40px/1 "Arial Black",Arial,sans-serif}
.sub{margin:22px 0 0;color:#55554f;font-size:15px}.nav{margin:10px 0 0;font-size:15px}
.nav a{margin-right:14px}h1.page{font-size:22px;margin:26px 0 6px}
article{border-top:1px solid #d8d6cd;padding:22px 0}article img{width:100%;max-height:420px;
object-fit:cover;border-radius:6px;background:#e4e2da}article h2{font-size:21px;line-height:1.3;margin:12px 0 8px}
.meta{font:13px "Courier New",monospace;color:#77766f}.tags a{font:14px "Courier New",monospace;
margin-right:10px;text-decoration:none;color:#55554f}.tags a:hover{color:#E5392B}
.src{font-size:14px}footer{border-top:1px solid #d8d6cd;margin:30px 0;padding:18px 0;color:#77766f;font-size:13px}
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


def _date(ts):
    d = datetime.fromisoformat(ts).astimezone(MSK)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}, {d:%H:%M}"


def _article(p, depth):
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
    return (f'<article>{img}<h2>{html.escape(p["title"])}</h2><p>{text}</p>'
            f'<div class="meta">{_date(p["ts"])}{src}{tg}</div><div class="tags">{tag_links}</div></article>')


def _page(title, desc, items, depth="", heading=""):
    arts = "\n".join(_article(p, depth) for p in items) or "<p>Пока пусто.</p>"
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(desc, quote=True)}">
<meta property="og:title" content="{html.escape(title, quote=True)}">
<meta property="og:description" content="{html.escape(desc, quote=True)}">
<meta property="og:type" content="website">
<link rel="alternate" type="application/rss+xml" title="News from 1AC" href="{SITE_URL}/feed.xml">
<style>{CSS}</style></head><body><div class="wrap">
<header><a class="tape" href="{depth}index.html"><span class="dot"></span><span><small>NEWS FROM</small><b>1AC</b></span></a>
<p class="sub">Новости кинотехники для камерного цеха: камеры, оптика, свет, звук, грип, выставки и опыт операторов.</p>
<p class="nav"><a href="{TG}">Telegram</a><a href="{VK}">ВКонтакте</a><a href="{depth}tags.html">Все теги</a></p></header>
{f'<h1 class="page">{html.escape(heading)}</h1>' if heading else ''}
{arts}
<footer>Новости собираются и переводятся автоматически — сверяйте детали с источником.
Права на изображения принадлежат их владельцам; по вопросам удаления пишите в <a href="{TG}">Telegram-канал</a>.</footer>
</div></body></html>"""


def _write(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def build_site(posted):
    """Добавляет новые посты в архив и пересобирает сайт. Возвращает True, если что-то изменилось."""
    archive = _load()
    known = {_key(p) for p in archive}
    added = 0
    for p in posted:
        if p.get("title") and p.get("ts") and _key(p) not in known:
            archive.append({k: p.get(k) for k in
                            ("title", "text", "orig", "source", "ts", "image", "url", "plink", "tg_id")})
            known.add(_key(p))
            added += 1
    if not added and os.path.exists(os.path.join(DOCS, "index.html")):
        return False
    archive.sort(key=lambda p: p["ts"], reverse=True)
    archive = archive[:MAX_ITEMS]
    _write(ARCHIVE, json.dumps(archive, ensure_ascii=False, indent=1))

    desc = "Свежие новости кинотехники на русском: камеры, объективы, свет, звук, выставки NAB, IBC, CPS."
    _write(os.path.join(DOCS, "index.html"),
           _page("News from 1AC — новости кинотехники", desc, archive[:60]))

    by_tag = {}
    for p in archive:
        for t in _split(p.get("text"))[1]:
            by_tag.setdefault(t.lower(), (t, []))[1].append(p)
    for low, (t, items) in by_tag.items():
        _write(os.path.join(DOCS, "tag", f"{low}.html"),
               _page(f"#{t} — новости кинотехники | News from 1AC",
                     f"Все новости по теме #{t}: камеры, оптика, свет и другое оборудование для съёмок.",
                     items[:200], depth="../", heading=f"#{t}"))
    tag_list = " ".join(f'<a href="{_tag_url(t)}">#{html.escape(t)}</a>&nbsp;({len(items)})'
                        for t, items in sorted(by_tag.values(), key=lambda x: -len(x[1])))
    _write(os.path.join(DOCS, "tags.html"),
           _page("Все теги | News from 1AC", "Темы, бренды и сферы новостей кинотехники.", [],
                 heading="Все теги").replace("<p>Пока пусто.</p>", f'<p class="tags">{tag_list}</p>'))

    # RSS, карта сайта и служебные файлы
    rss_items = "".join(
        f"<item><title>{html.escape(p['title'])}</title><link>{SITE_URL}/</link>"
        f"<guid isPermaLink=\"false\">{html.escape(_key(p))}</guid>"
        f"<pubDate>{datetime.fromisoformat(p['ts']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{html.escape(_split(p.get('text'))[0])}</description></item>"
        for p in archive[:50])
    _write(os.path.join(DOCS, "feed.xml"),
           f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>News from 1AC</title>'
           f"<link>{SITE_URL}/</link><description>{desc}</description><language>ru</language>{rss_items}</channel></rss>")
    urls = [f"{SITE_URL}/", f"{SITE_URL}/tags.html"] + [f"{SITE_URL}/{_tag_url(t)}" for t, _ in by_tag.values()]
    _write(os.path.join(DOCS, "sitemap.xml"),
           '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")
    _write(os.path.join(DOCS, "robots.txt"), f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
    _write(os.path.join(DOCS, ".nojekyll"), "")
    return True
