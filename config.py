import os
import re

import yaml
from dotenv import load_dotenv

load_dotenv()


def _sub_env(obj):
    if isinstance(obj, str):
        return re.sub(
            r"\$\{(\w+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _sub_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sub_env(i) for i in obj]
    return obj


class Config:
    def __init__(self, config_path: str = "config.yaml"):
        self._data: dict = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        self._data = _sub_env(self._data)

    def get(self, *keys, default=None):
        node = self._data
        for k in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(k)
            if node is None:
                return default
        return node

    @property
    def tfs_url(self) -> str:
        return self.get("tfs", "url", default="http://localhost:8080/tfs").rstrip("/")

    @property
    def tfs_api_version(self) -> str:
        return self.get("tfs", "api_version", default="6.0")

    @property
    def tfs_timeout(self) -> int:
        return int(self.get("tfs", "timeout_seconds", default=30))

    @property
    def tfs_verify_ssl(self) -> bool:
        v = self.get("tfs", "verify_ssl", default=True)
        return str(v).lower() not in ("false", "0", "no")

    @property
    def tfs_projects(self) -> list:
        return self.get("tfs", "projects", default=[]) or []

    @property
    def sync_interval_hours(self) -> int:
        return int(self.get("sync", "interval_hours", default=6))

    @property
    def sync_default_period_days(self) -> int:
        return int(self.get("sync", "default_period_days", default=180))

    @property
    def server_host(self) -> str:
        return self.get("server", "host", default="0.0.0.0")

    @property
    def server_port(self) -> int:
        return int(self.get("server", "port", default=14731))

    @property
    def database_path(self) -> str:
        return self.get("database", "path", default="data/pulse.db")

    @property
    def log_level(self) -> str:
        return self.get("logging", "level", default="INFO")

    @property
    def log_file(self) -> str:
        return self.get("logging", "file", default="logs/app.log")
