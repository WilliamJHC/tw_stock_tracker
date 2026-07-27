# -*- coding: utf-8 -*-
"""SQLite data-access layer for the 2-year backtest database.

Pure data layer: opens connections, creates tables, and reads/writes rows.
No analysis logic lives here -- see backtest.py for signal stats / strategy
simulation, and run_backtest.py for the import/replay pipeline that fills
these tables.

All writes are idempotent (`INSERT OR REPLACE` on the primary key), so
re-running an import or a replay is always safe. Uses stdlib `sqlite3` only
-- no new dependency (see docs/backtest_plan.md Phase 1).

Column note: `foreign` is a SQL reserved word, so the foreign-investor net
column is named `foreign_net` throughout.
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "backtest.db")


def connect(path=None):
    """Open a connection to the backtest DB, creating the parent directory
    (`data/`) if needed. Row access by column name via `sqlite3.Row`."""
    db_path = path or DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    """Create all tables if they don't already exist. Safe to call on every
    startup -- `CREATE TABLE IF NOT EXISTS` throughout."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_bars (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            o REAL,
            h REAL,
            l REAL,
            c REAL,
            v REAL,
            source TEXT,
            PRIMARY KEY (code, date)
        );

        CREATE TABLE IF NOT EXISTS chip_daily (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            foreign_net REAL,
            trust_net REAL,
            margin_balance REAL,
            margin_delta REAL,
            short_balance REAL,
            short_delta REAL,
            foreign_ratio REAL,
            foreign_shares REAL,
            sbl_balance REAL,
            sbl_sales REAL,
            sbl_returns REAL,
            PRIMARY KEY (code, date)
        );

        CREATE TABLE IF NOT EXISTS score_history (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            tech_score REAL,
            chip_score REAL,
            composite REAL,
            priority TEXT,
            action TEXT,
            signals_json TEXT,
            PRIMARY KEY (code, date)
        );

        CREATE TABLE IF NOT EXISTS signal_stats (
            code TEXT NOT NULL,
            signal TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            n INTEGER,
            wins INTEGER,
            win_rate REAL,
            avg_ret REAL,
            computed_at TEXT,
            PRIMARY KEY (code, signal, horizon)
        );

        CREATE TABLE IF NOT EXISTS strategy_runs (
            run_id TEXT NOT NULL,
            code TEXT NOT NULL,
            params_json TEXT,
            n_trades INTEGER,
            win_rate REAL,
            avg_ret REAL,
            total_return REAL,
            buy_hold_return REAL,
            max_drawdown REAL,
            start_date TEXT,
            end_date TEXT,
            computed_at TEXT,
            PRIMARY KEY (run_id, code)
        );

        CREATE TABLE IF NOT EXISTS trades (
            run_id TEXT NOT NULL,
            code TEXT NOT NULL,
            entry_date TEXT,
            entry_price REAL,
            exit_date TEXT,
            exit_price REAL,
            ret_pct REAL,
            exit_reason TEXT,
            hold_days INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_trades_run_code
            ON trades (run_id, code);

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()


# ------------------------------------------------------------ daily_bars --

def upsert_daily_bars(conn, code, rows):
    """`rows`: iterable of dicts with keys date/o/h/l/c/v/source."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO daily_bars (code, date, o, h, l, c, v, source)
        VALUES (:code, :date, :o, :h, :l, :c, :v, :source)
        """,
        ({**r, "code": code} for r in rows),
    )
    conn.commit()


def query_daily_bars(conn, code, start_date=None, end_date=None):
    sql = "SELECT * FROM daily_bars WHERE code = ?"
    params = [code]
    if start_date:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        params.append(end_date)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ------------------------------------------------------------ chip_daily --

def upsert_chip_daily(conn, code, rows):
    """`rows`: iterable of dicts with keys date/foreign_net/trust_net/
    margin_balance/margin_delta/short_balance/short_delta/foreign_ratio/
    foreign_shares/sbl_balance/sbl_sales/sbl_returns."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO chip_daily (
            code, date, foreign_net, trust_net, margin_balance, margin_delta,
            short_balance, short_delta, foreign_ratio, foreign_shares,
            sbl_balance, sbl_sales, sbl_returns
        ) VALUES (
            :code, :date, :foreign_net, :trust_net, :margin_balance, :margin_delta,
            :short_balance, :short_delta, :foreign_ratio, :foreign_shares,
            :sbl_balance, :sbl_sales, :sbl_returns
        )
        """,
        ({**r, "code": code} for r in rows),
    )
    conn.commit()


