#!/usr/bin/env python3
"""sm_projects.py — реестр проектов из System Monitor (sm.office.tradesoft.ru).

Эндпоинт /api/project/<id> отдаёт без авторизации поля id, name, users
(логины, заведённые в Parts.Resource) и allowedIpList. Эндпоинт /info не
используется: он отдаёт apiKey/serviceKeys.

Активность проекта = в сервисе есть пользователь. Это реестр, а не текущие
сессии, поэтому он остаётся рабочим, когда сервер проекта недоступен.

Кэш: logs/sm_projects_cache.json, TTL — sm_cache_ttl_minutes.
Системный прокси игнорируется.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "logs" / "sm_projects_cache.json"
FMT = "%Y-%m-%d %H:%M:%S"

DEFAULT_SM: dict = {
    "url": "https://sm.office.tradesoft.ru",
    "timeout": 20,
    "retries": 2,
    "cache_ttl_minutes": 30,
}


def load_sm(path: str | Path | None = None) -> dict:
    target = Path(path) if path else BASE_DIR / "tg_support_config.json"
    sm = dict(DEFAULT_SM)
    try:
        cfg = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return sm
    section = cfg.get("sm_monitor")
    if isinstance(section, dict):
        sm.update(section)
    portal = cfg.get("projects_portal")
    if isinstance(portal, dict) and "sm_url" in portal:
        sm["url"] = portal["sm_url"]
    return sm


STATS = {"ok": 0, "errors": 0}


def _get_json(url: str, sm: dict) -> list[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last: Exception | None = None
    for _ in range(int(sm["retries"]) + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with opener.open(req, timeout=int(sm["timeout"])) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            STATS["ok"] += 1
            return data if isinstance(data, list) else []
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
    STATS["errors"] += 1
    raise RuntimeError(f"System Monitor недоступен: {last}")


def _read_cache(sm: dict) -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"updated": "", "projects": {}}


def _save_cache(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def project(prj_id: int, sm: dict | None = None, refresh: bool = False) -> dict | None:
    sm = sm or load_sm()
    cache = _read_cache(sm)
    entry = cache["projects"].get(str(prj_id))
    ttl = timedelta(minutes=float(sm["cache_ttl_minutes"]))
    if not refresh and isinstance(entry, dict) and entry.get("at"):
        try:
            if datetime.strptime(entry["at"], FMT) + ttl > datetime.now():
                return entry
        except ValueError:
            pass
    url = f"{sm['url'].rstrip('/')}/api/project/{urllib.parse.quote(str(prj_id))}"
    rows = _get_json(url, sm)
    found = next((row for row in rows if int(row.get("id", -1)) == prj_id), None)
    if found is None:
        return None
    record = {
        "id": prj_id,
        "name": found.get("name") or "",
        "users": list(found.get("users") or []),
        "allowed_ips": list(found.get("allowedIpList") or []),
        "at": datetime.now().strftime(FMT),
    }
    cache["projects"][str(prj_id)] = record
    cache["updated"] = record["at"]
    _save_cache(cache)
    return record


def has_users(prj_id: int, sm: dict | None = None, refresh: bool = False) -> bool:
    try:
        record = project(prj_id, sm, refresh)
    except RuntimeError:
        return False
    return bool(record and record.get("users"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Проект в System Monitor.")
    ap.add_argument("project_id", type=int)
    ap.add_argument("--refresh", action="store_true", help="обновить кэш")
    args = ap.parse_args()
    try:
        record = project(args.project_id, refresh=args.refresh)
    except RuntimeError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    if record is None:
        print(f"проект {args.project_id} в System Monitor не найден")
        return 1
    print(f"{record['id']} {record['name']}")
    print(f"  пользователей: {len(record['users'])} — {', '.join(record['users'])}")
    print(f"  разрешённых IP: {len(record['allowed_ips'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
