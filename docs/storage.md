# Хранилище

Один файл: `/srv/contextd/context.db` в режиме WAL. Бэкап — копия файла (вместе с
`-wal`, либо через `sqlite3 .backup`). На боевой базе на 16.11.2026: 9 212 фактов,
177 МБ, средняя длина записи 615 символов.

## Схема

```sql
CREATE TABLE facts(
  id INTEGER PRIMARY KEY,
  topic TEXT NOT NULL DEFAULT 'general',
  kind  TEXT NOT NULL DEFAULT 'fact',
  text  TEXT NOT NULL,
  tags  TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  pinned INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 50,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_facts ON facts(topic, kind, text);   -- точный дубль невозможен
CREATE INDEX ix_facts_topic ON facts(topic);
CREATE INDEX ix_facts_priority ON facts(priority DESC);

-- нормализованный ключ «того же самого факта»
CREATE TABLE fact_keys(
  fact_id INTEGER PRIMARY KEY,
  nkey TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_fact_keys ON fact_keys(nkey);

-- лексический поиск; content= значит «индексируй существующую таблицу»
CREATE VIRTUAL TABLE facts_fts USING fts5(
  text, topic, tags,
  content='facts', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

-- векторы: rowid = facts.id, косинус считается в SQLite
CREATE VIRTUAL TABLE fact_vec USING vec0(
  fact_id INTEGER PRIMARY KEY, embedding float[1024]
);
```

Синхронизация FTS с основной таблицей — триггеры `facts_ai` / `facts_au` / `facts_ad`.
Ошибка в них означает тихую рассинхронизацию: `count(*)` у `facts` и `facts_fts`
обязан совпадать, иначе поиск врёт.

## Почему так

- **`priority` в `facts`, не отдельной таблицей.** Приоритет нужен на каждом
  ранжировании; джойн ради одного числа — лишний.
- **`fact_keys` вместо индекса по выражению.** Ключ считается в Python (регулярки
  на `[ran tool: …]` в SQLite не сделать), поэтому лежит рядом и индексируется
  обычным индексом. Заодно даёт быстрый ответ «этот факт уже есть».
- **`vec0` отдельной виртуальной таблицей.** Векторы не мешают `VACUUM` и не
  раздувают основную таблицу; потерялись — `/reindex` досчитает.
- **`savepoints` нет, коммит на каждую запись.** Запись редкая (десятки в час),
  транзакции на пачку — оверинжиниринг.

## Миграции

`init_db()` на старте: `executescript(SCHEMA)` и затем `_migrate()`. Правило,
которое стоит соблюдать, если добавляешь колонку:

1. `SCHEMA` дополняется **только** тем, что безопасно выполнить повторно на старой
   базе: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`.
2. Новая колонка — `ALTER TABLE` внутри `_migrate()`, и **индекс по ней создаётся
   после** `ALTER TABLE`. Если положить `CREATE INDEX` в `SCHEMA`, он выполнится
   до миграции и упадёт с `no such column`, а сервис уйдёт в
   `Start request repeated too quickly`.
3. После миграции — проверка инвариантов: ключ для каждого факта есть, осиротевших
   ключей нет, `pinned` не ниже 100.

`_migrate()` идемпотентна и гоняется на каждом старте — это дешевле, чем таблица
версий схемы.

## Инварианты

```sql
select (select count(*) from facts) as facts,
       (select count(*) from facts_fts) as fts,
       (select count(*) from fact_keys) as keys;
-- три числа равны

select count(*) from fact_keys where fact_id not in (select id from facts);
-- 0

select count(*) from facts where priority not between 1 and 100;
-- 0
```

Про `fact_vec` — отдельный разговор.

## Известная утечка: векторы удалённых фактов

`vec0` не имеет колонки `fact_id` для `DELETE` — удалять надо по `rowid`. В
`/dedupe` и в старых миграциях удаление писалось как `DELETE FROM fact_vec WHERE
rowid IN (…)`, где подставлялись id **фактов**, а на боевой базе `rowid` вектора и
`fact_id` давно разошлись. Внутри `try/except: pass` это тихая утечка: факт удалён,
вектор остался.

Замер на 16.11.2026:

```
facts: 9 212      fact_vec: 31 388      «сирот»: 22 177
```

То есть в таблице векторов лежит втрое больше строк, чем фактов. На выдачу это
не влияет (поиск возвращает `fact_id`, которого уже нет в `facts`, такой хит
отбрасывается), но база растёт и `k = limit*2` может тратиться на мёртвые строки.

Лечение:

```sql
-- удалять по rowid, который равен fact_id только при чистой пересборке
DELETE FROM fact_vec WHERE fact_id NOT IN (SELECT id FROM facts);   -- не сработает
```

Правильный путь — пересборка таблицы (`INSERT INTO fact_vec_new … SELECT` из живых
фактов) или аккуратное удаление по `rowid` из `fact_vec_rowids`. В репозитории это
описано как **известное ограничение**, код не менялся: на текущих объёмах выдача
не страдает, а массовую уборку без бэкапа делать нельзя.

## Размеры и рост

| Часть | Объём |
|---|---|
| `facts` (тексты, 9 212 шт.) | ~15 МБ |
| `facts_fts` | ~10 МБ |
| `fact_vec` (31 388 × 1024 float32) | ~128 МБ |
| служебное SQLite | остальное |

Векторы — самая тяжёлая часть. 22 177 сирот — это ~90 МБ, которые можно вернуть
пересборкой.

## Бэкап и восстановление

```bash
# горячий бэкап без остановки сервиса
sqlite3 /srv/contextd/context.db ".backup /srv/contextd/backups/ctx-$(date +%F).db"
# восстановление
systemctl stop contextd
cp /srv/contextd/backups/ctx-2026-11-16.db /srv/contextd/context.db
systemctl start contextd
```

Копирование файла на живой базе без `.backup` тоже обычно работает (WAL), но
`.backup` не даёт поймать половину транзакции. Перед `/dedupe?apply=true`,
`/reindex` и любым `UPDATE` пачкой — бэкап обязателен.
