import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from collector import TFSClient, sync_repository
from database import Database
from logger import get_logger

log = get_logger("scheduler")

_sync_status: dict = {"running": False, "started_at": None, "message": "", "progress": 0, "total": 0}
_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()


def get_sync_status() -> dict:
    return dict(_sync_status)


def run_sync(db: Database, from_date: str, to_date: str):
    with _lock:
        if _sync_status["running"]:
            log.warning("Синхронизация уже запущена, пропуск")
            return
        _sync_status.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                              "message": "Запуск...", "progress": 0, "total": 0})

    log.info("Синхронизация запущена: %s → %s", from_date, to_date)
    try:
        settings = db.get_settings()
        if not settings.get("pat") or not settings.get("collection"):
            _sync_status.update({"running": False, "message": "Ошибка: PAT или коллекция не настроены"})
            log.error("Синхронизация прервана: PAT/коллекция не заданы")
            return

        client = TFSClient(
            url=settings.get("tfs_url", ""),
            pat=settings["pat"],
            collection=settings["collection"],
        )

        ok, msg = client.test_connection()
        if not ok:
            _sync_status.update({"running": False, "message": f"Ошибка подключения: {msg}"})
            log.error("Синхронизация прервана: %s", msg)
            return

        all_projects = client.get_projects()
        selected_ids = db.get_selected_projects()
        if selected_ids:
            projects = [p for p in all_projects if p["id"] in selected_ids]
            log.info("Фильтр проектов: %d из %d выбрано", len(projects), len(all_projects))
        else:
            projects = all_projects
            log.info("Проекты не выбраны — синхронизируются все (%d)", len(projects))

        repos_all = []
        for proj in projects:
            repos = client.get_repositories(proj["id"])
            for r in repos:
                repos_all.append((proj, r))

        _sync_status["total"] = len(repos_all)
        _sync_status["message"] = f"Найдено {len(repos_all)} репозиториев"

        for i, (proj, repo) in enumerate(repos_all, 1):
            _sync_status["progress"] = i
            _sync_status["message"] = f"[{i}/{len(repos_all)}] {proj['name']} / {repo['name']}"
            sync_repository(
                client=client,
                db=db,
                project_id=proj["id"],
                project_name=proj["name"],
                repo=repo,
                from_date=from_date,
                to_date=to_date,
                collection=settings["collection"],
            )

        _sync_status.update({"running": False, "message": f"Завершено. Обработано репозиториев: {len(repos_all)}"})
        log.info("Синхронизация завершена: %d репозиториев", len(repos_all))
    except Exception as e:
        _sync_status.update({"running": False, "message": f"Ошибка: {e}"})
        log.exception("Синхронизация завершилась с ошибкой")


def start_sync_async(db: Database, from_date: str, to_date: str):
    t = threading.Thread(target=run_sync, args=(db, from_date, to_date), daemon=True)
    t.start()


def start_scheduler(db: Database, interval_hours: int, default_period_days: int):
    global _scheduler

    def _job():
        to_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        from_date = (datetime.now(timezone.utc) - timedelta(days=default_period_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_sync(db, from_date, to_date)

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(_job, "interval", hours=interval_hours, id="auto_sync")
    _scheduler.start()
    log.info("Планировщик запущен, интервал: %d ч", interval_hours)


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Планировщик остановлен")
