# AzureDevOpsPulse — описание проекта

Python-приложение на Flask для отображения статистики разработчиков из локального TFS (Team Foundation Server / Azure DevOps Server). Подключается к TFS REST API, синхронизирует данные в SQLite и отображает дашборды через веб-интерфейс.

## Стек

- **Python 3.11**, Flask 3.0, APScheduler 3.10, requests, PyYAML, python-dotenv
- **БД:** SQLite (`data/pulse.db`), WAL-режим для параллельных чтений
- **Деплой:** Docker / docker-compose, порт **14731**

## Запуск

```bash
python app.py            # напрямую
docker compose up        # в контейнере
```

Перед запуском: заполнить `.env` на основе `.env.example` (TFS_URL, PORT, LOG_LEVEL).  
PAT (Personal Access Token) для TFS хранится **в БД** (таблица `app_settings`), задаётся через `/settings` в UI.

## Структура файлов

```
app.py          — Flask-приложение, все HTTP-маршруты
collector.py    — TFS REST API клиент + логика синхронизации
config.py       — класс Config (config.yaml + env vars)
database.py     — SQLite-слой (все запросы и upsert-методы)
logger.py       — настройка логирования (RotatingFileHandler)
scheduler.py    — фоновая синхронизация (APScheduler)
check_user.py   — CLI-диагностика пользователей в БД

config.yaml     — основной конфиг (TFS URL, интервал синхронизации и т.д.)
.env            — переменные окружения (не коммитить)
data/pulse.db   — SQLite-база (данные, не коммитить)
logs/app.log    — лог-файл
templates/      — Jinja2-шаблоны (index, developer, repos, compare, settings)
static/         — CSS и JS (style.css, app.js, pulse.js)
```

## Модули подробно

### `app.py` — маршруты Flask
Инициализирует Config, Database, логгеры, 60-секундный in-memory кэш. При старте (если настроен PAT) запускает планировщик.

**Страницы:** `/`, `/developer/<email>`, `/repos`, `/settings`

**API:**
- `GET /api/health` — healthcheck
- `GET|POST /api/settings` — TFS URL, collection, PAT
- `GET /api/test-connection` — проверка соединения с TFS
- `GET /api/overview` — сводная статистика для дашборда
- `GET /api/developers` — таблица всех разработчиков (параллельные запросы через ThreadPoolExecutor)
- `GET /api/developer/<email>` — детальная статистика одного разработчика
- `GET /api/compare` — сравнение до 5 разработчиков
- `GET /api/repositories` + `/api/repository/<repo_id>` — репозитории и их статистика
- `GET /api/projects` + `POST /api/projects/selected` — проекты TFS (живой запрос)
- `GET|POST /api/employees/selected` — фильтр по сотрудникам
- `GET|POST /api/teams`, `PUT|DELETE /api/teams/<id>` — управление командами
- `GET|POST /api/aliases`, `DELETE /api/aliases/<email>` — псевдонимы email (слияние аккаунтов)
- `GET|POST /api/display-names` — кастомные отображаемые имена
- `POST /api/sync` — ручной запуск синхронизации (async, параметр `days`)
- `GET /api/sync-status` — прогресс синхронизации
- `GET /api/sync-log` — лог последних синхронизаций
- `POST /api/clear-data` — очистка всех синхронизированных данных

### `collector.py` — TFS API клиент
**`TFSClient`** — аутентификация через Basic Auth (PAT, base64 `:<PAT>`), `verify_ssl=False`.

Методы: `get_projects()`, `get_repositories(project_id)`, `get_commits(project_id, repo_id, from_date, to_date)`, `get_pull_requests(...)` (с клиентской фильтрацией по дате), `get_work_items_for_project(...)` (WIQL + batch fetch), `test_connection()`.

Вспомогательные функции: `_is_merge(comment)` — определение merge-коммитов, `_resolve_email(...)` — цепочка резолюций email из TFS-идентити, `sync_repository(...)` — полная синхронизация одного репозитория, `sync_work_items(...)`.

### `database.py` — SQLite-слой
13 таблиц: `app_settings`, `projects`, `repositories`, `commits`, `pull_requests`, `pr_reviews`, `sync_log`, `employee_aliases`, `teams`, `team_members`, `author_display_names`, `work_items`.

Ключевые методы запросов: `get_overview`, `get_developers`, `get_developer_stats`, `get_developer_work_item_stats`, `get_compare_stats`, `get_repositories`, `get_canonical_email`, `add_alias`, `get_sync_log`.

### `scheduler.py` — планировщик
APScheduler BackgroundScheduler (UTC). Поддерживает `_sync_status` dict (running, progress, message) с threading.Lock.  
`run_sync(db, from_date, to_date)` — основная функция синхронизации.  
По умолчанию интервал 6 часов, период синхронизации — последние 180 дней.

### `check_user.py` — CLI-диагностика
`python check_user.py <email>` — статистика пользователя.  
`python check_user.py` — поиск проблем: "двойники" (один логин, разные домены), "сироты" (в PR без коммитов), нестандартные email.

## Схема данных (ключевые таблицы)

- **commits**: author_email, author_name, author_date, add_count, edit_count, delete_count, is_merge
- **pull_requests**: creator_email, status, created_date, closed_date, source_branch, target_branch
- **pr_reviews**: pr_id, reviewer_email, vote (числовой)
- **work_items**: type, state, created_by_email, resolved_by_email, closed_by_email, created_date, resolved_date, closed_date
- **employee_aliases**: alias_email → primary_email (для слияния аккаунтов)
- **app_settings**: key/value — хранит PAT, TFS URL, collection, selected employees/projects/team

## Конфигурация

`config.yaml` поддерживает подстановку `${VAR}` из env vars.  
`.env` подхватывается через python-dotenv при старте.

Основные параметры:
- `TFS_URL` — базовый URL TFS (напр. `https://rxtfs.directum.ru/`)
- `PORT` — порт сервера (по умолчанию 14731)
- `LOG_LEVEL` — уровень логирования

## Известные особенности

- TFS API не поддерживает фильтрацию PR по дате — фильтрация делается на клиенте в `collector.py`
- Merge-коммиты определяются эвристически по тексту сообщения (`_is_merge`)
- `verify_ssl=False` — для работы с локальным TFS без валидного сертификата
- PAT хранится в БД, не в файловой системе — через Settings UI
- Алиасы (aliases) нужны для объединения одного человека с несколькими email (напр. `user@directum.ru` и `user@local`)
