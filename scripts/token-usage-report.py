#!/usr/bin/env python3
"""token-usage-report.py — read-only прибор расхода токенов рантайма.

НАЗНАЧЕНИЕ
    Повторно используемый прибор: снимает срез расхода токенов из боевой БД
    рантайма и отдаёт его в двух синхронных форматах — JSON (полный снимок
    метрик) и Markdown (человекочитаемый отчёт). Числа в MD и JSON одни и те же,
    всё считает сам прибор, «от руки» ничего не берётся.

ЧТО ЧИТАЕТ
    База:  ~/.minimax/v2/sqlite/runtime-state.sqlite (по умолчанию; --db для иного пути)
    Таблица: local_runtime_token_usage
    Колонки: id, session_id, agent_name, framework_type, turn_id, model, ts,
             input_tokens, output_tokens, reasoning_tokens, cache_read_tokens,
             cache_write_tokens, cost_usd, raw
    Тип ts — integer, epoch-миллисекунды (UTC). Суточные корзины считаются в
    локальной таймзоне Europe/Moscow.
    cost_usd не читается и никуда не переводится (в базе он нулевой).

ТОЛЬКО ЧТЕНИЕ, mode=ro
    Подключение строго read-only, без права записи в файл БД и без создания
    sidecar-файлов (-wal/-journal). Прибор не создаёт БД, если её нет: если
    файла нет — выход с кодом 2. Никаких INSERT/UPDATE/DELETE/ALTER/CREATE/
    DROP, никаких PRAGMA на смену journal_mode.

    Точная строка подключения (реализация — _ro_uri()):

        sqlite3.connect(
            "file:~/.minimax/v2/sqlite/runtime-state.sqlite?mode=ro",
            uri=True,
        )

ОПРЕДЕЛЕНИЯ МЕТРИК (канонические, не менять)
    Ход (turn)            — группа строк с одним turn_id ВНУТРИ сессии;
                            ключ группы = (session_id, turn_id).
    Запросов на ход       — число строк в группе (COUNT(*) по ключу группы).
    Разогнанный ход       — ход с БОЛЕЕ ЧЕМ 50 запросами (> 50).
    Финальный контекст    — input_tokens + cache_read_tokens ПОСЛЕДНЕГО запроса
      сессии               сессии; последний запрос = максимум по ts, затем по id.
    «Толстая» сессия      — финальный контекст > 120 000 токенов.
    Суточный расход       — сумма (input + output + cache_read) за календарные
                            сутки по ts в Europe/Moscow; пропущенные сутки = 0.
    Медиана суточного     — медиана суточных сумм за N календарных суток
      расхода               СТРОГО ДО даты среза данных (сутки среза в окно не
                            входят). Окно по умолчанию N = 14.
    Пик                   — сутки, где суточный расход выше этой медианы.
    Покрытие              — все строки области выборки, без выборки (выборки
                            строк не делается никогда).
    Доля «разогнанных»    — доля ходов (> 50 запросов) в штуках и процентах и
      ходов                доля расхода (токенов), пришедшегося на них.
    Метод перцентиля      — nearest-rank по возрастанию: sorted[int(n * p)]
                            (индекс 0-based), канон рантайма
                            (_skeptic/B_sessions.py). Медиана — statistics.median.

ПРИМЕР ЗАПУСКА
    python token-usage-report.py --json out.json --md out.md
    python token-usage-report.py --db frozen.sqlite --until 2026-11-05 --quiet
    python token-usage-report.py --since 2026-10-01 --until 1793882915662

    Коды выхода: 0 — ок, 2 — нет БД / ошибка подключения, 3 — ошибка данных.

ВЕРСИЯ
    2026-11-05
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

VERSION_DATE = "2026-11-05"
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".minimax", "v2", "sqlite", "runtime-state.sqlite")
TABLE = "local_runtime_token_usage"
COLS = (
    "id, session_id, agent_name, framework_type, turn_id, model, ts, "
    "input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, "
    "cache_write_tokens, cost_usd, raw"
)

RUNAWAY_REQUESTS = 50          # разогнанный ход: запросов > 50
FAT_CONTEXT = 120_000          # «толстая» сессия: финальный контекст > 120 000
DEFAULT_DAILY_WINDOW = 14      # суток строго до даты среза (окно медианы)

# Корзины «запросов на ход»: (label, lo, hi) включительно, hi=None — хвост.
TURN_BUCKETS = [
    ("1", 1, 1),
    ("2-5", 2, 5),
    ("6-10", 6, 10),
    ("11-20", 11, 20),
    ("21-50", 21, 50),
    ("51-100", 51, 100),
    ("101-500", 101, 500),
    ("501+", 501, None),
]

# Корзины финального контекста: (label, lo, hi) включительно, hi=None — хвост.
CONTEXT_BUCKETS = [
    ("<20k", 0, 19_999),
    ("20-50k", 20_000, 49_999),
    ("50-80k", 50_000, 79_999),
    ("80-120k", 80_000, 119_999),
    ("120-200k", 120_000, 199_999),
    ("200-300k", 200_000, 299_999),
    ("300k+", 300_000, None),
]


# --------------------------------------------------------------------------
# Таймзона
# --------------------------------------------------------------------------
def _moscow_tz():
    """Europe/Moscow; при отсутствии tzdata — фиксированное MSK (UTC+3).

    Москва не переводит часы с 2014 г., поэтому фиксированный +03:00 точен
    для всего диапазона данных.
    """
    try:
        return ZoneInfo("Europe/Moscow"), "Europe/Moscow (IANA)"
    except Exception:  # noqa: BLE001 — нет tzdata в системе
        return timezone(timedelta(hours=3), "MSK"), "MSK UTC+03:00 (fallback, без tzdata)"


MSK, MSK_LABEL = _moscow_tz()
# Смещение Europe/Moscow в секундах (Москва не переводит часы с 2014 г.).
MOSCOW_OFFSET_SEC = int(
    datetime(2026, 1, 1, tzinfo=MSK).utcoffset().total_seconds()
)


# --------------------------------------------------------------------------
# Подключение: строго только чтение
# --------------------------------------------------------------------------
def _ro_uri(path: str) -> str:
    """URI для sqlite3.connect(..., uri=True) в режиме mode=ro."""
    p = Path(path).expanduser().resolve()
    return "file:" + quote(p.as_posix(), safe="/:") + "?mode=ro"


def _ro_uri_for_read(path: str) -> str:
    """URI только на чтение с учётом наличия -wal sidecar.

    Живая БД идёт через mode=ro: так видны данные из -wal.
    Автономный срез (рядом нет -wal) открывается через mode=ro&immutable=1:
    SQLite не создаёт -wal/-shm рядом с файлом и не пишет в него ничего.
    """
    p = Path(path).expanduser().resolve()
    wal = Path(str(p) + "-wal")
    if not wal.exists():
        return "file:" + quote(p.as_posix(), safe="/:") + "?mode=ro&immutable=1"
    return "file:" + quote(p.as_posix(), safe="/:") + "?mode=ro"


def open_ro(db_path: str):
    """Открыть БД только на чтение. Не создаёт файл, если его нет."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(_ro_uri_for_read(db_path), uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


