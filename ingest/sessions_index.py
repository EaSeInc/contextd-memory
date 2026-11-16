#!/usr/bin/env python3
"""Индекс истории чатов Hermes (state.db) в память contextd на srv-ai.

Берёт сообщения user/assistant из сессий, режет на чанки, шлёт в /remember_bulk.
Инкрементально: хеш текста сессии в sessions_index.state.json.
Запуск: py -3 sessions_index.py [--dry] [--force] [--limit N] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skills_index as si  # noqa: E402  (api, push_items, load_env, chunk_text)

DB = os.path.expanduser("~/AppData/Local/hermes/state.db")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions_index.state.json")
KIND = "chat"
SOURCE = "chat-index"
MIN_MSG = 40          # короткие «да», «ок» в память не нужны
MAX_SESSION = 200_000  # символов на сессию, дальше режем

SKIP_PREFIX = (
    "[IMPORTANT:", "[CONTEXT COMPACTION", "[OUT-OF-BAND", "[INTERNAL NOTIFICATION",
    "[SYSTEM:", "[Background process", "Память contextd",
)
SECRET_RX = re.compile(
    r"(?i)(password|passwd|пароль|token|токен|secret|api[_-]?key|authorization|bearer)"
    r"(\s*[:=]\s*|\s+)(\S{6,})"
)


def redact(text: str) -> str:
    return SECRET_RX.sub(lambda m: f"{m.group(1)}{m.group(2)}<скрыто>", text)


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


def sessions(since: str | None = None) -> list[dict]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    rows = c.execute(
        """select s.id, s.source, s.title, s.display_name,
                  datetime(coalesce(s.started_at, min(m.timestamp)), 'unixepoch'),
                  count(m.id)
             from sessions s join messages m on m.session_id = s.id
            where m.role in ('user', 'assistant') and coalesce(m.content, '') <> ''
            group by s.id order by 5"""
    ).fetchall()
    out = []
    for sid, source, title, name, started, cnt in rows:
        if since and (started or "") < since:
            continue
        out.append({"id": sid, "source": source, "title": title or name or "",
                    "started": started or "", "count": cnt})
    c.close()
    return out


def dialog(c: sqlite3.Connection, sid: str) -> str:
    """Склеенный диалог сессии: «U: …» / «A: …»."""
    parts: list[str] = []
    total = 0
    for role, content in c.execute(
        "select role, content from messages where session_id = ? and role in ('user','assistant')"
        " and coalesce(content,'') <> '' order by id", (sid,)
    ):
        text = redact(content or "").strip()
        if len(text) < MIN_MSG or text.startswith(SKIP_PREFIX):
            continue
        tag = "U" if role == "user" else "A"
        block = f"{tag}: {text}"
        if total + len(block) > MAX_SESSION:
            parts.append("… (обрезано)")
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry" in args
    force = "--force" in args
    limit = None
    since = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        if a == "--since" and i + 1 < len(args):
            since = args[i + 1]

    if not os.path.exists(DB):
        print("нет state.db:", DB)
        return 2
    si.load_env()

    state = load_state()
    todo = []
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    for s in sessions(since):
        text = dialog(c, s["id"])
        if len(text) < 200:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        old = state.get(s["id"]) or {}
        if not force and old.get("hash") == digest:
            continue
        todo.append((s, text, digest))
        if limit and len(todo) >= limit:
            break
    c.close()

    print(f"сессий с текстом: {len(todo)} (всего в state {len(state)})")
    if dry:
        for s, text, _ in todo[:15]:
            print(f"  {s['id'][:18]} {s['started'][:10]} {s['source']:<6} "
                  f"{len(text):>7} симв → ~{len(text)//700 + 1} чанков | {s['title'][:40]}")
        if len(todo) > 15:
            print(f"   … ещё {len(todo) - 15}")
        return 0

    sent = failed = 0
    t0 = time.time()
    for s, text, digest in todo:
        month = (s["started"] or "")[:7] or "unknown"
        topic = f"chat/{month}"
        chunks = si.chunk_text(text)
        old_ids = (state.get(s["id"]) or {}).get("ids") or []
        for fid in old_ids:
            try:
                si.api(f"/forget?id={fid}", method="DELETE", timeout=30)
            except Exception:
                pass
        ids: list[int] = []
        ok = True
        for start in range(0, len(chunks), si.BATCH):
            part = chunks[start:start + si.BATCH]
            items = [{"text": ch, "topic": topic, "kind": KIND, "source": SOURCE,
                      "tags": s["id"][:18]} for ch in part]
            got, good, bad = si.push_items(items)
            ids += got
            sent += good
            failed += bad
            if bad:
                ok = False
        if not ok:
            print(f"  ! {s['id'][:18]}: часть чанков не залилась, повтор в следующий раз")
            continue
        state[s["id"]] = {"hash": digest, "ids": ids, "chunks": len(chunks),
                          "month": month, "title": s["title"][:80]}
        save_state(state)  # после каждой сессии — чтобы обрыв не терял прогресс
        print(f"  {s['id'][:18]} {month} +{len(chunks)} (всего {sent})")

    save_state(state)
    print(f"готово: чанков залито {sent}, ошибок {failed}, {time.time() - t0:.0f} с, "
          f"сессий в state {len(state)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
