#!/usr/bin/env python3
"""Индекс скилов в contextd.

Обходит каталоги скилов (Hermes + OpenCode), режет каждый файл на чанки по
секциям и заливает их в contextd как факты с topic ``skill/<имя>``.
Инкрементально: файл переиндексируется только при смене хеша, старые чанки
удаляются по id из state-файла.

    python skills_index.py            # инкрементально
    python skills_index.py --dry      # показать, что будет залито
    python skills_index.py --force    # переиндексировать всё
    python skills_index.py --limit 50 # первые N файлов
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, "AppData", "Local", "hermes")
ROOTS = [
    os.path.join(HERMES, "skills"),
    os.path.join(HOME, ".config", "opencode", "skills"),
]
STATE = os.path.join(HERMES, "agent-hooks", "skills_index.state.json")
ENV_FILE = os.path.join(HOME, ".hermes_srv_ai.env")

CTXD = os.environ.get("CONTEXTD_URL", "http://127.0.0.1:8090")
TEXT_EXT = {".md", ".txt", ".py", ".sh", ".cmd", ".yaml", ".yml", ".toml"}
SKIP_DIRS = {"assets", "node_modules", ".git", "__pycache__", ".venv", "venv",
             "outputs", "templates"}
CHUNK = 700
MIN_CHUNK = 80
BATCH = 32
KIND = "skill"
SOURCE = "skills-index"
ERRLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills_index.err.log")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


TOKEN = load_env().get("CTXD_TOKEN", "")


def api(path: str, payload=None, method: str = "POST", timeout: float = 120.0):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        CTXD + path,
        data=data,
        method=method,
        headers={"X-Context-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ------------------------------------------------------------------ сбор файлов

def skill_dirs(root: str) -> list[str]:
    """Каталоги скилов: те, где лежит SKILL.md."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        if "SKILL.md" in filenames:
            out.append(dirpath)
    return out


