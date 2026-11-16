#!/usr/bin/env python3
"""contextd - local memory service for limited-context LLMs.

Facts live in SQLite (source of truth + FTS5). Vectors go to Qdrant (HNSW)
when it is reachable, otherwise sqlite-vec is used as a fallback.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import struct
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("CONTEXTD_DB", "/srv/contextd/context.db")
TOKEN = os.environ.get("CONTEXTD_TOKEN", "").strip()
EMBED_URL = os.environ.get("CONTEXTD_EMBED_URL", "").strip()
EMBED_MODEL = os.environ.get("CONTEXTD_EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("CONTEXTD_EMBED_DIM", "1024"))
DEFAULT_BUDGET = int(os.environ.get("CONTEXTD_BUDGET", "800"))
QDRANT_URL = os.environ.get("CONTEXTD_QDRANT_URL", "").strip().rstrip("/")
QCOLL = os.environ.get("CONTEXTD_QDRANT_COLLECTION", "contextd")

_lock = threading.Lock()
_vec_ready = False
_q_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY,
  topic TEXT NOT NULL DEFAULT 'general',
  kind TEXT NOT NULL DEFAULT 'fact',
  text TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  pinned INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 50,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_facts ON facts(topic, kind, text);
CREATE INDEX IF NOT EXISTS ix_facts_topic ON facts(topic);
CREATE TABLE IF NOT EXISTS fact_keys(
  fact_id INTEGER PRIMARY KEY,
  nkey TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_fact_keys ON fact_keys(nkey);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  text, topic, tags,
  content='facts', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, topic, tags) VALUES (new.id, new.text, new.topic, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, topic, tags) VALUES ('delete', old.id, old.text, old.topic, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, topic, tags) VALUES ('delete', old.id, old.text, old.topic, old.tags);
  INSERT INTO facts_fts(rowid, text, topic, tags) VALUES (new.id, new.text, new.topic, new.tags);
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        import sqlite_vec

        sqlite_vec.load(con)
    except Exception:
        pass
    return con


def _ensure_qdrant() -> bool:
    if not QDRANT_URL:
        return False
    try:
        with httpx.Client(timeout=5) as c:
            r = c.put(
                f"{QDRANT_URL}/collections/{QCOLL}",
                json={"vectors": {"size": EMBED_DIM, "distance": "Cosine"}},
            )
            return r.status_code < 300
    except Exception as exc:
        print(f"contextd: qdrant unavailable ({exc})", flush=True)
        return False


# --- дедупликация и приоритеты при записи ------------------------------------

# Пороги из TencentDB, переложенные на наши kind. constraint — «жёсткий приказ»:
# его нельзя вытеснить из бюджета болтовнёй. chat всегда внизу.
PRIORITY_BY_KIND = {
    "constraint": 75,
    "decision": 70,
    "settings": 65,
    "fact": 60,
    "skill": 40,
    "chat": 30,
}
PRIORITY_DEFAULT = 50
PRIORITY_MIN, PRIORITY_MAX = 1, 100

# Семантический merge включаем только там, где запись — отдельный факт.
# У skill и chat «похожий текст» чаще означает нарезку одного файла на чанки
# или перекрытие окон индексатора: склеивать их — терять содержимое.
NEAR_DUP_KINDS = {"fact", "decision", "constraint", "settings"}

_TOOL_RX = re.compile(r"\[ran tool:[^\]]*\]")
_ROLE_RX = re.compile(r"^\s*(?:[UuАаA]:|>|<)\s*")
_NONWORD = re.compile(r"[^\w]+", re.UNICODE)
_WS = re.compile(r"\s+")
_INCOMPLETE = ("did not complete", "this turn did not complete", "обрезано")


def _nkey(text: str) -> str:
    """Ключ «того же самого факта» независимо от обёртки.

    Один и тот же диалог приходит как «U: текст», «> текст» и с вкраплениями
    «[ran tool: ...]» — без нормализации это три разных записи. Если после
    чистки не осталось ни слова, ключ делается уникальным, иначе весь мусор
    схлопнется в одну группу.
    """
    t = _TOOL_RX.sub(" ", text or "")
    t = _ROLE_RX.sub("", t).lower().replace("ё", "е")
    bare = _NONWORD.sub("", t)
    if not bare:
        return "e" + hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:23]
    return hashlib.sha1(bare.encode("utf-8")).hexdigest()[:24]


def _residue(text: str) -> str:
    return _WS.sub(" ", _TOOL_RX.sub(" ", text or "")).strip()


def _garbage(text: str, kind: str) -> str:
    """Причина не сохранять запись. Пустая строка — запись годная."""
    resid = _residue(text)
    low = resid.lower()
    if not resid:
        return "пусто"
    if len(resid) < 80 and _TOOL_RX.search(text or ""):
        return "только вызовы тулов"
    if len(resid) < 40 and kind not in ("fact", "constraint"):
        return "слишком коротко"
    if len(resid) < 300 and any(m in low for m in _INCOMPLETE):
        return "оборванный ход"
    return ""


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


RERANK_URL = os.environ.get("CONTEXTD_RERANK_URL", "http://127.0.0.1:8083/rerank")
FTS_W = float(os.environ.get("CONTEXTD_FTS_W", "0.3"))
VEC_W = float(os.environ.get("CONTEXTD_VEC_W", "1.0"))
RECENCY_W = float(os.environ.get("CONTEXTD_RECENCY_W", "0.0"))
RECENCY_DAYS = float(os.environ.get("CONTEXTD_RECENCY_DAYS", "14"))
CHAT_W = float(os.environ.get("CONTEXTD_CHAT_W", "1.0"))


def _age_days(fact: dict) -> float:
    """Возраст записи в днях. Битые даты считаем свежими, чтобы не наказывать их дважды."""
    ts = fact.get("created_at") or fact.get("updated_at") or ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def _final_score(pair: tuple[dict, float], recency_w: float | None = None,
                 days: float | None = None, chat_w: float = 1.0) -> float:
    """RRF, взвешенный приоритетом. Вес 0.5–1.5, чтобы важное всплывало,
    но не переворачивало релевантность.

    Свежесть — множитель до 1+recency_w с экспоненциальным спадом: запись
    недельной давности получает почти полный буст, месячной — около трети.
    По умолчанию выключена (recency_w=0), включается параметром recall.
    """
    fact, rrf = pair
    prio = fact.get("priority")
    if prio is None:
        prio = PRIORITY_DEFAULT
    weight = 0.5 + max(0, min(100, int(prio))) / 100.0
    if str(fact.get("kind", "")) == "chat" and chat_w != 1.0:
        weight *= chat_w
    w = RECENCY_W if recency_w is None else recency_w
    if w > 0:
        hl = RECENCY_DAYS if days is None else days
        weight *= 1.0 + w * math.exp(-_age_days(fact) / max(1.0, hl))
    return rrf * weight


def _priority_for(kind: str, pinned: bool) -> int:
    if pinned:
        return PRIORITY_MAX
    return PRIORITY_BY_KIND.get(kind, PRIORITY_DEFAULT)


def _migrate(con: sqlite3.Connection) -> None:
    """Доводит рабочую базу до текущей схемы. Идемпотентно, на каждом старте."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(facts)")}
    if "priority" not in cols:
        con.execute("ALTER TABLE facts ADD COLUMN priority INTEGER NOT NULL DEFAULT 50")
        for kind, prio in PRIORITY_BY_KIND.items():
            con.execute("UPDATE facts SET priority=? WHERE kind=?", (prio, kind))
        con.commit()
        print("contextd: миграция — добавлен priority", flush=True)
    con.execute("CREATE INDEX IF NOT EXISTS ix_facts_priority ON facts(priority DESC)")
    con.commit()
    missing = con.execute(
        "SELECT count(*) FROM facts f LEFT JOIN fact_keys k ON k.fact_id=f.id "
        "WHERE k.fact_id IS NULL"
    ).fetchone()[0]
    if missing:
        rows = con.execute(
            "SELECT f.id, f.text, f.kind FROM facts f "
            "LEFT JOIN fact_keys k ON k.fact_id=f.id WHERE k.fact_id IS NULL"
        ).fetchall()
        con.executemany(
            "INSERT OR REPLACE INTO fact_keys(fact_id,nkey,kind) VALUES(?,?,?)",
            [(r["id"], _nkey(r["text"]), r["kind"]) for r in rows],
        )
        con.commit()
        print(f"contextd: миграция — ключи для {missing} фактов", flush=True)
    con.execute("UPDATE facts SET priority=? WHERE pinned=1 AND priority<?", (PRIORITY_MAX, PRIORITY_MAX))
    orphan = con.execute(
        "SELECT count(*) FROM fact_keys WHERE fact_id NOT IN (SELECT id FROM facts)"
    ).fetchone()[0]
    if orphan:
        con.execute("DELETE FROM fact_keys WHERE fact_id NOT IN (SELECT id FROM facts)")
        print(f"contextd: миграция — убрано {orphan} ключей от удалённых фактов", flush=True)
    con.commit()


