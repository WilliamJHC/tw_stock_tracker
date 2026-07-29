# -*- coding: utf-8 -*-
"""Unit tests for chip_analysis()/compute_attention()/label_for() with hand-computed fixtures."""


import analysis as an

VOL20 = 1_000_000  # shares, round number for hand-checkable fractions


def _inst(net_per_day, days=5):
    """days entries, each foreign=net_per_day, trust=0."""
    return [{"date": f"2026-01-{i+1:02d}", "foreign": net_per_day, "trust": 0} for i in range(days)]


def _margin(bal_first, bal_last, short_last=0, n=6):
    rows = [{"date": f"2026-01-{i+1:02d}", "margin_balance": bal_first, "margin_delta": 0,
             "short_balance": 0, "short_delta": 0} for i in range(n - 1)]
    rows.append({"date": f"2026-01-{n:02d}", "margin_balance": bal_last, "margin_delta": 0,
                 "short_balance": short_last, "short_delta": 0})
    return rows


def _shareholding(ratio_first, ratio_last, n=20):
    rows = [{"date": f"2026-01-{i+1:02d}", "foreign_ratio": ratio_first, "foreign_shares": 0}
            for i in range(n - 1)]
    rows.append({"date": f"2026-01-{n:02d}", "foreign_ratio": ratio_last, "foreign_shares": 0})
    return rows


def _sbl(bal_first, bal_last, n=6):
    rows = [{"date": f"2026-01-{i+1:02d}", "sbl_balance": bal_first, "sbl_sales": 0, "sbl_returns": 0}
            for i in range(n - 1)]
    rows.append({"date": f"2026-01-{n:02d}", "sbl_balance": bal_last, "sbl_sales": 0, "sbl_returns": 0})
    return rows


# ------------------------------------------------------------ institutional --

def test_institutional_only_normalizes_to_full_component_score():
    # net5 = 5 * 100,000 = 500,000 shares; threshold for c=1 is 0.10 * 5*VOL20 = 500,000
    inst = _inst(100_000)
    chip = an.chip_analysis(inst, None, None, None, VOL20)
    assert chip is not None
    assert chip["coverage"] == ["institutional"]
    assert chip["score"] == 100          # only component present -> score = round(100*c)
    assert chip["label"] == "強多"


def test_institutional_negative_and_clamped_beyond_threshold():
    inst = _inst(-500_000)               # far beyond the -0.10 threshold -> clamp to -1
    chip = an.chip_analysis(inst, None, None, None, VOL20)
    assert chip["components"]["institutional"]["c"] == -1.0
    assert chip["score"] == -100


# ------------------------------------------------------------------- margin --

def test_margin_negative_component_when_balance_grows():
    # +5% growth over the window -> c = clamp(-0.05/0.05) = -1 (bearish)
    margin = _margin(bal_first=1_000_000, bal_last=1_050_000)
    chip = an.chip_analysis(None, margin, None, None, VOL20)
    assert chip["components"]["margin"]["c"] == -1.0
    assert chip["score"] == -100


def test_margin_positive_component_when_balance_shrinks():
    # -5% shrink -> c = clamp(0.05/0.05) = 1 (bullish: retail deleveraging)
    margin = _margin(bal_first=1_000_000, bal_last=950_000)
    chip = an.chip_analysis(None, margin, None, None, VOL20)
    assert chip["components"]["margin"]["c"] == 1.0


def test_margin_short_ratio_signal_does_not_affect_score():
    margin = _margin(bal_first=1_000_000, bal_last=1_000_000, short_last=400_000)  # 40% short ratio
    chip = an.chip_analysis(None, margin, None, None, VOL20)
    assert chip["components"]["margin"]["c"] == 0.0
    texts = [s["text"] for s in chip["signals"]]
    assert "券資比偏高，留意軋空" in texts
    bias0 = [s for s in chip["signals"] if s["text"] == "券資比偏高，留意軋空"][0]
    assert bias0["bias"] == 0


