# -*- coding: utf-8 -*-
"""Unit tests for db.py's SQLite data-access layer. Each test opens its own
connection against a pytest tmp_path file, never the real data/backtest.db."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_db(conn)
    return conn


def test_init_db_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    db.init_db(conn)  # second call must not raise (CREATE TABLE IF NOT EXISTS)
    db.upsert_daily_bars(conn, "2330", [
        {"date": "2024-01-01", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "source": "finmind"},
    ])
    db.init_db(conn)  # a third call must not wipe existing data
    assert len(db.query_daily_bars(conn, "2330")) == 1
    conn.close()


def test_upsert_overwrites_same_pk_not_duplicates(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_daily_bars(conn, "2330", [
        {"date": "2024-01-01", "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000, "source": "finmind"},
    ])
    db.upsert_daily_bars(conn, "2330", [
        {"date": "2024-01-01", "o": 999, "h": 999, "l": 999, "c": 999, "v": 999, "source": "finmind"},
    ])
    rows = db.query_daily_bars(conn, "2330")
    assert len(rows) == 1
    assert rows[0]["c"] == 999
    conn.close()


def test_daily_bars_round_trip(tmp_path):
    conn = _conn(tmp_path)
    rows_in = [
        {"date": "2024-01-01", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000, "source": "finmind"},
        {"date": "2024-01-02", "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.0, "v": 1200, "source": "finmind"},
    ]
    db.upsert_daily_bars(conn, "2330", rows_in)
    rows_out = db.query_daily_bars(conn, "2330")
    assert [r["date"] for r in rows_out] == ["2024-01-01", "2024-01-02"]  # ORDER BY date ASC
    assert rows_out[0]["c"] == 100.5 and rows_out[1]["v"] == 1200
    assert db.query_daily_bars(conn, "2330", start_date="2024-01-02") == [rows_out[1]]
    assert db.query_daily_bars(conn, "9999") == []
    conn.close()


def test_chip_daily_round_trip(tmp_path):
    conn = _conn(tmp_path)
    row = {"date": "2024-01-01", "foreign_net": 1000, "trust_net": -500, "margin_balance": 2000,
           "margin_delta": 10, "short_balance": 300, "short_delta": -5, "foreign_ratio": 0.75,
           "foreign_shares": 500000, "sbl_balance": 100, "sbl_sales": 20, "sbl_returns": 5}
    db.upsert_chip_daily(conn, "2330", [row])
    out = db.query_chip_daily(conn, "2330")
    assert len(out) == 1 and out[0]["foreign_net"] == 1000 and out[0]["sbl_returns"] == 5
    conn.close()


def test_score_history_round_trip(tmp_path):
    conn = _conn(tmp_path)
    row = {"date": "2024-01-01", "tech_score": 60, "chip_score": 55, "composite": 58,
           "priority": 36, "action": "準備進場", "signals_json": '["ma_cross"]'}
    db.upsert_score_history(conn, "2330", [row])
    out = db.query_score_history(conn, "2330")
    assert len(out) == 1
    assert out[0]["action"] == "準備進場"
    assert out[0]["priority"] == 36  # numeric, not a stringified "36" (Phase 4 schema fix)
    assert out[0]["c_maPosition"] is None  # row predates Phase 2b -- NULL, not a KeyError
    conn.close()


def test_score_history_round_trip_with_components(tmp_path):
    """docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
    Phase 2b: the 9 unweighted score components round-trip through
    upsert_score_history()/query_score_history()."""
    conn = _conn(tmp_path)
    row = {"date": "2024-01-01", "tech_score": 60, "chip_score": 55, "composite": 58,
           "priority": 36, "action": "準備進場", "signals_json": '["ma_cross"]',
           "c_maPosition": 0.5, "c_maAlign": 1, "c_volumePrice": -0.25, "c_trend": 0.1,
           "c_vpPosition": 1.0, "c_orderFlow": -0.4, "c_gapTracker": 0, "c_donchianPosition": 0.2,
           "c_rsRanking": -0.6}
    db.upsert_score_history(conn, "2330", [row])
    out = db.query_score_history(conn, "2330")[0]
    for col in db.SCORE_HISTORY_COMPONENT_COLS:
        assert out[col] == row[col]
    conn.close()


def test_migrate_adds_score_history_component_columns_to_old_db(tmp_path):
    """A DB created before Phase 2b (score_history without c_* columns) must
    gain them via migrate() without losing existing rows."""
    conn = _conn(tmp_path)
    conn.execute(
        """
        CREATE TABLE score_history_old (
            code TEXT NOT NULL, date TEXT NOT NULL, tech_score REAL, chip_score REAL,
            composite REAL, priority REAL, action TEXT, signals_json TEXT,
            PRIMARY KEY (code, date)
        )
        """
    )
    conn.execute("DROP TABLE score_history")
    conn.execute("ALTER TABLE score_history_old RENAME TO score_history")
    conn.execute(
        "INSERT INTO score_history (code, date, tech_score) VALUES ('2330', '2024-01-01', 42)"
    )
    conn.commit()
    db.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(score_history)").fetchall()}
    assert set(db.SCORE_HISTORY_COMPONENT_COLS) <= cols
    row = conn.execute("SELECT * FROM score_history WHERE code='2330'").fetchone()
    assert row["tech_score"] == 42
    assert row["c_maPosition"] is None
    conn.close()


def test_signal_stats_round_trip_and_filters(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_signal_stats(conn, "2330", [
        {"signal": "ma_cross", "horizon": 20, "n": 10, "wins": 7, "win_rate": 70.0,
         "avg_ret": 5.0, "computed_at": "2024-01-01T00:00:00"},
        {"signal": "ma_cross", "horizon": 5, "n": 12, "wins": 6, "win_rate": 50.0,
         "avg_ret": 1.0, "computed_at": "2024-01-01T00:00:00"},
    ])
    all_rows = db.query_signal_stats(conn, "2330")
    assert len(all_rows) == 2
    only_20 = db.query_signal_stats(conn, "2330", horizon=20)
    assert len(only_20) == 1 and only_20[0]["n"] == 10
    only_signal = db.query_signal_stats(conn, "2330", signal="ma_cross", horizon=5)
    assert len(only_signal) == 1 and only_signal[0]["win_rate"] == 50.0
    conn.close()


def test_strategy_run_round_trip_and_latest(tmp_path):
    conn = _conn(tmp_path)
    summary = {
        "params_json": "{}", "n_trades": 5, "win_rate": 0.6, "avg_ret": 0.03,
        "total_return": 0.15, "buy_hold_return": 0.10, "max_drawdown": -0.08,
        "start_date": "2024-01-01", "end_date": "2024-12-31", "computed_at": "2024-12-31T00:00:00",
    }
    db.upsert_strategy_run(conn, "run1", "2330", summary)
    run = db.query_strategy_run(conn, "2330")
    assert run["run_id"] == "run1" and run["n_trades"] == 5
    assert db.query_strategy_run(conn, "2330", run_id="run1")["run_id"] == "run1"
    assert db.query_strategy_run(conn, "nonexistent") is None
    conn.close()


def test_trades_insert_replaces_prior_rows_for_same_run_and_code(tmp_path):
    conn = _conn(tmp_path)
    db.insert_trades(conn, "run1", "2330", [
        {"entry_date": "2024-01-02", "entry_price": 101, "exit_date": "2024-01-10",
         "exit_price": 105, "ret_pct": 3.96, "exit_reason": "take_profit", "hold_days": 8},
    ])
    # re-running the same run_id/code (e.g. a re-simulate) must replace, not append
    db.insert_trades(conn, "run1", "2330", [
        {"entry_date": "2024-02-01", "entry_price": 110, "exit_date": "2024-02-10",
         "exit_price": 108, "ret_pct": -1.8, "exit_reason": "stop_loss", "hold_days": 9},
    ])
    trades = db.query_trades(conn, "run1", "2330")
    assert len(trades) == 1
    assert trades[0]["entry_date"] == "2024-02-01"
    conn.close()


def test_meta_round_trip_and_default(tmp_path):
    conn = _conn(tmp_path)
    db.set_meta(conn, "last_run_at", "2024-12-31T00:00:00")
    assert db.get_meta(conn, "last_run_at") == "2024-12-31T00:00:00"
    assert db.get_meta(conn, "missing_key") is None
    assert db.get_meta(conn, "missing_key", default="fallback") == "fallback"
    db.set_meta(conn, "last_run_at", "2025-01-01T00:00:00")  # overwrite, not duplicate
    assert db.get_meta(conn, "last_run_at") == "2025-01-01T00:00:00"
    conn.close()