def init_db() -> None:
    global _vec_ready, _q_ready
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = _conn()
    con.executescript(SCHEMA)
    _migrate(con)
    try:
        import sqlite_vec

        sqlite_vec.load(con)
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fact_vec USING vec0("
            f"fact_id INTEGER PRIMARY KEY, embedding float[{EMBED_DIM}])"
        )
        _vec_ready = True
    except Exception as exc:
        print(f"contextd: sqlite-vec disabled ({exc})", flush=True)
    con.commit()
    con.close()
    _q_ready = _ensure_qdrant()
    print(f"contextd: vectors qdrant={_q_ready} sqlite_vec={_vec_ready}", flush=True)


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 3.2))


_WORD = re.compile(r"[\w\.]+", re.UNICODE)


def _shorten(text: str, limit: int) -> str:
    """Обрезает длинный факт по границе слова.

    Суть записи почти всегда в первых строках, а чанки истории чата бывают по
    600–700 символов: одна такая запись съедала половину бюджета recall и
    вытесняла остальные. Обрезка — только в выдаче, в базе текст целый.
    """
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > int(limit * 0.6):
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-—") + " …"


def _fts_match(q: str) -> str:
    toks = _WORD.findall(q)[:12]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)


async def _embed(text: str):
    if not EMBED_URL:
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(EMBED_URL, json={"model": EMBED_MODEL, "input": text})
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


