# Эксплуатация

## Установка

```bash
sudo install -d /opt/contextd /srv/contextd /etc/contextd
python3 -m venv /opt/contextd/.venv
/opt/contextd/.venv/bin/pip install fastapi uvicorn httpx pydantic sqlite-vec
sudo install -m 644 server/contextd.py /opt/contextd/
sudo install -m 755 server/session_ingest.py /opt/contextd/
```

`/etc/contextd/contextd.env` (права `640`, владелец root + сервисный пользователь):

```ini
CONTEXTD_DB=/srv/contextd/context.db
CONTEXTD_TOKEN=<32+ случайных символа>
CONTEXTD_BUDGET=800
CONTEXTD_EMBED_URL=http://127.0.0.1:8081/v1/embeddings
CONTEXTD_EMBED_MODEL=bge-m3
CONTEXTD_EMBED_DIM=1024
CONTEXTD_FTS_W=0.3
CONTEXTD_VEC_W=1.0
# CONTEXTD_RERANK_URL=http://127.0.0.1:8083/rerank
# CONTEXTD_QDRANT_URL=http://127.0.0.1:6333
```

Токен генерируется один раз:

```bash
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
```

Юнит `deploy/contextd.service`: `Restart=always`, `StartLimitBurst=5`,
`EnvironmentFile=/etc/contextd/contextd.env`, uvicorn с `--no-access-log`
(доступ пишется в journal и там же теряется в шуме).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now contextd
systemctl status contextd
```

## Перезапуск без sudo

Юнит работает от имени `user`, но `systemctl restart` требует root. Если root под
рукой только через пароль, работает такой приём:

```bash
pid=$(systemctl show -p MainPID --value contextd)
kill "$pid"          # Restart=always поднимет новую версию через ~3 с
```

Не `pkill -f contextd`: шаблон совпадёт с собственной командой ssh и убьёт сессию.

## Диагностика

```bash
curl -s localhost:8090/health | jq
journalctl -u contextd -n 50 --no-pager
journalctl -u contextd | grep recall_hook | tail -5   # n, длина запроса, темы
```

`/health` отвечает даже когда векторная часть мертва:

| Поле | Норма | Что значит отклонение |
|---|---|---|
| `vectors` | `true` | `false` — нет `CONTEXTD_EMBED_URL` или ни один бэкенд не поднялся |
| `sqlite_vec` | `true` | `false` — `sqlite-vec` не установлен: поиск только лексикой |
| `qdrant` | `false` | `true` — включён внешний бэкенд (не обязателен) |
| `facts` | растёт | падает — кто-то зовёт `/forget` (индексаторы делают это штатно) |

Журнал `recall_hook` пишет `n`, длину запроса и темы, без текста — этого хватает,
чтобы понять, сработал хук или нет.

## Тюнинг выдачи

Порядок, который избавляет от гадания:

1. Снять baseline: `bench/recall_bench.py --gold bench/gold.json` (попал/не попал).
2. Менять по одному: `fts_w`, `max_chars`, `per_topic`, `budget`.
3. Сравнить MRR и gold-hit, а не «на глаз».
4. Дефолты менять только если улучшение держится на двух наборах запросов.

Известные безопасные диапазоны: `fts_w` 0.15–0.5 (ниже 0.15 роняет gold-кейс),
`max_chars` 200–400, `per_topic` 2–3, `budget` 350–800.

## Схема с эмбеддером

```bash
llama-server -m /srv/models/bge-m3-q8.gguf --embeddings -c 8192 --port 8081
```

`CONTEXTD_EMBED_URL=http://127.0.0.1:8081/v1/embeddings`, `CONTEXTD_EMBED_DIM=1024`.
Модель принимает **не больше 512 токенов** на вход — это причина, по которой
индексаторы режут чанки до 700 символов.

На CPU (i3-4170T) один эмбеддинг ~140 мс, то есть `/recall` упирается в него.
Если задержка критична — кешировать эмбеддинг запроса или держать модель на GPU
с Vulkan.

## Бэкап

```bash
sqlite3 /srv/contextd/context.db ".backup /srv/contextd/backups/ctx-$(date +%F).db"
find /srv/contextd/backups -name 'ctx-*.db' -mtime +14 -delete
```

`.backup`, а не `cp`: на живой базе с WAL копия файла может поймать половину
транзакции. Восстановление — остановить сервис, положить файл, запустить.

## Обслуживание

```sql
-- инварианты
select (select count(*) from facts) facts,
       (select count(*) from facts_fts) fts,
       (select count(*) from fact_keys) keys;

-- мусор, который почему-то прошёл
select id, kind, length(text), substr(text,1,60) from facts
 where length(text) < 40 and kind not in ('fact','constraint') limit 20;
```

`VACUUM` — только после массовых удалений и **на остановленном сервисе**, иначе
блокировка на минуты. Перед `VACUUM` — бэкап: база на 177 МБ требует свободного
места под копию.

## Рост

| Что растёт | Оценка |
|---|---|
| `facts` + `facts_fts` | ~2.5 КБ на запись |
| `fact_vec` | ~4 КБ на запись (1024 float32) |
| 100 тыс. записей | ~400 МБ фактов + 400 МБ векторов |

До сотен тысяч записей SQLite-подход жив: FTS5 и `vec0` работают по индексу.
Дальше — вопрос не скорости, а качества: 100 тыс. чанков чата утопят ручные факты,
и понадобится либо более жёсткая тематизация, либо отдельная коллекция.

## Что делать, когда не отвечает

1. `systemctl is-active contextd` — `failed` или `activating`.
2. `journalctl -u contextd -n 40` — чаще всего `no such column` после миграции или
   `Address already in use`.
3. `ss -ltnp | grep :8090` — слушает ли порт.
4. `sqlite3 /srv/contextd/context.db 'select 1'` — открывается ли база вообще
   (битый WAL лечится `pragma wal_checkpoint(TRUNCATE)`).
5. Если юнит в `Start request repeated too quickly`: `systemctl reset-failed
   contextd` и только потом `start` — без сброса счётчика он не поднимется даже
   после исправления.
