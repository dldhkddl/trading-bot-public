"""아침/저녁 브리핑. 기본은 dry-run(콘솔 미리보기)이며,
토큰이 없으면 오류 대신 미리보기만 보여준다. 숫자는 파일에서 계산한다."""
import os
import sys
import argparse

import pandas as pd
from dotenv import load_dotenv


def _read_csv(path):
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def build_morning(max_words=200):
    trades = _read_csv("trades.csv")
    daily = _read_csv("daily_pnl.csv")
    lines = ["☀️ 아침 브리핑"]
    if not daily.empty and "pnl" in daily.columns:
        lines.append(f"어제 손익: {daily['pnl'].iloc[-1]:,.2f}")
    else:
        lines.append("어제 손익: 기록 없음")
    if not trades.empty:
        lines.append(f"누적 체결 수: {len(trades)}")
        if "pnl" in trades.columns:
            wins = (trades['pnl'] > 0).sum()
            lines.append(f"최근 승률(누적): {wins/len(trades):.0%}")
    else:
        lines.append("오픈 포지션/체결 기록 없음 (아직 거래 전)")
    lines.append("리스크 플래그: 손절 근접·상관필터·낙폭 5% 초과 여부는 state 파일 기준으로 확인")
    return "\n".join(lines)


def build_evening(max_words=200):
    trades = _read_csv("trades.csv")
    lines = ["🌙 저녁 리포트"]
    if not trades.empty and "pnl" in trades.columns:
        today = trades  # 실제로는 당일 필터링. 기록 없으면 전체.
        lines.append(f"오늘(누적) 체결 수: {len(today)}")
        lines.append(f"손익 합계: {today['pnl'].sum():,.2f}")
        best = today.loc[today['pnl'].idxmax()]
        worst = today.loc[today['pnl'].idxmin()]
        lines.append(f"베스트: {best.get('symbol','?')} {best['pnl']:,.2f}")
        lines.append(f"워스트: {worst.get('symbol','?')} {worst['pnl']:,.2f}")
    else:
        lines.append("오늘 체결 기록 없음")
    lines.append("백테스트 기대치 대비 추세는 daily_pnl.csv 로 비교")
    return "\n".join(lines)


def send_telegram(text, env, dry_run):
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if dry_run or not token or not chat_id:
        print("----- [dry-run 미리보기] 텔레그램 전송 안 함 -----")
        print(text)
        print("------------------------------------------------")
        return False
    import urllib.request, urllib.parse, json
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    with urllib.request.urlopen(url, data=data, timeout=10) as r:
        json.load(r)
    print("텔레그램 전송 완료")
    return True


def main(argv=None):
    load_dotenv()
    env = dict(os.environ)
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["morning", "evening"])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    dry = args.dry_run or str(env.get("TELEGRAM_DRY_RUN", "true")).lower() != "false"
    text = build_morning() if args.kind == "morning" else build_evening()
    send_telegram(text, env, dry)


if __name__ == "__main__":
    main()