def _payload(fact: dict) -> dict:
    return {
        "text": fact.get("text", ""),
        "topic": fact.get("topic", ""),
        "kind": fact.get("kind", ""),
        "tags": fact.get("tags", ""),
        "pinned": bool(fact.get("pinned", 0)),
    }


async def _rerank_scores(q: str, docs: list[str]) -> list[float] | None:
    """Кросс-энкодер bge-reranker-v2-m3 на GPU (llama.cpp Vulkan, порт 8083).

    Возвращает score в порядке docs либо None — тогда порядок RRF остаётся.
    Падение реранкера не должно ломать recall.
    """
    if not RERANK_URL or not docs:
        return None
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.post(RERANK_URL, json={"query": q, "documents": docs})
            r.raise_for_status()
            res = r.json().get("results") or []
        scores = [0.0] * len(docs)
        for item in res:
            idx = int(item.get("index", -1))
            if 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevance_score", item.get("score", 0.0)))
        return scores
    except Exception as exc:
        print(f"contextd: rerank failed ({exc})", flush=True)
        return None


def _q_upsert(fid: int, vec: list[float], payload: dict) -> None:
    try:
        with httpx.Client(timeout=10) as c:
            c.put(
                f"{QDRANT_URL}/collections/{QCOLL}/points?wait=true",
                json={"points": [{"id": int(fid), "vector": vec, "payload": payload}]},
            ).raise_for_status()
    except Exception as exc:
        print(f"contextd: qdrant upsert failed ({exc})", flush=True)


def _q_delete(fid: int) -> None:
    if not _q_ready:
        return
    try:
        with httpx.Client(timeout=10) as c:
            c.post(
                f"{QDRANT_URL}/collections/{QCOLL}/points/delete?wait=true",
                json={"points": [int(fid)]},
            ).raise_for_status()
    except Exception as exc:
        print(f"contextd: qdrant delete failed ({exc})", flush=True)


def _q_search(vec: list[float], k: int) -> list[tuple[int, float]]:
    if not _q_ready:
        return []
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"{QDRANT_URL}/collections/{QCOLL}/points/search",
                json={"vector": vec, "limit": k, "with_payload": False},
            )
            r.raise_for_status()
            return [(int(p["id"]), float(p["score"])) for p in r.json().get("result", [])]
    except Exception as exc:
        print(f"contextd: qdrant search failed ({exc})", flush=True)
        return []


def _store_vector(fid: int, vec: list[float], payload: dict | None = None) -> None:
    if _q_ready:
        _q_upsert(fid, vec, payload or {})
        return
    if not _vec_ready:
        return
    con = _conn()
    try:
        with _lock:
            con.execute("DELETE FROM fact_vec WHERE fact_id=?", (fid,))
            con.execute(
                "INSERT INTO fact_vec(fact_id, embedding) VALUES(?,?)",
                (fid, struct.pack(f"{len(vec)}f", *vec)),
            )
            con.commit()
    except Exception as exc:
        print(f"contextd: vector upsert failed ({exc})", flush=True)
    finally:
        con.close()