def query_chip_daily(conn, code, start_date=None, end_date=None):
    sql = "SELECT * FROM chip_daily WHERE code = ?"
    params = [code]
    if start_date:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        params.append(end_date)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------- score_history --

def upsert_score_history(conn, code, rows):
    """`rows`: iterable of dicts with keys date/tech_score/chip_score/
    composite/priority/action/signals_json."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO score_history (
            code, date, tech_score, chip_score, composite, priority, action, signals_json
        ) VALUES (
            :code, :date, :tech_score, :chip_score, :composite, :priority, :action, :signals_json
        )
        """,
        ({**r, "code": code} for r in rows),
    )
    conn.commit()


def query_score_history(conn, code, start_date=None, end_date=None):
    sql = "SELECT * FROM score_history WHERE code = ?"
    params = [code]
    if start_date:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        params.append(end_date)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------- signal_stats --

def upsert_signal_stats(conn, code, rows):
    """`rows`: iterable of dicts with keys signal/horizon/n/wins/win_rate/
    avg_ret/computed_at."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO signal_stats (
            code, signal, horizon, n, wins, win_rate, avg_ret, computed_at
        ) VALUES (
            :code, :signal, :horizon, :n, :wins, :win_rate, :avg_ret, :computed_at
        )
        """,
        ({**r, "code": code} for r in rows),
    )
    conn.commit()


def query_signal_stats(conn, code, signal=None, horizon=None):
    sql = "SELECT * FROM signal_stats WHERE code = ?"
    params = [code]
    if signal is not None:
        sql += " AND signal = ?"
        params.append(signal)
    if horizon is not None:
        sql += " AND horizon = ?"
        params.append(horizon)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# -------------------------------------------------------- strategy_runs --

def upsert_strategy_run(conn, run_id, code, summary):
    """`summary`: dict with keys params_json/n_trades/win_rate/avg_ret/
    total_return/buy_hold_return/max_drawdown/start_date/end_date/computed_at."""
    row = {**summary, "run_id": run_id, "code": code}
    conn.execute(
        """
        INSERT OR REPLACE INTO strategy_runs (
            run_id, code, params_json, n_trades, win_rate, avg_ret,
            total_return, buy_hold_return, max_drawdown, start_date, end_date, computed_at
        ) VALUES (
            :run_id, :code, :params_json, :n_trades, :win_rate, :avg_ret,
            :total_return, :buy_hold_return, :max_drawdown, :start_date, :end_date, :computed_at
        )
        """,
        row,
    )
    conn.commit()


def query_strategy_run(conn, code, run_id=None):
    """Return the strategy run for `code`. Without `run_id`, returns the
    latest one (by `computed_at`), or None if there isn't one yet."""
    if run_id is not None:
        row = conn.execute(
            "SELECT * FROM strategy_runs WHERE code = ? AND run_id = ?",
            (code, run_id),
        ).fetchone()
        return dict(row) if row else None
    row = conn.execute(
        """
        SELECT * FROM strategy_runs WHERE code = ?
        ORDER BY computed_at DESC LIMIT 1
        """,
        (code,),
    ).fetchone()
    return dict(row) if row else None


# -------------------------------------------------------------- trades --

def insert_trades(conn, run_id, code, rows):
    """`rows`: iterable of dicts with keys entry_date/entry_price/exit_date/
    exit_price/ret_pct/exit_reason/hold_days. Trades have no natural PK, so
    re-running a strategy for the same run_id/code first clears prior rows
    to stay idempotent."""
    conn.execute(
        "DELETE FROM trades WHERE run_id = ? AND code = ?", (run_id, code)
    )
    conn.executemany(
        """
        INSERT INTO trades (
            run_id, code, entry_date, entry_price, exit_date, exit_price,
            ret_pct, exit_reason, hold_days
        ) VALUES (
            :run_id, :code, :entry_date, :entry_price, :exit_date, :exit_price,
            :ret_pct, :exit_reason, :hold_days
        )
        """,
        ({**r, "run_id": run_id, "code": code} for r in rows),
    )
    conn.commit()


def query_trades(conn, run_id, code):
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT * FROM trades WHERE run_id = ? AND code = ?
            ORDER BY entry_date ASC
            """,
            (run_id, code),
        ).fetchall()
    ]


# ---------------------------------------------------------------- meta --

def set_meta(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
