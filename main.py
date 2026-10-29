"""트레이딩 봇 진입점(CLI).

명령:
  python main.py backtest --months 6 [--slippage-bps N]
  python main.py paper --dry-run
  python main.py paper --once
  python main.py live --once            # 3중 잠금 없으면 자동으로 paper로 강등
  python main.py status
  python main.py stop --close-all
  python main.py resume

안전 기본값: 환경변수가 없으면 무조건 모의 엔드포인트로만 접속한다.
"""
import os
import sys
import time
import argparse
import datetime as dt

import yaml
from dotenv import load_dotenv

# 한글 Windows 콘솔(cp949)에서 이모지 출력 시 UnicodeEncodeError 로 죽는 것을 방지.
# 인코딩 불가 문자는 대체 문자로 출력하고 프로그램은 계속 진행한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import broker
import portfolio
from risk import manager as risk


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env():
    load_dotenv()
    return dict(os.environ)


def _print_cost_assumptions(cfg):
    c = cfg["costs"]
    print(f"[가정] 수수료 {c['commission_bps']}bps · 슬리피지 {c['slippage_bps']}bps "
          f"· 거래당 위험 {cfg['account']['risk_per_trade']:.0%} "
          f"· 최대낙폭 정지 {cfg['account']['max_drawdown_stop']:.0%}")


# ---------------- backtest ----------------
def cmd_backtest(args, cfg, env):
    import pandas as pd
    from data import get_bars
    from backtest import run_backtest, metrics

    if args.slippage_bps is not None:
        cfg["costs"]["slippage_bps"] = args.slippage_bps
    _print_cost_assumptions(cfg)

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)
    os.makedirs("results", exist_ok=True)

    all_trades = []
    print(f"\n=== {args.months}개월 백테스트 (슬리피지 {cfg['costs']['slippage_bps']}bps) ===")
    header = f"{'자산':<10}{'거래':>6}{'승률':>8}{'손익비':>8}{'MaxDD':>8}{'샤프':>7}{'총수익':>9}"
    print(header)
    for symbol, a in cfg["assets"].items():
        df = get_bars(symbol, a["timeframe"], start, end, env=env, allow_synthetic=True)
        trades, curve = run_backtest(df, a["strategy"], a["params"], cfg, symbol=symbol)
        m = metrics(trades, curve)
        all_trades.extend(trades)
        print(f"{symbol:<10}{m['trades']:>6}{m['win_rate']:>8.0%}"
              f"{str(m['profit_factor']):>8}{m['max_drawdown']:>8.1%}"
              f"{m['sharpe']:>7}{m['total_return']:>9.1%}")

    if all_trades:
        pd.DataFrame(all_trades).to_csv("results/backtest_trades.csv", index=False)
        print(f"\n총 {len(all_trades)}건 → results/backtest_trades.csv 저장")
    else:
        print("\n거래 0건 — 신호 조건이나 워밍업 구간을 확인하세요.")
    print("\n주의: 6개월은 1차 걸러내기일 뿐, 합격증이 아닙니다.")


# ---------------- check (연결 확인) ----------------
def cmd_check(args, cfg, env):
    """모의계좌 연결과 잔고 조회를 확인한다. 키 값은 절대 출력하지 않는다."""
    has_key = bool(env.get("ALPACA_API_KEY")) and env.get("ALPACA_API_KEY") not in (
        "", "여기에_모의계좌_키")
    has_sec = bool(env.get("ALPACA_SECRET_KEY")) and env.get("ALPACA_SECRET_KEY") not in (
        "", "여기에_모의계좌_시크릿")
    paper = str(env.get("ALPACA_PAPER", "true")).lower()
    print(f"키 입력됨: {has_key} / 시크릿 입력됨: {has_sec} / 모의 모드(ALPACA_PAPER): {paper}")
    if not (has_key and has_sec):
        print("⛔ .env 에 키가 아직 안 들어갔습니다. .env 파일을 열어 값을 넣어주세요.")
        return
    if paper != "true":
        print("⚠️ ALPACA_PAPER 가 true 가 아닙니다. 모의 확인 단계에서는 true 로 두세요.")

    try:
        b = broker.AlpacaBroker(env, live_flag=False)
        acct = b.account()
        status = getattr(acct, "status", "?")
        equity = float(getattr(acct, "equity", 0.0))
        cash = float(getattr(acct, "cash", 0.0))
        print(f"✅ 연결 성공! 계좌 상태: {status} / 잔고(equity): {equity:,.2f} / 현금: {cash:,.2f}")
        print("   상태가 ACTIVE 이고 잔고가 보이면 정상입니다.")
    except ModuleNotFoundError:
        print("⛔ alpaca-py 미설치. 먼저: pip install -r requirements.txt")
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 연결 실패: {type(e).__name__}: {e}")
        print("   키가 '모의계좌' 키가 맞는지, 오타가 없는지 확인하세요.")


# ---------------- compare (전략 여러 개 비교, 코인) ----------------
COMPARE_ASSETS = ["BTC/USD", "ETH/USD", "LTC/USD", "BCH/USD"]
COMPARE_STRATS = {
    "추세추종": ("trend_following", {"fast_ema": 50, "slow_ema": 200, "atr_mult": 3.0, "trailing": True}),
    "돌파":   ("breakout", {"lookback": 20, "vol_mult": 1.5, "atr_mult": 2.0, "trailing": True}),
    "평균회귀": ("mean_reversion", {"sma_period": 20, "entry_std": 2.0, "atr_mult": 2.0, "trailing": False}),
}

# 주봉(1Week)용 파라미터 — 봉이 길어 기간을 짧게 잡는다
COMPARE_STRATS_WEEK = {
    "추세추종": ("trend_following", {"fast_ema": 10, "slow_ema": 40, "atr_mult": 3.0, "trailing": True}),
    "돌파":   ("breakout", {"lookback": 10, "vol_mult": 1.5, "atr_mult": 2.0, "trailing": True}),
    "평균회귀": ("mean_reversion", {"sma_period": 10, "entry_std": 2.0, "atr_mult": 2.0, "trailing": False}),
}

BARS_PER_MONTH = {"1Day": 30.4, "1Week": 4.34}


def _strats_for(tf):
    return COMPARE_STRATS_WEEK if tf == "1Week" else COMPARE_STRATS


# 미주 ETF 후보 (오버레이 검증용) — 섹터·지역·자산군을 고루 포함
US_ASSETS = ["SPY", "QQQ", "IWM", "GLD", "TLT", "EFA"]