def require_token(request: Request) -> None:
    if not TOKEN:
        return
    supplied = request.headers.get("x-context-token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if supplied != TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


class RememberIn(BaseModel):
    text: str = Field(min_length=1)
    topic: str = "general"
    kind: str = "fact"
    tags: str = ""
    source: str = "manual"
    pinned: bool = False
    priority: int | None = None


class BulkIn(BaseModel):
    items: list[RememberIn]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="contextd", version="0.2.0", lifespan=lifespan)


async def _remember_one(item: RememberIn) -> dict:
    text = " ".join(item.text.split())
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    topic = (item.topic or "general").strip() or "general"
    kind = (item.kind or "fact").strip() or "fact"
    tags = " ".join((item.tags or "").split())
    prio = _priority_for(kind, item.pinned)
    if item.priority is not None:
        prio = max(PRIORITY_MIN, min(PRIORITY_MAX, int(item.priority)))

    why = _garbage(text, kind)
    if why:
        return {"id": None, "action": "skipped", "reason": why}

    ts = _now()
    nk = _nkey(text)
    con = _conn()
    fid = None
    action = "inserted"
    stored_text = text
    try:
        with _lock:
            # 1. тот же факт в другой обёртке («U:», «>», пробелы, [ran tool: …])
            hit = con.execute("SELECT fact_id FROM fact_keys WHERE nkey=?", (nk,)).fetchone()
            if hit:
                fid = hit["fact_id"]
                con.execute(
                    "UPDATE facts SET updated_at=?, "
                    "tags=CASE WHEN tags='' THEN ? ELSE tags END, "
                    "pinned=max(pinned,?), priority=max(priority,?) WHERE id=?",
                    (ts, tags, 1 if item.pinned else 0, prio, fid),
                )
                con.commit()
                return {"id": fid, "action": "duplicate"}

            # 2. почти-дубль: тот же смысл другими словами
            old = _near_dup(con, text, kind) if kind in NEAR_DUP_KINDS else None
            if old is not None:
                old_id, old_text, old_tags = old["id"], old["text"], old["tags"]
                merged_tags = " ".join(dict.fromkeys((old_tags + " " + tags).split()))
                keep = old_text
                if len(text) > len(old_text) and _nkey(old_text) in _nkey(text):
                    keep = text  # новый текст — расширение старого
                elif len(old_text) > len(text) and nk in _nkey(old_text):
                    keep = old_text
                con.execute(
                    "UPDATE facts SET text=?, tags=?, updated_at=?, "
                    "pinned=max(pinned,?), priority=max(priority,?) WHERE id=?",
                    (keep, merged_tags, ts, 1 if item.pinned else 0, prio, old_id),
                )
                con.execute(
                    "INSERT OR REPLACE INTO fact_keys(fact_id,nkey,kind) VALUES(?,?,?)",
                    (old_id, _nkey(keep), kind),
                )
                con.commit()
                fid, action, stored_text = old_id, "merged", keep

            if fid is None:
                cur = con.execute(
                    "INSERT INTO facts(topic,kind,text,tags,source,pinned,priority,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(topic,kind,text) DO UPDATE SET "
                    "tags=excluded.tags, source=excluded.source, "
                    "pinned=max(facts.pinned, excluded.pinned), "
                    "priority=max(facts.priority, excluded.priority), "
                    "updated_at=excluded.updated_at "
                    "RETURNING id, (created_at = ?) AS created",
                    (topic, kind, text, tags, item.source, 1 if item.pinned else 0,
                     prio, ts, ts, ts),
                )
                row = cur.fetchone()
                fid = row["id"]
                action = "inserted" if row["created"] else "updated"
                con.execute(
                    "INSERT OR REPLACE INTO fact_keys(fact_id,nkey,kind) VALUES(?,?,?)",
                    (fid, nk, kind),
                )
                con.commit()
    finally:
        con.close()

    vec = await _embed(stored_text)
    if vec is not None:
        _store_vector(
            fid,
            vec,
            {"text": stored_text, "topic": topic, "kind": kind, "tags": tags,
             "pinned": bool(item.pinned)},
        )
    return {"id": fid, "action": action, "priority": prio}


def _near_dup(con: sqlite3.Connection, text: str, kind: str,
              threshold: float = 0.9) -> sqlite3.Row | None:
    """Самый похожий факт того же kind выше порога. Кандидаты — через FTS."""
    stems = _stems(text)
    if len(stems) < 6:
        return None
    query = " OR ".join(f'"{s}"*' for s in sorted(stems)[:12])
    try:
        rows = con.execute(
            "SELECT f.id, f.text, f.tags FROM facts_fts "
            "JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND f.kind = ? AND f.text <> ? "
            "ORDER BY rank LIMIT 8",
            (query, kind, text),
        ).fetchall()
    except sqlite3.Error:
        return None
    best, best_score = None, threshold
    for r in rows:
        score = _jaccard(stems, _stems(r["text"]))
        if score >= best_score:
            best, best_score = r, score
    return best


