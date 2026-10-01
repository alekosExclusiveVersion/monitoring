#!/usr/bin/env python3
"""check_ch_schema.py — есть ли в ClickHouse разрез по сайтам для секции single-событий.

Только чтение: 1 запрос к system.columns + пристрелочные GROUP BY за последний
завершённый час. Ничего не пишет, state не трогает, уведомлений не шлёт.

Запуск (на хосте с доступом к Grafana):
    .venv/Scripts/python.exe check_ch_schema.py

Вердикт:
  Исход А — site-колонка есть и заполнена: статистику по сайтам берём из
             ClickHouse (таймауты + секунды, 2 лёгких запроса на событие).
  Исход Б — колонки нет: идём путём MySQL (--provider в detect через wl_provider,
             только таймауты, без секунд с привязкой к провайдеру).
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import pricing_alert as pa  # noqa: E402 (переиспользуем _grafana_query/_rows)

TABLES = ("provider_logs", "provider_runtime_logs")
SITE_RE = re.compile(r"site|host|database|dbname|domain|project|shop|client",
                     re.IGNORECASE)


def _q(cfg: dict, raw: str) -> list[dict]:
    now = int(time.time())
    return pa._rows(pa._grafana_query(cfg, raw, now - 3600, now))


def main() -> int:
    cfg = {}
    try:
        cols = _q(cfg, "SELECT table, name, type FROM system.columns "
                       "WHERE database='provider' "
                       "AND table IN ('provider_logs','provider_runtime_logs')")
    except Exception as e:
        print(f"check: Grafana/ClickHouse недоступен: {str(e)[:150]}")
        print("check: запустите скрипт на хосте, где работает pricing_alert.py "
              "(оттуда Grafana доступна, секреты — в Credential Manager).")
        return 2
    if not cols:
        print("check: system.columns пуст — нет прав или не тот кластер. Исход неясен.")
        return 2

    by_table: dict[str, list[str]] = {}
    for r in cols:
        by_table.setdefault(str(r["table"]), []).append(
            f"{r['name']} ({r['type']})")
    for t in TABLES:
        print(f"check: {t}: {len(by_table.get(t, []))} колонок")
        for c in sorted(by_table.get(t, [])):
            print(f"    {c}")

    names = {str(r["name"]) for r in cols if str(r["table"]) == "provider_logs"}
    cands = sorted(n for n in names if SITE_RE.search(n))
    print(f"check: кандидаты site-разреза в provider_logs: {cands or '—'}")

    h_to = int(time.time())
    h_to -= h_to % 3600
    h_from = h_to - 3600
    usable: list[str] = []
    for col in cands[:3]:
        qcol = col.replace("`", "")
        try:
            rows = _q(cfg, f"SELECT `{qcol}` s, count() c "
                           "FROM provider.provider_logs "
                           f"WHERE timestamp>=toDateTime({h_from}) "
                           f"AND timestamp<toDateTime({h_to}) "
                           f"GROUP BY s ORDER BY c DESC LIMIT 5")
        except Exception as e:
            print(f"check: проба `{col}` не удалась: {str(e)[:120]}")
            continue
        total = sum(int(r["c"]) for r in rows)
        tops = ", ".join(f"{r['s'] or '<пусто>'}={r['c']}" for r in rows[:5])
        print(f"check: проба `{col}`: топ: {tops}")
        non_empty = sum(int(r["c"]) for r in rows if r["s"])
        if rows and non_empty >= total * 0.5:
            usable.append(col)

    print()
    if usable:
        print(f"check: ВЕРДИКТ — исход А. Site-разрез: {usable} "
              f"(шаблон: SELECT `{usable[0]}`, count(), "
              f"countIf(statusCode>=500), avg(totalTime) ... WHERE provider='X' "
              f"GROUP BY `{usable[0]}`).")
        return 0
    if cands:
        print("check: ВЕРДИКТ — неясно: кандидаты есть, но пустые/недоступные. "
              "Нужен взгляд в дашборд provider-logs.")
        return 3
    print("check: ВЕРДИКТ — исход Б. Site-разреза в ClickHouse нет, "
          "идём путём MySQL (--provider через wl_provider).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
