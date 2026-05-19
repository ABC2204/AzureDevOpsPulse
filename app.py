import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, render_template, request

from collector import TFSClient
from config import Config
from database import Database
from logger import setup_logger
from scheduler import get_sync_status, start_scheduler, start_sync_async, stop_scheduler

cfg = Config()
log = setup_logger("app", log_file=cfg.log_file, level=cfg.log_level)
setup_logger("collector", log_file=cfg.log_file, level=cfg.log_level)
setup_logger("database", log_file=cfg.log_file, level=cfg.log_level)
setup_logger("scheduler", log_file=cfg.log_file, level=cfg.log_level)

db = Database(cfg.database_path)
db.create_tables()

app = Flask(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok(data):
    return jsonify({"success": True, "data": data, "error": None})


def _err(msg: str, status: int = 400):
    log.warning("API error (%s): %s", status, msg)
    return jsonify({"success": False, "data": None, "error": msg}), status


def _dates():
    to_date = request.args.get("to_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_date = request.args.get("from_date") or (
        datetime.now(timezone.utc) - timedelta(days=cfg.sync_default_period_days)
    ).strftime("%Y-%m-%d")
    return from_date + "T00:00:00Z", to_date + "T23:59:59Z"


def _client_from_db():
    s = db.get_settings()
    if not s.get("pat") or not s.get("collection"):
        return None, "PAT или коллекция не настроены. Откройте /settings"
    client = TFSClient(
        url=s.get("tfs_url") or cfg.tfs_url,
        pat=s["pat"],
        collection=s["collection"],
        api_version=cfg.tfs_api_version,
        timeout=cfg.tfs_timeout,
        verify_ssl=cfg.tfs_verify_ssl,
    )
    return client, None


# ── page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/developer/<path:email>")
def developer(email: str):
    return render_template("developer.html", email=email)


@app.route("/compare")
def compare():
    return render_template("compare.html")


@app.route("/repos")
def repos():
    return render_template("repos.html")


@app.route("/settings")
def settings():
    s = db.get_settings()
    return render_template(
        "settings.html",
        tfs_url=s.get("tfs_url") or cfg.tfs_url,
        collection=s.get("collection", ""),
        has_pat=bool(s.get("pat")),
    )


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return _ok({"status": "ok"})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        s = db.get_settings()
        return _ok({
            "tfs_url": s.get("tfs_url") or cfg.tfs_url,
            "collection": s.get("collection", ""),
            "has_pat": bool(s.get("pat")),
        })
    data = request.get_json(force=True) or {}
    tfs_url = (data.get("tfs_url") or cfg.tfs_url).rstrip("/")
    collection = data.get("collection", "").strip()
    pat = data.get("pat", "").strip()
    if not collection:
        return _err("Коллекция не может быть пустой")
    if not pat:
        return _err("PAT не может быть пустым")
    db.save_settings(tfs_url, collection, pat)
    return _ok({"saved": True})


@app.route("/api/test-connection")
def api_test_connection():
    client, err = _client_from_db()
    if err:
        return _err(err)
    ok, msg = client.test_connection()
    if ok:
        return _ok({"message": msg})
    return _err(msg, 502)


@app.route("/api/overview")
def api_overview():
    from_date, to_date = _dates()
    data = db.get_overview(from_date, to_date)
    data["cached_range"] = db.get_cached_range()
    return _ok(data)


@app.route("/api/developers")
def api_developers():
    from_date, to_date = _dates()
    return _ok(db.get_developers(from_date, to_date))


@app.route("/api/developer/<path:email>")
def api_developer(email: str):
    from_date, to_date = _dates()
    return _ok(db.get_developer_stats(email, from_date, to_date))


@app.route("/api/compare")
def api_compare():
    emails_raw = request.args.get("emails", "")
    emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
    if not emails:
        return _err("Не указаны email разработчиков (параметр emails)")
    if len(emails) > 5:
        return _err("Максимум 5 разработчиков для сравнения")
    from_date, to_date = _dates()
    return _ok(db.get_compare_stats(emails, from_date, to_date))


@app.route("/api/repositories")
def api_repositories():
    from_date, to_date = _dates()
    return _ok(db.get_repositories(from_date, to_date))


@app.route("/api/repository/<repo_id>")
def api_repository(repo_id: str):
    from_date, to_date = _dates()
    return _ok(db.get_repository_stats(repo_id, from_date, to_date))


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if get_sync_status()["running"]:
        return _err("Синхронизация уже выполняется", 409)
    data = request.get_json(force=True) or {}
    to_dt = datetime.now(timezone.utc)
    days = int(data.get("days", cfg.sync_default_period_days))
    from_dt = to_dt - timedelta(days=days)
    from_date = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_date = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_sync_async(db, from_date, to_date)
    return _ok({"message": f"Синхронизация запущена за {days} дней", "from": from_date, "to": to_date})


@app.route("/api/sync-status")
def api_sync_status():
    return _ok(get_sync_status())


@app.route("/api/sync-log")
def api_sync_log():
    limit = int(request.args.get("limit", 50))
    return _ok(db.get_sync_log(limit))


# ── startup ───────────────────────────────────────────────────────────────────

def shutdown(signum, frame):
    log.info("Завершение работы (сигнал %s)...", signum)
    stop_scheduler()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("=" * 60)
    log.info("AzureDevOps Pulse запускается")
    log.info("TFS URL (из конфига): %s", cfg.tfs_url)
    log.info("БД: %s", cfg.database_path)
    log.info("Порт: %s", cfg.server_port)
    log.info("=" * 60)

    s = db.get_settings()
    if s.get("pat") and s.get("collection"):
        log.info("Настройки найдены, запускаем планировщик")
        start_scheduler(db, cfg.sync_interval_hours, cfg.sync_default_period_days)
    else:
        log.warning("PAT/коллекция не настроены. Откройте http://localhost:%d/settings", cfg.server_port)

    app.run(
        host=cfg.server_host,
        port=cfg.server_port,
        debug=False,
        use_reloader=False,
    )