# --------------------------------------------------------------------------
# Разбор границ фильтра
# --------------------------------------------------------------------------
def parse_bound(value: str, is_end: bool):
    """Разбор --since/--until -> (lo_ms, hi_ms).

    Принимается: YYYY-MM-DD; YYYY-MM-DDTHH:MM[:SS] (локальное время
    Europe/Moscow); «сырой» epoch-millis. Сравнение включительное по границе
    дня: --since DDDD-MM-DD начинается с 00:00:00 MSK, --until DDDD-MM-DD
    заканчивается на 23:59:59.999 MSK.
    """
    v = value.strip()
    try:
        if v.isdigit():
            ms = int(v)
            return (ms, ms) if is_end else (ms, None)
    except ValueError:
        pass

    dt_text = v.replace(" ", "T")
    try:
        if len(dt_text) == 10:
            base = datetime.strptime(dt_text, "%Y-%m-%d").replace(tzinfo=MSK)
            if is_end:
                nxt = base + timedelta(days=1)
                return (None, int(nxt.timestamp() * 1000) - 1)
            return (int(base.timestamp() * 1000), None)
        if len(dt_text) in (16, 19):
            fmt = "%Y-%m-%dT%H:%M" if len(dt_text) == 16 else "%Y-%m-%dT%H:%M:%S"
            base = datetime.strptime(dt_text, fmt).replace(tzinfo=MSK)
            ms = int(base.timestamp() * 1000)
            return (ms, ms + 999) if is_end else (ms, None)
    except ValueError:
        pass

    raise ValueError(
        "не разобрана граница %r: ожидается YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS "
        "или epoch-millis" % value
    )


def build_filter(since_raw: str | None, until_raw: str | None):
    """-> (where_sql, params, description)."""
    clauses, params, parts = [], [], []
    if since_raw:
        lo, _ = parse_bound(since_raw, is_end=False)
        if lo is not None:
            clauses.append("ts >= ?")
            params.append(lo)
        parts.append("since=%s" % since_raw)
    if until_raw:
        _, hi = parse_bound(until_raw, is_end=True)
        if hi is not None:
            clauses.append("ts <= ?")
            params.append(hi)
        parts.append("until=%s" % until_raw)
    where = " AND ".join(clauses) if clauses else ""
    return where, params, ("; ".join(parts) if parts else None)


# --------------------------------------------------------------------------
# Статистика (метод канона)
# --------------------------------------------------------------------------
def pct(values, p):
    """nearest-rank по возрастанию: sorted[int(n * p)] (индекс 0-based).

    Совпадает с каноном рантайма (_skeptic/B_sessions.py): sorted[int(len*0.9)].
    """
    if not values:
        return None
    s = sorted(values)
    idx = int(len(s) * p)
    if idx >= len(s):
        idx = len(s) - 1
    return s[idx]