def test_margin_all_zero_balance_excluded_from_coverage_no_exception():
    margin = _margin(bal_first=0, bal_last=0, short_last=0)   # ETF-like: no margin trading at all
    inst = _inst(100_000)
    chip = an.chip_analysis(inst, margin, None, None, VOL20)
    assert chip is not None                     # must not raise ZeroDivisionError
    assert "margin" not in chip["coverage"]
    assert "institutional" in chip["coverage"]


# ------------------------------------------------------------- shareholding --

def test_shareholding_positive_component():
    sh = _shareholding(ratio_first=70.0, ratio_last=71.0)   # +1.0pp -> c = 1
    chip = an.chip_analysis(None, None, sh, None, VOL20)
    assert chip["components"]["shareholding"]["c"] == 1.0
    assert chip["score"] == 100


def test_shareholding_negative_component():
    sh = _shareholding(ratio_first=70.0, ratio_last=69.0)   # -1.0pp -> c = -1
    chip = an.chip_analysis(None, None, sh, None, VOL20)
    assert chip["components"]["shareholding"]["c"] == -1.0


# --------------------------------------------------------------------- sbl --

def test_sbl_positive_component_when_balance_shrinks():
    # delta5 = -250,000; threshold for c=1 is 0.05 * 5*VOL20 = 250,000 -> shrink hits full score
    sbl = _sbl(bal_first=1_000_000, bal_last=750_000)
    chip = an.chip_analysis(None, None, None, sbl, VOL20)
    assert chip["components"]["sbl"]["c"] == 1.0


def test_sbl_negative_component_when_balance_grows():
    sbl = _sbl(bal_first=1_000_000, bal_last=1_250_000)
    chip = an.chip_analysis(None, None, None, sbl, VOL20)
    assert chip["components"]["sbl"]["c"] == -1.0


def test_sbl_and_institutional_excluded_when_vol20_missing():
    sbl = _sbl(bal_first=1_000_000, bal_last=1_250_000)
    inst = _inst(100_000)
    # both institutional and sbl require vol20_last; with it missing and no
    # margin/shareholding data either, every component is guarded out -> None
    chip = an.chip_analysis(inst, None, None, sbl, None)
    assert chip is None


def test_vol20_missing_excludes_only_vol_dependent_components():
    sbl = _sbl(bal_first=1_000_000, bal_last=1_250_000)
    inst = _inst(100_000)
    sh = _shareholding(ratio_first=70.0, ratio_last=71.0)
    chip = an.chip_analysis(inst, None, sh, sbl, None)   # vol20_last missing
    assert chip is not None
    assert chip["coverage"] == ["shareholding"]


# ------------------------------------------------------------- weak signal --

def test_weak_component_below_threshold_emits_no_signal():
    # alternating sign keeps the streak at 1 day (no +/-0.2 bonus) and net5 small,
    # so |c| stays well below the 0.3 emission threshold
    inst = [
        {"date": "2026-01-01", "foreign": 10_000, "trust": 0},
        {"date": "2026-01-02", "foreign": -10_000, "trust": 0},
        {"date": "2026-01-03", "foreign": 10_000, "trust": 0},
        {"date": "2026-01-04", "foreign": -10_000, "trust": 0},
        {"date": "2026-01-05", "foreign": 10_000, "trust": 0},
    ]
    # net5 = 10,000; c = clamp(10000 / (5*VOL20) / 0.10) = clamp(0.02) = 0.02
    chip = an.chip_analysis(inst, None, None, None, VOL20)
    assert abs(chip["components"]["institutional"]["c"] - 0.02) < 1e-9
    assert chip["signals"] == []


# ------------------------------------------------------------- no data --

def test_all_missing_returns_none():
    assert an.chip_analysis(None, None, None, None, VOL20) is None
    assert an.chip_analysis(None, None, None, None, None) is None


# ---------------------------------------------------------------- label_for --

