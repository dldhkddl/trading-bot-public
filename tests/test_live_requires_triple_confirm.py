"""실계좌 3중 잠금 검증.
세 조건(ALPACA_PAPER=false + ALPACA_LIVE_CONFIRM + --live) 중
하나라도 빠지면 모의로 강등되고, 셋 다 있어야만 실계좌 경로가 열린다."""
import itertools
import broker

PHRASE = broker.LIVE_CONFIRM_PHRASE


def _env(paper_false, confirm_ok):
    return {
        "ALPACA_PAPER": "false" if paper_false else "true",
        "ALPACA_LIVE_CONFIRM": PHRASE if confirm_ok else "",
    }


def test_all_three_present_opens_live():
    d = broker.resolve_mode(_env(True, True), live_flag=True)
    assert d.is_live is True
    assert d.mode == "live"
    assert d.endpoint == broker.LIVE_ENDPOINT


def test_missing_any_one_downgrades_to_paper():
    # (paper_false, confirm_ok, live_flag)의 8가지 조합 중 (T,T,T)만 live
    for paper_false, confirm_ok, live_flag in itertools.product([True, False], repeat=3):
        d = broker.resolve_mode(_env(paper_false, confirm_ok), live_flag=live_flag)
        if paper_false and confirm_ok and live_flag:
            assert d.is_live is True
        else:
            assert d.is_live is False, (paper_false, confirm_ok, live_flag)
            assert d.mode == "paper"
            assert d.endpoint == broker.PAPER_ENDPOINT
            assert d.reasons  # 강등 사유가 남아야 함


def test_default_env_is_paper():
    # 환경변수가 하나도 없으면 무조건 모의
    d = broker.resolve_mode({}, live_flag=True)
    assert d.is_live is False
    assert d.endpoint == broker.PAPER_ENDPOINT


def test_wrong_confirm_phrase_blocks_live():
    env = {"ALPACA_PAPER": "false", "ALPACA_LIVE_CONFIRM": "yes_please"}
    d = broker.resolve_mode(env, live_flag=True)
    assert d.is_live is False