def med(values):
    if not values:
        return None
    v = statistics.median(values)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _int_if_whole(v):
    """Целое float -> int: чтобы JSON и MD показывали одно и то же число."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def iso_ms(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=MSK).isoformat(timespec="milliseconds")


def bucketize(values, buckets):
    """Раскладка значений по корзинам -> список dict(label, count, share_pct)."""
    total = len(values)
    out = []
    for label, lo, hi in buckets:
        n = 0
        for v in values:
            if v >= lo and (hi is None or v <= hi):
                n += 1
        out.append({
            "bucket": label,
            "count": n,
            "share_pct": round(n / total * 100, 2) if total else 0.0,
        })
    return out


# --------------------------------------------------------------------------
# Сбор данных
# --------------------------------------------------------------------------
def fetch(con, where, params):
    cur = con.cursor()
    w = (" WHERE " + where) if where else ""

    agg = cur.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts), "
        "SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
        "SUM(cache_write_tokens), SUM(reasoning_tokens), "
        "COUNT(DISTINCT session_id), COUNT(DISTINCT turn_id) "
        f"FROM {TABLE}{w}",
        params,
    ).fetchone()

    rows_total = cur.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    turns = cur.execute(
        "SELECT session_id, turn_id, COUNT(*), "
        "SUM(input_tokens + output_tokens + cache_read_tokens) "
        f"FROM {TABLE}{w} GROUP BY session_id, turn_id",
        params,
    ).fetchall()

    sess_ctx = cur.execute(
        "SELECT session_id, input_tokens + cache_read_tokens FROM ("
        "  SELECT session_id, input_tokens, cache_read_tokens,"
        "         ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY ts DESC, id DESC) rn"
        f"  FROM {TABLE}{w}) WHERE rn = 1",
        params,
    ).fetchall()

    sess_req = cur.execute(
        "SELECT session_id, COUNT(*), SUM(input_tokens + output_tokens + cache_read_tokens) "
        f"FROM {TABLE}{w} GROUP BY session_id",
        params,
    ).fetchall()

    # Суточные суммы: SQL-корзины по московским суткам.
    min_ts, max_ts = agg[1], agg[2]
    offset = _fixed_offset(min_ts, max_ts)
    daily = None
    if offset is not None:
        daily = cur.execute(
            "SELECT date(CAST(ts AS REAL)/1000.0 + ?, 'unixepoch') AS d, "
            "SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), COUNT(*) "
            f"FROM {TABLE}{w} GROUP BY d",
            [offset] + params,
        ).fetchall()
    else:
        daily = _daily_python(cur, where, params)

    cur.close()
    return agg, rows_total, turns, sess_ctx, sess_req, daily


def _fixed_offset(min_ts, max_ts):
    """Смещение Europe/Moscow в секундах, если оно постоянно на [min_ts, max_ts]."""
    if min_ts is None or max_ts is None:
        return None
    offs = set()
    for ts in (min_ts, max_ts):
        offs.add(int(datetime.fromtimestamp(ts / 1000, tz=MSK).utcoffset().total_seconds()))
    if len(offs) != 1:
        return None
    return offs.pop()


def _daily_python(cur, where, params):
    """Запасной путь: корзины по суткам считаются в Python (редкий случай)."""
    w = (" WHERE " + where) if where else ""
    rows = cur.execute(
        f"SELECT ts, input_tokens, output_tokens, cache_read_tokens FROM {TABLE}{w}",
        params,
    ).fetchall()
    acc = {}
    for ts, i, o, c in rows:
        day = datetime.fromtimestamp(ts / 1000, tz=MSK).strftime("%Y-%m-%d")
        b = acc.setdefault(day, [0, 0, 0, 0])
        b[0] += i or 0
        b[1] += o or 0
        b[2] += c or 0
        b[3] += 1
    return [(d, v[0], v[1], v[2], v[3]) for d, v in sorted(acc.items())]


# --------------------------------------------------------------------------
# Метрики
# --------------------------------------------------------------------------
def build_metrics(agg, rows_total, turns, sess_ctx, sess_req, daily,
                  filter_desc, db_path, args):
    (n_rows, ts_min, ts_max, sum_in, sum_out, sum_cache,
     sum_cache_write, sum_reasoning, n_sessions, n_turn_ids) = agg

    sum_in = sum_in or 0
    sum_out = sum_out or 0
    sum_cache = sum_cache or 0
    sum_cache_write = sum_cache_write or 0
    sum_reasoning = sum_reasoning or 0
    spend = sum_in + sum_out + sum_cache

    # --- ходы ---
    turn_reqs = [t[2] for t in turns]
    turn_toks = {t[0:2]: (t[3] or 0) for t in turns}
    runaway = [t for t in turns if t[2] > RUNAWAY_REQUESTS]
    runaway_tokens = sum(t[3] or 0 for t in runaway)
    turn_tokens_total = sum(t[3] or 0 for t in turns)

    # --- сессии ---
    finals = [v for _sid, v in sess_ctx]
    fat = [v for v in finals if v > FAT_CONTEXT]
    sess_reqs = [r[1] for r in sess_req]
    sess_tokens = [r[2] or 0 for r in sess_req]

    # --- сутки ---
    day_map = {d: (i or 0, o or 0, c or 0, n or 0) for d, i, o, c, n in daily}
    anchor_day = max(day_map) if day_map else None
    win = args.daily_window
    daily_rows = []
    if anchor_day:
        a = datetime.strptime(anchor_day, "%Y-%m-%d").date()
        for k in range(win, 0, -1):
            d = (a - timedelta(days=k)).strftime("%Y-%m-%d")
            i, o, c, cnt = day_map.get(d, (0, 0, 0, 0))
            daily_rows.append({
                "date": d,
                "input_tokens": i,
                "output_tokens": o,
                "cache_read_tokens": c,
                "total_tokens": i + o + c,
                "rows": cnt,
                "present": d in day_map,
            })
    daily_totals = [r["total_tokens"] for r in daily_rows]
    daily_median = med(daily_totals)
    peaks = []
    if daily_median is not None:
        for r in daily_rows:
            if r["total_tokens"] > daily_median:
                peaks.append({
                    "date": r["date"],
                    "total_tokens": r["total_tokens"],
                    "x_median": round(r["total_tokens"] / daily_median, 2) if daily_median else None,
                })

    # Пики по всей истории области выборки (медиана — по окну N суток).
    all_peaks = []
    if daily_median is not None:
        for d in sorted(day_map):
            i, o, c, _n = day_map[d]
            tot = i + o + c
            if tot > daily_median:
                all_peaks.append({
                    "date": d,
                    "total_tokens": tot,
                    "x_median": round(tot / daily_median, 2) if daily_median else None,
                })
    max_day = None
    if day_map:
        dmax = max(day_map, key=lambda d: sum(day_map[d][:3]))
        max_day = {
            "date": dmax,
            "total_tokens": sum(day_map[dmax][:3]),
        }

    meta = {
        "tool": "token-usage-report.py",
        "tool_version": VERSION_DATE,
        "generated_at": datetime.now(tz=MSK).isoformat(timespec="seconds"),
        "timezone": MSK_LABEL,
        "snapshot_at": iso_ms(ts_max),
        "snapshot_at_epoch_ms": ts_max,
        "db_path": str(Path(db_path).resolve()),
        "db_size_bytes": os.path.getsize(db_path),
        "db_wal_size_bytes": _sidecar_size(db_path, "-wal"),
        "args": {
            "db": str(Path(db_path).resolve()),
            "since": args.since,
            "until": args.until,
            "daily_window": args.daily_window,
            "title": args.title,
            "json": args.json,
            "md": args.md,
            "quiet": args.quiet,
        },
        "filter": filter_desc,
        "scope": "filtered" if filter_desc else "all_rows",
        "scope_label": ("все строки (без фильтра)" if not filter_desc
                        else "все строки области выборки, с фильтром"),
        "coverage": "все строки в области выборки, не выборка",
        "rows_total_db": rows_total,
        "rows_in_scope": n_rows,
        "sessions": n_sessions,
        "turns": len(turns),
        "distinct_turn_ids": n_turn_ids,
        "ts_min_epoch_ms": ts_min,
        "ts_max_epoch_ms": ts_max,
        "ts_min": iso_ms(ts_min),
        "ts_max": iso_ms(ts_max),
        "percentile_method": "nearest-rank: sorted[int(n * p)] (индекс 0-based), канон _skeptic/B_sessions.py",
        "median_method": "statistics.median",
        "final_context_rule": "последний запрос сессии: максимум по ts, затем по id",
        "day_bucketing": MSK_LABEL,
    }

    totals = {
        "requests": n_rows,
        "input_tokens": sum_in,
        "output_tokens": sum_out,
        "cache_read_tokens": sum_cache,
        "total_tokens": spend,
        "cache_write_tokens": sum_cache_write,
        "reasoning_tokens": sum_reasoning,
        "cost_usd_used": False,
    }

    turns_sec = {
        "total": len(turns),
        "requests_per_turn": {
            "median": med(turn_reqs),
            "p90": pct(turn_reqs, 0.9),
            "max": max(turn_reqs) if turn_reqs else None,
            "mean": round(statistics.mean(turn_reqs), 2) if turn_reqs else None,
            "min": min(turn_reqs) if turn_reqs else None,
        },
        "runaway": {
            "threshold_requests": RUNAWAY_REQUESTS,
            "count": len(runaway),
            "share_pct": round(len(runaway) / len(turns) * 100, 2) if turns else 0.0,
            "tokens": runaway_tokens,
            "tokens_share_pct": round(runaway_tokens / turn_tokens_total * 100, 2) if turn_tokens_total else 0.0,
        },
        "tokens_total": turn_tokens_total,
        "distribution": bucketize(turn_reqs, TURN_BUCKETS),
    }

    sessions_sec = {
        "total": len(sess_ctx),
        "final_context": {
            "median": med(finals),
            "p90": pct(finals, 0.9),
            "max": max(finals) if finals else None,
            "mean": round(statistics.mean(finals), 2) if finals else None,
            "min": min(finals) if finals else None,
        },
        "fat": {
            "threshold_context": FAT_CONTEXT,
            "count": len(fat),
            "share_pct": round(len(fat) / len(finals) * 100, 2) if finals else 0.0,
        },
        "requests_per_session": {
            "median": med(sess_reqs),
            "p90": pct(sess_reqs, 0.9),
            "max": max(sess_reqs) if sess_reqs else None,
        },
        "tokens_per_session": {
            "median": med(sess_tokens),
            "max": max(sess_tokens) if sess_tokens else None,
        },
        "distribution": bucketize(finals, CONTEXT_BUCKETS),
    }

    daily_sec = {
        "anchor_date": anchor_day,
        "anchor_day_total": _int_if_whole(sum(day_map.get(anchor_day, (0, 0, 0, 0))[:3])) if anchor_day else None,
        "window_days": win,
        "window_start": daily_rows[0]["date"] if daily_rows else None,
        "window_end": daily_rows[-1]["date"] if daily_rows else None,
        "median_total": daily_median,
        "peaks": peaks,
        "peaks_count": len(peaks),
        "peaks_all_history": all_peaks,
        "peaks_all_history_count": len(all_peaks),
        "max_day": max_day,
        "days_missing": sum(1 for r in daily_rows if not r["present"]),
        "rows": daily_rows,
    }

    sources = {
        "meta.rows_in_scope":
            "%s: COUNT(*) по области выборки (все строки, выборки нет)" % TABLE,
        "meta.sessions":
            "%s: COUNT(DISTINCT session_id) по области выборки" % TABLE,
        "meta.turns":
            "%s: COUNT(*) GROUP BY session_id, turn_id (ключ хода = (session_id, turn_id))" % TABLE,
        "meta.ts_min / meta.ts_max":
            "%s: MIN(ts) / MAX(ts), ts = epoch-millis (integer)" % TABLE,
        "meta.db_size_bytes":
            "файл БД на диске: os.path.getsize(db_path)",
        "totals.input_tokens":
            "%s: SUM(input_tokens) по области выборки" % TABLE,
        "totals.output_tokens":
            "%s: SUM(output_tokens) по области выборки" % TABLE,
        "totals.cache_read_tokens":
            "%s: SUM(cache_read_tokens) по области выборки" % TABLE,
        "totals.total_tokens":
            "%s: SUM(input_tokens + output_tokens + cache_read_tokens) по области выборки" % TABLE,
        "totals.cache_write_tokens":
            "%s: SUM(cache_write_tokens) по области выборки (справочно)" % TABLE,
        "totals.reasoning_tokens":
            "%s: SUM(reasoning_tokens) по области выборки (справочно)" % TABLE,
        "turns.requests_per_turn.median":
            "%s: COUNT(*) GROUP BY session_id, turn_id -> statistics.median" % TABLE,
        "turns.requests_per_turn.p90":
            "%s: COUNT(*) GROUP BY session_id, turn_id -> nearest-rank sorted[int(n*0.9)]" % TABLE,
        "turns.requests_per_turn.max / mean":
            "%s: COUNT(*) GROUP BY session_id, turn_id -> max / statistics.mean" % TABLE,
        "turns.runaway.count":
            "%s: ходы с COUNT(*) > %d в группе (session_id, turn_id)" % (TABLE, RUNAWAY_REQUESTS),
        "turns.runaway.share_pct":
            "turns.runaway.count / turns.total * 100",
        "turns.runaway.tokens":
            "%s: SUM(input_tokens + output_tokens + cache_read_tokens) по ходам с COUNT(*) > %d"
            % (TABLE, RUNAWAY_REQUESTS),
        "turns.runaway.tokens_share_pct":
            "turns.runaway.tokens / turns.tokens_total * 100",
        "turns.distribution":
            "%s: COUNT(*) GROUP BY session_id, turn_id -> разложение по корзинам "
            "1 / 2-5 / 6-10 / 11-20 / 21-50 / 51-100 / 101-500 / 501+" % TABLE,
        "sessions.final_context.median":
            "%s: (input_tokens + cache_read_tokens) последнего запроса сессии "
            "(ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY ts DESC, id DESC) = 1) -> statistics.median"
            % TABLE,
        "sessions.final_context.p90":
            "%s: финальный контекст сессии -> nearest-rank sorted[int(n*0.9)]" % TABLE,
        "sessions.final_context.max":
            "%s: финальный контекст сессии -> max" % TABLE,
        "sessions.fat.count":
            "%s: финальный контекст сессии > %d" % (TABLE, FAT_CONTEXT),
        "sessions.fat.share_pct":
            "sessions.fat.count / sessions.total * 100",
        "sessions.distribution":
            "%s: финальный контекст сессии -> разложение по корзинам "
            "<20k / 20-50k / 50-80k / 80-120k / 120-200k / 200-300k / 300k+" % TABLE,
        "daily.rows":
            "%s: SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) "
            "GROUP BY date(ts/1000 + %d, 'unixepoch') — календарные сутки %s"
            % (TABLE, MOSCOW_OFFSET_SEC, MSK_LABEL),
        "daily.median_total":
            "statistics.median по суммам суточных расходов за %d календарных суток строго "
            "до даты среза (anchor_date); пропущенные сутки = 0" % win,
        "daily.peaks":
            "сутки окна, где суточный расход > daily.median_total",
        "daily.peaks_all_history":
            "все сутки области выборки, где суточный расход > daily.median_total",
        "daily.max_day":
            "сутки с максимальным суточным расходом по области выборки",
        "daily.days_missing":
            "сутки окна без строк в области выборки (считаются как 0 токенов)",
    }

    return {
        "meta": meta,
        "totals": totals,
        "turns": turns_sec,
        "sessions": sessions_sec,
        "daily": daily_sec,
        "sources": sources,
    }


def _sidecar_size(db_path, suffix):
    p = Path(str(db_path) + suffix)
    return p.stat().st_size if p.exists() else None


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def _n(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        if v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _ratio(v):
    """Множитель «x к медиане» — не процент."""
    return "—" if v is None else f"{v:.2f}x"


def _pct(v):
    # Точка как десятичный разделитель: число в MD совпадает с числом в JSON.
    return "—" if v is None else f"{v:.2f} %"


def render_md(rep, title):
    m, t, tn, s, d = (rep["meta"], rep["totals"], rep["turns"],
                      rep["sessions"], rep["daily"])
    L = []
    A = L.append

    A("# %s" % title)
    A("")
    A("- **Снимок отчёта:** %s" % m["generated_at"])
    A("- **Дата среза данных:** %s (ts max = %s)"
      % (m["snapshot_at"], _n(m["snapshot_at_epoch_ms"])))
    A("- **БД:** `%s`" % m["db_path"])
    A("- **Размер файла БД:** %s Б%s" % (
        _n(m["db_size_bytes"]),
        " (+ -wal %s Б)" % _n(m["db_wal_size_bytes"]) if m.get("db_wal_size_bytes") else ""))
    A("- **Область выборки:** %s | строк: %s | сессий: %s | ходов: %s"
      % (m["scope_label"], _n(m["rows_in_scope"]), _n(m["sessions"]), _n(m["turns"])))
    A("- **Охват: все строки, не выборка**")
    if m.get("filter"):
        A("- **Фильтр:** %s" % m["filter"])
    else:
        A("- **Фильтр:** не задан")
    A("- **ts:** %s … %s (epoch-millis, integer)" % (_n(m["ts_min_epoch_ms"]), _n(m["ts_max_epoch_ms"])))
    A("- **Метод перцентиля:** %s" % m["percentile_method"])
    A("- **Правило финального контекста:** %s" % m["final_context_rule"])
    A("")

    A("## 1. Суммарные токены")
    A("")
    A("| Метрика | Значение |")
    A("|---|---|")
    A("| Запросов (строк) | %s |" % _n(t["requests"]))
    A("| input | %s |" % _n(t["input_tokens"]))
    A("| output | %s |" % _n(t["output_tokens"]))
    A("| cache_read | %s |" % _n(t["cache_read_tokens"]))
    A("| **Всего (input+output+cache_read)** | **%s** |" % _n(t["total_tokens"]))
    A("| cache_write (справочно) | %s |" % _n(t["cache_write_tokens"]))
    A("| reasoning (справочно) | %s |" % _n(t["reasoning_tokens"]))
    A("")
    A("Источник: `%s` — SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
      "SUM(cache_write_tokens), SUM(reasoning_tokens) по области выборки; cost_usd не используется."
      % TABLE)
    A("")

    A("## 2. По ходам")
    A("")
    A("### 2.1. Запросов на ход")
    A("")
    A("| Показатель | Значение |")
    A("|---|---|")
    A("| Всего ходов | %s |" % _n(tn["total"]))
    A("| Медиана | %s |" % _n(tn["requests_per_turn"]["median"]))
    A("| p90 | %s |" % _n(tn["requests_per_turn"]["p90"]))
    A("| max | %s |" % _n(tn["requests_per_turn"]["max"]))
    A("| Среднее | %s |" % _n(tn["requests_per_turn"]["mean"]))
    A("| min | %s |" % _n(tn["requests_per_turn"]["min"]))
    A("")
    A("Источник: `%s` — COUNT(*) GROUP BY session_id, turn_id; медиана statistics.median, "
      "p90 nearest-rank sorted[int(n*0.9)]." % TABLE)
    A("")
    A("### 2.2. Разогнанные ходы (> %d запросов)" % RUNAWAY_REQUESTS)
    A("")
    A("| Показатель | Значение |")
    A("|---|---|")
    A("| Разогнанных ходов | %s |" % _n(tn["runaway"]["count"]))
    A("| Доля ходов | %s |" % _pct(tn["runaway"]["share_pct"]))
    A("| Токенов на разогнанных ходах | %s |" % _n(tn["runaway"]["tokens"]))
    A("| Доля расхода | %s |" % _pct(tn["runaway"]["tokens_share_pct"]))
    A("")
    A("Источник: `%s` — ходы с COUNT(*) > %d в группе (session_id, turn_id); "
      "токены = SUM(input_tokens + output_tokens + cache_read_tokens) по этим ходам."
      % (TABLE, RUNAWAY_REQUESTS))
    A("")
    A("### 2.3. Распределение запросов на ход")
    A("")
    A("| Корзина | Ходов | % |")
    A("|---|---|---|")
    for b in tn["distribution"]:
        A("| %s | %s | %s |" % (b["bucket"], _n(b["count"]), _pct(b["share_pct"])))
    A("")
    A("Источник: `%s` — COUNT(*) GROUP BY session_id, turn_id; корзины включительные "
      "по обоим краям, последняя — хвост (501+)." % TABLE)
    A("")

    A("## 3. По сессиям")
    A("")
    A("### 3.1. Финальный контекст сессии")
    A("")
    A("| Показатель | Значение |")
    A("|---|---|")
    A("| Всего сессий | %s |" % _n(s["total"]))
    A("| Медиана | %s |" % _n(s["final_context"]["median"]))
    A("| p90 | %s |" % _n(s["final_context"]["p90"]))
    A("| max | %s |" % _n(s["final_context"]["max"]))
    A("| Среднее | %s |" % _n(s["final_context"]["mean"]))
    A("| min | %s |" % _n(s["final_context"]["min"]))
    A("")
    A("Источник: `%s` — (input_tokens + cache_read_tokens) последнего запроса сессии "
      "(ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY ts DESC, id DESC) = 1); "
      "p90 nearest-rank sorted[int(n*0.9)]." % TABLE)
    A("")
    A("### 3.2. «Толстые» сессии (финальный контекст > %s)" % _n(FAT_CONTEXT))
    A("")
    A("| Показатель | Значение |")
    A("|---|---|")
    A("| «Толстых» сессий | %s |" % _n(s["fat"]["count"]))
    A("| Доля сессий | %s |" % _pct(s["fat"]["share_pct"]))
    A("")
    A("Источник: `%s` — финальный контекст сессии > %d; доля = count / sessions.total * 100."
      % (TABLE, FAT_CONTEXT))
    A("")
    A("### 3.3. Распределение финального контекста")
    A("")
    A("| Корзина | Сессий | % |")
    A("|---|---|---|")
    for b in s["distribution"]:
        A("| %s | %s | %s |" % (b["bucket"], _n(b["count"]), _pct(b["share_pct"])))
    A("")
    A("Источник: `%s` — финальный контекст сессии; корзины включительные по обоим краям, "
      "последняя — хвост (300k+)." % TABLE)
    A("")

    A("## 4. Суточные суммы")
    A("")
    A("Окно: %d календарных суток строго до даты среза (%s … %s); сутки среза в окно не входят."
      % (d["window_days"], d["window_start"], d["window_end"]))
    A("")
    A("| Дата | input | output | cache_read | Всего |")
    A("|---|---|---|---|---|")
    for r in d["rows"]:
        A("| %s | %s | %s | %s | %s |" % (
            r["date"], _n(r["input_tokens"]), _n(r["output_tokens"]),
            _n(r["cache_read_tokens"]), _n(r["total_tokens"])))
    A("")
    A("**Медиана суточного расхода** (окно %d суток): **%s** токенов. Пропущенных суток в окне: %s."
      % (d["window_days"], _n(d["median_total"]), _n(d["days_missing"])))
    A("")
    if d["peaks"]:
        A("Пики (сутки выше медианы):")
        A("")
        A("| Дата | Всего токенов | x к медиане |")
        A("|---|---|---|")
        for p in d["peaks"]:
            A("| %s | %s | %s |" % (p["date"], _n(p["total_tokens"]), _ratio(p["x_median"])))
    else:
        A("Пиков нет: ни одни сутки окна не превысили медиану.")
    A("")
    A("По всей истории области выборки суток выше медианы: %s."
      % _n(d["peaks_all_history_count"]))
    A("")
    if d.get("max_day"):
        A("Максимальный суточный расход по области выборки: **%s** токенов (%s)."
          % (_n(d["max_day"]["total_tokens"]), d["max_day"]["date"]))
        A("")
    A("Источник: `%s` — SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) "
      "GROUP BY date(ts/1000 + %d, 'unixepoch') — календарные сутки %s; медиана "
      "statistics.median по окну; пики — сутки с расходом выше медианы."
      % (TABLE, MOSCOW_OFFSET_SEC, MSK_LABEL))
    A("")

    A("## 5. Методика")
    A("")
    A("- Ход = группа строк с одним `turn_id` в рамках сессии; ключ `(session_id, turn_id)`.")
    A("- Финальный контекст = `input_tokens + cache_read_tokens` последнего запроса сессии "
      "(максимум по `ts`, затем по `id`).")
    A("- Суточный расход = `input + output + cache_read` за календарные сутки по `ts` (%s); "
      "пропущенные сутки = 0 токенов." % MSK_LABEL)
    A("- Перцентиль: nearest-rank `sorted[int(n * p)]`; медиана: `statistics.median`.")
    A("- `cost_usd` не используется, перевод в деньги не делается.")
    A("- Прибор работает только на чтение (`mode=ro`), выборки строк не делает.")
    A("- Версия прибора: %s." % VERSION_DATE)
    A("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="token-usage-report.py",
        description="Read-only прибор расхода токенов (local_runtime_token_usage).",
    )
    p.add_argument("--db", default=DEFAULT_DB,
                   help="путь к БД (по умолчанию живая БД; для замороженного среза)")
    p.add_argument("--since", default=None,
                   help="YYYY-MM-DD | YYYY-MM-DDTHH:MM:SS (Europe/Moscow) | epoch-millis")
    p.add_argument("--until", default=None,
                   help="YYYY-MM-DD | YYYY-MM-DDTHH:MM:SS (Europe/Moscow) | epoch-millis")
    p.add_argument("--json", dest="json", default=None, help="путь для полного снимка метрик (JSON)")
    p.add_argument("--md", dest="md", default=None, help="путь для Markdown-отчёта")
    p.add_argument("--title", default="Срез расхода токенов", help="заголовок отчёта")
    p.add_argument("--daily-window", dest="daily_window", type=int, default=DEFAULT_DAILY_WINDOW,
                   help="окно (в сутках) для медианы суточного расхода, строго до даты среза")
    p.add_argument("--quiet", action="store_true", help="не печатать сводку в stdout")
    return p


def write_text(path, text):
    p = Path(path).expanduser()
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def summarize(rep):
    m, t, tn, s, d = (rep["meta"], rep["totals"], rep["turns"],
                      rep["sessions"], rep["daily"])
    out = []
    out.append("token-usage-report %s | БД %s Б | строк %s | сессий %s | ходов %s | ts %s..%s"
               % (m["tool_version"], _n(m["db_size_bytes"]), _n(m["rows_in_scope"]),
                  _n(m["sessions"]), _n(m["turns"]),
                  _n(m["ts_min_epoch_ms"]), _n(m["ts_max_epoch_ms"])))
    out.append("  охват: все строки, не выборка | фильтр: %s" % (m.get("filter") or "нет"))
    out.append("  ходы: всего %s | запросов на ход median=%s p90=%s max=%s"
               % (_n(tn["total"]),
                  _n(tn["requests_per_turn"]["median"]),
                  _n(tn["requests_per_turn"]["p90"]),
                  _n(tn["requests_per_turn"]["max"])))
    out.append("  разогнанные (>%d): %s (%s) ходов, %s токенов (%s)"
               % (RUNAWAY_REQUESTS, _n(tn["runaway"]["count"]),
                  _pct(tn["runaway"]["share_pct"]),
                  _n(tn["runaway"]["tokens"]),
                  _pct(tn["runaway"]["tokens_share_pct"])))
    out.append("  сессии: финальный контекст median=%s p90=%s max=%s | >%s: %s (%s)"
               % (_n(s["final_context"]["median"]), _n(s["final_context"]["p90"]),
                  _n(s["final_context"]["max"]), _n(FAT_CONTEXT),
                  _n(s["fat"]["count"]), _pct(s["fat"]["share_pct"])))
    out.append("  токены: input=%s output=%s cache_read=%s всего=%s"
               % (_n(t["input_tokens"]), _n(t["output_tokens"]),
                  _n(t["cache_read_tokens"]), _n(t["total_tokens"])))
    out.append("  суточно: медиана(%d суток, %s..%s)=%s | пиков: %s | сутки среза: %s"
               % (d["window_days"], d["window_start"], d["window_end"],
                  _n(d["median_total"]), _n(d["peaks_count"]),
                  _n(d["anchor_day_total"])))
    return "\n".join(out)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.daily_window < 1:
        print("ОШИБКА: --daily-window должен быть >= 1", file=sys.stderr)
        return 3

    try:
        where, params, filter_desc = build_filter(args.since, args.until)
    except ValueError as exc:
        print("ОШИБКА: %s" % exc, file=sys.stderr)
        return 3

    if not os.path.isfile(args.db):
        print("ОШИБКА: БД не найдена: %s" % args.db, file=sys.stderr)
        return 2

    try:
        con = open_ro(args.db)
    except sqlite3.Error as exc:
        print("ОШИБКА: не удалось подключиться к БД только на чтение: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("ОШИБКА: БД недоступна: %s" % exc, file=sys.stderr)
        return 2

    try:
        data = fetch(con, where, params)
    except sqlite3.Error as exc:
        print("ОШИБКА ДАННЫХ: %s" % exc, file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print("ОШИБКА ДАННЫХ: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 3
    finally:
        con.close()

    try:
        rep = build_metrics(*data, filter_desc=filter_desc, db_path=args.db, args=args)
    except Exception as exc:  # noqa: BLE001
        print("ОШИБКА ДАННЫХ при расчёте метрик: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 3

    if args.json:
        try:
            write_text(args.json, json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            print("ОШИБКА: не удалось записать JSON: %s" % exc, file=sys.stderr)
            return 3
    if args.md:
        try:
            write_text(args.md, render_md(rep, args.title))
        except OSError as exc:
            print("ОШИБКА: не удалось записать MD: %s" % exc, file=sys.stderr)
            return 3

    if not args.quiet:
        print(summarize(rep))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
