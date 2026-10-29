"""워크포워드 검증 — 데이터를 여러 '안 본 구간'으로 쪼개 반복 채점한다.

한 구간에서만 우위처럼 보이는 건 운, 여러 구간(상승장·하락장 모두)에서
꾸준히 무작위+Buy&Hold를 이겨야 진짜 실마리다.

각 fold(구간)마다 앞부분은 지표 워밍업용 lead-in으로 쓰고,
그 뒤 test 구간의 성과만 evaluate_asset 으로 채점한다(진입~채점 분리).
"""
from benchmark import evaluate_asset, _warmup_of


def walk_forward(df, strategy_name, params, config, symbol,
                 window_months=6, trials=30, bars_per_month=30.4):
    atr_period = config["risk_indicators"]["atr_period"]
    warmup = _warmup_of(params, atr_period)
    window_bars = max(int(window_months * bars_per_month), 12)
    n = len(df)
    folds = []
    i = warmup
    while i + window_bars <= n:
        sl = df.iloc[i - warmup: i + window_bars]
        r = evaluate_asset(sl, strategy_name, params, config, symbol, trials=trials)
        a = df.index[i]
        b = df.index[min(i + window_bars - 1, n - 1)]
        label = f"{_d(a)}~{_d(b)}"
        folds.append({"period": label, **r})
        i += window_bars
    return folds


def _d(ts):
    try:
        return str(ts.date())
    except Exception:  # noqa: BLE001
        return str(ts)[:10]
