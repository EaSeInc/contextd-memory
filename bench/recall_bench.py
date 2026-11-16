#!/usr/bin/env python3
"""Замер /recall по gold-набору: попадание нужного факта, MRR, латентность.

Зачем: считать «сколько записей пришло» бессмысленно — пять кусков одного
разговора выглядят так же хорошо, как пять разных фактов. Поэтому каждый кейс
несёт подстроку (`needle`), которая обязана появиться в выдаче.

    export CONTEXTD_URL=http://127.0.0.1:8090
    export CONTEXTD_TOKEN=...
    python bench/recall_bench.py --gold bench/gold-example.json
    python bench/recall_bench.py --gold bench/gold-example.json --compare
    python bench/recall_bench.py --gold bench/gold-example.json --noise "погода в лионе"

`--compare` прогоняет четыре конфигурации весов и печатает таблицу — так проверяется,
что дефолт fts_w=0.3 / vec_w=1.0 не хуже альтернатив на *вашей* базе.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

CONFIGS = [
    ("default", {"fts_w": 0.3, "vec_w": 1.0}),
    ("equal", {"fts_w": 1.0, "vec_w": 1.0}),
    ("fts-only", {"fts_w": 1.0, "vec_w": 0.0}),
    ("vec-only", {"fts_w": 0.0, "vec_w": 1.0}),
]


def token_from_env_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("CONTEXTD_TOKEN=") or line.startswith("CTXD_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def recall(base: str, token: str, q: str, extra: dict) -> tuple[dict, float]:
    params = {"q": q, **extra}
    url = f"{base}/recall?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Context-Token": token})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    return body, (time.perf_counter() - t0) * 1000


def rank_of(body: dict, needle: str) -> int:
    """Позиция первой записи, где встречается игла (1-based), иначе 0."""
    for i, it in enumerate(body.get("items") or [], 1):
        hay = (it.get("text", "") + " " + it.get("topic", "")).lower()
        if needle.lower() in hay:
            return i
    return 0


def run(base: str, token: str, gold: list[dict], extra: dict, warm: bool = True) -> dict:
    if warm:  # первый вызов всегда медленный: прогрев модели эмбеддингов
        try:
            recall(base, token, "прогрев", {"limit": 1, "budget": 100})
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    rows, lat = [], []
    for case in gold:
        body, ms = recall(base, token, case["q"], {"limit": 5, "budget": 400, **extra})
        lat.append(ms)
        rows.append({"q": case["q"], "needle": case["needle"], "ms": round(ms),
                     "rank": rank_of(body, case["needle"]),
                     "top": (body.get("items") or [{}])[0].get("text", "")[:60],
                     "cands": (body.get("dropped") or {}).get("cands")})
    ranks = [r["rank"] for r in rows]
    hits = sum(1 for r in ranks if r)
    mrr = sum(1 / r for r in ranks if r) / len(ranks) if ranks else 0.0
    return {"rows": rows, "hits": hits, "n": len(rows), "mrr": round(mrr, 3),
            "med": round(statistics.median(lat)), "p90": round(_p90(lat))}


def _p90(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.9 * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("CONTEXTD_URL", "http://127.0.0.1:8090"))
    ap.add_argument("--token", default=os.environ.get("CONTEXTD_TOKEN", ""))
    ap.add_argument("--env-file", default=os.path.expanduser("~/.hermes_srv_ai.env"))
    ap.add_argument("--gold", required=True, help="JSON: [{q, needle}, …]")
    ap.add_argument("--compare", action="store_true", help="прогнать 4 конфигурации весов")
    ap.add_argument("--noise", action="append", default=[],
                    help="посторонний запрос: должно быть пусто/низкий скор (можно несколько)")
    ap.add_argument("--json", metavar="PATH", help="сохранить сырые результаты")
    args = ap.parse_args()

    token = args.token or token_from_env_file(args.env_file)
    if not token:
        print("нет токена: --token, CONTEXTD_TOKEN или CONTEXTD_TOKEN= в --env-file", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")

    with open(args.gold, encoding="utf-8") as fh:
        gold = json.load(fh)
    if not gold:
        print("пустой gold-набор", file=sys.stderr)
        return 2

    out = {"url": base, "n": len(gold), "runs": {}}
    configs = CONFIGS if args.compare else [("default", {})]
    print(f"{'config':<10} {'hits':>6} {'MRR':>6} {'med,ms':>7} {'p90,ms':>7}")
    for name, extra in configs:
        res = run(base, token, gold, extra)
        if "error" in res:
            print(f"{name:<10} ошибка: {res['error']}")
            continue
        out["runs"][name] = res
        print(f"{name:<10} {res['hits']}/{res['n']:<4} {res['mrr']:>6} {res['med']:>7} {res['p90']:>7}")

    for name, res in out["runs"].items():
        if not args.compare:
            print(f"\n[{name}] построчно (rank — позиция иглы, 0 = не нашлось):")
            for r in res["rows"]:
                mark = "ok " if r["rank"] else "MISS"
                print(f"  {mark} rank={r['rank']} {r['ms']:>5}ms cands={r['cands']!s:<3} "
                      f"{r['q'][:34]:<34} → {r['needle']}")

    for q in args.noise:
        body, ms = recall(base, token, q, {"limit": 5, "budget": 400})
        items = body.get("items") or []
        top = items[0]["score"] if items else None
        out.setdefault("noise", []).append({"q": q, "items": len(items), "top_score": top, "ms": round(ms)})
        print(f"\nпосторонний: {q!r} → {len(items)} записей, топ-скор {top}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print(f"\nсырые результаты: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