@app.get("/health")
async def health():
    con = _conn()
    try:
        facts = con.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
        pinned = con.execute("SELECT COUNT(*) c FROM facts WHERE pinned=1").fetchone()["c"]
    finally:
        con.close()
    qpoints = None
    if _q_ready:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{QDRANT_URL}/collections/{QCOLL}")
                qpoints = r.json().get("result", {}).get("points_count")
        except Exception:
            qpoints = None
    return {
        "ok": True,
        "facts": facts,
        "pinned": pinned,
        "vectors": bool(EMBED_URL and (_q_ready or _vec_ready)),
        "qdrant": _q_ready,
        "qdrant_points": qpoints,
        "sqlite_vec": _vec_ready,
        "db": DB_PATH,
    }


@app.post("/remember", dependencies=[Depends(require_token)])
async def remember(item: RememberIn):
    res = await _remember_one(item)
    return {"ok": True, **res}


@app.post("/remember_bulk", dependencies=[Depends(require_token)])
async def remember_bulk(body: BulkIn):
    out = []
    for item in body.items:
        out.append(await _remember_one(item))
    return {"ok": True, "count": len(out), "items": out}


@app.post("/reindex", dependencies=[Depends(require_token)])
async def reindex(limit: int = Query(100000, ge=1, le=1000000)):
    con = _conn()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM facts ORDER BY id LIMIT ?", (limit,)
        )]
    finally:
        con.close()
    done = 0
    for fact in rows:
        vec = await _embed(fact["text"])
        if vec is None:
            continue
        _store_vector(fact["id"], vec, _payload(fact))
        done += 1
    return {"ok": True, "indexed": done, "total": len(rows), "backend": "qdrant" if _q_ready else "sqlite-vec"}


