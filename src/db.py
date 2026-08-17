# -*- coding: utf-8 -*-
"""SQLite data-access layer for the backtest database.

Pure data layer: opens connections, creates tables, and reads/writes rows.
No analysis logic lives here -- see backtest.py for signal stats / strategy
simulation, and run_backtest.py for the import/replay pipeline that fills
these tables.

All writes are idempotent (`INSERT OR REPLACE` on the primary key), so
re-running an import or a replay is always safe. Uses stdlib `sqlite3` only
-- no new dependency (see docs/archive/backtest_plan.md Phase 1).

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
            adj_factor REAL,
            PRIMARY KEY (code, date)
        );

        CREATE TABLE IF NOT EXISTS corporate_actions (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            pre_close REAL,
            ref_price REAL,
            ratio REAL NOT NULL,
            kind TEXT,
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
            priority REAL,
            action TEXT,
            signals_json TEXT,
            c_maPosition REAL,
            c_maAlign REAL,
            c_volumePrice REAL,
            c_trend REAL,
            c_vpPosition REAL,
            c_orderFlow REAL,
            c_gapTracker REAL,
            c_donchianPosition REAL,
            c_rsRanking REAL,
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


def migrate(conn):
    """Add columns/tables introduced after a DB was first created, without
    touching existing rows (docs/archive/backtest_plan.md Phase 1a). Safe to
    call on every startup, after init_db(): `ALTER TABLE ADD COLUMN` with a
    NULL default is an O(1) metadata-only change on SQLite -- it does not
    rewrite existing rows, so this is safe to run against a multi-million-row
    daily_bars table. corporate_actions is created here too (also covered by
    init_db's CREATE TABLE IF NOT EXISTS, but listed here so a single
    migrate() call brings an old DB fully up to date). The 9 score_history
    c_* columns are the unweighted per-dimension components analyze() now
    returns (docs/https-finlab-finance-blog-python-get-tai-memoized-
    lighthouse.md Phase 2b) -- letting SCORE_WEIGHTS re-tuning read a dot
    product off stored rows instead of re-running replay_scores()."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(daily_bars)").fetchall()}
    if "adj_factor" not in cols:
        conn.execute("ALTER TABLE daily_bars ADD COLUMN adj_factor REAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corporate_actions (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            pre_close REAL,
            ref_price REAL,
            ratio REAL NOT NULL,
            kind TEXT,
            source TEXT,
            PRIMARY KEY (code, date)
        )
        """
    )
    score_history_cols = {row["name"] for row in conn.execute("PRAGMA table_info(score_history)").fetchall()}
    for comp_col in ("c_maPosition", "c_maAlign", "c_volumePrice", "c_trend", "c_vpPosition",
                      "c_orderFlow", "c_gapTracker", "c_donchianPosition", "c_rsRanking"):
        if comp_col not in score_history_cols:
            conn.execute(f"ALTER TABLE score_history ADD COLUMN {comp_col} REAL")
    conn.commit()


# ------------------------------------------------------------ daily_bars --

def upsert_daily_bars(conn, code, rows):
    """`rows`: iterable of dicts with keys date/o/h/l/c/v/source.

    Does NOT preserve adj_factor -- INSERT OR REPLACE overwrites the whole
    row, and adj_factor isn't one of `rows`' keys, so it reverts to NULL for
    every date touched. Re-running --refresh-data therefore requires
    re-running scripts/build_adj_factors.py afterwards (see
    docs/archive/backtest_plan.md Phase 1a)."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO daily_bars (code, date, o, h, l, c, v, source)
        VALUES (:code, :date, :o, :h, :l, :c, :v, :source)
        """,
        ({**r, "code": code} for r in rows),
    )
    conn.commit()


def update_adj_factors(conn, code, factors_by_date):
    """`factors_by_date`: {date: adj_factor}. Updates existing daily_bars
    rows in place -- does not insert new rows (a date with no matching
    daily_bars row is silently a no-op, same as an UPDATE with no match)."""
    conn.executemany(
        "UPDATE daily_bars SET adj_factor = :adj_factor WHERE code = :code AND date = :date",
        [{"code": code, "date": d, "adj_factor": f} for d, f in factors_by_date.items()],
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

SCORE_HISTORY_COMPONENT_COLS = (
    "c_maPosition", "c_maAlign", "c_volumePrice", "c_trend", "c_vpPosition",
    "c_orderFlow", "c_gapTracker", "c_donchianPosition", "c_rsRanking",
)


def upsert_score_history(conn, code, rows):
    """`rows`: iterable of dicts with keys date/tech_score/chip_score/
    composite/priority/action/signals_json, plus the optional 9 unweighted
    SCORE_WEIGHTS component columns (SCORE_HISTORY_COMPONENT_COLS, see
    analysis.analyze()'s `components` output) -- NULL if a row's dict
    doesn't have them (e.g. a hand-built row that predates Phase 2b)."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO score_history (
            code, date, tech_score, chip_score, composite, priority, action, signals_json,
            c_maPosition, c_maAlign, c_volumePrice, c_trend, c_vpPosition,
            c_orderFlow, c_gapTracker, c_donchianPosition, c_rsRanking
        ) VALUES (
            :code, :date, :tech_score, :chip_score, :composite, :priority, :action, :signals_json,
            :c_maPosition, :c_maAlign, :c_volumePrice, :c_trend, :c_vpPosition,
            :c_orderFlow, :c_gapTracker, :c_donchianPosition, :c_rsRanking
        )
        """,
        ({**{c: None for c in SCORE_HISTORY_COMPONENT_COLS}, **r, "code": code} for r in rows),
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


# -------------------------------------------------------- corporate_actions --

def upsert_corporate_actions(conn, code, rows):
    """`rows`: iterable of dicts with keys date/pre_close/ref_price/ratio/
    kind/source (docs/archive/backtest_plan.md Phase 1b). Re-running
    scripts/build_adj_factors.py for the same code replaces prior rows for
    dates it recomputes but leaves other dates alone (INSERT OR REPLACE on
    the (code, date) primary key, not a delete-then-insert)."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO corporate_actions (
            code, date, pre_close, ref_price, ratio, kind, source
        ) VALUES (
            :code, :date, :pre_close, :ref_price, :ratio, :kind, :source
        )
        """,
        ({**r, "code": code} for r in rows),
    )
    conn.commit()


def query_corporate_actions(conn, code):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM corporate_actions WHERE code = ? ORDER BY date ASC",
            (code,),
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
