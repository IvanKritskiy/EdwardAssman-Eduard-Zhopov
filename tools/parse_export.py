#!/usr/bin/env python3
"""Разбор HTML-экспорта Telegram Desktop в data/posts.json.

Читает messages.html, messages2.html, ... из папки экспорта и сохраняет
посты в одном JSON-файле, с которым дальше работает build_site.py.

    python3 tools/parse_export.py "Эдуард Жопов" -o data/posts.json

Требуется beautifulsoup4.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

ALLOWED_INLINE = {"strong", "b", "em", "i", "u", "s", "del", "code", "pre", "blockquote"}
DATE_RE = re.compile(
    r"(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4}) (?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})"
    r"(?: (?:UTC|GMT)(?P<tz>[+-]\d{2}):(?P<tzm>\d{2}))?"
)
MSG_ID_RE = re.compile(r"message(-?\d+)")
GOTO_RE = re.compile(r"(?:(?P<file>messages\d*\.html))?#go_to_message(?P<id>\d+)")
HASHTAG_RE = re.compile(r"ShowHashtag\(\"(.+?)\"\)")
SIZE_RE = re.compile(r"width:\s*(\d+)px;\s*height:\s*(\d+)px")


def page_sort_key(path: Path) -> int:
    m = re.fullmatch(r"messages(\d*)\.html", path.name)
    return int(m.group(1) or 1) if m else 10**9


def parse_date(title: str | None) -> str | None:
    if not title:
        return None
    m = DATE_RE.search(title)
    if not m:
        return None
    tz = None
    if m.group("tz"):
        sign = -1 if m.group("tz").startswith("-") else 1
        hours = abs(int(m.group("tz")))
        tz = timezone(sign * timedelta(hours=hours, minutes=int(m.group("tzm"))))
    dt = datetime(
        int(m.group("y")), int(m.group("m")), int(m.group("d")),
        int(m.group("H")), int(m.group("M")), int(m.group("S")), tzinfo=tz,
    )
    return dt.isoformat()


def clean_text(node: Tag | None) -> tuple[str, str, list[str]]:
    """Возвращает (безопасный html, простой текст, хэштеги) для div.text."""
    if node is None:
        return "", "", []
    hashtags: list[str] = []

    def walk(el) -> str:
        if isinstance(el, NavigableString):
            return escape(str(el))
        if not isinstance(el, Tag):
            return ""
        name = el.name
        inner = "".join(walk(c) for c in el.children)
        if name == "br":
            return "<br>"
        if name == "a":
            onclick = el.get("onclick", "")
            tag = HASHTAG_RE.search(onclick)
            if tag:
                value = tag.group(1)
                hashtags.append(value.lstrip("#"))
                return f'<span class="hashtag" data-tag="{escape(value.lstrip("#"))}">{inner}</span>'
            href = el.get("href", "")
            if href.startswith(("http://", "https://", "mailto:", "tg://")):
                return f'<a href="{escape(href)}" rel="noopener">{inner}</a>'
            return inner
        if name == "span" and "spoiler" in (el.get("class") or []):
            return f'<span class="spoiler">{inner}</span>'
        if name in ALLOWED_INLINE:
            name = {"b": "strong", "i": "em", "del": "s"}.get(name, name)
            return f"<{name}>{inner}</{name}>"
        return inner

    html = "".join(walk(c) for c in node.children).strip()
    html = re.sub(r"^(?:<br>\s*)+|(?:\s*<br>)+$", "", html)

    text_parts = []
    for el in node.descendants:
        if isinstance(el, NavigableString):
            text_parts.append(str(el))
        elif isinstance(el, Tag) and el.name == "br":
            text_parts.append("\n")
    text = "".join(text_parts).strip()
    return html, text, hashtags


def escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def size_of(img: Tag | None) -> tuple[int | None, int | None]:
    if img is None:
        return None, None
    m = SIZE_RE.search(img.get("style", ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def parse_media(scope: Tag) -> tuple[list[dict], dict | None]:
    media: list[dict] = []
    poll = None
    wrap = scope.find("div", class_="media_wrap", recursive=False)
    if wrap is None:
        return media, poll

    for a in wrap.find_all("a", recursive=True):
        cls = set(a.get("class") or [])
        href = a.get("href")
        if "photo_wrap" in cls:
            img = a.find("img")
            w, h = size_of(img)
            media.append({"type": "photo", "src": href, "thumb": img.get("src") if img else None, "w": w, "h": h})
        elif "video_file_wrap" in cls or "animated_wrap" in cls:
            img = a.find("img")
            w, h = size_of(img)
            dur = a.find(class_="video_duration")
            media.append({
                "type": "animation" if "animated_wrap" in cls else "video",
                "src": href, "thumb": img.get("src") if img else None, "w": w, "h": h,
                "duration": dur.get_text(strip=True) if dur else None,
            })
        elif "sticker_wrap" in cls:
            img = a.find("img")
            media.append({"type": "sticker", "src": href, "thumb": img.get("src") if img else None})
        elif "media" in cls:
            kind = next((c[len("media_"):] for c in cls if c.startswith("media_")), "file")
            title = a.find(class_="title")
            status = a.find(class_="status")
            media.append({
                "type": {"voice_message": "voice", "audio_file": "audio", "video": "round_video"}.get(kind, kind),
                "src": href,
                "title": title.get_text(" ", strip=True) if title else None,
                "status": status.get_text(" ", strip=True) if status else None,
            })

    # медиа, которые не попали в экспорт: div.media без ссылки
    for div in wrap.find_all("div", class_="media", recursive=False):
        cls = set(div.get("class") or [])
        kind = next((c[len("media_"):] for c in cls if c.startswith("media_")), "file")
        title = div.find(class_="title")
        desc = div.find(class_="description")
        media.append({
            "type": kind, "src": None, "missing": True,
            "title": title.get_text(" ", strip=True) if title else None,
            "description": desc.get_text(" ", strip=True) if desc else None,
        })

    poll_div = wrap.find("div", class_="media_poll")
    if poll_div is not None:
        question = poll_div.find(class_="question")
        answers = []
        for ans in poll_div.find_all("div", class_="answer"):
            votes = ans.find(class_="details")
            votes_text = votes.get_text(strip=True) if votes else ""
            if votes:
                votes.extract()
            answers.append({
                "text": ans.get_text(" ", strip=True).lstrip("-– ").strip(),
                "votes": votes_text,
            })
        total = poll_div.find(class_="total")
        poll = {
            "question": question.get_text(" ", strip=True) if question else "",
            "answers": answers,
            "total": total.get_text(" ", strip=True) if total else "",
        }
    return media, poll


def parse_message(div: Tag, page: str) -> dict | None:
    m = MSG_ID_RE.fullmatch(div.get("id", ""))
    msg_id = int(m.group(1)) if m else None
    classes = set(div.get("class") or [])

    if "service" in classes:
        body = div.find("div", class_="body")
        return {
            "kind": "service", "id": msg_id, "page": page,
            "text": body.get_text(" ", strip=True) if body else "",
        }

    body = div.find("div", class_="body", recursive=False)
    if body is None:
        return None

    date_div = body.find("div", class_="date", recursive=False)
    from_div = body.find("div", class_="from_name", recursive=False)
    signature = body.find("div", class_="signature", recursive=False)
    reply = body.find("div", class_="reply_to", recursive=False)
    fwd = body.find("div", class_="forwarded", recursive=False)

    content_scope = fwd if fwd is not None else body
    text_div = content_scope.find("div", class_="text", recursive=False)
    html, text, hashtags = clean_text(text_div)
    media, poll = parse_media(content_scope)

    forwarded_from = forwarded_date = None
    if fwd is not None:
        fname = fwd.find("div", class_="from_name", recursive=False)
        if fname is not None:
            fdate = fname.find(class_="date")
            if fdate is not None:
                forwarded_date = parse_date(fdate.get("title") or fdate.get_text())
                fdate.extract()
            forwarded_from = fname.get_text(" ", strip=True)

    reply_to = None
    if reply is not None:
        link = reply.find("a")
        gm = GOTO_RE.search(link.get("href", "")) if link else None
        if gm:
            reply_to = int(gm.group("id"))

    reactions = []
    for r in body.select(".reactions .reaction"):
        emoji = r.find(class_="emoji")
        count = r.find(class_="count")
        reactions.append({
            "emoji": emoji.get_text(strip=True) if emoji else r.get_text(" ", strip=True),
            "count": int(re.sub(r"\D", "", count.get_text()) or 0) if count else None,
        })

    date_title = date_div.get("title") if date_div else None
    return {
        "kind": "message",
        "id": msg_id,
        "page": page,
        "joined": "joined" in classes,
        "date": parse_date(date_title),
        "date_raw": date_title,
        "author": from_div.get_text(" ", strip=True) if from_div else None,
        "signature": signature.get_text(" ", strip=True) if signature else None,
        "forwarded_from": forwarded_from,
        "forwarded_date": forwarded_date,
        "reply_to": reply_to,
        "html": html,
        "text": text,
        "hashtags": hashtags,
        "media": media,
        "poll": poll,
        "reactions": reactions,
    }


def group_albums(messages: list[dict]) -> list[dict]:
    """Склеивает альбомы: в экспорте каждая фотография альбома идёт отдельным
    сообщением с той же секундой и классом joined."""
    posts: list[dict] = []
    for msg in messages:
        prev = posts[-1] if posts else None
        is_album_part = (
            prev is not None
            and msg["joined"]
            and msg["media"] and prev["media"]
            and msg["date_raw"] == prev["date_raw"]
            and msg["forwarded_from"] == prev["forwarded_from"]
            and not (msg["text"] and prev["text"])
        )
        if is_album_part:
            prev["ids"].append(msg["id"])
            prev["media"].extend(msg["media"])
            if msg["text"]:
                prev["html"], prev["text"] = msg["html"], msg["text"]
                prev["hashtags"] = prev["hashtags"] + msg["hashtags"]
            prev["reactions"] = prev["reactions"] or msg["reactions"]
            continue
        post = dict(msg)
        post["ids"] = [msg["id"]]
        posts.append(post)

    # подпись автора у joined-сообщений наследуется от предыдущего
    last_author = None
    for p in posts:
        if p["author"]:
            last_author = p["author"]
        else:
            p["author"] = last_author
        p.pop("joined", None)
        p.pop("kind", None)
    return posts


def parse_export(export_dir: Path) -> dict:
    pages = sorted(export_dir.glob("messages*.html"), key=page_sort_key)
    if not pages:
        raise SystemExit(f"В {export_dir} нет файлов messages*.html")

    channel = None
    messages: list[dict] = []
    service: list[dict] = []
    for page in pages:
        soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
        if channel is None:
            header = soup.select_one(".page_header .text")
            channel = header.get_text(" ", strip=True) if header else None
        for div in soup.select(".history > div.message"):
            item = parse_message(div, page.name)
            if item is None:
                continue
            (service if item["kind"] == "service" else messages).append(item)

    posts = group_albums(messages)
    return {
        "channel": channel,
        "source_pages": [p.name for p in pages],
        "export_dir": export_dir.name,
        "posts": posts,
        "service": service,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir", nargs="?", default="Эдуард Жопов", type=Path)
    ap.add_argument("-o", "--output", default=Path("data/posts.json"), type=Path)
    args = ap.parse_args(argv)

    data = parse_export(args.export_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    posts = data["posts"]
    photos = sum(1 for p in posts for m in p["media"] if m["type"] == "photo")
    dated = [p["date"] for p in posts if p["date"]]
    print(f"Канал: {data['channel']}")
    print(f"Страниц: {len(data['source_pages'])}, постов: {len(posts)}, фото: {photos}")
    if dated:
        print(f"Период: {min(dated)[:10]} — {max(dated)[:10]}")
    print(f"Сохранено в {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
