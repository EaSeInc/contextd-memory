#!/usr/bin/env python3
"""Тонкий клиент автопамяти для shell-хука Hermes (pre_llm_call).

Вся логика отбора (recall, пороги, основы слов, бюджет) живёт в contextd на
srv-ai: POST /recall_hook. Здесь только stdin -> HTTP -> stdout, поэтому править
и тюнить нечего: меняешь contextd.py на сервере, клиент остаётся как есть.

Формат входа:  {"hook_event_name":"pre_llm_call","session_id":"...","extra":{"user_message":"..."}}
Формат выхода: {"context":"..."} или пусто (тогда хук ничего не добавляет).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("CONTEXTD_URL", "http://127.0.0.1:8090").rstrip("/")
ENV_FILE = os.path.join(os.path.expanduser("~"), ".hermes_srv_ai.env")
TIMEOUT = 4.0
MIN_LEN = 8
QUERY_CHARS = 400


def _token() -> str:
    tok = os.environ.get("CTXD_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("CTXD_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _user_message(payload: dict) -> str:
    extra = payload.get("extra") or {}
    msg = extra.get("user_message")
    if msg is None:
        msg = payload.get("user_message") or payload.get("prompt") or ""
    if isinstance(msg, list):
        parts = []
        for part in msg:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        msg = " ".join(parts)
    return (msg or "").strip() if isinstance(msg, str) else ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    query = _user_message(payload)
    if len(query) < MIN_LEN:
        return
    token = _token()
    if not token:
        return
    body = json.dumps({"q": query[:QUERY_CHARS]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/recall_hook",
        data=body,
        headers={"X-Context-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return  # contextd недоступен — молчим, ход не ломаем
    ctx = (data.get("context") or "").strip()
    if not ctx:
        return
    print(json.dumps({"context": ctx}, ensure_ascii=False))


if __name__ == "__main__":
    main()