def skill_name(root: str, path: str) -> str:
    """Имя ближайшего каталога с SKILL.md (скилы лежат как <категория>/<скил>/)."""
    cur = os.path.dirname(path)
    while os.path.normcase(os.path.abspath(cur)) != os.path.normcase(os.path.abspath(root)):
        if os.path.exists(os.path.join(cur, "SKILL.md")):
            return os.path.basename(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    rel = os.path.relpath(path, root).replace("\\", "/")
    return rel.split("/")[0] if "/" in rel else os.path.splitext(rel)[0]


def collect(limit: int | None = None) -> list[tuple[str, str, str]]:
    """[(path, skill, rel_inside_skill)] — только текстовые файлы внутри скилов."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for sdir in skill_dirs(root):
            skill = os.path.basename(sdir)
            for dirpath, dirnames, filenames in os.walk(sdir):
                dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
                for fname in sorted(filenames):
                    if os.path.splitext(fname)[1].lower() not in TEXT_EXT:
                        continue
                    path = os.path.join(dirpath, fname)
                    key = os.path.normcase(os.path.abspath(path))
                    if key in seen:
                        continue
                    seen.add(key)
                    rel = os.path.relpath(path, sdir).replace("\\", "/")
                    found.append((path, skill, rel))
                    if limit and len(found) >= limit:
                        return found
    return found


# ------------------------------------------------------------------ чанкер

def chunk_text(text: str, size: int = CHUNK) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\t", "    ")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    out: list[str] = []
    cur = ""
    for block in blocks:
        if len(cur) + len(block) + 2 <= size:
            cur = f"{cur}\n\n{block}" if cur else block
            continue
        if cur:
            out.append(cur)
            cur = ""
        while len(block) > size:
            cut = block.rfind("\n", 0, size)
            if cut < size // 2:
                cut = block.rfind(". ", 0, size)
                cut = cut + 1 if cut > 0 else size
            out.append(block[:cut].strip())
            block = block[cut:].lstrip()
        cur = block
    if cur:
        out.append(cur)
    return [c for c in out if len(c) >= MIN_CHUNK]


def make_chunks(path: str, skill: str, rel: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if not text.strip():
        return []
    head = f"{skill} · {rel}" if rel != "SKILL.md" else skill
    chunks = chunk_text(text)
    # в первом чанке держим frontmatter-заголовок, чтобы поиск ловил описание скила
    return [f"[{head}]\n{c}" for c in chunks]


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ main

def _too_large(exc: urllib.error.HTTPError) -> bool:
    """Эмбеддер bge-m3 на сервере: batch size 512 токенов, длинный код не влезает."""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        return False
    return "too large" in body.lower()


def push_items(items: list[dict], depth: int = 0) -> tuple[list[int], int, int]:
    """Залить пачку; при «too large» резать пополам. → (ids, залито, ошибок)."""
    try:
        res = api("/remember_bulk", {"items": items})
        ids = [int(x["id"]) for x in (res.get("items") or []) if x.get("id")]
        return ids, len(items), 0
    except urllib.error.HTTPError as exc:
        if len(items) > 1:
            mid = len(items) // 2
            a = push_items(items[:mid], depth)
            b = push_items(items[mid:], depth)
            return a[0] + b[0], a[1] + b[1], a[2] + b[2]
        if _too_large(exc) and depth < 6:
            text = items[0]["text"]
            cut = text.rfind("\n", 0, len(text) // 2) or len(text) // 2
            halves = [text[:cut].strip(), text[cut:].strip()]
            halves = [h for h in halves if len(h) >= MIN_CHUNK]
            if len(halves) == 2:
                ids: list[int] = []
                sent = failed = 0
                for half in halves:
                    r = push_items([{**items[0], "text": half}], depth + 1)
                    ids += r[0]
                    sent += r[1]
                    failed += r[2]
                return ids, sent, failed
        with open(ERRLOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {items[0].get('tags')} "
                     f"{exc.code} {str(exc)[:160]}\n")
        return [], 0, 1
    except (urllib.error.URLError, ValueError) as exc:
        with open(ERRLOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {items[0].get('tags')} "
                     f"{type(exc).__name__} {str(exc)[:160]}\n")
        return [], 0, 1


def main() -> int:
    args = set(sys.argv[1:])
    dry = "--dry" in args
    force = "--force" in args
    limit = None
    for a in args:
        if a.startswith("--limit"):
            limit = int(a.split("=", 1)[1]) if "=" in a else None
    if "--limit" in args:
        idx = sys.argv.index("--limit")
        if idx + 1 < len(sys.argv):
            limit = int(sys.argv[idx + 1])

    if not TOKEN:
        print("нет CTXD_TOKEN в ~/.hermes_srv_ai.env")
        return 2

    state: dict[str, dict] = {}
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}

    files = collect(limit)
    todo: list[tuple[str, str, str, str]] = []
    for path, skill, rel in files:
        digest = file_hash(path)
        prev = state.get(path)
        if prev and prev.get("hash") == digest and not force:
            continue
        todo.append((path, skill, rel, digest))

    print(f"файлов всего: {len(files)}, к индексации: {len(todo)}")
    if dry:
        for path, skill, rel, _ in todo[:15]:
            print("  ", skill, "/", rel)
        if len(todo) > 15:
            print(f"   … ещё {len(todo) - 15}")
        return 0

    t0 = time.time()
    sent = failed = 0
    for path, skill, rel, digest in todo:
        try:
            chunks = make_chunks(path, skill, rel)
        except OSError as exc:
            print("skip", path, exc)
            continue
        old_ids = (state.get(path) or {}).get("ids") or []
        for fid in old_ids:
            try:
                api(f"/forget?id={fid}", method="DELETE", timeout=30)
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
                pass
        new_ids: list[int] = []
        topic = f"skill/{skill}"
        ok = True
        for start in range(0, len(chunks), BATCH):
            part = chunks[start:start + BATCH]
            items = [
                {"text": c, "topic": topic, "kind": KIND, "source": SOURCE, "tags": rel}
                for c in part
            ]
            ids, good, bad = push_items(items)
            new_ids += ids
            sent += good
            failed += bad
            if bad:
                ok = False
            print(f"  {skill}/{rel}: +{good} (всего {sent})")
        if not ok:
            print(f"  ! {path}: часть чанков не залилась, повтор в следующий прогон")
            continue
        state[path] = {"hash": digest, "ids": new_ids, "chunks": len(chunks),
                       "skill": skill, "rel": rel}

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)

    print(f"готово: чанков залито {sent}, ошибок {failed}, "
          f"{time.time() - t0:.0f} с, файлов в state {len(state)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
