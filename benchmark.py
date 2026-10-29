"""벤치마크 비교 — "이 전략에 우위(edge)가 있는가"를 판정하는 go/no-go 관문.

두 가지 기준과 비교한다:
  1) Buy & Hold: 그냥 사서 들고 있기.
  2) 무작위 진입 + 동일 리스크 관리: 진입/청산을 난수로 하되
     1% 사이징·ATR 손절 등 리스크 규칙은 실제 전략과 똑같이 적용한 N개의 봇.

핵심 논리(투자 리서처 원칙): 내 전략이 '무작위 진입'을 이기지 못하면,
수익은 전략이 아니라 리스크 관리에서 나온 것이고 전략 자체엔 우위가 없다.
전략 성과가 무작위 분포의 상위 5%(95 백분위) 밖이면 '우위 근거 있음'으로 본다.
"""
import numpy as np

from backtest import run_backtest, metrics


def buy_and_hold(df, warmup):
    """워밍업 이후 구간을 그냥 보유했을 때의 총수익률."""
    sub = df.iloc[warmup:]
    if len(sub) < 2:
        return 0.0
    return float(sub["close"].iloc[-1] / sub["close"].iloc[0] - 1)


def _warmup_of(params, atr_period):
    return max(int(params.get("slow_ema", 0)),
              int(params.get("sma_period", 0)),
              int(params.get("lookback", 0)), atr_period) + 2


def make_random_strategy(seed, p_enter=0.04, p_exit=0.10):
    """무작위 진입/청산 시그널 생성기. 리스크 관리는 backtest 엔진이 동일 적용."""
    rng = np.random.default_rng(seed)

    def sig(df, params, position=None):
        if position is None:
            r = rng.random()
            if r < p_enter / 2:
                return "enter_long"
            if r < p_enter:
                return "enter_short"
            return "hold"
        return "exit" if rng.random() < p_exit else "hold"

    return sig


def random_distribution(df, params, config, symbol, trials=200, seed0=0):
    """무작위 봇 trials개를 돌려 총수익률·샤프 분포를 만든다."""
    atr_period = config["risk_indicators"]["atr_period"]
    rets, sharpes = [], []
    for t in range(trials):
        strat = make_random_strategy(seed0 + t)
        trades, curve = run_backtest(df, strat, params, config, symbol=symbol)
        m = metrics(trades, curve)
        rets.append(m["total_return"])
        sharpes.append(m["sharpe"])
    return np.array(rets), np.array(sharpes)


def percentile_of(value, distribution):
    """value 가 분포에서 상위 몇 백분위인지(0~100)."""
    if len(distribution) == 0:
        return float("nan")
    return float((distribution < value).mean() * 100)


def evaluate_asset(df, strategy_name, params, config, symbol, trials=200):
    atr_period = config["risk_indicators"]["atr_period"]
    warmup = _warmup_of(params, atr_period)

    trades, curve = run_backtest(df, strategy_name, params, config, symbol=symbol)
    m = metrics(trades, curve)
    strat_ret = m["total_return"]
    strat_sharpe = m["sharpe"]

    bh = buy_and_hold(df, warmup)
    rnd_ret, rnd_sharpe = random_distribution(df, params, config, symbol, trials=trials)

    pct_ret = percentile_of(strat_ret, rnd_ret)
    pct_sharpe = percentile_of(strat_sharpe, rnd_sharpe)

    beats_bh = strat_ret > bh
    beats_random = pct_ret >= 95.0  # 무작위 대비 상위 5% 안
    verdict = "우위 근거 있음" if (beats_bh and beats_random) else "우위 근거 없음"

    return {
        "symbol": symbol,
        "trades": m["trades"],
        "strat_return": strat_ret,
        "buy_hold": bh,
        "random_median": float(np.median(rnd_ret)),
        "random_p95": float(np.percentile(rnd_ret, 95)),
        "pct_vs_random": pct_ret,
        "pct_sharpe_vs_random": pct_sharpe,
        "beats_bh": beats_bh,
        "beats_random": beats_random,
        "verdict": verdict,
    }
