# -*- coding: utf-8 -*-
"""Direct tests for the shared .env/environment loader. data_sources and
alerts both route through this, so lock its behaviour here once."""
import env_utils


def test_prefers_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("SOME_KEY", "from-env")
    (tmp_path / ".env").write_text("SOME_KEY=from-file\n", encoding="utf-8")
    assert env_utils.load_env_value("SOME_KEY", str(tmp_path)) == "from-env"


def test_reads_from_env_file_when_var_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_KEY", raising=False)
    (tmp_path / ".env").write_text("SOME_KEY=from-file\n", encoding="utf-8")
    assert env_utils.load_env_value("SOME_KEY", str(tmp_path)) == "from-file"


def test_strips_matching_quotes(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_KEY", raising=False)
    (tmp_path / ".env").write_text('SOME_KEY="quoted value"\n', encoding="utf-8")
    assert env_utils.load_env_value("SOME_KEY", str(tmp_path)) == "quoted value"


def test_skips_comments_and_blank_lines(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "# SOME_KEY=ignored\n\nSOME_KEY=real\n", encoding="utf-8"
    )
    assert env_utils.load_env_value("SOME_KEY", str(tmp_path)) == "real"


def test_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert env_utils.load_env_value("SOME_KEY", str(tmp_path)) == ""
