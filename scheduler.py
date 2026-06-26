import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from collector import TFSClient, sync_repository, sync_work_items
from database import Database
from logger import get_logger

log = get_logger("scheduler")

# Статус синхронизации хранится отдельно по каждому инстансу:
#   _sync_status[instance_id] = {running, started_at, message, progress, total}
_sync_status: dict = {}
_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()

# Максимальное число параллельных синхронизаций репозиториев.
# При увеличении свыше 8 польза падает — TFS начинает throttle.
_REPO_WORKERS = 4

# Стаггеринг запуска инстансов планировщика: задержка между стартами.
_INSTANCE_STAGGER_MINUTES = 5

# Рекомендуемое ограничение глубины первичной синхронизации.
# При $skip-пагинации TFS выполняет полный скан offset раз (O(n²)).
# Для первичной загрузки за >2 лет рекомендуется несколько инкрементальных запусков.
_SUGGESTED_MAX_FULL_SYNC_DAYS = 365


def _status(instance_id: str) -> dict:
    """Вернуть (создав при необходимости) словарь статуса для инстанса."""
    return _sync_status.setdefault(
        instance_id,
        {"running": False, "started_at": None, "message": "", "progress": 0, "total": 0},
    )


def get_sync_status(instance_id: str = "default") -> dict:
    return dict(_status(instance_id))


def _last_synced_to_from_date(ts: str, fallback: str) -> str:
    """Конвертирует last_synced timestamp в from_date для TFS API с запасом 1 час."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = dt - timedelta(hours=1)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return fallback


def _max_date_to_from_date(ts: str, buffer_hours: int = 2) -> str:
    """Конвертирует MAX(author_date) / MAX(closed_date) в from_date с буфером.

    Точнее, чем _last_synced_to_from_date: опирается на реальные данные,
    а не на wall-clock момент последней синхронизации.
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = dt - timedelta(hours=buffer_hours)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts


def _build_repo_from_date(
    db: Database,
    repo_id: str,
    global_from_date: str,
    incremental: bool,
) -> str:
    """Вычисляет from_date для репо при инкрементальной синхронизации.

    Приоритет: MAX(author_date) из commits (точнее) → last_synced (fallback) → global_from_date.
    """
    if not incremental:
        return global_from_date
    # Самый точный источник — последний коммит в БД для этого репо
    max_commit_date = db.get_repo_max_commit_date(repo_id)
    if max_commit_date:
        result = _max_date_to_from_date(max_commit_date)
        log.debug("Инкремент: репо %s — MAX(author_date)=%s → from=%s", repo_id, max_commit_date, result)
        return result
    # Fallback: wall-clock last_synced
    last_ts = db.get_repo_last_synced(repo_id)
    if last_ts:
        result = _last_synced_to_from_date(last_ts, global_from_date)
        log.debug("Инкремент: репо %s — last_synced=%s → from=%s", repo_id, last_ts, result)
        return result
    return global_from_date


def _build_wi_from_date(
    db: Database,
    project_id: str,
    global_from_date: str,
    incremental: bool,
) -> str:
    """Вычисляет from_date для work items при инкрементальной синхронизации."""
    if not incremental:
        return global_from_date
    max_wi_date = db.get_project_max_wi_date(project_id)
    if max_wi_date:
        result = _max_date_to_from_date(max_wi_date)
        log.debug("WI инкремент: проект %s — MAX(wi_date)=%s → from=%s", project_id, max_wi_date, result)
        return result
    wi_last_ts = db.get_project_wi_last_synced(project_id)
    if wi_last_ts:
        result = _last_synced_to_from_date(wi_last_ts, global_from_date)
        log.debug("WI инкремент: проект %s — last_synced=%s → from=%s", project_id, wi_last_ts, result)
        return result
    return global_from_date


