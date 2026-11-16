# contextd-memory

Память для агентов и локальных моделей на домашнем сервере: **один HTTP-сервис, одна
SQLite-база**, гибридный поиск (FTS5 + вектор), запись с дедупликацией и приоритетами,
и хук, который сам подкладывает нужные факты в начало каждого хода модели.

Главная мысль: окно модели не расширяют — **выбирают, какие 400–800 токенов в него
положить**. Всё остальное лежит в базе и всплывает, когда спросили.

```
   агент / модель                 контекст хода
  ┌──────────────┐   pre_llm_call  ┌───────────────────────────┐
  │ Hermes       │───► hook ──────►│ - [print/petg] сопло 240… │
  │ OpenCode     │      (4 мс)     │ - [infra/power] PF450 …   │
  └──────────────┘                 └───────────────────────────┘
          │  remember / recall                ▲
          ▼                                   │
  ┌───────────────────────────────────────────────────────────┐
  │ contextd :8090   FastAPI + SQLite (WAL)                    │
  │   facts ──► facts_fts (FTS5 bm25)                          │
  │     │        fact_vec (sqlite-vec, 1024d)                  │
  │     └──► RRF с весом priority ──► бюджет ──► выдача         │
  └───────────────────────────────────────────────────────────┘
          ▲                    ▲                    ▲
     llama-embed          индексаторы          ctxagent :8091
     bge-m3 :8081        (скилы, чаты)         (MCP, синк Nextcloud)
```

## Что это решает

- Агент начинает каждый разговор с нуля и через сутки повторяет одни и те же вопросы
  («какое сопло для PETG», «на каком порту WebDAV»).
- Долгая история не влезает в контекст: 4B-модель на 4 ГБ VRAM вообще не держит
  больше нескольких тысяч токенов.
- Решения и ограничения («эту карту не брать», «sudo-пароль тут не работает»)
  теряются между сессиями, хотя стоят дороже любого кода.

contextd держит их как короткие атомарные записи и отдаёт ровно тот набор, который
нужен под текущий запрос, в жёстком бюджете токенов.

## Что уже работает (замеры на боевой базе)

| Метрика | Значение | Как мерили |
|---|---|---|
| Фактов в базе | 9 212 | `select count(*) from facts` |
| Тем | 120 | `count(distinct topic)` |
| Размер базы | 177 МБ | `context.db` + WAL |
| Средняя длина записи | 615 символов | `avg(length(text))` |
| `/recall` латентность | 159 мс медиана, 166 мс p90 | 60 запросов, warm |
| Хук в промпт | 9/12 целевых запросов, 3/3 посторонних молчат | 15 прогонов `/recall_hook` |
| Средний блок в промпте | 466 символов (~145 токенов) | по тем же прогонам |
| Попадание нужного факта в `/recall` | 10/12, MRR 0.729 | gold-набор, `bench/recall_bench.py` |
| Новых дублей при записи | 0 групп на 9 212 фактов | `POST /dedupe?apply=false` |

Оговорки честные: набор для бенча — 12 запросов, то есть **±1 кейс это шум**. Числа
показывают порядок, а не «качество 83 %». Методика и сырые прогоны — `docs/benchmarks.md`.

## Из чего состоит

| Слой | Файл | Роль |
|---|---|---|
| Сервис | `server/contextd.py` | FastAPI: запись, поиск, дедуп, миграции, 10 эндпоинтов |
| MCP | `server/contextd_mcp.py` | память как инструменты для агента (stdio JSON-RPC) |
| Извлечение | `server/session_ingest.py` | транскрипт → атомарные факты через локальную модель |
| Хук | `clients/contextd_hook.py` | `stdin → /recall_hook → stdout`, вся логика на сервере |
| CLI | `clients/ctx.sh` | health / recall / remember / reindex |
| Индексатор скилов | `ingest/skills_index.py` | 89 скилов, 5 870 чанков, тема `skill/<имя>` |
| Индексатор чатов | `ingest/sessions_index.py` | история сессий, тема `chat/<YYYY-MM>` |
| Деплой | `deploy/` | systemd-юнит, пример env, установка |
| Замер | `bench/recall_bench.py` | gold-набор, MRR, латентность, вклад каналов |

Подробности: [архитектура](docs/architecture.md) ·
[API](docs/api.md) · [хранилище](docs/storage.md) · [поиск](docs/retrieval.md) ·
[запись](docs/writing.md) · [наполнение](docs/ingestion.md) ·
[замеры](docs/benchmarks.md) · [эксплуатация](docs/ops.md) · [грабли](docs/design-notes.md)

## Быстрый старт

Нужен Python 3.11+, `sqlite-vec` и любой OpenAI-совместимый эмбеддер (`bge-m3`, 1024
измерения). Векторная часть необязательна: без эмбеддера сервис работает на одном FTS.

```bash
git clone https://github.com/EaSeInc/contextd-memory.git
cd contextd-memory
python -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn httpx pydantic sqlite-vec

export CONTEXTD_DB=$PWD/context.db
export CONTEXTD_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
export CONTEXTD_EMBED_URL=http://127.0.0.1:8081/v1/embeddings   # опционально
uvicorn --app-dir server contextd:app --port 8090
```

Проверка:

```bash
curl -s localhost:8090/health
curl -s -X POST localhost:8090/remember \
  -H "X-Context-Token: $CONTEXTD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"PETG печатать: сопло 240, стол 80","topic":"print/petg","kind":"settings"}'
curl -s -G localhost:8090/recall -H "X-Context-Token: $CONTEXTD_TOKEN" \
  --data-urlencode 'q=какое сопло для петг'
```

Ответ `/recall`:

```json
{"query":"какое сопло для петг","context":"- [print/petg] PETG печатать: сопло 240, стол 80",
 "items":[{"id":1,"topic":"print/petg","kind":"settings","text":"…","priority":65,"score":0.032}],
 "tokens_est":18,"budget":800,"dropped":{"dup":0,"topic":0,"cands":1},"vectors":true}
```

## Установка на сервер

```bash
sudo install -d /opt/contextd /srv/contextd /etc/contextd
sudo install -m 644 server/contextd.py /opt/contextd/
sudo install -m 600 deploy/contextd.env.example /etc/contextd/contextd.env   # вписать токен
sudo install -m 644 deploy/contextd.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now contextd
```

Питон-окружение, эмбеддер и бэкапы — `docs/ops.md`.

## Чего здесь нет

- **Векторной базы отдельным сервисом.** Qdrant поддерживается кодом, но на этом
  сервере не поднят: `sqlite-vec` в том же файле быстрее и не требует демона.
  `"qdrant": false` в `/health` — норма, а не поломка.
- **Реранка в горячем пути.** Он есть (`rerank=true`), но по умолчанию выключен:
  переставляет только top-12, а промахи рождаются раньше, на отборе.
- **Генеративной модели.** Извлечение фактов моделью — необязательный шаг, поиск и
  запись работают без неё.

## Лицензия

MIT. Секретов, базы и персональных данных в репозитории нет: токен приходит только
из окружения, а база — это отдельный файл на сервере.
