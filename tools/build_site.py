#!/usr/bin/env python3
"""Сборка статического сайта «Архив Эдуарда Жопова».

    python3 tools/parse_export.py      # экспорт Telegram -> data/posts.json
    python3 tools/build_site.py        # data/posts.json + content/ -> docs/

Фотографии берутся из папки экспорта («Эдуард Жопов/photos/...»), если она есть.
Требуются jinja2, Pillow и beautifulsoup4.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import shutil
import sys
import tomllib
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
              "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[  -]?){3}\d{4}(?!\d)")
PHONE_RE = re.compile(r"\+7[  (-]*\d{3}[  )-]*\d{3}[  -]*\d{2}[  -]*\d{2}")
REDACTED = '<span class="redacted" title="Реквизиты для пожертвований в архив не включены">изъято</span>'
DONATION_LINE_RE = re.compile(r"(Сбер|помо(чь|щь) проекту|Помощь проекту|можете оказать|донат|пожертвова)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
# фразы о переводах денег; вырезаются только из записей, перечисленных в cleanup.strip
DONATE_RE = re.compile(
    r"(\bСбер\b|\+7[ (]*9\d\d|(?:\d{4}[ -]?){3}\d{4}|Помощь проекту|Поддержать проект|Благотворительность|Получатель"
    r"|можете (оказать )?помо|помочь материально|Жертвовать|поддержать рубл|Реквизиты|за помощь|Благодарим (наших |дорогих )?подписчиков"
    r"|\d+ тысяч\w* рублей на|часть денег|мини-тираж|скинув нам)", re.I)
# признаки связи записи с Мэтром и его окружением (для пересланных записей)
RELATED_RE = re.compile(
    r"(Жопов|Мэтр|Глыб|Эдуард\w* Борисович|Женсонесс|Ягодичкин|Тошницк|Сосиписос|Кусь|Хабаровск|херес|64 тысяч"
    r"|бомжар|ельцинск|ноги отчима|Мразь|Чернухин)", re.I)
# подписи, которые без реквизитов теряют смысл; убираются из всех записей
LABEL_ONLY_RE = re.compile(r"^\s*(Помощь проекту|Поддержать проект|Благотворительность|Получатель)\b\s*:?.{0,60}$", re.I)
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B05-\u2B55\u23CF\u23E9-\u23FA\u2190-\u21FF"
    "\uFE0F\u200D\u20E3\U000E0020-\U000E007F]+")


def strip_emoji(s: str) -> str:
    s = EMOJI_RE.sub("", s)
    s = re.sub(r"[ \u00a0]+([!?.,:;)»])", r"\1", s)
    s = re.sub(r"([«(])[ \u00a0]+", r"\1", s)
    return re.sub(r"[ \u00a0]{2,}", " ", s)


SENT_SPLIT = re.compile(r"(?<=[.!?…»)])\s+(?=[«(\-–—A-ZА-ЯЁ0-9])")


# ------------------------------------------------------------------ helpers

def strip_tags(s: str) -> str:
    return htmllib.unescape(TAG_RE.sub("", s))


def redact(s: str) -> str:
    s = CARD_RE.sub(REDACTED, s)
    return PHONE_RE.sub(REDACTED, s)


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "без даты"
    d = datetime.fromisoformat(iso)
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year}"


def month_key(iso: str) -> str:
    return iso[:7]


def month_title(key: str) -> str:
    y, m = key.split("-")
    return f"{MONTHS_NOM[int(m) - 1]} {y}"


def html_lines(html: str) -> list[str]:
    return re.split(r"<br\s*/?>", html)


def paragraphs(html_or_lines, mode: str = "prose") -> str:
    """prose: каждая строка — абзац; verse: строфы через пустую строку, строки через <br>."""
    lines = html_or_lines if isinstance(html_or_lines, list) else html_lines(html_or_lines)
    if mode == "prose":
        return "\n".join(f"<p>{ln.strip()}</p>" for ln in lines if strip_tags(ln).strip())
    blocks, cur = [], []
    for ln in lines:
        if strip_tags(ln).strip():
            cur.append(ln.strip())
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return "\n".join("<p>" + "<br>".join(b) + "</p>" for b in blocks)


def first_sentence(text: str, limit: int = 220) -> str:
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    m = re.search(r"^(.{60,}?[.!?…»])\s", cut)
    return (m.group(1) if m else cut.rsplit(" ", 1)[0] + "…")


# ------------------------------------------------------------------ mini-markdown

def inline_md(s: str, link) -> str:
    s = htmllib.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)

    def repl(m):
        text, href = m.group(1), m.group(2)
        if re.fullmatch(r"#\d+", href):
            return f'<a href="{link(int(href[1:]))}" class="ref">{text}</a>'
        return f'<a href="{href}">{text}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", repl, s)


def markdown(src: str, link) -> str:
    out, para, quote, items = [], [], [], []

    def flush():
        nonlocal para, quote, items
        if para:
            out.append("<p>" + inline_md(" ".join(para), link) + "</p>")
        if quote:
            out.append("<blockquote>" + "".join(f"<p>{inline_md(q, link)}</p>" for q in quote) + "</blockquote>")
        if items:
            out.append("<ul>" + "".join(f"<li>{inline_md(i, link)}</li>" for i in items) + "</ul>")
        para, quote, items = [], [], []

    for raw in src.strip().splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
        elif line.startswith("### "):
            flush(); out.append(f"<h3>{inline_md(line[4:], link)}</h3>")
        elif line.startswith("## "):
            flush()
            title = line[3:]
            anchor = re.sub(r"[^\w]+", "-", title.lower()).strip("-")
            out.append(f'<h2 id="{anchor}">{inline_md(title, link)}</h2>')
        elif line.startswith("> "):
            if para or items:
                flush()
            quote.append(line[2:])
        elif line.startswith("- "):
            if para or quote:
                flush()
            items.append(line[2:])
        else:
            if quote or items:
                flush()
            para.append(line.strip())
    flush()
    return "\n".join(out)


# ------------------------------------------------------------------ archive model

class Archive:
    def __init__(self, data: dict, export_dir: Path, out: Path, cleanup: dict | None = None):
        cleanup = cleanup or {}
        self.channel = data.get("channel")
        exclude = set()
        for key, ids in cleanup.items():
            if key.startswith("exclude"):
                exclude |= set(ids)
        keep_fwd = set(cleanup.get("keep_forwards_from", []))
        self.removed: list[dict] = []
        posts = []
        for p in data["posts"]:
            if not p.get("date"):
                continue
            unrelated_fwd = (cleanup.get("drop_unrelated_forwards") and p.get("forwarded_from")
                             and p["forwarded_from"] not in keep_fwd and not RELATED_RE.search(p["text"]))
            blank = not p["text"].strip() and not p["media"] and not p.get("poll")
            short_reply = (cleanup.get("drop_short_replies") and not p.get("forwarded_from") and not p["media"]
                           and not p.get("poll") and len(p["text"].strip()) < 40 and not RELATED_RE.search(p["text"]))
            if exclude & set(p["ids"]) or unrelated_fwd or blank or short_reply:
                self.removed.append(p)
            else:
                posts.append(p)
        self.posts = posts
        self.strip_ids = set(cleanup.get("strip", []))
        self.emoji_off = bool(cleanup.get("strip_emoji"))
        self.keep_emoji = set(cleanup.get("keep_emoji", []))
        self.strip_phrases = [re.escape(x) for x in cleanup.get("strip_phrases", [])]
        self.strip_re = re.compile(DONATE_RE.pattern + ("|" + "|".join(self.strip_phrases) if self.strip_phrases else ""), re.I)
        self.by_id: dict[int, dict] = {}
        for p in self.posts:
            for i in p["ids"]:
                self.by_id[i] = p
        self.export_dir = export_dir
        self.out = out
        self.months: "OrderedDict[str, list[dict]]" = OrderedDict()
        for p in sorted(self.posts, key=lambda p: (p["date"], p["id"])):
            self.months.setdefault(month_key(p["date"]), []).append(p)
        self.copied_media: set[str] = set()
        for p in self.posts:
            html = self.strip_donations(p["html"]) if self.strip_ids & set(p["ids"]) else p["html"]
            p["html_clean"] = self.clean_html(html)
            if self.emoji_off and not (self.keep_emoji & set(p["ids"])):
                p["html_clean"] = strip_emoji(p["html_clean"]).strip()
                if p.get("forwarded_from"):
                    p["forwarded_from"] = strip_emoji(p["forwarded_from"]).strip()
            p["text"] = "\n".join(strip_tags(ln).strip() for ln in html_lines(p["html_clean"])).strip()

    # -- links
    def post_href(self, pid: int, root: str = "") -> str:
        p = self.by_id.get(pid)
        if not p:
            return f"https://t.me/{CHANNEL}/{pid}"
        return f"{root}letopis/{month_key(p['date'])}.html#z{p['id']}"

    def strip_donations(self, html: str) -> str:
        """Убирает фразы о переводах денег и строки с реквизитами, остальной текст не трогает."""
        kept = []
        for ln in html_lines(html):
            plain = strip_tags(ln)
            if not self.strip_re.search(plain):
                kept.append(ln)
                continue
            if len(plain) < 220:  # короткая строка с просьбой уходит целиком, без обрывков
                continue
            parts = [s for s in SENT_SPLIT.split(ln.strip()) if not self.strip_re.search(strip_tags(s))]
            if parts:
                kept.append(" ".join(parts))
        out = "<br>".join(kept)
        out = re.sub(r"(<br>\s*){3,}", "<br><br>", out)
        return re.sub(r"^(\s*<br>)+|(<br>\s*)+$", "", out)

    def clean_html(self, html: str) -> str:
        # строки с реквизитами убираются целиком, остатки на всякий случай скрываются
        def keep(ln: str) -> bool:
            t = strip_tags(ln)
            return not (CARD_RE.search(t) or PHONE_RE.search(t) or LABEL_ONLY_RE.match(t))
        html = "<br>".join(ln for ln in html_lines(html) if keep(ln))
        html = re.sub(r"(<br>\s*){3,}", "<br><br>", html)
        html = re.sub(r"^(\s*<br>)+|(<br>\s*)+$", "", html.strip())
        # после вырезания строк закрываем оборванные теги
        html = str(BeautifulSoup(html, "html.parser")).replace("<br/>", "<br>")
        html = redact(html)
        # ссылки на записи канала ведут внутрь архива
        def tme(m):
            pid = int(m.group(1))
            return f'href="{{ROOT}}{self.post_href(pid)}"' if pid in self.by_id else m.group(0)
        return re.sub(rf'href="https?://t\.me/{CHANNEL}/(\d+)[^"]*"', tme, html)

    # -- media
    # Полноразмерные снимки копируются в media/photos/, превью сборщик делает сам
    # в media/thumbs/ (файлы *_thumb.jpg из экспорта Telegram не нужны).
    THUMB_SIZE = 520

    def media_url(self, rel: str | None, root: str, kind: str = "full") -> str | None:
        if not rel:
            return None
        src = self.export_dir / rel
        if not src.is_file():
            return None
        name = Path(rel).name
        sub = "photos" if kind == "full" else "thumbs"
        key = f"{sub}/{name}"
        if key not in self.copied_media:
            dst = self.out / "media" / sub / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if kind == "full":
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dst)
            elif not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    im.thumbnail((self.THUMB_SIZE, self.THUMB_SIZE))
                    im.save(dst, "JPEG", quality=78, optimize=True, progressive=True)
            self.copied_media.add(key)
        return f"{root}media/{key}"

    def thumb_url(self, rel: str | None, root: str) -> str | None:
        return self.media_url(rel, root, "thumb")

    def photos(self, p: dict) -> list[dict]:
        return [m for m in p["media"] if m["type"] == "photo" and m.get("src")]

    def photo_ref(self, ref: str | None, root: str) -> str | None:
        """'233:0' -> превью первой фотографии записи № 233."""
        if not ref:
            return None
        pid, _, idx = ref.partition(":")
        p = self.by_id.get(int(pid))
        if not p:
            return None
        ph = self.photos(p)
        i = int(idx or 0)
        return self.thumb_url(ph[i]["src"], root) if i < len(ph) else None

    def prune_media(self) -> int:
        removed = 0
        for sub in ("photos", "thumbs"):
            d = self.out / "media" / sub
            if d.is_dir():
                for f in d.iterdir():
                    if f"{sub}/{f.name}" not in self.copied_media:
                        f.unlink()
                        removed += 1
        return removed


CHANNEL = "e_jopov"


# ------------------------------------------------------------------ stories

AUTHOR_LINES = {"Эдуард Жопов": "Эдуард Жопов", "Эдуард Ягодичкин": "Эдуард Ягодичкин"}
YEAR_RE = re.compile(r"(?:[Дд]атирован\w*|[Нн]аписан\w*|[Зз]апре\w+|опубликован\w*)[^.\d]{0,20}?(\d{4})")
FOOTER_YEAR_RE = re.compile(r"(\d{4})\s*г\.?\s*»?\.?$")


def parse_story(p: dict) -> dict:
    lines = [ln.strip() for ln in html_lines(p["html_clean"])]
    plain = [strip_tags(ln).strip() for ln in lines]
    author_idx = next((i for i, t in enumerate(plain) if t in AUTHOR_LINES), None)
    if author_idx is None:  # эссе без строки автора: заголовок — вторая непустая строка
        nonempty = [i for i, t in enumerate(plain) if t]
        author_idx = nonempty[0]
        author = "Эдуард Жопов"
        pre_idx = nonempty[:1]
        title_idx = nonempty[1]
    else:
        author = AUTHOR_LINES[plain[author_idx]]
        pre_idx = list(range(author_idx))
        title_idx = next(i for i in range(author_idx + 1, len(plain)) if plain[i])
    title = plain[title_idx]
    title = re.sub(r"\s*\((рассказ|эссе)\)\s*$", "", title).strip()
    if title.startswith("«") and title.endswith("»") and title.count("«") == 1:
        title = title[1:-1]
    body_start = title_idx + 1
    while body_start < len(plain) and plain[body_start] in ("", "(рассказ)", "(эссе)"):
        body_start += 1
    preface = [lines[i] for i in pre_idx
               if plain[i] and not DONATION_LINE_RE.search(plain[i]) and "изъято" not in plain[i]
               and plain[i].lower() != "жолотой фонд мэтра"]
    pre_text = " ".join(plain[i] for i in pre_idx)
    year = None
    m = YEAR_RE.search(pre_text)
    if m:
        year = m.group(1)
    else:
        tail = next((t for t in reversed(plain) if t), "")
        m2 = FOOTER_YEAR_RE.search(tail)
        if m2:
            year = m2.group(1)
    return {
        "id": p["id"], "post": p, "author": author, "title": title, "year": year,
        "preface_html": " ".join(preface),
        "body_html": paragraphs(lines[body_start:], "prose"),
        "length": len(" ".join(plain[body_start:])),
    }


# ------------------------------------------------------------------ auto-collections

PRIZE_RE = re.compile(r"(преми\w* имени|жоповк|ПРЕМИ\w* ИМЕНИ|ПРЕМИИ ЭДУАРДА|ПРЕМИЯ ИМЕНИ|лауреат премии имени)", re.I)
QUESTION_RE = re.compile(
    r"^(?:[^.?!]{0,80})(спрашива\w*|задают вопрос|осмеливаются спросить|интересуются|звучал вопрос|задавались вопросом)",
    re.I)
RARE_RE = re.compile(r"(Редк\w+ (фото|кадр|рисун|карикатур|фотограф|архивн|видео)|Редкие кадры)", re.I)


def collect_prizes(arch: Archive, exclude=()) -> list[dict]:
    return [p for p in arch.posts
            if PRIZE_RE.search(p["text"][:180]) and len(p["text"]) > 120
            and p["id"] not in exclude and not p.get("forwarded_from")]


def collect_questions(arch: Archive, exclude=()) -> list[dict]:
    res = []
    for p in arch.posts:
        head = p["text"][:220]
        if p["id"] in exclude or p.get("forwarded_from"):
            continue
        if QUESTION_RE.search(head) and "?" in head and len(p["text"]) > 150:
            res.append(p)
    extra = [1440, 1441]
    for i in extra:
        if i in arch.by_id and arch.by_id[i] not in res:
            res.append(arch.by_id[i])
    return sorted(res, key=lambda p: p["date"])


# ------------------------------------------------------------------ build

def build(args) -> None:
    data = json.loads(args.data.read_text(encoding="utf-8"))
    canon = tomllib.loads((ROOT / "content/canon.toml").read_text(encoding="utf-8"))
    intros = tomllib.loads((ROOT / "content/texts.toml").read_text(encoding="utf-8"))
    global CHANNEL
    CHANNEL = canon["site"].get("channel", CHANNEL)

    out: Path = args.out
    if out.exists():
        for child in out.iterdir():
            if child.name == "media":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    arch = Archive(data, args.export_dir, out, canon.get("cleanup"))

    env = Environment(loader=FileSystemLoader(TOOLS / "templates"),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["date"] = fmt_date
    env.filters["month_title"] = month_title
    env.globals.update(site=canon["site"], CHANNEL=CHANNEL, arch=arch, first_sentence=first_sentence,
                       paragraphs=paragraphs, nav=NAV)

    def md(key: str, root: str) -> str:
        return markdown(intros[key], lambda pid: arch.post_href(pid, root)) if key in intros else ""

    def render(path: str, template: str, **ctx):
        depth = path.count("/")
        root = "../" * depth
        ctx.setdefault("page", path)
        html = env.get_template(template).render(root=root, md=lambda k: md(k, root),
                                                 href=lambda pid: arch.post_href(pid, root), **ctx)
        html = html.replace("{ROOT}", root)
        dst = out / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(html, encoding="utf-8")

    # --- stories
    volumes = []
    all_stories = []
    for vol in canon["volumes"]:
        works = []
        for pid in vol["works"]:
            s = parse_story(arch.by_id[pid])
            s["volume"] = vol["title"]
            works.append(s)
            all_stories.append(s)
        volumes.append({**vol, "stories": works})
    for i, s in enumerate(all_stories):
        s["prev"] = all_stories[i - 1] if i > 0 else None
        s["next"] = all_stories[i + 1] if i + 1 < len(all_stories) else None

    # --- poems
    poem_sections: "OrderedDict[str, list]" = OrderedDict()
    for poem in canon["poems"]:
        p = arch.by_id[poem["post"]]
        poem_sections.setdefault(poem["section"], []).append({**poem, "p": p})

    # --- photos
    photo_posts = [p for p in arch.posts if arch.photos(p)]
    rare = [p for p in photo_posts if RARE_RE.search(p["text"][:120])]
    photos_by_year: "OrderedDict[str, list]" = OrderedDict()
    for p in photo_posts:
        photos_by_year.setdefault(p["date"][:4], []).append(p)
    have_photos = any((args.export_dir / m["src"]).is_file()
                      for p in photo_posts for m in arch.photos(p) if m.get("src"))

    people_groups: "OrderedDict[str, list]" = OrderedDict()
    for person in canon["people"]:
        people_groups.setdefault(person["group"], []).append(person)

    auto = canon.get("auto", {})
    prizes = collect_prizes(arch, auto.get("prize_exclude", []))
    questions = collect_questions(arch, auto.get("question_exclude", []))
    stats = {
        "posts": len(arch.posts),
        "photos": sum(len(arch.photos(p)) for p in arch.posts),
        "stories": len([s for s in all_stories if s["author"] == "Эдуард Жопов"]),
        "books": len(canon["books"]),
        "witnesses": len(canon["witnesses"]),
        "first": arch.posts[0]["date"], "last": arch.posts[-1]["date"],
        "months": len(arch.months),
    }

    # портрет Мэтра
    portrait_src = args.export_dir / canon["site"].get("portrait", "photos/Zhopov_portrait.jpg")
    if portrait_src.is_file():
        (out / "media").mkdir(parents=True, exist_ok=True)
        shutil.copy2(portrait_src, out / "media" / "portrait.jpg")
        env.globals["portrait"] = "media/portrait.jpg"
    else:
        env.globals["portrait"] = None

    zhensonessa = sorted((p for p in arch.posts if p.get("forwarded_from") == "Женсонесса Рысь"),
                         key=lambda p: p["date"])
    stats["zhensonessa"] = len(zhensonessa)

    render("index.html", "index.html", stats=stats, volumes=volumes, have_photos=have_photos,
           random_ids=[arch.post_href(p["id"]) for p in arch.posts if len(p["text"]) > 300])
    render("zhizn.html", "zhizn.html", timeline=canon["timeline"])
    render("sochineniya.html", "sochineniya.html", volumes=volumes, books=canon["books"], stats=stats)
    for s in all_stories:
        render(f"tekst/{s['id']}.html", "story.html", s=s)
    render("stihi.html", "stihi.html", sections=poem_sections)
    render("svidetelstva.html", "svidetelstva.html",
           items=[{**w, "p": arch.by_id[w["post"]]} for w in canon["witnesses"]])
    render("lyudi.html", "lyudi.html", groups=people_groups)
    render("zhensonessa.html", "feed.html", title="Записки Женсонессы Рысь", intro_key="zhensonessa",
           posts=zhensonessa, kicker="Окружение · мемуары", page="lyudi.html")
    render("zavety.html", "zavety.html", sayings=canon["sayings"],
           posts=[arch.by_id[i] for i in canon["zavety"]["posts"]])
    render("premiya.html", "feed.html", title="Премия имени Эдуарда Жопова", intro_key="premiya",
           posts=prizes, kicker="Хроника вручений")
    render("voprosy.html", "feed.html", title="Вопросы читателей", intro_key="voprosy",
           posts=questions, kicker="Жоповедение")
    render("foto.html", "foto.html", rare=rare, by_year=photos_by_year, have_photos=have_photos,
           total=stats["photos"])
    render("letopis/index.html", "letopis_index.html", months=arch.months)
    keys = list(arch.months)
    for i, key in enumerate(keys):
        render(f"letopis/{key}.html", "letopis_month.html", key=key, posts=arch.months[key],
               prev_key=keys[i - 1] if i else None, next_key=keys[i + 1] if i + 1 < len(keys) else None)
    render("o-sobranii.html", "prose.html", title="О собрании", key="o_sobranii", stats=stats)
    render("404.html", "prose.html", title="Документ не найден", key="not_found", stats=stats,
           base_href=canon["site"].get("base_path"))

    pruned = arch.prune_media()

    static = TOOLS / "static"
    for f in static.iterdir():
        shutil.copy2(f, out / f.name)
    (out / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Собрано в {out}: {sum(1 for _ in out.rglob('*.html'))} страниц, "
          f"рассказов {len(all_stories)}, премия {len(prizes)}, вопросы {len(questions)}, "
          f"фото-записей {len(photo_posts)} (редких {len(rare)}), медиафайлов {len(arch.copied_media)}"
          f"{f', удалено устаревших {pruned}' if pruned else ''}"
          f"; исключено записей: {len(arch.removed)}"
          + ("" if have_photos else " — папки photos/ нет, фото показаны описью"))


NAV = [
    ("zhizn.html", "Жизнеописание"),
    ("sochineniya.html", "Сочинения"),
    ("stihi.html", "Стихи"),
    ("svidetelstva.html", "Свидетельства"),
    ("lyudi.html", "Окружение"),
    ("zavety.html", "Заветы"),
    ("premiya.html", "Премия"),
    ("foto.html", "Фотоархив"),
    ("letopis/index.html", "Летопись"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=ROOT / "data/posts.json")
    ap.add_argument("--export-dir", type=Path, default=ROOT / "Эдуард Жопов")
    ap.add_argument("--out", type=Path, default=ROOT / "docs")
    build(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