def run_sync(db: Database, from_date: str, to_date: str, incremental: bool = False,
             instance_id: str = "default"):
    st = _status(instance_id)
    with _lock:
        if st["running"]:
            log.warning("[%s] Синхронизация уже запущена, пропуск", instance_id)
            return
        mode = "инкрементальная" if incremental else "полная"
        st.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                   "message": f"Запуск ({mode})...", "progress": 0, "total": 0})

    log.info("[%s] Синхронизация запущена (%s): %s → %s", instance_id, mode, from_date, to_date)
    try:
        settings = db.get_settings()
        if not settings.get("pat") or not settings.get("collection"):
            st.update({"running": False, "message": "Ошибка: PAT или коллекция не настроены"})
            log.error("[%s] Синхронизация прервана: PAT/коллекция не заданы", instance_id)
            return

        client = TFSClient(
            url=settings.get("tfs_url", ""),
            pat=settings["pat"],
            collection=settings["collection"],
        )

        ok, msg = client.test_connection()
        if not ok:
            st.update({"running": False, "message": f"Ошибка подключения: {msg}"})
            log.error("[%s] Синхронизация прервана: %s", instance_id, msg)
            return

        all_projects = client.get_projects()
        selected_ids = db.get_selected_projects()
        if selected_ids:
            projects = [p for p in all_projects if p["id"] in selected_ids]
            log.info("Фильтр проектов: %d из %d выбрано", len(projects), len(all_projects))
        else:
            projects = all_projects
            log.info("Проекты не выбраны — синхронизируются все (%d)", len(projects))

        # ── Параллельный fetch репозиториев по всем проектам ─────────────────
        st["message"] = "Получение списка репозиториев..."
        repos_all: list[tuple] = []

        def _get_repos(proj):
            try:
                return [(proj, r) for r in client.get_repositories(proj["id"])]
            except Exception:
                log.exception("Ошибка получения репо для проекта %s", proj["name"])
                return []

        with ThreadPoolExecutor(max_workers=min(_REPO_WORKERS, len(projects) or 1)) as pool:
            futures = [pool.submit(_get_repos, proj) for proj in projects]
            for fut in as_completed(futures):
                repos_all.extend(fut.result())

        st["total"] = len(repos_all)
        st["message"] = f"Найдено {len(repos_all)} репозиториев"
        log.info("[%s] Всего репозиториев: %d", instance_id, len(repos_all))

        # ── Предзагрузка кэшей для разрешения email ──────────────────────────
        # Строится один раз и передаётся во все потоки по ссылке (read-only).
        st["message"] = "Загрузка кэша email..."
        name_to_email = db.get_name_to_email_map()
        login_to_email = db.get_login_to_email_map()
        log.info("[%s] Кэш: name_to_email=%d, login_to_email=%d",
                 instance_id, len(name_to_email), len(login_to_email))

        # ── Параллельная синхронизация репозиториев ───────────────────────────
        progress_counter = [0]  # list для мутации внутри closure

        def _sync_one(proj_repo: tuple) -> tuple[str, bool]:
            proj, repo = proj_repo
            repo_from = _build_repo_from_date(db, repo["id"], from_date, incremental)
            try:
                sync_repository(
                    client=client,
                    db=db,
                    project_id=proj["id"],
                    project_name=proj["name"],
                    repo=repo,
                    from_date=repo_from,
                    to_date=to_date,
                    collection=settings["collection"],
                    name_to_email=name_to_email,
                    login_to_email=login_to_email,
                )
                return repo["name"], True
            except Exception:
                log.exception("Ошибка синхронизации репо %s/%s", proj["name"], repo["name"])
                return repo["name"], False

        with ThreadPoolExecutor(max_workers=_REPO_WORKERS) as pool:
            futures_map = {pool.submit(_sync_one, pr): pr for pr in repos_all}
            for fut in as_completed(futures_map):
                repo_name, ok = fut.result()
                with _lock:
                    progress_counter[0] += 1
                    done = progress_counter[0]
                    st["progress"] = done
                    st["message"] = (
                        f"[{done}/{len(repos_all)}] {repo_name}"
                        + ("" if ok else " (ошибка)")
                    )

        # ── Work items — синхронизируем по уникальным проектам ───────────────
        synced_projects = list({proj["id"] for proj, _ in repos_all})
        log.info("[%s] Work items: %d проектов", instance_id, len(synced_projects))

        def _sync_wi(proj_id: str):
            wi_from = _build_wi_from_date(db, proj_id, from_date, incremental)
            try:
                sync_work_items(
                    client=client,
                    db=db,
                    project_id=proj_id,
                    from_date=wi_from,
                    to_date=to_date,
                    name_to_email=name_to_email,
                    login_to_email=login_to_email,
                )
            except Exception:
                log.exception("Ошибка work items для проекта %s", proj_id)

        for pi, proj_id in enumerate(synced_projects, 1):
            st["message"] = f"Work items [{pi}/{len(synced_projects)}]..."
            _sync_wi(proj_id)

        st["message"] = "Обновление карты логинов..."
        db.rebuild_login_map()
        st.update({"running": False, "message": f"Завершено ({mode}). Репозиториев: {len(repos_all)}"})
        log.info("[%s] Синхронизация завершена (%s): %d репозиториев", instance_id, mode, len(repos_all))
    except Exception as e:
        st.update({"running": False, "message": f"Ошибка: {e}"})
        log.exception("[%s] Синхронизация завершилась с ошибкой", instance_id)