US_SCREEN_POOL = [
    # 광범위 지수
    "SPY", "QQQ", "IWM", "DIA", "VTI", "MDY",
    # 섹터
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
    # 성장·테마
    "SMH", "IGV", "XBI", "ITB", "KRE", "OIH",
    # 지역·해외
    "EFA", "EEM", "EWJ", "EWY", "FXI", "VGK", "INDA",
    # 자산군
    "GLD", "SLV", "TLT", "IEF", "VNQ", "DBC", "HYG",
]


def _overlay_assets(cfg, args=None):
    """운영 자산: --us 면 미주, 아니면 config.yaml 의 overlay_assets."""
    if args is not None and getattr(args, "us", False):
        return US_ASSETS
    return cfg.get("overlay_assets") or COMPARE_ASSETS


def cmd_compare(args, cfg, env):
    from data import get_bars
    from benchmark import evaluate_asset

    tf = args.timeframe
    strats = _strats_for(tf)
    _print_cost_assumptions(cfg)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)
    tfname = "주봉" if tf == "1Week" else "일봉"
    print(f"\n=== 전략 비교 (코인 {args.months}개월 {tfname}, 무작위 {args.trials}회) ===")
    print("각 칸: 전략수익 / 무작위대비 백분위 / 판정. 우위=무작위 95%+ & B&H 초과.\n")

    edges = []
    for a in COMPARE_ASSETS:
        try:
            df = get_bars(a, tf, start, end, env=env, allow_synthetic=False)
        except Exception as e:  # noqa: BLE001
            print(f"[{a}] 데이터 실패({type(e).__name__}) — 건너뜀")
            continue
        print(f"■ {a}")
        bh_shown = False
        for label, (sname, params) in strats.items():
            r = evaluate_asset(df, sname, params, cfg, a, trials=args.trials)
            if not bh_shown:
                print(f"   (Buy&Hold: {r['buy_hold']:+.1%})")
                bh_shown = True
            mark = "✅우위" if r["verdict"] == "우위 근거 있음" else "—"
            print(f"   {label:<6} 수익 {r['strat_return']:+7.1%} | 무작위대비 {r['pct_vs_random']:>3.0f}% | {mark}")
            if r["verdict"] == "우위 근거 있음":
                edges.append((a, label, r["strat_return"]))
        print()

    if edges:
        print("우위 신호가 나온 조합:")
        for a, l, ret in edges:
            print(f"  - {a} / {l} : {ret:+.1%}")
        print("→ 이 조합만 워크포워드 검증으로 넘깁니다(그래도 확정 아님).")
    else:
        print("우위 신호가 나온 조합이 하나도 없습니다.")
        print("→ 코인에서도 이 전략류로는 우위를 못 찾았습니다. 전략기획팀 재논의 필요.")
    print("\n주의: 데이터 실패한 자산은 제외됨. 실데이터에서만 판정이 유효합니다.")


# ---------------- benchmark (우위 판정) ----------------
def cmd_benchmark(args, cfg, env):
    from data import get_bars
    from benchmark import evaluate_asset

    _print_cost_assumptions(cfg)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)

    print(f"\n=== 벤치마크 비교 ({args.months}개월, 무작위 시행 {args.trials}회) ===")
    print("전략이 (1) Buy&Hold 를 이기고 (2) 무작위 진입 상위 5% 안에 들어야 '우위 근거 있음'.\n")
    header = (f"{'자산':<10}{'전략수익':>9}{'B&H':>9}"
              f"{'무작위중앙':>11}{'무작위P95':>10}{'무작위대비':>10}  판정")
    print(header)

    results = []
    for symbol, a in cfg["assets"].items():
        df = get_bars(symbol, a["timeframe"], start, end, env=env, allow_synthetic=True)
        r = evaluate_asset(df, a["strategy"], a["params"], cfg, symbol, trials=args.trials)
        results.append(r)
        print(f"{symbol:<10}{r['strat_return']:>8.1%}{r['buy_hold']:>9.1%}"
              f"{r['random_median']:>10.1%}{r['random_p95']:>10.1%}"
              f"{r['pct_vs_random']:>9.0f}%  {r['verdict']}")

    n_edge = sum(1 for r in results if r["verdict"] == "우위 근거 있음")
    print(f"\n판정 요약: {len(results)}개 자산 중 {n_edge}개만 '우위 근거 있음'.")
    if n_edge == 0:
        print("→ 어떤 자산에서도 무작위+동일리스크관리를 이기지 못했습니다.")
        print("  수익이 났다면 전략이 아니라 리스크 관리 덕분입니다. 실계좌로 가지 마세요.")
    else:
        print("→ 우위 신호가 있는 자산만 워크포워드 검증으로 넘기세요(그래도 확정은 아님).")
    print("\n주의: 합성 데이터로 돌리면 결과는 무의미합니다. 실데이터에서만 판정이 유효합니다.")


# ---------------- walkforward (강화 검증) ----------------
def _params_for(strategy_name, tf="1Day"):
    for _, (sn, pr) in _strats_for(tf).items():
        if sn == strategy_name:
            return dict(pr)
    return {}


