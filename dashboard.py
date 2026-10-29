"""Alpaca Paper 계좌와 로컬 오버레이 신호를 보여주는 운영 대시보드.

사용법:
  python dashboard.py            # http://localhost:8787 서버 실행(자동 새로고침)
  python dashboard.py --once     # dashboard.html 정적 파일로 한 번 저장
  python dashboard.py --port 9000
추가 설치 없이 표준 라이브러리 + pandas(이미 requirements에 있음)만 사용.
"""
import os
import io
import json
import argparse
import datetime as dt
import html as html_lib
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd
from dotenv import load_dotenv
from overlay_status import load_cycle
from chart_data import build_chart_payload
from paper_dashboard import (
    get_paper_snapshot,
    load_equity_history,
    record_equity_snapshot,
)

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

STARTING_EQUITY = 100_000.0
OPS_SESSION_TOKEN = secrets.token_urlsafe(32)


def _escape(value):
    return html_lib.escape(str(value))


def _script_json(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def _chart_payload_for_request(symbol, range_key):
    """Build a read-only chart payload for configured or currently held assets."""
    config = _load_config()
    snapshot = get_paper_snapshot()
    allowed = set(config.get("overlay_assets") or [])
    allowed.update((config.get("assets") or {}).keys())
    allowed.update((snapshot.get("positions") or {}).keys())
    return build_chart_payload(
        symbol,
        range_key,
        allowed_symbols=allowed,
        snapshot=snapshot,
    )


# ---------------- 데이터 수집 ----------------
def _read_csv(path):
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:  # noqa: BLE001
            return pd.DataFrame()
    return pd.DataFrame()


def _load_state():
    path = os.path.join("state", "portfolio_state.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return {"status": "RUNNING", "high_water_mark": 0.0, "positions": {}}


def _load_config():
    if yaml and os.path.exists("config.yaml"):
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:  # noqa: BLE001
            pass
    return {"account": {"risk_per_trade": 0.01, "max_drawdown_stop": 0.10},
            "assets": {}}


def _load_ops_mode():
    try:
        import ops_mode
        return ops_mode.load_mode()
    except Exception:  # noqa: BLE001
        return {"mode": "PAUSED_ALL", "reason": "모드 조회 실패", "degraded": True}


def _load_watch():
    try:
        import watch
        st = watch.load_state()
        return st if st.get("assets") or st.get("last_error") else None
    except Exception:  # noqa: BLE001
        return None


def _resume_guard(state, now=None):
    """Fail closed unless every watched asset is fresh and error-free."""
    import watch

    now = now or dt.datetime.now(dt.timezone.utc)
    if not state or state.get("corrupt"):
        return False, "감시 상태가 없거나 손상되었습니다"
    if state.get("last_error") or state.get("failures"):
        return False, "감시 실패가 남아 있습니다"
    assets = state.get("assets") or {}
    if not assets:
        return False, "감시 자산 기록이 없습니다"
    for asset, item in assets.items():
        checked = watch.parse_timestamp(item.get("checked_at"))
        if checked is None:
            return False, f"{asset}의 정상 확인 시간이 없습니다"
        threshold = int(item.get("stale_after_sec") or 1200)
        if (now - checked).total_seconds() > threshold:
            return False, f"{asset} 감시 정보가 너무 오래되었습니다"
    return True, "감시 상태 정상"


def _perform_operation(path, payload):
    """Execute one allow-listed operational action; never place buy/sell orders."""
    import ops_mode
    from order_lock import OrderBusyError, order_lock

    confirmation = str(payload.get("confirmation") or "").strip()
    mode_actions = {
        "/api/operations/buy-lock": (
            "신규매수 잠금", ops_mode.BUY_LOCKED, "대시보드 신규매수 잠금"
        ),
        "/api/operations/pause": (
            "전체정지", ops_mode.PAUSED_ALL, "대시보드 전체정지"
        ),
        "/api/operations/resume": (
            "자동주문 재개", ops_mode.NORMAL, "대시보드 자동주문 재개"
        ),
    }
    if path in mode_actions:
        expected, target, reason = mode_actions[path]
        if confirmation != expected:
            return 400, {"ok": False, "error": "확인 문구가 일치하지 않습니다"}
        if target == ops_mode.NORMAL:
            current = ops_mode.load_mode()
            if current.get("degraded"):
                return 409, {
                    "ok": False,
                    "error": "재개 거부: 운영 모드 상태 파일이 손상되었거나 없습니다",
                }
            allowed, guard_reason = _resume_guard(_load_watch())
            if not allowed:
                return 409, {"ok": False, "error": f"재개 거부: {guard_reason}"}
        try:
            before = ops_mode.load_mode().get("mode")
            with order_lock(f"dashboard-mode-change:{target}"):
                saved = ops_mode.save_mode(target, reason=reason, actor="dashboard")
        except OrderBusyError as exc:
            return 409, {"ok": False, "error": f"주문 작업 진행 중: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return 500, {"ok": False, "error": f"모드 저장 실패: {type(exc).__name__}: {str(exc)[:160]}"}
        return 200, {
            "ok": True,
            "request_id": saved.get("request_id"),
            "before": before,
            "mode": target,
            "audit_warning": saved.get("audit_warning"),
        }

    if path == "/api/orders/cancel-open":
        if confirmation != "미체결 주문 취소":
            return 400, {"ok": False, "error": "확인 문구가 일치하지 않습니다"}
        request_id = secrets.token_hex(16)
        try:
            import broker
            import liquidate

            load_dotenv()
            ops_mode.audit_event({
                "event": "cancel_open_authorized",
                "request_id": request_id,
                "actor": "dashboard",
            })
            client = broker.AlpacaBroker(dict(os.environ), live_flag=False).connect()
            with order_lock("dashboard-cancel-open"):
                tried, remaining, error = liquidate.cancel_open_orders(client)
        except OrderBusyError as exc:
            return 409, {"ok": False, "request_id": request_id,
                         "error": f"주문 작업 진행 중: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return 502, {"ok": False, "error": f"Paper 주문 취소 실패: {type(exc).__name__}: {str(exc)[:160]}"}
        if error or remaining != 0:
            detail = error or f"취소 후 미체결 주문 {remaining}건 잔존"
            try:
                ops_mode.audit_event({
                    "event": "cancel_open_failed", "request_id": request_id,
                    "actor": "dashboard", "tried": tried,
                    "remaining": remaining, "error": detail,
                })
            except OSError:
                pass
            return 502, {"ok": False, "error": detail, "tried": tried,
                         "remaining": remaining, "request_id": request_id}
        audit_warning = None
        try:
            ops_mode.audit_event({
                "event": "cancel_open_completed", "request_id": request_id,
                "actor": "dashboard", "tried": tried, "remaining": 0,
            })
        except OSError as exc:
            audit_warning = str(exc)
        return 200, {"ok": True, "tried": tried, "remaining": 0,
                     "request_id": request_id, "audit_warning": audit_warning}

    return 404, {"ok": False, "error": "허용되지 않은 작업입니다"}


def _load_overlay():
    """overlay_log.csv 의 가장 최근 실행 결과(자산별 in/out)를 읽는다."""
    df = _read_csv("overlay_log.csv")
    if df.empty:
        return None
    time_col = "finished_at" if "finished_at" in df.columns else "time"
    if time_col not in df.columns:
        return None
    last_t = df[time_col].iloc[-1]
    rows = df[df[time_col] == last_t]
    assets, invested = [], 0
    for _, r in rows.iterrows():
        held = str(r.get("held")).lower() in ("true", "1", "예")
        if held:
            invested += 1
        assets.append({"asset": str(r.get("asset")), "signal": str(r.get("signal")),
                       "price": r.get("price"), "ma": r.get("ma"),
                       "held": held, "action": str(r.get("action"))})
    n = len(assets)
    eq = rows["equity"].iloc[-1] if "equity" in rows.columns else None
    if "dry_run" in rows.columns:
        dry = str(rows["dry_run"].iloc[-1])
    elif "status" in rows.columns:
        dry = str((rows["status"] == "dry_run").all())
    else:
        dry = ""
    return {"time": str(last_t), "assets": assets, "invested": invested, "n": n,
            "cash_pct": (1 - invested / n) if n else 1.0, "equity": eq, "dry_run": dry}


def _norm_symbol(symbol):
    return str(symbol).replace("/", "").upper()


def _load_paper_positions(client_factory=None):
    """Alpaca Paper의 현재 포지션만 읽는다. 주문 관련 API는 호출하지 않는다."""
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        return {"positions": {}, "updated_at": None, "error": "Paper 계좌 키가 없습니다"}
    if client_factory is None:
        from alpaca.trading.client import TradingClient
        client_factory = TradingClient
    try:
        client = client_factory(api_key, secret_key, paper=True)
        raw_positions = client.get_all_positions()
        positions = {}
        for position in raw_positions:
            side = getattr(position, "side", None)
            side = getattr(side, "value", side)
            positions[_norm_symbol(position.symbol)] = {
                "side": str(side),
                "qty": str(getattr(position, "qty", "")),
                "avg_entry_price": str(getattr(position, "avg_entry_price", "")),
            }
        return {
            "positions": positions,
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        for secret in (api_key, secret_key):
            message = message.replace(secret, "***")
        return {"positions": {}, "updated_at": None, "error": message}


def _reconcile_overlay_positions(overlay, snapshot):
    """최근 신호/행동은 유지하고 현재 보유 필드만 Paper 계좌 값으로 교정한다."""
    if overlay is None:
        return None
    reconciled = dict(overlay)
    assets = []
    positions = snapshot.get("positions", {})
    lookup_failed = bool(snapshot.get("error")) and not positions
    invested = 0
    for original in overlay.get("assets", []):
        item = dict(original)
        if lookup_failed:
            item.update(held=None, qty=None, avg_entry_price=None)
        else:
            position = positions.get(_norm_symbol(item.get("asset", "")))
            item["held"] = position is not None
            item["qty"] = position.get("qty") if position else None
            item["avg_entry_price"] = (
                position.get("avg_entry_price") if position else None
            )
            invested += int(position is not None)
        assets.append(item)
    reconciled["assets"] = assets
    reconciled["invested"] = None if lookup_failed else invested
    n = len(assets)
    reconciled["n"] = n
    reconciled["cash_pct"] = None if lookup_failed else (1 - invested / n if n else 1.0)
    return reconciled


def _overlay_equity_curve():
    """overlay_log.csv 의 날짜별 모의계좌 잔고 추이."""
    df = _read_csv("overlay_log.csv")
    if df.empty or "equity" not in df.columns:
        return []
    time_col = "finished_at" if "finished_at" in df.columns else "time"
    if time_col not in df.columns:
        return []
    g = df.groupby(time_col)["equity"].last()
    return [round(float(x), 2) for x in g.tolist()]


def _load_overlay_run(path=os.path.join("state", "overlay_run_status.json")):
    try:
        cycle = load_cycle(path)
    except ValueError as exc:
        return {"load_error": str(exc)}
    return cycle.to_dict() if cycle else None


def _paper_dashboard_data(snapshot, history):
    """Build every operational metric from one Paper account snapshot."""
    account = snapshot.get("account", {})
    positions = snapshot.get("positions", {})
    equity = float(account.get("equity") or STARTING_EQUITY)
    last_equity = float(account.get("last_equity") or equity)
    curve = [round(float(row["equity"]), 2) for row in history]
    current_equity = round(equity, 2)
    if not curve or curve[-1] != current_equity:
        curve.append(current_equity)
    running_high = 0.0
    drawdown_curve = []
    for value in curve:
        running_high = max(running_high, value)
        drawdown_curve.append(
            round((running_high - value) / running_high * 100, 4)
            if running_high > 0 else 0.0
        )
    hwm = max(curve) if curve else equity
    per_asset = {}
    for symbol, position in positions.items():
        per_asset[str(symbol)] = {
            "pnl": round(float(position.get("unrealized_pl") or 0.0), 2),
            "return_pct": round(float(position.get("unrealized_plpc") or 0.0) * 100, 6),
            "market_value": round(float(position.get("market_value") or 0.0), 2),
        }
    return {
        "equity": round(equity, 2),
        "total_pnl": round(equity - STARTING_EQUITY, 2),
        "daily_pnl": round(equity - last_equity, 2),
        "cash": round(float(account.get("cash") or 0.0), 2),
        "buying_power": round(float(account.get("buying_power") or 0.0), 2),
        "n_positions": len(positions),
        "hwm": round(hwm, 2),
        "drawdown": round((hwm - equity) / hwm, 6) if hwm > 0 else 0.0,
        "equity_curve": curve,
        "drawdown_curve": drawdown_curve,
        "per_asset": per_asset,
    }


def collect():
    """동일한 Paper 스냅숏에서 대시보드 전체 수치를 계산한다."""
    cfg = _load_config()
    state = _load_state()
    snapshot = get_paper_snapshot()
    history = load_equity_history(
        "state/paper_equity_history.csv",
        seed_path="overlay_log.csv",
    )
    record_equity_snapshot(snapshot)
    history = load_equity_history("state/paper_equity_history.csv")
    overlay = _load_overlay()
    overlay = _reconcile_overlay_positions(overlay, snapshot)

    max_dd_stop = cfg["account"].get("max_drawdown_stop", 0.10)
    d = {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": state.get("status", "RUNNING"),
        "source": "Alpaca Paper 계좌 (읽기 전용)",
        "max_dd_stop": max_dd_stop,
        "positions": snapshot.get("positions", {}),
        "assets_cfg": cfg.get("assets", {}),
        "overlay": overlay,
        "overlay_run": _load_overlay_run(),
        "ops_mode": _load_ops_mode(),
        "watch": _load_watch(),
        "paper_positions": snapshot,
        "paper_snapshot": snapshot,
    }
    d.update(_paper_dashboard_data(snapshot, history))
    return d


# ---------------- HTML 렌더링 ----------------
def render_html(d, auto_refresh=15, controls_enabled=False, ops_token=""):
    pos = d["positions"]
    dd_pct = d["drawdown"] * 100
    dd_limit = d["max_dd_stop"] * 100
    dd_danger = dd_pct >= dd_limit
    dd_warn = dd_pct >= dd_limit / 2

    # 리스크 플래그
    flags = []
    if d["status"] == "STOPPED":
        flags.append(("STOPPED — 사람이 resume 전까지 신규 주문 없음", "danger"))
    if dd_danger:
        flags.append((f"최대 낙폭 정지선 도달: {dd_pct:.1f}% ≥ {dd_limit:.0f}%", "danger"))
    elif dd_warn:
        flags.append((f"낙폭 경고: {dd_pct:.1f}% (정지선 {dd_limit:.0f}%)", "warn"))
    # 상관 필터
    spy = pos.get("SPY", {}).get("side")
    qqq = pos.get("QQQ", {}).get("side")
    if spy == "long" and qqq == "long":
        flags.append(("상관 필터 작동 중: SPY·QQQ 롱 → BTC 신규 롱 차단", "warn"))
    if not flags:
        flags.append(("특이 리스크 신호 없음", "ok"))

    pnl_color = "pos" if d["total_pnl"] >= 0 else "neg"
    daily_pnl = d.get("daily_pnl", 0.0)
    daily_color = "pos" if daily_pnl >= 0 else "neg"
    status_color = "ok" if d["status"] == "RUNNING" else "danger"

    paper_positions = d.get("paper_snapshot") or d.get("paper_positions")
    if paper_positions is None:
        paper_positions_html = ""
    elif paper_positions.get("stale"):
        paper_error = _escape(paper_positions.get("error") or "원인 정보 없음")
        paper_updated = _escape(paper_positions.get("updated_at") or "시간 없음")
        paper_positions_html = (
            "<div class='overlay-run status-danger'><b>Paper 계좌 조회 실패 · 오래된 정보</b>"
            f"<span>{paper_error}</span><span>마지막 정상 갱신 {paper_updated}</span></div>"
        )
    elif paper_positions.get("error"):
        paper_error = _escape(paper_positions["error"])
        paper_positions_html = (
            "<div class='overlay-run status-danger'><b>Paper 계좌 조회 실패</b>"
            f"<span>{paper_error}</span></div>"
        )
    else:
        paper_updated = _escape(paper_positions.get("updated_at") or "시간 없음")
        paper_positions_html = (
            "<div class='overlay-run status-ok'><b>Paper 계좌 동기화 완료</b>"
            f"<span>현재 보유 상태 갱신 {paper_updated}</span></div>"
        )

    overlay_run = d.get("overlay_run")
    if overlay_run is None:
        overlay_run_html = (
            "<div class='overlay-run status-muted'><b>오버레이 실행 기록 없음</b>"
            "<span>아직 확인형 실행 결과가 저장되지 않았습니다.</span></div>"
        )
    elif overlay_run.get("load_error"):
        error = html_lib.escape(str(overlay_run["load_error"]))
        overlay_run_html = (
            "<div class='overlay-run status-danger'><b>오버레이 상태 파일 읽기 실패</b>"
            f"<span>{error}</span></div>"
        )
    else:
        asset_results = overlay_run.get("assets", [])
        counts = {}
        for item in asset_results:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        failed = [
            item for item in asset_results
            if item.get("status") in {"failed", "unconfirmed"}
        ]
        confirmed = counts.get("confirmed", 0)
        deferred = counts.get("deferred", 0)
        blocked = counts.get("blocked", 0)
        failed_count = len(failed)
        finished = html_lib.escape(str(overlay_run.get("finished_at", "시간 없음")))
        summary = (
            f"확인 {confirmed}건 · 보류 {deferred}건 · 차단 {blocked}건 · "
            f"실패 {failed_count}건 · "
            f"완료 {finished}"
        )
        exit_code = int(overlay_run.get("exit_code", 2))
        if exit_code == 0 and blocked == 0:
            overlay_run_html = (
                "<div class='overlay-run status-ok'><b>최근 실행 성공</b>"
                f"<span>{summary}</span></div>"
            )
        elif exit_code == 0:
            blocked_details = []
            for item in asset_results:
                if item.get("status") != "blocked":
                    continue
                asset = _escape(item.get("asset", "?"))
                code = _escape(item.get("error_code") or "BLOCKED")
                blocked_details.append(f"<span>{asset} [{code}] 주문 차단</span>")
            overlay_run_html = (
                "<div class='overlay-run status-warning'><b>주문 차단 상태</b>"
                f"<span>{summary}</span>{''.join(blocked_details)}</div>"
            )
        else:
            title = "오버레이 치명적 실패" if exit_code == 2 else "오버레이 실행 실패"
            details = []
            fatal_error = overlay_run.get("fatal_error")
            if fatal_error:
                details.append(html_lib.escape(str(fatal_error)))
            for item in failed:
                asset = html_lib.escape(str(item.get("asset", "?")))
                code = html_lib.escape(str(item.get("error_code") or "UNKNOWN_ERROR"))
                error = html_lib.escape(str(item.get("error") or "원인 정보 없음"))
                details.append(f"{asset} [{code}] {error}")
            detail_html = "".join(f"<span>{detail}</span>" for detail in details)
            overlay_run_html = (
                f"<div class='overlay-run status-danger'><b>{title}</b>"
                f"<span>{summary}</span>{detail_html}</div>"
            )

    # 포지션 행
    if pos:
        pos_rows = ""
        for symbol, position in pos.items():
            chart_button = (
                f'<button type="button" class="asset-chart-link" '
                f'data-chart-symbol="{_escape(symbol)}">{_escape(symbol)}</button>'
            )
            try:
                qty = f"{float(position.get('qty')):,.9f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                qty = _escape(position.get("qty", "-"))
            def money(field):
                try:
                    return f"{float(position.get(field)):,.2f}"
                except (TypeError, ValueError):
                    return "-"
            unrealized = float(position.get("unrealized_pl") or 0.0)
            unrealized_class = "pos" if unrealized >= 0 else "neg"
            pos_rows += (
                f"<tr><td>{chart_button}</td><td>{_escape(position.get('side', ''))}</td>"
                f"<td>{qty}</td><td>{money('avg_entry_price')}</td>"
                f"<td>{money('current_price')}</td><td>{money('market_value')}</td>"
                f"<td class='{unrealized_class}'>{unrealized:,.2f}</td></tr>"
            )
    else:
        pos_rows = "<tr><td colspan='7' class='muted'>보유 포지션 없음</td></tr>"

    # 자산별 데이터 → 차트용 배열
    pa = d["per_asset"]
    pa_labels = list(pa.keys())
    pa_pnl = [pa[s]["pnl"] for s in pa_labels]
    pa_return = [pa[s].get("return_pct", 0.0) for s in pa_labels]
    pa_value = [pa[s].get("market_value", 0.0) for s in pa_labels]

    ov = d.get("overlay")
    if ov:
        a_rows = ""
        for x in ov["assets"]:
            chart_button = (
                f'<button type="button" class="asset-chart-link" '
                f'data-chart-symbol="{_escape(x["asset"])}">{_escape(x["asset"])}</button>'
            )
            sig_cls = "ok" if x["signal"] == "in" else "muted"
            sig_txt = "보유(in)" if x["signal"] == "in" else "현금(out)"
            if x.get("held") is None:
                held_txt = "확인 실패"
            else:
                held_txt = "예" if x["held"] else "아니오"
            qty = _escape(x.get("qty") or "-")
            avg_entry = _escape(x.get("avg_entry_price") or "-")
            try:
                price = f"{float(x['price']):,.2f}"
            except Exception:
                price = _escape(x["price"])
            try:
                ma = f"{float(x['ma']):,.2f}"
            except Exception:
                ma = _escape(x["ma"])
            a_rows += (f"<tr><td>{chart_button}</td><td class='{sig_cls}'>{sig_txt}</td>"
                       f"<td>{price}</td><td>{ma}</td><td>{held_txt}</td>"
                       f"<td>{qty}</td><td>{avg_entry}</td>"
                       f"<td>{_escape(x['action'])}</td></tr>")
        if ov.get("cash_pct") is None:
            allocation_summary = "투자/현금 비중 확인 실패"
        else:
            cashp = ov["cash_pct"] * 100
            allocation_summary = (
                f"투자 {ov['invested']}/{ov['n']}종목 · 현금 비중 <b>{cashp:.0f}%</b>"
            )
        drytag = " · dry-run(계획만)" if str(ov.get("dry_run", "")).lower() == "true" else ""
        overlay_html = (
            f"<div class='panel'><h2>🛡️ 오버레이 현황 — 200일선 추세 필터 (갱신 {_escape(ov['time'])})</h2>"
            f"<div style='margin-bottom:10px;font-size:13px;'>{allocation_summary}{drytag}</div>"
            f"<div class=\"table-scroll\">"
            f"<table><thead><tr><th>자산</th><th>신호</th><th>현재가</th><th>200이평</th>"
            f"<th>보유</th><th>수량</th><th>평균 체결가</th><th>최근 행동</th></tr></thead>"
            f"<tbody>{a_rows}</tbody></table></div></div>")
    else:
        overlay_html = ("<div class='panel'><h2>🛡️ 오버레이 현황</h2>"
                        "<div class='muted'>아직 기록 없음 — 터미널에서 <b>python main.py run</b> 을 한 번 실행하세요.</div></div>")

    # ----- 운영 모드 배너 -----
    _m = d.get("ops_mode") or {}
    _mode = _m.get("mode", "PAUSED_ALL")
    _MODE_TXT = {"NORMAL": ("정상", "ok"),
                 "BUY_LOCKED": ("신규매수 잠금", "warn"),
                 "PAUSED_ALL": ("전체정지", "danger")}
    _mtxt, _mcls = _MODE_TXT.get(_mode, ("알 수 없음", "danger"))
    _extra = ""
    if _mode == "PAUSED_ALL":
        _extra = " — 방어 매도까지 차단됩니다(위험)"
    elif _mode == "BUY_LOCKED":
        _extra = " — 신규 매수만 차단, 방어 매도는 작동"
    if _m.get("degraded"):
        _extra += " · 상태 파일 이상으로 안전 정지"
    mode_html = (f"<div class='panel'><h2>운영 모드</h2>"
                 f"<div class='flag {_mcls}'><span class='{_mcls}'>●</span> "
                 f"<b>{_mtxt}</b>{_extra}</div>"
                 f"<div class='muted' style='text-align:left;font-size:12px'>"
                 f"변경: python main.py mode --set BUY_LOCKED | 긴급: python main.py liquidate --execute</div></div>")

    if controls_enabled:
        controls_html = (
            "<div class='panel'><h2>운영 안전 제어</h2>"
            "<div class='ops-controls'>"
            "<button type='button' class='ops-button warn' data-ops-path='/api/operations/buy-lock' "
            "data-ops-confirm='신규매수 잠금'>신규매수 잠금</button>"
            "<button type='button' class='ops-button danger' data-ops-path='/api/operations/pause' "
            "data-ops-confirm='전체정지'>전체정지</button>"
            "<button type='button' class='ops-button ok' data-ops-path='/api/operations/resume' "
            "data-ops-confirm='자동주문 재개'>자동주문 재개</button>"
            "<button type='button' class='ops-button' data-ops-path='/api/orders/cancel-open' "
            "data-ops-confirm='미체결 주문 취소'>미체결 주문 취소</button>"
            "</div><div id='ops-status' class='muted ops-status' aria-live='polite'>"
            "매수·매도 수동 주문 기능은 제공하지 않습니다.</div></div>"
        )
    else:
        controls_html = ""

    # ----- 10분 감시 패널 -----
    _w = d.get("watch") or {}
    _STAGE_CLS = {"안정": "ok", "관찰": "warn", "임박": "warn",
                  "이탈": "danger", "진입후보": "ok"}
    if _w.get("assets"):
        _rows = ""
        _stale_count = 0
        _now_utc = dt.datetime.now(dt.timezone.utc)
        for _a, _v in _w["assets"].items():
            _cls = _STAGE_CLS.get(_v.get("stage"), "")
            try:
                _checked = dt.datetime.fromisoformat(
                    str(_v.get("checked_at", "")).replace("Z", "+00:00")
                )
                if _checked.tzinfo is None:
                    _checked = _checked.replace(tzinfo=dt.timezone.utc)
                _is_stale = ((_now_utc - _checked).total_seconds()
                             > int(_v.get("stale_after_sec") or 1200))
            except (TypeError, ValueError):
                _is_stale = True
            if _is_stale:
                _stale_count += 1
                _cls = "danger"
            try:
                _price = f"{float(_v.get('price')):,.2f}"
            except (TypeError, ValueError):
                _price = "-"
            try:
                _ma = f"{float(_v.get('ma')):,.2f}"
            except (TypeError, ValueError):
                _ma = "-"
            try:
                _distance = f"{float(_v.get('distance_pct')):+.1f}%"
            except (TypeError, ValueError):
                _distance = "-"
            _status_text = "오래됨" if _is_stale else _v.get("stage", "")
            _rows += (f"<tr><td>{_escape(_a)}</td><td>{_price}</td>"
                      f"<td>{_ma}</td><td>{_distance}</td>"
                      f"<td>{'예' if _v.get('held') else '아니오'}</td>"
                      f"<td>{_escape(_v.get('market_state', ''))}</td>"
                      f"<td class='{_cls}'>{_escape(_status_text)}</td></tr>")
        _mk = "정규장 진행 중" if _w.get("market_open") else "장 마감 중 · 마지막 정규장 종가 기준"
        _nx = f" | 다음 개장 {str(_w.get('next_open',''))[:16]}" if _w.get("next_open") else ""
        _fail = _w.get("failures") or []
        def _failure_asset(value):
            return value.get("asset", "?") if isinstance(value, dict) else value[0]
        _failhtml = (f"<div class='flag danger'>조회 실패 {len(_fail)}건: "
                     + ", ".join(_escape(_failure_asset(x)) for x in _fail)
                     + "</div>") if _fail else ""
        if _stale_count:
            _failhtml += f"<div class='flag danger'>감시 정보 오래됨 {_stale_count}건</div>"
        if _w.get("last_error"):
            _failhtml += f"<div class='flag danger'>{_escape(_w['last_error'])}</div>"
        watch_html = (f"<div class='panel'><h2>🔎 위험 감시 (읽기 전용)</h2>"
                      f"<div style='font-size:12px' class='muted'>"
                      f"마지막 감시 {str(_w.get('last_success',''))[:19]} · {_mk}{_nx}</div>"
                      f"{_failhtml}"
                      f"<table><thead><tr><th>자산</th><th>현재가</th><th>200일선</th>"
                      f"<th>거리</th><th>보유</th><th>시장</th><th>상태</th></tr></thead>"
                      f"<tbody>{_rows}</tbody></table></div>")
    elif _w.get("last_error"):
        watch_html = (f"<div class='panel'><h2>🔎 위험 감시</h2>"
                      f"<div class='flag danger'>감시 실패: {_w['last_error'][:120]}</div></div>")
    else:
        watch_html = ("<div class='panel'><h2>🔎 위험 감시</h2>"
                      "<div class='muted'>실행 기록 없음 — python main.py watch 를 실행하세요.</div></div>")

    curve = _script_json(d["equity_curve"])
    dd_curve = _script_json(d.get("drawdown_curve", []))
    j_labels = _script_json(pa_labels)
    j_pnl = _script_json(pa_pnl)
    j_return = _script_json(pa_return)
    j_value = _script_json(pa_value)
    escaped_status = _escape(d["status"])
    escaped_source = _escape(d["source"])
    escaped_generated = _escape(d["generated"])

    return f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>트레이딩 봇 대시보드</title>
<script defer src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system, "Segoe UI", Roboto, "Malgun Gothic", sans-serif;
         background:#0d1117; color:#e6edf3; padding:14px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#8b949e; font-size:12px; margin-bottom:12px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:12px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:11px 12px; }}
  .card .label {{ color:#8b949e; font-size:12px; }}
  .card .val {{ font-size:19px; font-weight:600; margin-top:4px; }}
  .pos {{ color:#3fb950; }} .neg {{ color:#f85149; }}
  .ok {{ color:#3fb950; }} .warn {{ color:#d29922; }} .danger {{ color:#f85149; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; font-weight:600; }}
  .badge.ok {{ background:#12261a; color:#3fb950; }}
  .badge.danger {{ background:#2d1114; color:#f85149; }}
  .panel {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:11px 12px; margin-bottom:10px; min-width:0; }}
  .panel h2 {{ font-size:13px; margin:0 0 8px; color:#c9d1d9; }}
  .table-scroll {{ width:100%; overflow-x:auto; }}
  table {{ width:100%; min-width:680px; border-collapse:collapse; font-size:11.5px; line-height:1.25; }}
  th,td {{ text-align:left; padding:5px 6px; border-bottom:1px solid #21262d; white-space:nowrap; }}
  th {{ color:#8b949e; font-weight:500; }}
  .muted {{ color:#6e7681; text-align:center; }}
  .flag {{ padding:8px 12px; border-radius:8px; margin-bottom:8px; font-size:13px; }}
  .flag.ok {{ background:#12261a; }} .flag.warn {{ background:#2b2410; }} .flag.danger {{ background:#2d1114; }}
  .overlay-run {{ display:flex; flex-direction:column; gap:5px; border-radius:10px;
                  padding:8px 11px; margin-bottom:9px; font-size:12px; border:1px solid; }}
  .overlay-run.status-ok {{ background:#12261a; border-color:#238636; color:#7ee787; }}
  .overlay-run.status-danger {{ background:#2d1114; border-color:#da3633; color:#ff7b72; }}
  .overlay-run.status-warning {{ background:#2b2410; border-color:#9e6a03; color:#e3b341; }}
  .overlay-run.status-muted {{ background:#161b22; border-color:#30363d; color:#8b949e; }}
  .ops-controls {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .ops-button {{ border:1px solid #30363d; border-radius:7px; background:#21262d;
                 color:#e6edf3; padding:7px 11px; cursor:pointer; font-weight:600; }}
  .ops-button.warn {{ border-color:#9e6a03; color:#e3b341; }}
  .ops-button.danger {{ border-color:#da3633; color:#ff7b72; }}
  .ops-button.ok {{ border-color:#238636; color:#7ee787; }}
  .ops-button:disabled {{ opacity:.55; cursor:wait; }}
  .ops-status {{ margin-top:8px; text-align:left; }}
  .two {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:10px; }}
  .positions-row {{ grid-template-columns:minmax(240px,.7fr) minmax(0,1.3fr); }}
  .chart-frame {{ position:relative; width:100%; height:160px; }}
  .chart-frame.chart-wide {{ height:150px; }}
  .chart-frame.chart-small {{ height:160px; }}
  .chart-frame canvas {{ width:100% !important; height:100% !important; }}
  .asset-chart-link {{ border:0; background:none; color:#58a6ff; padding:0; font:inherit;
                       font-weight:650; cursor:pointer; }}
  .asset-chart-link:hover {{ text-decoration:underline; }}
  .asset-chart-link:focus-visible {{ outline:2px solid #58a6ff; outline-offset:3px; border-radius:2px; }}
  .chart-modal[hidden] {{ display:none; }}
  .chart-modal {{ position:fixed; inset:0; z-index:1000; display:grid; place-items:center;
                  padding:18px; background:rgba(1,4,9,.82); backdrop-filter:blur(3px); }}
  .chart-dialog {{ width:min(1080px,96vw); max-height:92vh; overflow:auto; background:#161b22;
                   border:1px solid #30363d; border-radius:12px; box-shadow:0 24px 70px rgba(0,0,0,.55); }}
  .chart-modal-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px;
                       padding:13px 15px 9px; }}
  .chart-modal-head h2 {{ margin:0; font-size:17px; color:#e6edf3; }}
  .chart-close {{ width:34px; height:34px; border:1px solid #30363d; border-radius:7px;
                  background:#21262d; color:#e6edf3; font-size:22px; cursor:pointer; }}
  .chart-toolbar {{ display:flex; align-items:center; flex-wrap:wrap; gap:7px; padding:0 15px 10px; }}
  .chart-range {{ border:1px solid #30363d; border-radius:7px; background:#0d1117; color:#8b949e;
                  padding:6px 10px; cursor:pointer; font-size:12px; }}
  .chart-range.active {{ border-color:#58a6ff; background:#13233a; color:#79c0ff; }}
  .chart-meta {{ margin-left:auto; color:#8b949e; font-size:12px; }}
  .chart-status {{ min-height:28px; padding:5px 15px 8px; color:#8b949e; font-size:12px; }}
  .chart-status.error {{ color:#ff7b72; background:#2d1114; border-block:1px solid #da3633; padding:9px 15px; }}
  .asset-chart-wrap {{ position:relative; height:min(570px,65vh); min-height:330px; padding:0 8px 10px; }}
  #asset-candle-canvas {{ width:100%; height:100%; display:block; }}
  .chart-legend {{ display:flex; flex-wrap:wrap; gap:12px; padding:0 15px 10px; color:#8b949e; font-size:11px; }}
  .legend-dot {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:4px; }}
  @media(max-width:720px){{ .two {{ grid-template-columns:1fr; }} }}
  @media(max-width:720px){{ .chart-meta {{ width:100%; margin-left:0; }} .asset-chart-wrap {{ min-height:300px; }} }}
  .foot {{ color:#6e7681; font-size:11px; margin-top:12px; }}
</style></head>
<body>
  <h1>🤖 트레이딩 봇 대시보드
    <span class="badge {status_color}">{escaped_status}</span></h1>
  <div class="sub">데이터 출처: {escaped_source} · 갱신 {escaped_generated} · {auto_refresh}초마다 자동 새로고침</div>

  {overlay_run_html}
  {paper_positions_html}

  <div class="grid">
    <div class="card"><div class="label">현재 자산</div><div class="val">{d['equity']:,.2f}</div></div>
    <div class="card"><div class="label">누적손익</div><div class="val {pnl_color}">{d['total_pnl']:+,.2f}</div>
      <div class="label">초기 자산 {STARTING_EQUITY:,.0f} 기준</div></div>
    <div class="card"><div class="label">당일손익</div><div class="val {daily_color}">{daily_pnl:+,.2f}</div></div>
    <div class="card"><div class="label">고점 대비 낙폭</div>
      <div class="val {'danger' if dd_danger else ('warn' if dd_warn else '')}">{dd_pct:.1f}%</div>
      <div class="label">정지선 {dd_limit:.0f}%</div></div>
    <div class="card"><div class="label">현금</div><div class="val">{d.get('cash', 0.0):,.2f}</div></div>
    <div class="card"><div class="label">매수 가능 금액</div><div class="val">{d.get('buying_power', 0.0):,.2f}</div></div>
  </div>

  {mode_html}

  {controls_html}

  {watch_html}

  {overlay_html}

  <div class="panel">
    <h2>리스크 신호</h2>
    {''.join(f'<div class="flag {c}"><span class="{c}">●</span> {t}</div>' for t,c in flags)}
  </div>

  <div class="two chart-row">
    <div class="panel">
      <h2>자산 곡선 (시작 {STARTING_EQUITY:,.0f} 기준)</h2>
      <div class="chart-frame chart-wide"><canvas id="eq"></canvas></div>
    </div>
    <div class="panel">
      <h2>낙폭 추이 (고점 대비 %, 정지선 {dd_limit:.0f}%)</h2>
      <div class="chart-frame chart-wide"><canvas id="dd"></canvas></div>
    </div>
  </div>

  <div class="two">
    <div class="panel">
      <h2>현재 포지션 미실현손익</h2>
      <div class="chart-frame chart-small"><canvas id="pnlBar"></canvas></div>
    </div>
    <div class="panel">
      <h2>현재 포지션 수익률 (%)</h2>
      <div class="chart-frame chart-small"><canvas id="returnBar"></canvas></div>
    </div>
  </div>

  <div class="two positions-row">
    <div class="panel">
      <h2>현재 보유 비중</h2>
      <div class="chart-frame chart-small"><canvas id="allocation"></canvas></div>
    </div>
    <div class="panel">
      <h2>Paper 계좌 보유 포지션 ({d.get('n_positions', len(pos))}종목)</h2>
      <div class="table-scroll">
      <table><thead><tr><th>자산</th><th>방향</th><th>수량</th><th>평균 체결가</th><th>현재가</th><th>평가액</th><th>미실현손익</th></tr></thead>
      <tbody>{pos_rows}</tbody></table></div>
    </div>
  </div>

  <div id="asset-chart-modal" class="chart-modal" role="dialog" aria-modal="true"
       aria-labelledby="asset-chart-title" hidden>
    <div class="chart-dialog">
      <div class="chart-modal-head">
        <h2 id="asset-chart-title">자산 캔들차트</h2>
        <button type="button" id="asset-chart-close" class="chart-close" aria-label="차트 닫기">&times;</button>
      </div>
      <div class="chart-toolbar" aria-label="차트 기간">
        <button type="button" class="chart-range" data-chart-range="1D">1일</button>
        <button type="button" class="chart-range" data-chart-range="1W">1주</button>
        <button type="button" class="chart-range active" data-chart-range="1M">1개월</button>
        <button type="button" class="chart-range" data-chart-range="3M">3개월</button>
        <button type="button" class="chart-range" data-chart-range="1Y">1년</button>
        <span id="asset-chart-meta" class="chart-meta"></span>
      </div>
      <div class="chart-legend">
        <span><i class="legend-dot" style="background:#3fb950"></i>상승</span>
        <span><i class="legend-dot" style="background:#f85149"></i>하락</span>
        <span><i class="legend-dot" style="background:#58a6ff"></i>200봉 이동평균</span>
        <span><i class="legend-dot" style="background:#d29922"></i>평균매수가</span>
        <span><i class="legend-dot" style="background:#6e7681"></i>거래량</span>
      </div>
      <div id="asset-chart-status" class="chart-status" aria-live="polite"></div>
      <div class="asset-chart-wrap"><canvas id="asset-candle-canvas"></canvas></div>
    </div>
  </div>

  <div class="foot">Alpaca Paper 계좌를 읽기 전용으로 조회하고 평가액 이력만 로컬에 저장합니다. 수익을 보장하지 않으며, 투자 판단과 책임은 본인에게 있습니다.</div>

<script>
  const chartModal = document.getElementById('asset-chart-modal');
  const chartCanvas = document.getElementById('asset-candle-canvas');
  const chartStatus = document.getElementById('asset-chart-status');
  const chartMeta = document.getElementById('asset-chart-meta');
  const chartTitle = document.getElementById('asset-chart-title');
  const chartState = {{ symbol:null, range:'1M', payload:null, opener:null }};
  const chartApiOrigin = window.location.protocol === 'file:' ? 'http://127.0.0.1:8787' : '';
  const opsToken = {_script_json(ops_token if controls_enabled else '')};
  const opsStatus = document.getElementById('ops-status');

  document.querySelectorAll('[data-ops-path]').forEach(button => {{
    button.addEventListener('click', async () => {{
      const confirmation = button.dataset.opsConfirm;
      const typed = window.prompt(`계속하려면 "${{confirmation}}" 을(를) 정확히 입력하세요.`);
      if (typed === null) return;
      document.querySelectorAll('[data-ops-path]').forEach(item => item.disabled = true);
      if (opsStatus) opsStatus.textContent = '처리 중…';
      try {{
        const response = await fetch(button.dataset.opsPath, {{
          method:'POST',
          headers:{{'Content-Type':'application/json', 'X-Ops-Token':opsToken}},
          body:JSON.stringify({{confirmation:typed}}),
        }});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        if (opsStatus) opsStatus.textContent = '완료되었습니다. 화면을 갱신합니다.';
        window.setTimeout(() => window.location.reload(), 500);
      }} catch (error) {{
        if (opsStatus) {{
          opsStatus.textContent = `실패: ${{error.message}}`;
          opsStatus.classList.add('danger');
        }}
      }} finally {{
        document.querySelectorAll('[data-ops-path]').forEach(item => item.disabled = false);
      }}
    }});
  }});

  window.setInterval(() => {{
    if (chartModal.hidden) window.location.reload();
  }}, {int(auto_refresh)} * 1000);

  function priceText(value) {{
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
    const number = Number(value);
    const decimals = Math.abs(number) < 10 ? 4 : 2;
    return number.toLocaleString('ko-KR', {{ maximumFractionDigits:decimals }});
  }}

  function drawAssetChart() {{
    const payload = chartState.payload;
    if (!payload || !payload.bars || !payload.bars.length || chartModal.hidden) return;
    const bars = payload.bars;
    const rect = chartCanvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    chartCanvas.width = Math.round(rect.width * ratio);
    chartCanvas.height = Math.round(rect.height * ratio);
    const ctx = chartCanvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const width = rect.width, height = rect.height;
    const pad = {{ left:56, right:16, top:12, bottom:28 }};
    const priceBottom = Math.round(height * .76);
    const volumeTop = Math.round(height * .81);
    const plotWidth = width - pad.left - pad.right;
    const priceHeight = priceBottom - pad.top;
    const values = [];
    bars.forEach(bar => {{ values.push(Number(bar.low), Number(bar.high)); if (bar.ma200 !== null) values.push(Number(bar.ma200)); }});
    if (payload.avg_entry_price !== null && payload.avg_entry_price !== undefined) values.push(Number(payload.avg_entry_price));
    let minPrice = Math.min(...values), maxPrice = Math.max(...values);
    const margin = Math.max((maxPrice - minPrice) * .06, Math.abs(maxPrice) * .002, .0001);
    minPrice -= margin; maxPrice += margin;
    const maxVolume = Math.max(...bars.map(bar => Number(bar.volume) || 0), 1);
    const xAt = index => pad.left + (index + .5) * plotWidth / bars.length;
    const yAt = value => pad.top + (maxPrice - Number(value)) / (maxPrice - minPrice) * priceHeight;

    ctx.clearRect(0, 0, width, height);
    ctx.font = '11px -apple-system, Segoe UI, sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let row = 0; row <= 4; row++) {{
      const y = pad.top + row * priceHeight / 4;
      const value = maxPrice - row * (maxPrice - minPrice) / 4;
      ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      ctx.fillStyle = '#8b949e'; ctx.fillText(priceText(value), pad.left - 7, y);
    }}

    const candleSpace = plotWidth / bars.length;
    const bodyWidth = Math.max(1, Math.min(12, candleSpace * .66));
    bars.forEach((bar, index) => {{
      const x = xAt(index), rising = Number(bar.close) >= Number(bar.open);
      const color = rising ? '#3fb950' : '#f85149';
      const volumeHeight = (Number(bar.volume) || 0) / maxVolume * Math.max(height - volumeTop - pad.bottom, 1);
      ctx.fillStyle = rising ? 'rgba(63,185,80,.28)' : 'rgba(248,81,73,.28)';
      ctx.fillRect(x - bodyWidth / 2, height - pad.bottom - volumeHeight, bodyWidth, volumeHeight);
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, yAt(bar.high)); ctx.lineTo(x, yAt(bar.low)); ctx.stroke();
      const openY = yAt(bar.open), closeY = yAt(bar.close);
      ctx.fillStyle = color;
      ctx.fillRect(x - bodyWidth / 2, Math.min(openY, closeY), bodyWidth, Math.max(1.5, Math.abs(openY - closeY)));
    }});

    ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.6; ctx.setLineDash([]); ctx.beginPath();
    let maStarted = false;
    bars.forEach((bar, index) => {{
      if (bar.ma200 === null) return;
      const x = xAt(index), y = yAt(bar.ma200);
      if (!maStarted) {{ ctx.moveTo(x, y); maStarted = true; }} else ctx.lineTo(x, y);
    }});
    if (maStarted) ctx.stroke();

    if (payload.avg_entry_price !== null && payload.avg_entry_price !== undefined) {{
      const y = yAt(payload.avg_entry_price);
      ctx.strokeStyle = '#d29922'; ctx.lineWidth = 1.3; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      ctx.setLineDash([]);
    }}

    ctx.fillStyle = '#8b949e'; ctx.textBaseline = 'top';
    const tickIndexes = [0, Math.floor((bars.length - 1) / 2), bars.length - 1];
    tickIndexes.forEach((index, tick) => {{
      const date = new Date(bars[index].time);
      ctx.textAlign = tick === 0 ? 'left' : (tick === 2 ? 'right' : 'center');
      ctx.fillText(date.toLocaleDateString('ko-KR', {{ month:'short', day:'numeric' }}), xAt(index), height - pad.bottom + 7);
    }});
  }}

  async function loadAssetChart() {{
    chartStatus.className = 'chart-status';
    chartStatus.textContent = '실제 시세를 불러오는 중…';
    chartMeta.textContent = '';
    chartState.payload = null;
    try {{
      const response = await fetch(chartApiOrigin + '/api/chart?symbol=' + encodeURIComponent(chartState.symbol) + '&range=' + encodeURIComponent(chartState.range), {{ cache:'no-store' }});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || ('HTTP ' + response.status));
      chartState.payload = payload;
      const last = payload.bars[payload.bars.length - 1];
      chartStatus.textContent = payload.bars.length + '개 봉 · 현재 ' + priceText(last.close) + ' · 평균매수가 ' + priceText(payload.avg_entry_price);
      chartMeta.textContent = payload.timeframe + ' · O ' + priceText(last.open) + ' H ' + priceText(last.high) + ' L ' + priceText(last.low) + ' C ' + priceText(last.close);
      drawAssetChart();
    }} catch (error) {{
      chartStatus.className = 'chart-status error';
      chartStatus.textContent = '차트 조회 실패: ' + error.message;
      chartMeta.textContent = '';
    }}
  }}

  function openAssetChart(button) {{
    chartState.symbol = button.dataset.chartSymbol;
    chartState.opener = button;
    chartTitle.textContent = chartState.symbol + ' 캔들차트';
    chartModal.hidden = false;
    document.body.style.overflow = 'hidden';
    document.getElementById('asset-chart-close').focus();
    loadAssetChart();
  }}

  function closeAssetChart() {{
    chartModal.hidden = true;
    document.body.style.overflow = '';
    chartState.payload = null;
    if (chartState.opener) chartState.opener.focus();
  }}

  document.querySelectorAll('.asset-chart-link').forEach(button => button.addEventListener('click', () => openAssetChart(button)));
  document.querySelectorAll('.chart-range').forEach(button => button.addEventListener('click', () => {{
    chartState.range = button.dataset.chartRange;
    document.querySelectorAll('.chart-range').forEach(item => item.classList.toggle('active', item === button));
    loadAssetChart();
  }}));
  document.getElementById('asset-chart-close').addEventListener('click', closeAssetChart);
  chartModal.addEventListener('click', event => {{ if (event.target === chartModal) closeAssetChart(); }});
  document.addEventListener('keydown', event => {{ if (event.key === 'Escape' && !chartModal.hidden) closeAssetChart(); }});
  window.addEventListener('resize', drawAssetChart);

  function renderSummaryCharts() {{
  if (typeof Chart === 'undefined') {{ window.setTimeout(renderSummaryCharts, 500); return; }}
  const GRID = '#21262d', TXT = '#8b949e';
  const noLegend = {{ plugins:{{legend:{{display:false}}}} }};

  const eq = {curve};
  if (eq.length) new Chart(document.getElementById('eq'), {{
    type:'line',
    data:{{ labels: eq.map((_,i)=>i+1),
      datasets:[{{ data:eq, borderColor:'#58a6ff', backgroundColor:'rgba(88,166,255,.12)',
        fill:true, pointRadius:eq.length === 1 ? 3 : 0, borderWidth:2, tension:.2 }}] }},
    options:{{ ...noLegend, maintainAspectRatio:false, scales:{{ x:{{display:false}},
      y:{{ticks:{{color:TXT}}, grid:{{color:GRID}}}} }} }}
  }});

  const dd = {dd_curve};
  if (dd.length) new Chart(document.getElementById('dd'), {{
    type:'line',
    data:{{ labels: dd.map((_,i)=>i+1),
      datasets:[{{ data:dd, borderColor:'#f85149', backgroundColor:'rgba(248,81,73,.15)',
        fill:true, pointRadius:0, borderWidth:2, tension:.2 }}] }},
    options:{{ ...noLegend, maintainAspectRatio:false, scales:{{ x:{{display:false}},
      y:{{reverse:true, ticks:{{color:TXT, callback:v=>v+'%'}}, grid:{{color:GRID}}}} }} }}
  }});

  const labels = {j_labels}, pnl = {j_pnl}, returns = {j_return}, values = {j_value};
  const PAL = ['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149'];

  if (labels.length) new Chart(document.getElementById('pnlBar'), {{
    type:'bar',
    data:{{ labels, datasets:[{{ data:pnl,
      backgroundColor: pnl.map(v=> v>=0 ? 'rgba(63,185,80,.75)' : 'rgba(248,81,73,.75)') }}] }},
    options:{{ ...noLegend, maintainAspectRatio:false, scales:{{ x:{{ticks:{{color:TXT}}, grid:{{display:false}}}},
      y:{{ticks:{{color:TXT}}, grid:{{color:GRID}}}} }} }}
  }});

  if (labels.length) new Chart(document.getElementById('returnBar'), {{
    type:'bar',
    data:{{ labels, datasets:[{{ data:returns,
      backgroundColor: returns.map(v=> v>=0 ? 'rgba(63,185,80,.75)' : 'rgba(248,81,73,.75)') }}] }},
    options:{{ ...noLegend, maintainAspectRatio:false, scales:{{ x:{{ticks:{{color:TXT}}, grid:{{display:false}}}},
      y:{{ticks:{{color:TXT, callback:v=>v+'%'}}, grid:{{color:GRID}}}} }} }}
  }});

  if (labels.length) new Chart(document.getElementById('allocation'), {{
    type:'doughnut',
    data:{{ labels, datasets:[{{ data:values, backgroundColor:PAL, borderColor:'#161b22', borderWidth:2 }}] }},
    options:{{ maintainAspectRatio:false, plugins:{{legend:{{position:'right', labels:{{color:TXT}}}}}} }}
  }});
  }}
  renderSummaryCharts();
</script>
</body></html>"""


# ---------------- 서버 ----------------
class DashboardServer(ThreadingHTTPServer):
    """Keep controls responsive while a Paper API page refresh is slow."""
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200, cors=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _local_host_request(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _authorized_operation_request(self):
        port = self.server.server_port
        origin = self.headers.get("Origin", "")
        allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        return (
            self._local_host_request()
            and origin in allowed_origins
            and self.headers.get("X-Ops-Token", "") == OPS_SESSION_TOKEN
            and self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            == "application/json"
        )

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        allowed = {
            "/api/operations/buy-lock",
            "/api/operations/pause",
            "/api/operations/resume",
            "/api/orders/cancel-open",
        }
        if parsed.path not in allowed:
            self._send_json({"ok": False, "error": "허용되지 않은 작업입니다"}, 404)
            return
        if not self._authorized_operation_request():
            self._send_json({"ok": False, "error": "요청 인증 실패"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("요청 크기가 올바르지 않습니다")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON 객체가 필요합니다")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        status, response = _perform_operation(parsed.path, payload)
        self._send_json(response, status)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not self._local_host_request():
            self._send_json({"error": "localhost 요청만 허용됩니다"}, status=403)
            return
        if parsed.path == "/api/chart":
            query = parse_qs(parsed.query)
            symbol = query.get("symbol", [""])[0]
            range_key = query.get("range", ["1M"])[0]
            try:
                payload = _chart_payload_for_request(symbol, range_key)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400, cors=True)
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {"error": f"차트 조회 실패: {type(exc).__name__}: {str(exc)[:240]}"},
                    status=502,
                    cors=True,
                )
            else:
                self._send_json(payload, cors=True)
            return

        if parsed.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        html = render_html(
            collect(), controls_enabled=True, ops_token=OPS_SESSION_TOKEN
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        try:
            self.wfile.write(html)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def log_message(self, *a):  # 조용히
        pass


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--once", action="store_true", help="dashboard.html 로 한 번 저장")
    p.add_argument("--refresh", type=int, default=15)
    args = p.parse_args(argv)

    if args.once:
        html = render_html(collect(), auto_refresh=args.refresh)
        with open("dashboard.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("dashboard.html 저장 완료 - 브라우저로 열어보세요.")
        return

    server = DashboardServer(("127.0.0.1", args.port), Handler)
    print(f"대시보드 실행 중: http://localhost:{args.port}")
    print("종료하려면 Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n대시보드 종료")


if __name__ == "__main__":
    main()
