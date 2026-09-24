"""
detect_pricing_degradation.py

Обнаружение деградации веб-проценки («Превышено время ожидания»,
рост времени проценки, аномальный всплеск сессий поиска).

По каждому MySQL-серверу и каждой БД с таблицами web_logs /
search_external_stat метрики за последний час сравниваются
с эталоном за предыдущие 24 часа.

Метрики:
  timeouts  — число ошибок «Превышено время ожидания»/«Превышение лимита»
              за час против почасового темпа за сутки;
  providers — число затронутых провайдеров за час против темпа за сутки;
  sessions  — число сессий проценки за час против почасового темпа за сутки;
  avg_time  — среднее время проценки (ses_external_services_time) за час
              против суточного среднего.

Вывод: SERVER | DATABASE | SITE | METRIC | CURRENT | BASELINE | DELTA
Результат сохраняется в CSV.

Режимы:
  по умолчанию  — последний час против предыдущих 24 часов;
  --cur-from/--cur-to — анализ произвольного окна (например, периода
  инцидента) против --base-hours часов эталона до его начала.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

PSA_ROOT = Path(os.environ.get("PSA_REPO", str(Path.home() / "Work/scripts/parallels-sql-admins")))
sys.path.insert(0, str(PSA_ROOT))


def _load_config():
    import common.config as C
    import common.paths as P

    APP = Path(P.app_data_dir())
    cfg = C.load_config(APP / "config.ini")
    object.__setattr__(cfg.advanced, "servers_file", str(APP / "servers.json"))
    C.config = cfg


_load_config()

from common.config import config  # noqa: E402
from common.logger import logger  # noqa: E402
from common.mysql_client import mysql  # noqa: E402
from common.worker import worker_pool  # noqa: E402
from backend.repository import Repository  # noqa: E402

TIMEOUT_PATTERNS = ("%Превышено время ожидания%", "%Превышение лимита%")
DEFAULT_CUR_HOURS = 1
DEFAULT_BASE_HOURS = 24
INCREASE_FACTOR = 2.0
MIN_TIMEOUTS = 10
MIN_AVG_DELTA = 5.0
MIN_SESSIONS = 50
TS_COL = "ses_sse_timestamp"
TIME_COL = "ses_external_services_time"
LOG_COL = "wl_datetime"
FMT = "%Y-%m-%d %H:%M:%S"


def _conn_query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _site(conn, db: str) -> str:
    rows = _conn_query(
        conn,
        f"SELECT stg_value FROM `{db}`.`cfg_settings` "
        "WHERE stg_name='csSiteDomain' LIMIT 1",
    )
    return str(rows[0]["stg_value"]) if rows else ""


def _has_tables(conn, db: str) -> set:
    rows = _conn_query(
        conn,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name IN ('web_logs','search_external_stat')",
        (db,),
    )
    return {r["table_name"] for r in rows}


def _count_window(conn, db: str, table: str, col: str, ts_from: str, ts_to: str,
                  extra: tuple = ()) -> int:
    where = f"{col}>=%s AND {col}<%s"
    params = (ts_from, ts_to) + extra
    if extra:
        where += " AND (wl_desc LIKE %s OR wl_desc LIKE %s)"
    rows = _conn_query(
        conn,
        f"SELECT COUNT(*) n FROM `{db}`.`{table}` WHERE {where}",
        params,
    )
    return int(rows[0]["n"] or 0)


def _avg_window(conn, db: str, table: str, ts_col: str, avg_col: str,
                ts_from: str, ts_to: str) -> tuple[int, float]:
    rows = _conn_query(
        conn,
        f"SELECT COUNT(*) n, AVG({avg_col}) a FROM `{db}`.`{table}` "
        f"WHERE {ts_col}>=%s AND {ts_col}<%s",
        (ts_from, ts_to),
    )
    n = int(rows[0]["n"] or 0)
    avg = float(rows[0]["a"] or 0)
    return n, avg


def process_server(host: str, cur_from: str, cur_to: str,
                   base_from: str, base_to: str, cur_hours: float,
                   base_hours: float):
    rows: list[dict] = []

    logger.info(f"{host}: подключение")

    try:
        databases = mysql.list_all_databases(host)
    except Exception as e:
        logger.error(f"{host}: {e}")
        return rows

    try:
        with mysql.connect(host) as conn:
            scanned = 0
            for db in databases:
                has = _has_tables(conn, db)
                if not has & {"web_logs", "search_external_stat"}:
                    continue
                scanned += 1
                site = _site(conn, db)

                if "web_logs" in has:
                    cur_t = _count_window(
                        conn, db, "web_logs", LOG_COL, cur_from, cur_to,
                        TIMEOUT_PATTERNS,
                    )
                    cur_p = 0
                    if cur_t:
                        rows_p = _conn_query(
                            conn,
                            f"SELECT COUNT(DISTINCT wl_provider) p FROM `{db}`.`web_logs` "
                            "WHERE wl_datetime>=%s AND wl_datetime<%s "
                            "AND (wl_desc LIKE %s OR wl_desc LIKE %s)",
                            (cur_from, cur_to) + TIMEOUT_PATTERNS,
                        )
                        cur_p = int(rows_p[0]["p"] or 0)
                    base_t = _count_window(
                        conn, db, "web_logs", LOG_COL, base_from, base_to,
                        TIMEOUT_PATTERNS,
                    ) / base_hours
                    cur_t = cur_t / cur_hours
                else:
                    cur_t = 0
                    cur_p = 0
                    base_t = 0.0

                if "search_external_stat" in has:
                    n, avg_c = _avg_window(conn, db, "search_external_stat", TS_COL, TIME_COL, cur_from, cur_to)
                    n_b, avg_b = _avg_window(conn, db, "search_external_stat", TS_COL, TIME_COL, base_from, base_to)
                    cur_s = float(n)
                    cur_a = avg_c if n >= MIN_SESSIONS else float("nan")
                    base_a = avg_b if n_b >= MIN_SESSIONS else float("nan")
                else:
                    cur_s = 0.0
                    cur_a = float("nan")
                    base_a = float("nan")

                if cur_t >= MIN_TIMEOUTS and base_t and cur_t > base_t * INCREASE_FACTOR:
                    rows.append({
                        "server": host, "database": db, "site": site,
                        "metric": "timeouts", "current": round(cur_t, 1),
                        "baseline": round(base_t, 1),
                        "delta": round(cur_t / base_t, 1),
                    })
                if cur_p >= MIN_TIMEOUTS and base_t and cur_p > base_t * INCREASE_FACTOR:
                    rows.append({
                        "server": host, "database": db, "site": site,
                        "metric": "providers", "current": round(cur_p / cur_hours, 1),
                        "baseline": round(base_t, 1),
                        "delta": round((cur_p / cur_hours) / base_t, 1),
                    })
                cur_s = cur_s / cur_hours
                base_s = 0.0 if not n_b else n_b / base_hours
                if cur_s >= MIN_SESSIONS and base_s and cur_s > base_s * INCREASE_FACTOR:
                    rows.append({
                        "server": host, "database": db, "site": site,
                        "metric": "sessions", "current": round(cur_s, 1),
                        "baseline": round(base_s, 1),
                        "delta": round(cur_s / base_s, 1),
                    })
                if cur_a == cur_a and base_a == base_a and cur_a - base_a >= MIN_AVG_DELTA:
                    rows.append({
                        "server": host, "database": db, "site": site,
                        "metric": "avg_time", "current": round(cur_a, 1),
                        "baseline": round(base_a, 1),
                        "delta": round(cur_a - base_a, 1),
                    })
    except Exception as e:
        logger.error(f"{host}: {e}")
        return rows

    logger.success(f"{host}: scanned={scanned} anomalies={len(rows)}")
    return rows


def save_csv(data: list[dict], out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SERVER", "DATABASE", "SITE", "METRIC", "CURRENT", "BASELINE", "DELTA"])
        for item in data:
            writer.writerow([
                item["server"], item["database"], item["site"], item["metric"],
                item["current"], item["baseline"], item["delta"],
            ])
    logger.success(f"CSV сохранён: {out_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Детекция деградации веб-проценки в MySQL-логах PSA."
    )
    parser.add_argument("--cur-from", help="Начало анализируемого окна, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--cur-to", help="Конец анализируемого окна, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--base-hours", type=float, default=DEFAULT_BASE_HOURS,
                        help="Эталонное окно: N часов до начала анализируемого (по умолчанию 24)")
    parser.add_argument("--cur-hours", type=float, default=DEFAULT_CUR_HOURS,
                        help="Только для режима 'последние N часов' без --cur-from (по умолчанию 1)")
    parser.add_argument("--out", default=None,
                        help="Путь к CSV-файлу результата (по умолчанию pricing_degradation.csv)")
    args = parser.parse_args()

    now = datetime.now()
    if args.cur_from:
        cur_from = datetime.strptime(args.cur_from, FMT)
        cur_to = datetime.strptime(args.cur_to, FMT) if args.cur_to else now
    else:
        cur_from = now - timedelta(hours=args.cur_hours)
        cur_to = now
    base_to = cur_from
    base_from = cur_from - timedelta(hours=args.base_hours)
    cur_hours = max((cur_to - cur_from).total_seconds() / 3600, 0.001)
    base_hours = args.base_hours

    servers = [s.host for s in Repository().load_servers() if s.engine == "mysql"]
    logger.info(f"MySQL-серверов: {len(servers)}")
    logger.info(f"Окно анализа: {cur_from:%H:%M}–{cur_to:%H:%M} "
                f"({cur_hours:.1f} ч); эталон: {base_from:%H:%M}–{base_to:%H:%M} "
                f"({base_hours:.0f} ч)")

    def _process(host: str):
        return process_server(
            host,
            cur_from.strftime(FMT), cur_to.strftime(FMT),
            base_from.strftime(FMT), base_to.strftime(FMT),
            cur_hours, base_hours,
        )

    results = worker_pool.run(servers, _process)

    all_rows = []
    print()
    print(f'{"SERVER":26} {"DATABASE":32} {"SITE":24} {"METRIC":10} '
          f'{"CUR":9} {"BASE":9} {"DELTA":7}')
    print("-" * 130)
    for result in results:
        if not result.success:
            continue
        for row in result.value:
            print(f'{row["server"]:26} {row["database"]:32} {row["site"]:24} '
                  f'{row["metric"]:10} {row["current"]:<9.1f} '
                  f'{row["baseline"]:<9.1f} {row["delta"]:<7.1f}')
            all_rows.append(row)

    save_csv(all_rows, Path(args.out) if args.out
             else Path(config.logging.directory) / "pricing_degradation.csv")
    logger.success(f"Всего аномалий: {len(all_rows)}")


if __name__ == "__main__":
    sys.exit(main())