async def _recall_run(
    q: str = "",
    limit: int = 8,
    budget: int = DEFAULT_BUDGET,
    max_chars: int = 250,
    per_topic: int = 2,
    dedup: bool = True,
    fts_w: float = FTS_W,
    vec_w: float = VEC_W,
    recency_w: float = RECENCY_W,
    recency_days: float = RECENCY_DAYS,
    rerank: bool = False,
    rerank_n: int = 12,
    chat_w: float = CHAT_W,
) -> dict:
    """Гибридный поиск: FTS (OR по словам) + вектор, RRF с весом priority.

    Выдача укладывается в бюджет тремя ограничителями, потому что режет её не
    матч, а размер записей: чанки истории чата по 600–700 символов съедали
    половину бюджета одной строкой и вытесняли остальные.
      max_chars  — обрезка длинного текста по границе слова (база не меняется);
      per_topic  — не больше N записей одной темы, иначе весь бюджет уходит
                   в десяток кусков одного и того же разговора;
      dedup      — почти одинаковые записи (Jaccard основ ≥0.72) отбрасываются.
    Ограничения мягкие: если с ними не набралось limit, второй проход добирает
    остаток без лимита на тему.

    Дефолты 250/2 — не на глаз: сетка 4×3 на 12 запросах с проверкой попадания
    нужного факта дала 12/12 при 250 и 11/12 при 400 и 600; per_topic 2 и 3
    равны по покрытию, у 2 разнообразие тем выше (3.2 против 3.0 на выдаче).

    Тело вынесено из эндпоинта: recall_hook и скрипты зовут его напрямую, а у
    эндпоинта дефолты — объекты Query, и в арифметике они дают TypeError.
    """

    merged: dict[int, list] = {}
    con = _conn()
    try:
        match = _fts_match(q)
        if match:
            cur = con.execute(
                "SELECT f.*, bm25(facts_fts) AS score FROM facts_fts "
                "JOIN facts f ON f.id=facts_fts.rowid "
                "WHERE facts_fts MATCH ? ORDER BY score LIMIT ?",
                (match, limit * 3),
            )
        else:
            cur = con.execute(
                "SELECT * FROM facts ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (limit * 3,),
            )
        for rank, r in enumerate(cur):
            merged[r["id"]] = [dict(r), fts_w / (60 + rank)]
    finally:
        con.close()

    if q.strip():
        vec = await _embed(q)
        if vec is not None:
            hits = _q_search(vec, limit * 2) if _q_ready else []
            if not hits and _vec_ready:
                con = _conn()
                try:
                    cur = con.execute(
                        "SELECT fact_id, distance FROM fact_vec "
                        "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                        (struct.pack(f"{len(vec)}f", *vec), limit * 2),
                    )
                    hits = [(int(r["fact_id"]), 1.0 - float(r["distance"])) for r in cur]
                except Exception as exc:
                    print(f"contextd: vector search failed ({exc})", flush=True)
                finally:
                    con.close()
            for rank, (fid, _score) in enumerate(hits):
                if fid in merged:
                    merged[fid][1] += vec_w / (60 + rank)
                else:
                    con = _conn()
                    try:
                        fr = con.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
                        if fr:
                            merged[fid] = [dict(fr), vec_w / (60 + rank)]
                    finally:
                        con.close()

    def _score(pair):
        return _final_score(pair, recency_w=recency_w, days=recency_days,
                            chat_w=chat_w)

    ordered = sorted(merged.values(), key=lambda x: -_score(x))

    if rerank and q.strip() and len(ordered) > 1:
        head = ordered[:max(2, rerank_n)]
        scores = await _rerank_scores(q, [f["text"] or "" for f, _ in head])
        if scores:
            order = sorted(range(len(head)), key=lambda i: -scores[i])
            ordered = [head[i] for i in order] + ordered[len(head):]
    items, lines, used = [], [], 0
    per_topic_count: dict[str, int] = {}
    seen_stems: list[set] = []
    dropped_dup = dropped_topic = 0

    def _try_add(fact, rrf, respect_topic: bool) -> bool:
        nonlocal used, dropped_dup, dropped_topic
        if len(items) >= limit:
            return False
        topic = fact["topic"]
        if respect_topic and per_topic_count.get(topic, 0) >= per_topic:
            dropped_topic += 1
            return False
        st = _stems(fact["text"])
        if dedup and st:
            for prev in seen_stems:
                union = len(st | prev)
                if union and len(st & prev) / union >= 0.72:
                    dropped_dup += 1
                    return False
        text = _shorten(fact["text"], max_chars)
        line = f"[{topic}/{fact['kind']}] {text}"
        if fact["tags"]:
            line += "  #" + " #".join(fact["tags"].split())
        cost = _tokens(line)
        if used + cost > budget and items:
            return False
        used += cost
        per_topic_count[topic] = per_topic_count.get(topic, 0) + 1
        seen_stems.append(st)
        items.append(
            {
                "id": fact["id"],
                "topic": topic,
                "kind": fact["kind"],
                "text": text,
                "cropped": len(text) < len(fact["text"] or ""),
                "tags": fact["tags"],
                "pinned": bool(fact["pinned"]),
                "priority": fact.get("priority", PRIORITY_DEFAULT),
                "score": round(_score((fact, rrf)), 5),
            }
        )
        lines.append("- " + line)
        return True

    for fact, rrf in ordered:
        if len(items) >= limit:
            break
        _try_add(fact, rrf, True)
    if len(items) < limit:
        for fact, rrf in ordered:
            if len(items) >= limit:
                break
            if fact["id"] in {i["id"] for i in items}:
                continue
            _try_add(fact, rrf, False)

    return {
        "query": q,
        "context": "\n".join(lines),
        "items": items,
        "tokens_est": used,
        "budget": budget,
        "dropped": {"dup": dropped_dup, "topic": dropped_topic,
                    "cands": len(ordered)},
        "vectors": bool(EMBED_URL and (_q_ready or _vec_ready)),
    }


@app.get("/recall", dependencies=[Depends(require_token)])
async def recall(
    q: str = Query(""),
    limit: int = Query(8, ge=1, le=50),
    budget: int = Query(DEFAULT_BUDGET, ge=100, le=8000),
    max_chars: int = Query(250, ge=60, le=8000),
    per_topic: int = Query(2, ge=1, le=50),
    dedup: bool = Query(True),
    fts_w: float = Query(0.3, ge=0.0, le=5.0),
    vec_w: float = Query(1.0, ge=0.0, le=5.0),
    recency_w: float = Query(0.0, ge=0.0, le=2.0),
    recency_days: float = Query(14.0, ge=1.0, le=365.0),
    rerank: bool = Query(False),
    rerank_n: int = Query(12, ge=2, le=40),
    chat_w: float = Query(1.0, ge=0.0, le=1.0),
):
    return await _recall_run(q=q, limit=limit, budget=budget, max_chars=max_chars,
                             per_topic=per_topic, dedup=dedup, fts_w=fts_w, vec_w=vec_w,
                             recency_w=recency_w, recency_days=recency_days,
                             rerank=rerank, rerank_n=rerank_n, chat_w=chat_w)


