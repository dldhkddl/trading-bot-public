"""10분 위험 감시 검증: 룩어헤드 방지, 분류, 주기, 중복 억제."""
import datetime as dt
from types import SimpleNamespace
import pandas as pd

import watch


def _df(values):
    idx = pd.date_range("2024-01-01", periods=len(values), freq="1D", tz="UTC")
    return pd.DataFrame({"close": values}, index=idx)


def test_defense_line_excludes_todays_unfinished_bar():
    # 마지막 봉(오늘 진행중)이 극단값이어도 방어선에 반영되면 안 된다
    df = _df([100.0] * 250 + [9999.0])
    assert watch.defense_line(df, 200, incomplete_last=True) == 100.0


def test_defense_line_keeps_latest_bar_when_market_is_closed():
    df = _df([100.0] * 199 + [200.0])
    assert watch.defense_line(df, 200, incomplete_last=False) == 100.5


def test_defense_line_none_when_not_enough_bars():
    assert watch.defense_line(_df([100.0] * 50), 200) is None


def test_classify_stages():
    # 보유 + 방어선 아래 = 이탈
    assert watch.classify(98, 100, held=True)[0] == watch.BREACHED
    # 1% 이내 = 임박
    assert watch.classify(100.5, 100, held=True)[0] == watch.IMMINENT
    assert watch.classify(99.5, 100, held=False)[0] == watch.IMMINENT
    # 1~3% = 관찰
    assert watch.classify(102, 100, held=False)[0] == watch.WATCH
    # 미보유 + 3% 초과 위 = 진입후보
    assert watch.classify(110, 100, held=False)[0] == watch.CANDIDATE
    # 보유 + 3% 초과 위 = 안정
    assert watch.classify(110, 100, held=True)[0] == watch.STABLE


def test_describe_mentions_intraday_provisional_for_breach():
    stage, d = watch.classify(97.9, 100, held=True)
    msg = watch.describe("SPY", stage, d, held=True)
    assert "이탈" in msg and "종가 확인 필요" in msg


def test_describe_imminent_predicts_next_close():
    stage, d = watch.classify(100.8, 100, held=True)
    msg = watch.describe("QQQ", stage, d, held=True)
    assert "임박" in msg and "매도 신호 가능" in msg


def test_interval_crypto_always_ten_minutes():
    now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    last = now - dt.timedelta(minutes=9)
    # 코인은 장 개폐와 무관하게 10분
    assert not watch.should_check("BTC/USD", False, last, now)
    assert watch.should_check("BTC/USD", False, now - dt.timedelta(minutes=11), now)


def test_interval_etf_slower_when_market_closed():
    now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    last = now - dt.timedelta(minutes=30)
    # 장중이면 10분 주기라 검사, 장외면 1시간 주기라 아직 아님
    assert watch.should_check("SPY", True, last, now)
    assert not watch.should_check("SPY", False, last, now)
    assert watch.should_check("SPY", False, now - dt.timedelta(minutes=61), now)


def test_last_bar_incomplete_is_asset_and_market_aware():
    now = dt.datetime(2024, 1, 10, 18, tzinfo=dt.timezone.utc)
    today = _df([100.0] * 3)
    today.index = pd.date_range("2024-01-08", periods=3, freq="1D", tz="UTC")
    assert watch.last_bar_is_incomplete(today, "BTC/USD", False, now)
    assert watch.last_bar_is_incomplete(today, "SPY", True, now)
    assert not watch.last_bar_is_incomplete(today, "SPY", False, now)


def test_stale_threshold_matches_asset_schedule():
    assert watch.stale_after("BTC/USD", False) == 1200
    assert watch.stale_after("SPY", True) == 1200
    assert watch.stale_after("SPY", False) == 4200


def test_timestamp_parser_is_fail_closed():
    assert watch.parse_timestamp("not-a-time") is None
    parsed = watch.parse_timestamp("2024-01-01T00:00:00+00:00")
    assert parsed.tzinfo is not None


def test_watch_targets_include_unconfigured_held_assets():
    positions = [
        SimpleNamespace(symbol="TLT", asset_class="us_equity"),
        SimpleNamespace(symbol="BTCUSD", asset_class="crypto"),
    ]
    assert watch.merge_targets(["SPY"], positions) == ["SPY", "TLT", "BTC/USD"]


def test_event_recorded_only_on_stage_change(tmp_path):
    log = tmp_path / "events.log"
    stages = {}
    assert watch.record_event("QQQ", watch.WATCH, "관찰 · QQQ", stages, log)
    # 같은 단계 반복은 기록하지 않는다
    assert not watch.record_event("QQQ", watch.WATCH, "관찰 · QQQ", stages, log)
    # 단계가 바뀌면 다시 기록
    assert watch.record_event("QQQ", watch.IMMINENT, "임박 · QQQ", stages, log)
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_health_failure_and_recovery_are_both_logged(tmp_path):
    log = tmp_path / "events.log"
    health = {}
    assert watch.record_health_event("QQQ", "failed", "network", health, log)
    assert not watch.record_health_event("QQQ", "failed", "network", health, log)
    assert watch.record_health_event("QQQ", "ok", "recovered", health, log)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "failed" in lines[0] and "recovered" in lines[1]


def test_state_save_is_atomic(tmp_path):
    p = tmp_path / "watch.json"
    watch.save_state({"assets": {}, "stages": {}}, p)
    assert p.exists() and not (tmp_path / "watch.json.tmp").exists()


def test_corrupt_state_is_flagged(tmp_path):
    p = tmp_path / "watch.json"
    p.write_text("{ broken", encoding="utf-8")
    assert watch.load_state(p).get("corrupt") is True