def test_label_for_boundaries_match_asymmetric_bands():
    assert an.label_for(60) == "強多"
    assert an.label_for(59) == "偏多"
    assert an.label_for(20) == "偏多"
    assert an.label_for(19) == "中性"
    assert an.label_for(-19) == "中性"
    assert an.label_for(-20) == "偏空"      # asymmetric: -20 falls to 偏空, not 中性
    assert an.label_for(-59) == "偏空"
    assert an.label_for(-60) == "強空"
    assert an.label_for(-61) == "強空"


# --------------------------------------------------------- compute_attention --

def _result(signals=None, of=None, vp_position=None):
    return {"signals": signals or [], "of": of, "vp_position": vp_position}


def test_compute_attention_composite_from_tech_only_when_chip_missing():
    a = an.compute_attention(40, None, _result())
    assert a["priority"] == 24            # round(0.6*40 + 0 trigger points)
    assert a["action"] == "無變化"         # priority 24 < 25


def test_compute_attention_buy_action_needs_both_composite_and_priority():
    of = {"streak": 0, "divergence": "bearish"}
    result = _result(of=of, vp_position="above_va")
    a = an.compute_attention(40, None, result)
    # trigger_points = 15 (divergence) + 5 (VA) = 20; priority = round(24+20) = 44 -> still below 50
    assert a["priority"] == 44
    assert a["action"] == "觀察"          # composite>=40 alone isn't enough without priority>=50


def test_compute_attention_buy_action_when_priority_crosses_50():
    of = {"streak": 0, "divergence": "bearish"}
    signals = [{"text": "爆量：今日量為 20 日均量 2.5 倍", "bias": 0, "bt": "爆量(>2倍均量)"}]
    result = _result(signals=signals, of=of, vp_position="above_va")
    a = an.compute_attention(40, None, result)
    # trigger_points = 15 + 10 + 5 = 30; priority = round(24+30) = 54 >= 50, composite=40 >= 40
    assert a["priority"] == 54
    assert a["action"] == "準備進場"


def test_compute_attention_composite_below_40_falls_to_watch_despite_high_priority():
    of = {"streak": 0, "divergence": "bearish"}
    signals = [{"text": "爆量", "bias": 0, "bt": "爆量(>2倍均量)"}]
    result = _result(signals=signals, of=of, vp_position="above_va")
    a = an.compute_attention(39, None, result)   # composite=39 < 40
    assert a["priority"] >= 50
    assert a["action"] == "今日關注"


def test_compute_attention_sell_action():
    of = {"streak": 0, "divergence": "bearish"}
    signals = [{"text": "爆量", "bias": 0, "bt": "爆量(>2倍均量)"}]
    result = _result(signals=signals, of=of, vp_position="above_va")
    a = an.compute_attention(-40, None, result)
    assert a["priority"] >= 50
    assert a["action"] == "減碼警示"


def test_compute_attention_priority_exactly_25_is_watch_not_no_change():
    chip = {"score": 0, "components": {"margin": {"c": 0.6}}, "signals": [], "coverage": ["margin"]}
    of = {"streak": 3, "divergence": None}
    result = _result(of=of, vp_position="below_va")
    a = an.compute_attention(0, chip, result)
    # trigger_points = 10 (of streak>=3) + 10 (chip component |c|>=0.5) + 5 (VA) = 25
    assert a["priority"] == 25
    assert a["action"] == "觀察"


def test_compute_attention_reason_falls_back_to_strongest_signal_when_no_triggers():
    signals = [{"text": "均線多頭排列", "bias": 1, "bt": "均線多頭排列"},
               {"text": "價漲量增", "bias": 1, "bt": None}]
    result = _result(signals=signals)
    a = an.compute_attention(10, None, result)
    assert a["triggers"] == []
    assert a["reason"] == "均線多頭排列"    # first signal with the largest |bias| (tie -> first)


def test_compute_attention_reason_defaults_when_nothing_available():
    a = an.compute_attention(0, None, _result())
    assert a["reason"] == "無明顯訊號"