# --- auto-recall для shell-хуков ------------------------------------------------

_STOP = {
    "какой", "какая", "какие", "каком", "какую", "каким", "можно", "нужно", "надо",
    "лучше", "стоит", "почему", "зачем", "когда", "сколько", "есть", "было", "будет",
    "про", "для", "там", "тут", "этот", "этом", "того", "чтобы", "также", "вообще",
    "просто", "самый", "более", "менее", "надо", "тебе", "меня", "мне", "нам",
    "which", "about", "what", "when", "where", "should", "could", "would", "please",
    "there", "their", "these", "those", "have", "from", "with", "your", "mine",
}


def _stems(text: str) -> set[str]:
    """Грубые основы слов: 4 символа для длинных слов, целиком для кодов и латиницы.

    Шесть и пять символов не ловили падежи («сопла» vs «сопло», «порту» vs «порт»),
    поэтому четыре. Коды моделей (P104, V100, PF450, B85) и латинские термины
    (PCIe, PETG) короче пяти — берутся как есть.
    """
    out: set[str] = set()
    for w in _WORD.findall((text or "").lower()):
        if w in _STOP:
            continue
        if len(w) >= 5:
            out.add(w[:4])
        elif len(w) >= 3 and (any(c.isdigit() for c in w) or w.isascii()):
            out.add(w)
    return out


class HookIn(BaseModel):
    q: str = ""
    limit: int = Field(4, ge=1, le=20)
    budget: int = Field(350, ge=100, le=2000)
    min_score: float = Field(0.015, ge=0.0, le=1.0)
    max_chars: int = Field(900, ge=100, le=4000)
    stem_boost: float = Field(0.02, ge=0.0, le=0.5)


@app.post("/recall_hook", dependencies=[Depends(require_token)])
async def recall_hook(body: HookIn):
    """Блок памяти, готовый к вставке в промпт: recall + фильтр по основам слов.

    Вызывается shell-хуком агента (pre_llm_call). contextd на любой запрос
    отдаёт хоть что-то, а RRF-скоры лежат в узкой полосе (0.015-0.035), поэтому
    решает не score, а пересечение основ слов запроса с текстом факта; score
    только поднимает в списке и отсекает совсем низ rank.
    """
    q = (body.q or "").strip()
    if len(q) < 8:
        return {"context": "", "n": 0, "reason": "short"}
    res = await _recall_run(q=q, limit=max(body.limit * 3, 8),
                            budget=body.budget * 3,
                            max_chars=min(body.max_chars, 500))
    q_stems = _stems(q)
    if not q_stems:
        return {"context": "", "n": 0, "reason": "no-stems"}

    scored = []
    for it in res["items"]:
        overlap = len(q_stems & _stems(it["text"] + " " + it["topic"]))
        if overlap == 0 or it["score"] < body.min_score:
            continue
        scored.append((it["score"] + body.stem_boost * overlap, overlap, it))
    scored.sort(key=lambda x: -x[0])

    # Режем каждую строку отдельно: бюджет блока 700 символов при записях по
    # 500 давал ровно одну строку в промпте. Четыре коротких факта полезнее
    # одного длинного куска переписки.
    per_line = max(120, body.max_chars // max(1, body.limit))
    lines: list[str] = []
    total = 0
    for _, overlap, it in scored[: body.limit]:
        line = f"- [{it['topic']}] {_shorten(it['text'], per_line)}"
        if total + len(line) > body.max_chars and lines:
            continue
        total += len(line) + 1
        lines.append(line)
    if not lines:
        return {"context": "", "n": 0, "dropped": len(res["items"])}
    print(
        f"contextd: recall_hook n={len(lines)} qlen={len(q)} topics={','.join(sorted({i['topic'] for _, _, i in scored[: len(lines)]}))}",
        flush=True,
    )
    return {
        "context": "Память contextd (найдено по запросу, может быть нерелевантно):\n"
        + "\n".join(lines),
        "n": len(lines),
        "dropped": len(res["items"]) - len(lines),
    }


@app.get("/context", dependencies=[Depends(require_token)])
async def context(
    budget: int = Query(DEFAULT_BUDGET, ge=100, le=8000),
    pinned_only: bool = False,
):
    con = _conn()
    try:
        where = "WHERE pinned=1" if pinned_only else ""
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM facts {where} ORDER BY pinned DESC, updated_at DESC LIMIT 300"
        )]
    finally:
        con.close()
    lines, used = [], 0
    for f in rows:
        line = f"- [{f['topic']}/{f['kind']}] {f['text']}"
        cost = _tokens(line)
        if used + cost > budget and lines:
            break
        used += cost
        lines.append(line)
    return {"context": "\n".join(lines), "tokens_est": used, "budget": budget}


