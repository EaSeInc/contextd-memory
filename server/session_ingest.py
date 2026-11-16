#!/usr/bin/env python3
"""session_ingest - turn agent transcripts into atomic facts in contextd.

Drops JSON/Markdown/text transcripts into an inbox, asks the local model to
extract only durable facts, and stores them via contextd. Falls back to a
conservative line filter when no model is reachable.

Usage:
  session_ingest.py --once            process the inbox and exit (timer)
  session_ingest.py --file PATH       process one file
  session_ingest.py --stdin           read a transcript from stdin
  session_ingest.py --once --dry-run  show what would be stored

Environment:
  CONTEXTD_URL, CONTEXTD_TOKEN
  INGEST_LLM_URL    default http://127.0.0.1:8080/v1/chat/completions
  INGEST_LLM_KEY    API key for llama-server
  INGEST_LLM_MODEL  default qwen3.5-4b
  INGEST_INBOX      default /srv/contextd/inbox
  INGEST_DONE       default /srv/contextd/processed
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = os.environ.get("INGEST_ENV", "/etc/contextd/ingest.env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

CTX_URL = os.environ.get("CONTEXTD_URL", "http://127.0.0.1:8090").rstrip("/")
CTX_TOKEN = os.environ.get("CONTEXTD_TOKEN", "").strip()
LLM_URL = os.environ.get("INGEST_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_KEY = os.environ.get("INGEST_LLM_KEY", "").strip()
LLM_MODEL = os.environ.get("INGEST_LLM_MODEL", "qwen3.5-4b")
INBOX = os.environ.get("INGEST_INBOX", "/srv/contextd/inbox")
DONE = os.environ.get("INGEST_DONE", "/srv/contextd/processed")
MAX_CHARS = int(os.environ.get("INGEST_MAX_CHARS", "12000"))

PROMPT = """Ты — экстрактор фактов. Ниже транскрипт работы над проектом.
Выдели только то, что стоит помнить между сессиями.

Правила:
- один факт = одно короткое утверждение, до 200 символов;
- только конкретика: решения, ограничения, размеры, настройки, пути, названия;
- не включай болтовню, приветствия и рассуждения модели;
- не более 10 фактов;
- пиши на языке оригинала.

Ответ — ТОЛЬКО JSON-массив без пояснений:
[{"text": "...", "topic": "...", "kind": "fact|decision|constraint|settings|path", "tags": "..."}]
"""


def _http(method: str, url: str, body=None, headers=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_transcript(path: str | None) -> str:
    raw = sys.stdin.read() if path is None else open(path, encoding="utf-8", errors="replace").read()
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except Exception:
            return raw[:MAX_CHARS]
        messages = data.get("messages") if isinstance(data, dict) else data
        if isinstance(messages, list):
            parts = []
            for m in messages:
                if not isinstance(m, dict):
                    continue
                role = m.get("role", "?")
                content = m.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if content:
                    parts.append(f"{role}: {content}")
            return "\n".join(parts)[:MAX_CHARS]
    return raw[:MAX_CHARS]


def extract_with_llm(text: str) -> list[dict]:
    body = {
        "model": LLM_MODEL,
        "temperature": 0.1,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text},
        ],
    }
    headers = {"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {}
    res = _http("POST", LLM_URL, body=body, headers=headers, timeout=180)
    content = res["choices"][0]["message"].get("content") or ""
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    start, end = content.find("["), content.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("model did not return a JSON array")
    items = json.loads(content[start:end + 1])
    if not isinstance(items, list):
        raise ValueError("model returned a non-array")
    return [i for i in items if isinstance(i, dict) and i.get("text")]


def extract_heuristic(text: str) -> list[dict]:
    """Conservative fallback: keep lines that look like decisions or settings."""
    out, seen = [], set()
    for line in text.splitlines():
        line = line.strip(" -•\t")
        line = re.sub(r"^(user|assistant|system)\s*:\s*", "", line, flags=re.I)
        if len(line) < 15 or len(line) > 220:
            continue
        if not re.search(r"[=:\d]|[А-ЯA-Z]{2,}", line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": line, "topic": "session/auto", "kind": "fact", "tags": ""})
        if len(out) >= 10:
            break
    return out


def store(items: list[dict], dry: bool) -> int:
    if not items:
        return 0
    payload = {
        "items": [
            {
                "text": i["text"],
                "topic": i.get("topic") or "session/auto",
                "kind": i.get("kind") or "fact",
                "tags": i.get("tags") or "",
                "source": "session",
            }
            for i in items
        ]
    }
    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return len(items)
    headers = {"X-Context-Token": CTX_TOKEN} if CTX_TOKEN else {}
    res = _http("POST", f"{CTX_URL}/remember_bulk", body=payload, headers=headers, timeout=60)
    return res.get("count", 0)


def process(text: str, dry: bool, allow_heuristic: bool) -> int:
    try:
        items = extract_with_llm(text)
        mode = "llm"
    except Exception as exc:
        if not allow_heuristic:
            print(f"ingest: llm extraction failed ({exc})", file=sys.stderr)
            return -1
        print(f"ingest: llm unavailable ({exc}); using heuristic", file=sys.stderr)
        items = extract_heuristic(text)
        mode = "heuristic"
    n = store(items, dry)
    print(f"ingest: {n} fact(s) via {mode}", file=sys.stderr)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="process the inbox and exit")
    ap.add_argument("--file")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--require-llm", action="store_true")
    args = ap.parse_args()

    if not (args.once or args.file or args.stdin):
        ap.print_help()
        return 1

    allow_heuristic = not args.require_llm
    total = 0

    if args.stdin:
        return 0 if process(read_transcript(None), args.dry_run, allow_heuristic) >= 0 else 1

    if args.file:
        text = read_transcript(args.file)
        total += process(text, args.dry_run, allow_heuristic)
        return 0

    os.makedirs(DONE, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    for name in sorted(os.listdir(INBOX)):
        path = os.path.join(INBOX, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        text = read_transcript(path)
        ok = 0
        if text.strip():
            ok = process(text, args.dry_run, allow_heuristic)
            total += max(ok, 0)
        if not args.dry_run and ok >= 0:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            os.replace(path, os.path.join(DONE, f"{stamp}-{name}"))
    print(f"ingest: total {total}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