def start_sync_async(db: Database, from_date: str, to_date: str, incremental: bool = False,
                     instance_id: str = "default"):
    t = threading.Thread(target=run_sync, args=(db, from_date, to_date, incremental, instance_id),
                         daemon=True)
    t.start()


def start_scheduler(dbs: dict, instances: list, interval_hours: int, default_period_days: int):
    """Запустить фоновую синхронизацию для всех настроенных инстансов.

    dbs       — {instance_id: Database}
    instances — [{id, name, db_path}, ...]

    Инстансы стартуют с _INSTANCE_STAGGER_MINUTES-минутным интервалом, чтобы
    не бить по TFS одновременно при нескольких инстансах.
    """
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")

    if default_period_days > _SUGGESTED_MAX_FULL_SYNC_DAYS:
        log.warning(
            "default_period_days=%d превышает рекомендуемый лимит %d. "
            "При первичной загрузке $skip-пагинация деградирует (O(n²) на стороне TFS). "
            "Рекомендуется несколько инкрементальных запусков или уменьшить период.",
            default_period_days, _SUGGESTED_MAX_FULL_SYNC_DAYS,
        )

    started = 0
    for i, inst in enumerate(instances):
        iid = inst["id"]
        db = dbs.get(iid)
        if db is None:
            continue
        s = db.get_settings()
        if not (s.get("pat") and s.get("collection")):
            log.info("[%s] Планировщик пропущен: PAT/коллекция не настроены", iid)
            continue

        def _job(db=db, iid=iid):
            to_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            from_date = (datetime.now(timezone.utc) - timedelta(days=default_period_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            run_sync(db, from_date, to_date, instance_id=iid)

        # Стаггеринг: каждый следующий инстанс стартует на N минут позже,
        # чтобы не создавать пиковую нагрузку на TFS при одновременном запуске.
        stagger = timedelta(minutes=i * _INSTANCE_STAGGER_MINUTES)
        first_run = datetime.now(timezone.utc) + stagger

        _scheduler.add_job(
            _job, "interval", hours=interval_hours,
            id=f"auto_sync_{iid}",
            next_run_time=first_run,
        )
        log.info("[%s] Задание запланировано, первый запуск через %s",
                 iid, f"{i * _INSTANCE_STAGGER_MINUTES} мин" if i else "сразу")
        started += 1

    if started:
        _scheduler.start()
        log.info("Планировщик запущен для %d инстанс(ов), интервал: %d ч", started, interval_hours)
    else:
        log.warning("Планировщик не запущен: ни один инстанс не настроен")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Планировщик остановлен")