def cmd_walkforward(args, cfg, env):
    from data import get_bars
    from walkforward import walk_forward

    tf = args.timeframe
    params = _params_for(args.strategy, tf)
    _print_cost_assumptions(cfg)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)
    try:
        df = get_bars(args.asset, tf, start, end, env=env, allow_synthetic=False)
    except Exception as e:  # noqa: BLE001
        print(f"[{args.asset}] 데이터 실패({type(e).__name__}) — 중단")
        return

    tfname = "주봉" if tf == "1Week" else "일봉"
    print(f"\n=== 워크포워드: {args.asset} / {args.strategy} / {tfname} "
          f"({args.months}개월, 창 {args.window}개월, 무작위 {args.trials}회) ===")
    print("각 창은 '안 본 구간'. 여러 창에서 꾸준히 이겨야 진짜 실마리.\n")
    print(f"{'구간':<24}{'전략수익':>9}{'B&H':>9}{'무작위대비':>10}  판정")

    folds = walk_forward(df, args.strategy, params, cfg, args.asset,
                         window_months=args.window, trials=args.trials,
                         bars_per_month=BARS_PER_MONTH[tf])
    if not folds:
        print("데이터가 부족해 창을 만들 수 없습니다. --months 를 늘려보세요.")
        return

    n_edge = 0
    for f in folds:
        mark = "✅우위" if f["verdict"] == "우위 근거 있음" else "—"
        if f["verdict"] == "우위 근거 있음":
            n_edge += 1
        print(f"{f['period']:<24}{f['strat_return']:>8.1%}{f['buy_hold']:>9.1%}"
              f"{f['pct_vs_random']:>9.0f}%  {mark}")

    k = len(folds)
    beat_bh = sum(1 for f in folds if f["beats_bh"])
    print(f"\n요약: {k}개 창 중 우위 {n_edge}개 / B&H 초과 {beat_bh}개")
    if n_edge >= max(2, (k + 1) // 2):
        print("→ 여러 구간에서 우위 유지. 드문 경우입니다. 다음: 다른 코인·전략에도 재현되는지 확인.")
    elif n_edge >= 1:
        print("→ 일부 구간만 우위. 아직 운과 구분 안 됨. 다른 창·자산에서 재현 필요.")
    else:
        print("→ 어떤 안 본 구간에서도 우위 없음. 이 실마리는 여기서 정직하게 종료 대상.")
    print("\n주의: 실데이터·충분한 기간에서만 유효. 통과해도 미래 수익 보장 아님.")


# ---------------- overlay (하락장 방어) ----------------
DEFAULT_MA = {"1Day": 200, "1Week": 40}
ANN_FACTOR = {"1Day": 365, "1Week": 52}


def cmd_overlay(args, cfg, env):
    from data import get_bars
    from overlay import run_overlay, stats

    tf = args.timeframe
    ma = args.ma if args.ma else DEFAULT_MA[tf]
    ann = ANN_FACTOR[tf]
    cost = cfg["costs"]["slippage_bps"]
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)

    tfname = "주봉" if tf == "1Week" else "일봉"
    print(f"\n=== 하락장 방어 오버레이 (코인 {args.months}개월 {tfname}, {ma}{tfname[0]} 이평 필터) ===")
    print("목표: 상승장은 따라가되 큰 낙폭은 회피. Buy&Hold 대비 낙폭·위험대비수익으로 판정.\n")
    print(f"{'자산':<9}{'B&H수익':>9}{'B&H낙폭':>9}{'방어수익':>9}{'방어낙폭':>9}"
          f"{'낙폭↓':>8}{'B&H칼마':>8}{'방어칼마':>8}{'체류%':>7}")

    wins = 0
    total_assets = 0
    for a in _overlay_assets(cfg, args):
        try:
            df = get_bars(a, tf, start, end, env=env, allow_synthetic=False)
        except Exception as e:  # noqa: BLE001
            print(f"{a:<9} 데이터 실패({type(e).__name__}) — 건너뜀")
            continue
        r = run_overlay(df, ma, cost_bps=cost)
        bh = stats(r["bh_eq"], r["bh_ret"], ann)
        st = stats(r["strat_eq"], r["strat_ret"], ann)
        dd_cut = (1 - st["mdd"] / bh["mdd"]) if bh["mdd"] > 0 else 0.0
        total_assets += 1
        # '방어 성공' = 낙폭을 의미 있게(30%+) 줄이고, 위험대비수익(칼마)이 개선
        good = dd_cut >= 0.30 and st["calmar"] >= bh["calmar"]
        if good:
            wins += 1
        print(f"{a:<9}{bh['total']:>8.0%}{bh['mdd']:>9.0%}{st['total']:>9.0%}"
              f"{st['mdd']:>9.0%}{dd_cut:>7.0%}{bh['calmar']:>8}{st['calmar']:>8}"
              f"{r['time_in_market']*100:>6.0f}%")

    print(f"\n요약: {total_assets}개 중 {wins}개에서 '방어 성공'(낙폭 30%+ 감소 & 칼마 개선).")
    print("칼마 = 수익/최대낙폭. 높을수록 위험 대비 효율적. 체류% = 시장에 타 있던 비율.")
    print("\n주의: 상승장 수익 일부는 포기합니다. 목표는 '이기기'가 아니라 '덜 다치기'입니다.")


