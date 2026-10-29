"""하락장 방어 오버레이 — 추세 필터(장기 이동평균) 기반 '보유 vs 현금' 전략.

규칙: 가격 > 장기 이평선이면 풀 투자(자산 수익을 그대로 받음),
      가격 < 이평선이면 현금(수익 0). 전환 시 비용 차감.
미래참조 방지: 신호는 전일 종가 기준으로 판단해 다음 봉부터 적용(shift 1).

평가: 이건 '무작위를 이기나'가 아니라 Buy&Hold 대비
      수익 추종·낙폭 감소·위험대비수익(Calmar/Sharpe)으로 판정한다.
"""
import numpy as np


def run_overlay(df, ma_period, cost_bps=5.0):
    close = df["close"]
    ma = close.rolling(ma_period).mean()
    in_mkt = (close > ma).shift(1, fill_value=False).astype(float)   # 전일 기준 → 다음 봉 적용
    ret = close.pct_change().fillna(0.0)
    switches = in_mkt.diff().abs().fillna(0.0)                    # 진입/청산 전환 횟수
    cost = switches * (cost_bps / 10000.0)
    strat_ret = ret * in_mkt - cost
    strat_eq = (1 + strat_ret).cumprod()
    bh_eq = (1 + ret).cumprod()
    return {
        "strat_eq": strat_eq, "bh_eq": bh_eq,
        "strat_ret": strat_ret, "bh_ret": ret, "in_mkt": in_mkt,
        "time_in_market": float(in_mkt.mean()),
        "switches": int(switches.sum()),
    }


def stats(eq, ret, ann):
    total = float(eq.iloc[-1] - 1.0)
    roll_max = eq.cummax()
    mdd = float(((roll_max - eq) / roll_max).max())
    sharpe = float(ret.mean() / ret.std() * np.sqrt(ann)) if ret.std() > 0 else 0.0
    calmar = (total / mdd) if mdd > 0 else float("inf")
    return {"total": total, "mdd": mdd, "sharpe": round(sharpe, 2),
            "calmar": round(calmar, 2)}


def _d(ts):
    try:
        return str(ts.date())
    except Exception:  # noqa: BLE001
        return str(ts)[:10]


def overlay_walk_forward(df, ma_period, window_bars, cost_bps, ann):
    """여러 안 본 구간에서 오버레이 vs B&H 를 반복 채점한다.
    각 구간 앞에 ma_period 만큼 lead-in 을 붙여 이평을 워밍업한다."""
    n = len(df)
    folds = []
    i = ma_period
    while i + window_bars <= n:
        sl = df.iloc[i - ma_period: i + window_bars]
        r = run_overlay(sl, ma_period, cost_bps)
        w = window_bars
        s_ret = r["strat_ret"].iloc[-w:]
        b_ret = r["bh_ret"].iloc[-w:]
        im = r["in_mkt"].iloc[-w:]
        st = stats((1 + s_ret).cumprod(), s_ret, ann)
        bh = stats((1 + b_ret).cumprod(), b_ret, ann)
        dd_cut = (1 - st["mdd"] / bh["mdd"]) if bh["mdd"] > 0 else 0.0
        folds.append({"period": f"{_d(sl.index[-w])}~{_d(sl.index[-1])}",
                      "bh": bh, "st": st, "dd_cut": dd_cut,
                      "tim": float(im.mean())})
        i += window_bars
    return folds
