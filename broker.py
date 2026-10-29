"""브로커 연결과 실계좌 3중 잠금.

resolve_mode()는 alpaca SDK 없이도 동작하는 순수 함수다(테스트 대상).
실계좌는 아래 세 가지가 '동시에' 충족될 때만 열린다:
  1) 환경변수 ALPACA_PAPER == 'false'
  2) 환경변수 ALPACA_LIVE_CONFIRM == 'I_UNDERSTAND_REAL_MONEY'
  3) 명령에 --live 플래그(live_flag=True)
하나라도 없으면 모의(paper)로 강등하고 이유를 남긴다.
"""
from dataclasses import dataclass, field

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
LIVE_ENDPOINT = "https://api.alpaca.markets"
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"


@dataclass
class ModeDecision:
    mode: str          # 'paper' | 'live'
    endpoint: str
    is_live: bool
    reasons: list = field(default_factory=list)


def resolve_mode(env: dict, live_flag: bool) -> ModeDecision:
    """세 조건을 검사해 최종 모드를 결정한다. 기본값은 항상 paper."""
    reasons = []
    paper_flag = str(env.get("ALPACA_PAPER", "true")).strip().lower()
    confirm = str(env.get("ALPACA_LIVE_CONFIRM", "")).strip()

    cond_paper_false = paper_flag == "false"
    cond_confirm = confirm == LIVE_CONFIRM_PHRASE
    cond_flag = bool(live_flag)

    if cond_paper_false and cond_confirm and cond_flag:
        return ModeDecision("live", LIVE_ENDPOINT, True,
                            ["3중 잠금 충족: 실계좌 경로 열림"])

    # 강등 사유 기록
    if not cond_paper_false:
        reasons.append("ALPACA_PAPER 가 false 가 아님")
    if not cond_confirm:
        reasons.append(f"ALPACA_LIVE_CONFIRM 이 '{LIVE_CONFIRM_PHRASE}' 가 아님")
    if not cond_flag:
        reasons.append("--live 플래그가 없음")
    reasons.append("→ 안전하게 모의(paper) 모드로 내려서 실행")
    return ModeDecision("paper", PAPER_ENDPOINT, False, reasons)


class AlpacaBroker:
    """실제 Alpaca 연결 래퍼. alpaca-py 는 여기서만 지연 임포트한다."""

    def __init__(self, env: dict, live_flag: bool = False):
        self.decision = resolve_mode(env, live_flag)
        self._api_key = env.get("ALPACA_API_KEY")
        self._secret = env.get("ALPACA_SECRET_KEY")
        self._client = None  # 실제 연결 시 생성

    def connect(self):
        from alpaca.trading.client import TradingClient
        self._client = TradingClient(
            self._api_key, self._secret,
            paper=not self.decision.is_live,
        )
        return self._client

    def account(self):
        if self._client is None:
            self.connect()
        return self._client.get_account()

    def max_loss_per_trade(self, equity, risk_pct):
        return equity * risk_pct