# ---------------- overlaywf (방어 오버레이 워크포워드) ----------------
def cmd_overlaywf(args, cfg, env):
    from data import get_bars
    from overlay import overlay_walk_forward

    tf = args.timeframe
    ma = args.ma if args.ma else DEFAULT_MA[tf]
    ann = ANN_FACTOR[tf]
    cost = cfg["costs"]["slippage_bps"]
    wbars = max(int(args.window * BARS_PER_MONTH[tf]), ma // 2)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)
    assets = [args.asset] if args.asset else _overlay_assets(cfg, args)
    tfname = "주봉" if tf == "1Week" else "일봉"

    print(f"\n=== 방어 오버레이 워크포워드 ({tfname} {ma}, 창 {args.window}개월) ===")
    print("각 구간(안 본 시기)마다 낙폭을 줄였는지 확인. 여러 시기에서 꾸준해야 진짜.\n")

    total_w, total_ok = 0, 0
    for a in assets:
        try:
            df = get_bars(a, tf, start, end, env=env, allow_synthetic=False)
        except Exception as e:  # noqa: BLE001
            print(f"[{a}] 데이터 실패({type(e).__name__}) — 건너뜀")
            continue
        folds = overlay_walk_forward(df, ma, wbars, cost, ann)
        if not folds:
            print(f"[{a}] 구간 부족 — 건너뜀")
            continue
        print(f"■ {a}")
        print(f"   {'구간':<24}{'B&H낙폭':>8}{'방어낙폭':>9}{'낙폭↓':>7}"
              f"{'B&H칼마':>8}{'방어칼마':>8}{'체류%':>7}")
        ok = 0
        for f in folds:
            good = f["dd_cut"] >= 0.30 and f["st"]["calmar"] >= f["bh"]["calmar"]
            if good:
                ok += 1
            mark = "✅" if good else "—"
            print(f"   {f['period']:<24}{f['bh']['mdd']:>7.0%}{f['st']['mdd']:>9.0%}"
                  f"{f['dd_cut']:>7.0%}{f['bh']['calmar']:>8}{f['st']['calmar']:>8}"
                  f"{f['tim']*100:>6.0f}% {mark}")
        total_w += len(folds)
        total_ok += ok
        print(f"   → {a}: {len(folds)}개 구간 중 {ok}개에서 방어 성공\n")

    if total_w:
        print(f"종합: 전체 {total_w}개 구간 중 {total_ok}개({total_ok/total_w*100:.0f}%)에서 방어 성공.")
        if total_ok / total_w >= 0.6:
            print("→ 여러 시기에서 꾸준히 방어. 이 오버레이를 운영 전략 후보로. 다음: 모의투자.")
        elif total_ok / total_w >= 0.3:
            print("→ 일부 시기만 방어. 특정 국면(폭락기)에 치우쳤을 가능성. 더 검토 필요.")
        else:
            print("→ 꾸준하지 않음. 앞선 좋은 결과는 한두 시기의 운이었을 수 있음.")
    print("\n주의: 낙폭 감소는 반복적 이득이나, 수익은 국면에 따라 B&H에 질 수도 있습니다.")


# ---------------- run (오버레이 모의 운영) ----------------
def cmd_run(args, cfg, env):
    from live_overlay import run_once

    tf = args.timeframe
    ma = args.ma if args.ma else DEFAULT_MA[tf]
    mode = "계획만(dry-run)" if args.dry_run else "모의 주문 실행"
    print(f"\n=== 오버레이 모의 운영 ({tf} {ma} 이평 필터, {mode}) ===")
    print("규칙: 가격 > 이평 → 보유, 아래 → 현금. 모의(paper) 전용.\n")
    try:
        assets = _overlay_assets(cfg, args)
        cycle = run_once(env, assets, ma, tf,
                         dry_run=args.dry_run, months=args.months)
    except ModuleNotFoundError:
        print("⛔ alpaca-py 미설치. 먼저: pip install -r requirements.txt")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 실행 실패: {type(e).__name__}: {e}")
        return 2

    if cycle.fatal_error:
        print(f"\n⛔ 오버레이 치명적 실패: {cycle.fatal_error}")
        print("상태 파일에서 상세 결과를 확인하세요.")
        return cycle.exit_code

    print(f"모의계좌 잔고: {cycle.equity:,.2f} / "
          f"자산별 목표비중: {cycle.equity/max(len(assets),1):,.0f}\n")
    print(f"{'자산':<9}{'신호':>5}{'현재가':>12}{'이평':>12}{'보유':>6}"
          f"{'행동':>10}{'결과':>14}")
    for result in cycle.assets:
        signal = result.signal or "?"
        price = result.price if result.price is not None else 0.0
        moving_average = result.ma if result.ma is not None else 0.0
        held = "예" if result.held else "아니오"
        print(f"{result.asset:<9}{signal:>5}{price:>12,.2f}{moving_average:>12,.2f}"
              f"{held:>6}{result.action:>10}{result.status:>14}")

    counts = cycle.counts
    print("\n실행 요약: "
          f"확인 {counts.get('confirmed', 0)}건 / "
          f"보류 {counts.get('deferred', 0)}건 / "
          f"계획 {counts.get('dry_run', 0)}건 / "
          f"무동작 {counts.get('no_action', 0)}건 / "
          f"실패 {counts.get('failed', 0) + counts.get('unconfirmed', 0)}건")

    failures = [r for r in cycle.assets if r.status in {"failed", "unconfirmed"}]
    if failures:
        print("⛔ 확인이 필요한 실패:")
        for result in failures:
            code = result.error_code or "UNKNOWN_ERROR"
            detail = result.error or "원인 정보 없음"
            print(f"   - {result.asset} [{code}] {detail}")
        print("성공으로 처리하지 않았습니다. 상태 JSON과 overlay_log.csv를 확인하세요.")
    elif args.dry_run:
        print("[dry-run] 주문 API를 호출하지 않았고 계획만 기록했습니다.")
    else:
        print("✅ 모든 필요한 모의 주문이 Alpaca에서 확인되었습니다.")
    print("overlay_log.csv 와 state/overlay_run_status.json 에 결과가 기록됩니다.")
    return cycle.exit_code


def _record_run_startup_failure(exc):
    from overlay_status import CycleResult, save_cycle

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    detail = str(exc).replace("\n", " ")[:200]
    cycle = CycleResult(
        started_at=now,
        finished_at=now,
        equity=0.0,
        assets=[],
        exit_code=2,
        fatal_error=f"configuration failed: {detail}",
    )
    try:
        save_cycle(cycle)
    except OSError as status_exc:
        print(f"⛔ 상태 기록 실패: {type(status_exc).__name__}: {status_exc}")
    print(f"⛔ 오버레이 치명적 실패: {cycle.fatal_error}")
    return 2


# ---------------- assets (거래가능 코인 목록) ----------------
def cmd_assets(args, cfg, env):
    """Alpaca 에서 실제 거래 가능한 USD 코인 목록을 보여준다."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass
    except ModuleNotFoundError:
        print("⛔ alpaca-py 미설치. 먼저: pip install -r requirements.txt")
        return
    try:
        c = TradingClient(env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY"), paper=True)
        assets = c.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO))
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 조회 실패: {type(e).__name__}: {e}")
        return
    usd = sorted({a.symbol for a in assets if a.tradable and a.symbol.endswith("/USD")})
    print(f"\n=== 거래 가능한 USD 코인: {len(usd)}개 ===")
    for i in range(0, len(usd), 6):
        print("  " + "  ".join(f"{x:<12}" for x in usd[i:i + 6]))
    print(f"\n현재 봇이 쓰는 자산({len(COMPARE_ASSETS)}개): {', '.join(COMPARE_ASSETS)}")
    print("config.yaml 의 overlay_assets 로 바꿀 수 있습니다.")


# ---------------- screen (자산 선별) ----------------
EXCLUDE = {"USDC/USD", "USDT/USD", "USDG/USD", "PAXG/USD"}  # 스테이블·금연동 제외


def cmd_screen(args, cfg, env):
    """거래가능 코인 전체에 오버레이를 돌려, 데이터·방어 기준을 통과한 것만 추린다."""
    from data import get_bars
    from overlay import run_overlay, stats
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass
    except ModuleNotFoundError:
        print("⛔ alpaca-py 미설치."); return

    tf, ma, ann = "1Day", args.ma or 200, 365
    cost = cfg["costs"]["slippage_bps"]
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)

    if getattr(args, "us", False):
        cands = list(US_SCREEN_POOL)
    else:
        try:
            c = TradingClient(env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY"), paper=True)
            alist = c.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO))
        except Exception as e:  # noqa: BLE001
            print(f"⛔ 목록 조회 실패: {type(e).__name__}"); return
        cands = sorted({a.symbol for a in alist
                        if a.tradable and a.symbol.endswith("/USD") and a.symbol not in EXCLUDE})

    print(f"\n=== 자산 선별 ({len(cands)}개 후보, {ma}일선, {args.months}개월) ===")
    print(f"통과 조건: 데이터 {args.min_bars}봉 이상 & 낙폭 {args.min_dd_cut:.0%}+ 감소 & 칼마 개선\n")
    print(f"{'자산':<12}{'봉수':>6}{'B&H낙폭':>9}{'방어낙폭':>9}{'낙폭↓':>8}"
          f"{'B&H칼마':>8}{'방어칼마':>8}  판정")

    passed, passed_detail = [], []
    for a in cands:
        try:
            df = get_bars(a, tf, start, end, env=env, allow_synthetic=False)
        except Exception:  # noqa: BLE001
            print(f"{a:<12}{'-':>6}  데이터 실패 — 제외"); continue
        n = len(df)
        if n < args.min_bars:
            print(f"{a:<12}{n:>6}  데이터 부족 — 제외"); continue
        r = run_overlay(df, ma, cost_bps=cost)
        bh = stats(r["bh_eq"], r["bh_ret"], ann)
        st = stats(r["strat_eq"], r["strat_ret"], ann)
        dd_cut = (1 - st["mdd"] / bh["mdd"]) if bh["mdd"] > 0 else 0.0
        ok = dd_cut >= args.min_dd_cut and st["calmar"] >= bh["calmar"]
        if ok:
            passed.append(a)
            passed_detail.append((a, st["calmar"] - bh["calmar"]))
        print(f"{a:<12}{n:>6}{bh['mdd']:>9.0%}{st['mdd']:>9.0%}{dd_cut:>8.0%}"
              f"{bh['calmar']:>8}{st['calmar']:>8}  {'✅통과' if ok else '—'}")

    print(f"\n통과: {len(passed)}개 / 후보 {len(cands)}개")
    if passed:
        ranked = sorted(passed_detail, key=lambda x: -x[1])
        print("  " + ", ".join(a for a, _ in ranked))
        top = ranked[:args.top]
        print(f"\n[상위 {len(top)}개 추천 — 칼마 개선폭 순]")
        for a, gain in top:
            print(f"  {a:<8} 칼마 개선 +{gain:.2f}")
        passed = [a for a, _ in top]
        print("\nconfig.yaml 에 아래를 넣으면 운영 자산이 바뀝니다:")
        print("overlay_assets:")
        for a in passed:
            print(f'  - "{a}"')
    print("\n주의: 이건 과거 한 구간 기준입니다. overlaywf 로 여러 시기 재확인을 권합니다.")


# ---------------- review (월간 재점검) ----------------
def cmd_review(args, cfg, env):
    from review import build_report
    import glob

    current = _overlay_assets(cfg)
    cands = list(US_SCREEN_POOL) + ["BTC/USD", "ETH/USD", "XRP/USD", "LTC/USD",
                                    "BCH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD",
                                    "LINK/USD", "DOT/USD", "UNI/USD", "AAVE/USD"]
    print(f"재점검 시작 — 현재 {len(current)}개 + 후보 {len(cands)}개 검증 중 (몇 분 소요)...")
    report = build_report(current, cands, cfg, env, months=args.months)

    os.makedirs("reviews", exist_ok=True)
    path = os.path.join("reviews", f"review_{dt.datetime.now():%Y-%m}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"\n리포트 저장: {path}")


# ---------------- mode (운영 모드 제어) ----------------
def cmd_mode(args, cfg, env):
    """운영 모드 조회·변경. 위험을 늘리는 방향(재개)은 확인문구를 요구한다."""
    import ops_mode
    from order_lock import OrderBusyError, order_lock

    st = ops_mode.load_mode()
    cur = st["mode"]

    if not args.set:
        print(f"\n현재 운영 모드: {ops_mode.label(cur)} ({cur})")
        if st.get("reason"):
            print(f"  사유: {st['reason']}")
        if st.get("degraded"):
            print("  ⚠️ 상태 파일 이상으로 안전 정지 상태입니다.")
        print("\n  NORMAL      정상 — 자동 매수·매도 모두 허용")
        print("  BUY_LOCKED  신규매수 잠금 — 방어 매도는 계속 작동")
        print("  PAUSED_ALL  전체정지 — 방어 매도까지 차단(위험)")
        print("\n변경: python main.py mode --set BUY_LOCKED --reason \"사유\"")
        return 0

    target = args.set
    if target == ops_mode.PAUSED_ALL:
        print("\n⚠️ 전체정지는 방어 매도까지 막습니다.")
        print("   하락장에서 포지션을 정리하지 못할 수 있습니다.")
    if target == ops_mode.NORMAL and cur != ops_mode.NORMAL:
        print("\n자동주문을 재개하면 다음 실행부터 매수·매도가 다시 나갑니다.")
        typed = input("재개하려면 '자동주문 재개' 를 입력하세요: ").strip()
        if typed != "자동주문 재개":
            print("확인문구 불일치 — 변경하지 않았습니다.")
            return 1
    try:
        with order_lock(f"mode-change:{target}"):
            ops_mode.save_mode(target, reason=args.reason or "", actor="cli")
    except OrderBusyError as e:
        print(f"⛔ 주문 작업 진행 중이라 모드를 변경하지 않았습니다: {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 모드 저장 실패: {type(e).__name__}: {e}")
        return 2
    print(f"운영 모드를 {ops_mode.label(target)} ({target}) 로 변경했습니다.")
    return 0


# ---------------- watch (10분 위험 감시, 읽기 전용) ----------------
def cmd_watch(args, cfg, env):
    """읽기 전용 감시. 주문은 절대 하지 않는다."""
    import ops_mode
    import watch as W
    from data import get_bars

    tf = "1Day"
    ma = args.ma if args.ma else DEFAULT_MA[tf]
    assets = _overlay_assets(cfg)
    mode_st = ops_mode.load_mode()

    state = W.load_state()
    stages = state.get("stages", {}) or {}
    health = state.get("health", {}) or {}
    started = dt.datetime.now(dt.timezone.utc)

    try:
        b = broker.AlpacaBroker(env, live_flag=False)
        client = b.connect()
        account = client.get_account()
        equity = float(account.equity)
        cash = float(getattr(account, "cash", 0) or 0)
        buying_power = float(getattr(account, "buying_power", 0) or 0)
        raw_positions = list(client.get_all_positions())
        positions = {p.symbol.replace("/", "").upper(): p
                     for p in raw_positions}
        open_orders = len(list(client.get_orders()))
        clock = client.get_clock()
        market_open = bool(clock.is_open)
        next_open = str(getattr(clock, "next_open", "") or "")
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:150]}"
        state["last_error"] = msg
        state["last_error_at"] = started.isoformat(timespec="seconds")
        W.save_state(state)
        print(f"⛔ 감시 실패(계좌/시장 조회): {msg}")
        return 2

    assets = W.merge_targets(assets, raw_positions)

    print(f"\n=== 위험 감시 ({ma}일선 기준, 읽기 전용) ===")
    print(f"운영 모드: {ops_mode.label(mode_st['mode'])} | 모의계좌 자산: {equity:,.2f}")
    if not market_open:
        print(f"미국장: 장 마감 중 · 마지막 정규장 종가 기준"
              + (f" | 다음 개장 {next_open[:16]}" if next_open else ""))
    else:
        print("미국장: 정규장 진행 중")
    print()
    print(f"{'자산':<9}{'현재가':>12}{'200일선':>12}{'거리':>8}{'보유':>6}  상태")

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * args.months)
    snapshot = dict(state.get("assets", {}) or {})
    failures = []
    for a in assets:
        held = a.replace("/", "").upper() in positions
        previous = dict(snapshot.get(a, {}) or {})
        market_state = ("24시간" if W.is_crypto(a)
                        else ("장중" if market_open else "장 마감 중"))
        last_checked = W.parse_timestamp(previous.get("checked_at"))
        if not W.should_check(a, market_open, last_checked, started):
            previous.update({
                "held": held,
                "market_state": market_state,
                "stale_after_sec": W.stale_after(a, market_open),
            })
            snapshot[a] = previous
            print(f"{a:<9}{'주기 대기':>12}{'-':>12}{'-':>8}"
                  f"{('예' if held else '아니오'):>6}  {market_state}")
            continue
        try:
            df = get_bars(a, tf, start, end, env=env, allow_synthetic=False)
            incomplete = W.last_bar_is_incomplete(df, a, market_open, end)
            mav = W.defense_line(df, ma, incomplete_last=incomplete)
            price = float(df["close"].iloc[-1])
        except Exception as e:  # noqa: BLE001
            code = type(e).__name__
            failures.append({"asset": a, "code": code, "error": str(e)[:150]})
            previous.update({
                "held": held,
                "market_state": market_state,
                "stale_after_sec": W.stale_after(a, market_open),
                "last_error": f"{code}: {str(e)[:150]}",
                "last_error_at": started.isoformat(timespec="seconds"),
                "stage": "조회 실패",
                "message": f"{a} 조회 실패: {code}",
            })
            snapshot[a] = previous
            W.record_health_event(a, "failed", code, health)
            print(f"{a:<9}{'조회 실패':>12}{'-':>12}{'-':>8}{'-':>6}  ⚠️ {type(e).__name__}")
            continue
        if mav is None:
            failures.append({"asset": a, "code": "INSUFFICIENT_BARS",
                             "error": "moving-average bars are insufficient"})
            print(f"{a:<9}{price:>12,.2f}{'데이터부족':>12}{'-':>8}{'':>6}  ⚠️ 봉 부족")
            previous.update({
                "held": held,
                "market_state": market_state,
                "stale_after_sec": W.stale_after(a, market_open),
                "stage": "조회 실패",
                "message": f"{a} 이동평균 데이터 부족",
                "last_error": "INSUFFICIENT_BARS",
                "last_error_at": started.isoformat(timespec="seconds"),
            })
            snapshot[a] = previous
            W.record_health_event(a, "failed", "INSUFFICIENT_BARS", health)
            continue

        stage, d = W.classify(price, mav, held)
        note = "" if market_open or W.is_crypto(a) else " (장 마감 중)"
        msg = W.describe(a, stage, d, held, market_note=note)
        W.record_event(a, stage, msg, stages)
        W.record_health_event(a, "ok", "recovered", health)
        snapshot[a] = {
            "price": round(price, 2),
            "ma": round(mav, 2),
            "distance_pct": round(d, 2),
            "held": held,
            "stage": stage,
            "message": msg,
            "checked_at": started.isoformat(timespec="seconds"),
            "price_time": str(df.index[-1]),
            "price_kind": ("현재 일봉 가격" if W.is_crypto(a) or market_open
                           else "마지막 정규장 종가"),
            "market_state": market_state,
            "stale_after_sec": W.stale_after(a, market_open),
        }
        print(f"{a:<9}{price:>12,.2f}{mav:>12,.2f}{d:>7.1f}%"
              f"{('예' if held else '아니오'):>6}  {stage}{note}")

    previous_high = float(state.get("high_water_mark", 0) or 0)
    high_water = max(previous_high, equity)
    drawdown_pct = ((high_water - equity) / high_water * 100.0
                    if high_water > 0 else 0.0)
    state.update({
        "last_run": started.isoformat(timespec="seconds"),
        "assets": snapshot, "stages": stages, "health": health, "equity": equity,
        "cash": cash, "buying_power": buying_power,
        "open_orders": open_orders,
        "high_water_mark": round(high_water, 2),
        "drawdown_pct": round(drawdown_pct, 3),
        "mode": mode_st["mode"], "market_open": market_open,
        "next_open": next_open, "failures": failures,
        "next_interval_sec": W.next_interval(assets, market_open),
    })
    if failures:
        state["last_error"] = f"자산 조회 실패 {len(failures)}건"
        state["last_error_at"] = started.isoformat(timespec="seconds")
    else:
        state.pop("last_error", None)
        state["last_success"] = started.isoformat(timespec="seconds")
    W.save_state(state)

    alerts = [v.get("message", "") for v in snapshot.values()
              if v.get("stage") in (W.IMMINENT, W.BREACHED)]
    if alerts:
        print("\n[주의]")
        for m in alerts:
            print(f"  - {m}")
    if failures:
        print(f"\n⚠️ 조회 실패 {len(failures)}건: "
              + ", ".join(f"{v['asset']}({v['code']})" for v in failures))
    print("\n감시는 읽기 전용입니다. 주문은 완성된 일봉과 자동전략이 결정합니다.")
    return 1 if failures else 0


# ---------------- liquidate (긴급 전체청산) ----------------
def cmd_liquidate(args, cfg, env):
    """긴급 전체청산. 모드와 무관하게 실행하되 시작과 동시에 PAUSED_ALL 로 고정한다."""
    import ops_mode
    import liquidate as L
    from order_lock import OrderBusyError, order_lock

    print("\n" + "=" * 56)
    print("  🚨 긴급 전체청산 — 모든 포지션을 정리합니다")
    print("=" * 56)

    try:
        b = broker.AlpacaBroker(env, live_flag=False)
        client = b.connect()
        account = client.get_account()
        positions = list(client.get_all_positions())
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 계좌 연결 실패: {type(e).__name__}: {str(e)[:150]}")
        return 2

    print(f"\n모의계좌 자산: {float(account.equity):,.2f}")
    print(f"{'자산':<10}{'수량':>14}{'평가금액':>14}")
    for p in positions:
        mv = float(getattr(p, "market_value", 0) or 0)
        print(f"{p.symbol:<10}{str(getattr(p, 'qty', '?')):>14}{mv:>14,.2f}")

    if not args.execute:
        print("\n[dry-run] 조회만 완료했습니다. 모드·주문·포지션은 변경하지 않았습니다.")
        return 0

    print("\n청산 후 자동주문은 '전체정지(PAUSED_ALL)' 로 고정됩니다.")
    print("방어 매도까지 막히므로, 재개는 직접 확인 후 수행해야 합니다.")
    typed = input("\n계속하려면 'PAPER 전체청산' 을 입력하세요: ").strip()
    if typed != "PAPER 전체청산":
        print("확인문구 불일치 — 중단했습니다.")
        return 1

    try:
        with order_lock("emergency-liquidate"):
            # 1) 정지 선저장 — 실패하면 청산을 시작하지 않는다
            ok, err = L.prepare(actor="cli-liquidate")
            if not ok:
                print(f"\n⛔ 치명적 실패: 운영 모드를 전체정지로 저장하지 못했습니다 ({err}).")
                print("   자동주문과 충돌할 수 있어 청산을 시작하지 않았습니다.")
                return 2
            print("\n[1/3] 운영 모드를 전체정지로 고정했습니다.")

            # 2) 미체결 주문 취소 — 오류나 잔존 주문은 치명적 중단
            tried, remaining, cerr = L.cancel_open_orders(client)
            if cerr or remaining != 0:
                detail = cerr or f"취소 후에도 미체결 주문 {remaining}건이 남아 있습니다"
                print(f"[2/3] ⛔ {detail}")
                print("   중복·상충 주문 위험 때문에 청산 주문을 시작하지 않았습니다.")
                return 2
            print(f"[2/3] 미체결 주문 {tried}건 취소 (남은 주문 0건)")

            if not positions:
                print("[3/3] 보유 포지션 없음 — 전체정지 상태로 종료합니다.")
                return 0

            # 3) 전체 청산
            results, fatal = L.close_all(client)
    except OrderBusyError as exc:
        print(f"\n⛔ 주문 실행 잠금 사용 중 — 청산을 시작하지 않았습니다: {exc}")
        return 2
    if fatal:
        print(f"[3/3] ⛔ {fatal}")
        return 2
    s = L.summarize(results)
    print(f"[3/3] 청산 완료 {len(s['closed'])}건 / "
          f"실패 {len(s['failed'])}건 / 미확인 {len(s['unconfirmed'])}건\n")

    for r in s["closed"]:
        print(f"  ✅ {r['symbol']} 청산됨")
    for r in s["failed"]:
        print(f"  ⛔ {r['symbol']} 실패 — {r['error']}")
    for r in s["unconfirmed"]:
        print(f"  ⚠️ {r['symbol']} 미확인 — {r['error']}")

    if s["unconfirmed"]:
        print("\n⚠️ 미확인 자산이 있습니다. Alpaca Paper 대시보드에서 직접 확인하세요.")
    print(f"\n운영 모드: {ops_mode.label(ops_mode.PAUSED_ALL)} 유지 중.")
    print("재개하려면: python main.py mode --set NORMAL")
    return 0 if s["ok"] else 1


# ---------------- paper / live ----------------
def cmd_trade(args, cfg, env, live_flag):
    decision = broker.resolve_mode(env, live_flag)

    if decision.is_live:
        _live_banner(cfg, env)
    else:
        print(f"[모드] 모의(paper) — 엔드포인트 {decision.endpoint}")
        for r in decision.reasons:
            print(f"   · {r}")

    state = portfolio.load()
    if portfolio.is_stopped(state):
        print("⛔ 상태가 STOPPED 입니다. 'python main.py resume' 전까지 신규 주문을 넣지 않습니다.")
        return

    _print_cost_assumptions(cfg)

    if args.dry_run:
        print("\n[dry-run] 주문은 계산만 하고 실제로 넣지 않습니다.")
        print("대상 자산:", ", ".join(cfg["assets"].keys()))
        print("각 자산의 예상 수량·손절 거리·위험 예산을 계산 후 '주문 건너뜀'.")
        return

    if args.once:
        print("\n[--once] 한 사이클만 실행합니다. (실제 연결/주문 로직은 브로커 연결 후 동작)")
        print("환경변수/키가 준비되면 broker.AlpacaBroker().connect() 로 계좌를 확인하세요.")
        return

    print("\n연속 루프 진입 지점입니다. 2주 모의 검증 전에는 주문 루프를 켜지 마세요.")


def _live_banner(cfg, env):
    print("=" * 56)
    print("  ⚠️  실계좌(LIVE) 모드 — 진짜 돈이 오갑니다")
    print("=" * 56)
    equity = 0.0
    try:
        b = broker.AlpacaBroker(env, live_flag=True)
        acct = b.account()
        equity = float(getattr(acct, "equity", 0.0))
    except Exception as e:  # noqa: BLE001
        print(f"  (잔고 조회 실패: {type(e).__name__} — 키 확인 필요)")
    max_loss = equity * cfg["account"]["risk_per_trade"]
    print(f"  현재 잔고: {equity:,.2f}")
    print(f"  이번 설정에서 한 번 매매 최대 손실: {max_loss:,.2f} "
          f"({cfg['account']['risk_per_trade']:.0%})")
    print("  10초 뒤 시작합니다. 중단하려면 Ctrl+C.")
    for s in range(10, 0, -1):
        print(f"   {s}...", end="", flush=True)
        time.sleep(1)
    print("\n시작")


# ---------------- status / stop / resume ----------------
def cmd_status(args, cfg, env):
    state = portfolio.load()
    print(f"상태: {state['status']}")
    print(f"잔고 최고점(HWM): {state['high_water_mark']:,.2f}")
    pos = state.get("positions", {})
    print(f"보유 포지션: {len(pos)}건")
    for sym, p in pos.items():
        print(f"  {sym}: {p}")


def cmd_stop(args, cfg, env):
    state = portfolio.load()
    if args.close_all:
        print("모든 포지션 정리 요청 → (브로커 연결 시 실제 청산 실행)")
        state["positions"] = {}
    portfolio.mark_stopped(state)
    portfolio.save(state)
    print("상태를 STOPPED 로 저장했습니다. 사람이 resume 하기 전까지 유지됩니다.")


def cmd_resume(args, cfg, env):
    state = portfolio.load()
    portfolio.resume(state)
    portfolio.save(state)
    print("상태를 RUNNING 으로 되돌렸습니다.")


def build_parser():
    p = argparse.ArgumentParser(description="5자산 트레이딩 봇 (기본: 모의투자)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest")
    b.add_argument("--months", type=int, default=6)
    b.add_argument("--slippage-bps", type=float, default=None)

    sub.add_parser("check")
    sub.add_parser("assets")

    wt = sub.add_parser("watch")
    wt.add_argument("--ma", type=int, default=0)
    wt.add_argument("--months", type=int, default=18)

    lq = sub.add_parser("liquidate")
    lq.add_argument(
        "--execute",
        action="store_true",
        help="실제 전체청산 실행(미지정 시 조회 전용 dry-run)",
    )

    md = sub.add_parser("mode")
    md.add_argument("--set", choices=["NORMAL", "BUY_LOCKED", "PAUSED_ALL"])
    md.add_argument("--reason", default="")

    rv = sub.add_parser("review")
    rv.add_argument("--months", type=int, default=60)

    sc = sub.add_parser("screen")
    sc.add_argument("--months", type=int, default=48)
    sc.add_argument("--ma", type=int, default=200)
    sc.add_argument("--min-bars", type=int, default=500)
    sc.add_argument("--min-dd-cut", type=float, default=0.30)
    sc.add_argument("--us", action="store_true", help="미국 ETF 후보군으로 선별")
    sc.add_argument("--top", type=int, default=7, help="추천 상위 N개")

    cp = sub.add_parser("compare")
    cp.add_argument("--months", type=int, default=24)
    cp.add_argument("--trials", type=int, default=20)
    cp.add_argument("--timeframe", default="1Day", choices=["1Day", "1Week"])

    wf = sub.add_parser("walkforward")
    wf.add_argument("--asset", default="ETH/USD")
    wf.add_argument("--strategy", default="breakout",
                    choices=["trend_following", "breakout", "mean_reversion"])
    wf.add_argument("--months", type=int, default=48)
    wf.add_argument("--window", type=int, default=6)
    wf.add_argument("--trials", type=int, default=30)
    wf.add_argument("--timeframe", default="1Week", choices=["1Day", "1Week"])

    ov = sub.add_parser("overlay")
    ov.add_argument("--months", type=int, default=60)
    ov.add_argument("--ma", type=int, default=0, help="이평 기간(0이면 기본: 일봉200/주봉40)")
    ov.add_argument("--timeframe", default="1Day", choices=["1Day", "1Week"])
    ov.add_argument("--us", action="store_true", help="미국 주식·ETF로 검증")

    owf = sub.add_parser("overlaywf")
    owf.add_argument("--asset", default="")
    owf.add_argument("--months", type=int, default=60)
    owf.add_argument("--ma", type=int, default=0)
    owf.add_argument("--window", type=int, default=12)
    owf.add_argument("--timeframe", default="1Day", choices=["1Day", "1Week"])
    owf.add_argument("--us", action="store_true", help="미국 주식·ETF로 검증")

    rn = sub.add_parser("run")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--ma", type=int, default=0)
    rn.add_argument("--months", type=int, default=18)
    rn.add_argument("--timeframe", default="1Day", choices=["1Day", "1Week"])

    bm = sub.add_parser("benchmark")
    bm.add_argument("--months", type=int, default=6)
    bm.add_argument("--trials", type=int, default=100,
                    help="무작위 봇 시행 횟수(많을수록 정확·느림). 빠른 확인은 --trials 30")

    pa = sub.add_parser("paper")
    pa.add_argument("--dry-run", action="store_true")
    pa.add_argument("--once", action="store_true")

    li = sub.add_parser("live")
    li.add_argument("--live", action="store_true", help="실계좌 3중 잠금의 세 번째 조건")
    li.add_argument("--dry-run", action="store_true")
    li.add_argument("--once", action="store_true")

    sub.add_parser("status")
    st = sub.add_parser("stop")
    st.add_argument("--close-all", action="store_true")
    sub.add_parser("resume")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        env = _env()
    except Exception as exc:  # noqa: BLE001
        if args.cmd == "run":
            return _record_run_startup_failure(exc)
        raise

    if args.cmd == "backtest":
        cmd_backtest(args, cfg, env)
    elif args.cmd == "check":
        cmd_check(args, cfg, env)
    elif args.cmd == "assets":
        cmd_assets(args, cfg, env)
    elif args.cmd == "mode":
        return cmd_mode(args, cfg, env)
    elif args.cmd == "watch":
        return cmd_watch(args, cfg, env)
    elif args.cmd == "liquidate":
        return cmd_liquidate(args, cfg, env)
    elif args.cmd == "review":
        cmd_review(args, cfg, env)
    elif args.cmd == "screen":
        cmd_screen(args, cfg, env)
    elif args.cmd == "compare":
        cmd_compare(args, cfg, env)
    elif args.cmd == "walkforward":
        cmd_walkforward(args, cfg, env)
    elif args.cmd == "overlay":
        cmd_overlay(args, cfg, env)
    elif args.cmd == "overlaywf":
        cmd_overlaywf(args, cfg, env)
    elif args.cmd == "run":
        return cmd_run(args, cfg, env)
    elif args.cmd == "benchmark":
        cmd_benchmark(args, cfg, env)
    elif args.cmd == "paper":
        cmd_trade(args, cfg, env, live_flag=False)
    elif args.cmd == "live":
        # --live 플래그가 있어야 3중 잠금의 세 번째 조건 충족
        cmd_trade(args, cfg, env, live_flag=getattr(args, "live", False))
    elif args.cmd == "status":
        cmd_status(args, cfg, env)
    elif args.cmd == "stop":
        cmd_stop(args, cfg, env)
    elif args.cmd == "resume":
        cmd_resume(args, cfg, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
