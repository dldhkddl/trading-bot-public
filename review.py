"""월간 재점검 — 자산 목록이 여전히 유효한지 다시 검증한다.

원칙: 리포트만 만들고 **자동으로 교체하지 않는다**.
매달 성적 좋은 것만 쫓아 갈아타면 과최적화에 빠지기 때문에,
최종 편입/제외 결정은 사람이 한다.

검사 항목:
  1) 현재 운영 자산이 아직 기준(낙폭 감소·칼마 개선·워크포워드)을 지키는지
  2) 탈락했던 후보 중 새로 기준을 통과한 것이 있는지
"""
import datetime as dt

from data import get_bars
from overlay import run_overlay, stats, overlay_walk_forward

ANN = 365
MIN_BARS = 500
MIN_DD_CUT = 0.30


def evaluate(symbol, cfg, env, months=60, ma=200, window_months=12):
    """단일 자산: 전구간 성과 + 워크포워드 통과 구간 수."""
    cost = cfg["costs"]["slippage_bps"]
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=30 * months)
    try:
        df = get_bars(symbol, "1Day", start, end, env=env, allow_synthetic=False)
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "error": type(e).__name__}
    if len(df) < MIN_BARS:
        return {"symbol": symbol, "error": f"데이터부족({len(df)}봉)"}

    r = run_overlay(df, ma, cost_bps=cost)
    bh = stats(r["bh_eq"], r["bh_ret"], ANN)
    st = stats(r["strat_eq"], r["strat_ret"], ANN)
    dd_cut = (1 - st["mdd"] / bh["mdd"]) if bh["mdd"] > 0 else 0.0

    wbars = max(int(window_months * 30.4), ma // 2)
    folds = overlay_walk_forward(df, ma, wbars, cost, ANN)
    wf_ok = sum(1 for f in folds
                if f["dd_cut"] >= MIN_DD_CUT and f["st"]["calmar"] >= f["bh"]["calmar"])

    screen_pass = dd_cut >= MIN_DD_CUT and st["calmar"] >= bh["calmar"]
    return {"symbol": symbol, "bars": len(df), "dd_cut": dd_cut,
            "bh_calmar": bh["calmar"], "st_calmar": st["calmar"],
            "screen_pass": screen_pass, "wf_ok": wf_ok, "wf_n": len(folds),
            "healthy": screen_pass and wf_ok >= 1}


def build_report(current, candidates, cfg, env, months=60):
    lines = []
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"# 월간 재점검 리포트 — {ts}")
    lines.append(f"\n기준: 낙폭 {MIN_DD_CUT:.0%}+ 감소 & 칼마 개선 & 워크포워드 1구간 이상 통과")
    lines.append("\n> 이 리포트는 제안일 뿐입니다. 자동 교체하지 않으며, 편입/제외는 사람이 결정합니다.\n")

    lines.append("## 1. 현재 운영 자산 상태\n")
    lines.append("| 자산 | 봉수 | 낙폭↓ | B&H칼마 | 방어칼마 | WF | 상태 |")
    lines.append("|---|---|---|---|---|---|---|")
    degraded = []
    for s in current:
        r = evaluate(s, cfg, env, months=months)
        if "error" in r:
            lines.append(f"| {s} | - | - | - | - | - | ⚠️ {r['error']} |")
            continue
        mark = "✅ 유지" if r["healthy"] else "⚠️ 기준미달"
        if not r["healthy"]:
            degraded.append(s)
        lines.append(f"| {s} | {r['bars']} | {r['dd_cut']:.0%} | {r['bh_calmar']} | "
                     f"{r['st_calmar']} | {r['wf_ok']}/{r['wf_n']} | {mark} |")

    lines.append("\n## 2. 신규 통과 후보 (현재 미편입)\n")
    news = []
    for s in candidates:
        if s in current:
            continue
        r = evaluate(s, cfg, env, months=months)
        if "error" in r or not r["healthy"]:
            continue
        news.append((s, r))
    if news:
        lines.append("| 자산 | 봉수 | 낙폭↓ | B&H칼마 | 방어칼마 | WF |")
        lines.append("|---|---|---|---|---|---|")
        for s, r in sorted(news, key=lambda x: -(x[1]["st_calmar"] - x[1]["bh_calmar"])):
            lines.append(f"| {s} | {r['bars']} | {r['dd_cut']:.0%} | {r['bh_calmar']} | "
                         f"{r['st_calmar']} | {r['wf_ok']}/{r['wf_n']} |")
    else:
        lines.append("_새로 기준을 통과한 후보 없음._")

    lines.append("\n## 3. 검토 제안\n")
    if degraded:
        lines.append(f"- **제외 검토**: {', '.join(degraded)} — 기준 미달로 떨어짐")
    else:
        lines.append("- 현재 자산 전부 기준 유지 중. 변경 불필요.")
    if news:
        lines.append(f"- **편입 검토**: {', '.join(s for s, _ in news[:5])}")
    lines.append("\n- 교체는 신중히. 매달 성적 좋은 것만 쫓으면 과최적화에 빠집니다.")
    lines.append("- 변경하려면 config.yaml 의 overlay_assets 를 직접 수정하세요.")
    return "\n".join(lines)
