"""Config loading: config.yaml for preferences, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


class Config:
    """Dotted-path accessor over config.yaml with .env overlay for secrets."""

    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Config not found at {p.resolve()}. Copy config.yaml from the repo root."
            )
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        cfg = cls(data, p)
        cfg.load_env()
        return cfg

    def load_env(self) -> None:
        """Load KEY=VALUE pairs from the .env file into os.environ (no overwrite).

        Kept dependency-free and tolerant: yfpy reads the same file for its own
        credentials, so we never rewrite or reformat it.
        """
        env_path = Path(self.get("paths.env_dir", ".")) / ".env"
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        # Treat an explicitly blank YAML value as "unset" so defaults apply.
        return default if node is None or node == "" else node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise KeyError(f"Required config value '{dotted}' is not set in {self.path}.")
        return value

    def set(self, dotted: str, value: Any) -> None:
        """Set a value in memory (use save() to persist)."""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self._data, fh, sort_keys=False, default_flow_style=False)

    @property
    def db_path(self) -> str:
        return self.get("paths.db", "data/league.db")

    @property
    def data(self) -> dict[str, Any]:
        return self._data


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)