@app.post("/pin", dependencies=[Depends(require_token)])
async def pin(id: int, pinned: bool = True):
    con = _conn()
    try:
        with _lock:
            cur = con.execute("UPDATE facts SET pinned=?, updated_at=? WHERE id=?",
                              (1 if pinned else 0, _now(), id))
            con.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="no such fact")
            fr = con.execute("SELECT * FROM facts WHERE id=?", (id,)).fetchone()
    finally:
        con.close()
    vec = await _embed(fr["text"])
    if vec is not None:
        _store_vector(id, vec, _payload(dict(fr)))
    return {"ok": True, "id": id, "pinned": pinned}


@app.delete("/forget", dependencies=[Depends(require_token)])
async def forget(id: int | None = None, topic: str | None = None):
    if id is None and not topic:
        raise HTTPException(status_code=400, detail="give id or topic")
    con = _conn()
    deleted = 0
    ids: list[int] = []
    try:
        with _lock:
            if id is not None:
                rows = con.execute("SELECT id FROM facts WHERE id=?", (id,)).fetchall()
                ids = [int(r["id"]) for r in rows]
                cur = con.execute("DELETE FROM facts WHERE id=?", (id,))
            else:
                rows = con.execute("SELECT id FROM facts WHERE topic=?", (topic,)).fetchall()
                ids = [int(r["id"]) for r in rows]
                cur = con.execute("DELETE FROM facts WHERE topic=?", (topic,))
            deleted = cur.rowcount
            con.commit()
            for fid in ids:
                try:
                    con.execute("DELETE FROM fact_vec WHERE rowid=?", (fid,))
                except Exception:
                    pass
                try:
                    con.execute("DELETE FROM fact_keys WHERE fact_id=?", (fid,))
                except Exception:
                    pass
            con.commit()
    finally:
        con.close()
    for fid in ids:
        _q_delete(fid)
    return {"ok": True, "deleted": deleted}


@app.post("/dedupe", dependencies=[Depends(require_token)])
def dedupe(apply: bool = Query(False), garbage: bool = Query(True),
           limit: int = Query(20000, ge=1, le=200000)):
    """Разовая уборка того, что уже накопилось.

    Группирует факты по нормализованному ключу (тот же текст без обёртки
    «U:»/«>» и без «[ran tool: …]») и оставляет одну запись из группы —
    самую длинную, при равенстве самую старую. С garbage=true дополнительно
    сносит записи, состоящие из вызовов тулов и оборванных ходов.
    """
    con = _conn()
    groups, victims = [], []
    try:
        groups = con.execute(
            "SELECT nkey, count(*) c, group_concat(fact_id) ids FROM fact_keys "
            "GROUP BY nkey HAVING c > 1 ORDER BY c DESC LIMIT ?", (limit,)
        ).fetchall()
        for g in groups:
            ids = [int(x) for x in str(g["ids"]).split(",") if x]
            rows = con.execute(
                f"SELECT id, length(text) l, pinned FROM facts WHERE id IN ({','.join('?'*len(ids))})",
                ids,
            ).fetchall()
            if len(rows) < 2:
                continue
            rows.sort(key=lambda r: (-(r["pinned"] or 0), -(r["l"] or 0), r["id"]))
            keep = rows[0]["id"]
            victims += [r["id"] for r in rows[1:]]
        if garbage:
            for r in con.execute("SELECT id, text, kind FROM facts"):
                if r["id"] in victims:
                    continue
                if _garbage(r["text"], r["kind"]):
                    victims.append(r["id"])
    finally:
        con.close()

    if not apply:
        return {"ok": True, "dry_run": True, "groups": len(groups),
                "would_delete": len(victims), "sample": victims[:20]}

    con = _conn()
    try:
        with _lock:
            for i in range(0, len(victims), 500):
                part = victims[i:i + 500]
                ph = ",".join("?" * len(part))
                con.execute(f"DELETE FROM facts WHERE id IN ({ph})", part)
                con.execute(f"DELETE FROM fact_keys WHERE fact_id IN ({ph})", part)
                try:
                    con.execute(f"DELETE FROM fact_vec WHERE rowid IN ({ph})", part)
                except Exception:
                    pass
                con.commit()
            con.execute("INSERT INTO facts_fts(facts_fts) VALUES('optimize')")
            con.commit()
    finally:
        con.close()
    for fid in victims:
        _q_delete(fid)
    return {"ok": True, "dry_run": False, "deleted": len(victims), "groups": len(groups)}

