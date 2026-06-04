# PBA X → Toss Securities Auto-Sync

PBA 인플루언서의 X(Twitter) 게시글을 감지하고, LLM으로 매매 신호를 해석한 뒤 [tossinvest-cli](https://github.com/JungHoonGhae/tossinvest-cli)(`tossctl`)로 토스증권 계좌에서 **비중 기반 자동 매수/매도** 및 **소프트웨어 스탑라인**을 실행합니다.

> **경고:** tossinvest-cli는 비공식 도구입니다. 토스증권 이용약관 위반 가능성이 있으며, 자동매매로 인한 손실 책임은 본인에게 있습니다.

## WSL 로그인 (headless)

`playwright` 오류가 나면 auth 전용 Python 환경을 먼저 설치:

```bash
bash scripts/install_tossctl_auth.sh
bash scripts/tossctl_auth_login.sh
```

또는:

```bash
source ~/.local/share/tossctl/auth.env
tossctl auth login --headless --qr-output /tmp/toss-qr.png
```

QR URL을 터미널에서 확인하거나 Windows에서 `\\wsl$\Ubuntu\tmp\toss-qr.png` 열기.

## X 수집 (구독글 포함)

공식 X API는 **공개 타임라인만** 제공합니다. PBA **구독 전용 글**은 Playwright + **내 X 계정 세션**으로 프로필을 스크래핑합니다.

### WSL 터미널(CLI) — 브라우저 안 뜸 (일반적)

tossctl은 `auth login --headless` + QR이라 WSL에서 됩니다. **X는 QR 로그인이 없어서** WSL에서 Chrome 창이 안 뜨는 경우가 많습니다 (Ubuntu 20.04 WSL 등).

**해결: Windows Chrome에서 쿠키만 복사 → WSL import**

```bash
bash scripts/install_x_auth.sh
bash scripts/x_auth_import.sh
```

Windows Chrome: `x.com` 로그인 → F12 → Application → Cookies → `auth_token`, `ct0` 복사  
또는 `~/.config/pba-toss-sync/x-cookies.env` 에 저장 후 import.

### GUI 있는 환경 (WSLg / 로컬 Linux 데스크톱)

```bash
bash scripts/x_auth_login.sh   # Chrome 창 → 로그인 → Enter
```

```bash
PYTHONPATH=. python -m src.main x-auth-status   # session exists: True
```

`config/settings.yaml` → `x.source: browser` (기본값). 공개 API만 쓰려면 `x.source: api` + `.env`의 `X_BEARER_TOKEN`.

## Quick Start

```bash
cd pba-toss-sync
bash scripts/setup.sh
cp .env.example .env   # vLLM 또는 GEMINI_API_KEY
# config/settings.yaml 에 pba.username 설정

bash scripts/x_auth_login.sh
tossctl auth login     # QR + "이 기기 로그인 유지"

PYTHONPATH=. python -m src.main status
PYTHONPATH=. python -m src.main daemon   # 기본 dry-run
```

## Commands


| Command                                      | Description                 |
| -------------------------------------------- | --------------------------- |
| `python -m src.main daemon`                  | 24/7 X 폴링 + 신호 처리 + 스탑 감시   |
| `python -m src.main backfill --limit 20`     | 최근 게시글 일괄 처리                |
| `python -m src.main parse "Bought $NVDA..."` | LLM/휴리스틱 파싱 테스트             |
| `python -m src.main status`                  | X/tossctl 세션·dry-run·PBA 상태 |
| `python -m src.main x-auth-status`           | X browser 세션 파일 확인          |
| `python -m src.main analyze --days 7`        | N일치 게시글 수집 + LLM 리포트        |


## Configuration

- `[config/settings.yaml](config/settings.yaml)` — PBA 핸들, 한도, dry-run, LLM 모델
- `[config/toss_gates.json](config/toss_gates.json)` — tossctl 거래 게이트 템플릿
- `[.env](.env.example)` — API 키

### Phase 1 (기본): Dry-run

- `trading.dry_run: true`
- `allow_live_order_actions: false` in tossctl config
- 주문 없이 audit log만 기록

### Phase 2: 소액 실거래

```bash
bash scripts/enable_live_trading.sh
# config/settings.yaml: trading.dry_run: false
```

### Phase 3: 24/7 systemd

```bash
sudo cp deploy/pba-toss-sync.service /etc/systemd/system/
sudo systemctl enable --now pba-toss-sync@$USER

# 세션 연장 cron
crontab -l 2>/dev/null; cat deploy/cron.auth_extend
```

## LLM (로컬 AGY)

기본 파서는 **Antigravity CLI** (`agy -p`)를 사용합니다. Gemma4 vLLM은 쓰지 않습니다.

```bash
# agy 로그인 필요 (한 번)
agy   # Antigravity 로그인 확인

# 설정: config/settings.yaml → llm.provider: agy
# .env → LLM_PROVIDER=agy, LLM_AGY_WORKDIR=$HOME/trading-bot
PYTHONPATH=. python -m src.main parse "Bought $NVDA at 120"
```

vLLM/Gemma4로 되돌리려면 `llm.provider: vllm` + `LLM_VLLM_ENABLED=1`.

## Architecture

```
X browser (Playwright) or API → LLM parse → PBA state → Position sizer → tossctl
                                                              ↘ Stop monitor (quote live)
```

## Safety

- 종목당 최대 비중 (`max_position_pct`)
- 일일 매수 한도 (`daily_buy_limit_krw`)
- tweet_id 멱등성 (중복 주문 방지)
- kill switch: `allow_live_order_actions: false`
- confidence < 0.85 → 주문 스킵

## X 수집: 프로필 + `/superfollows`

구독 전용 타임라인 `https://x.com/801010athlete/superfollows` 와 일반 프로필을 **둘 다** 스크롤해 합칩니다 (`x.include_superfollows: true`).

일부 글(예: Subscribers 라벨이 붙은 EOD 보유표)은 프로필에만 있고 superfollows에는 없을 수 있어, `--tweet-url` 보강은 여전히 유효합니다.

```bash
PYTHONPATH=. python -m src.main process-tweet https://x.com/i/status/TWEET_ID
PYTHONPATH=. python -m src.main analyze --days 1 --tweet-url https://x.com/i/status/TWEET_ID
```

## PBA "stop" = 조건매도 가격

PBA가 말하는 **stop / 스탑**은 **지금 매도**가 아니라 **조건매도 가격**입니다.

- 예: `Rebought $1721.16, stop yesterday low $1707.99` → 1721에 매수, **1707.99까지 떨어지면** 매도
- `Stopped $SNDK`처럼 **과거형**은 이미 청산된 뜻 → 즉시 `sell` (조건매도 아님)

tossctl은 브로커 조건매도 API가 없어 `stop_monitor.py`가 `quote batch --live`로 가격을 감시하고, 시세 ≤ `stop_price`일 때 **지정가 매도**를 실행합니다 (`stop_price` = PBA 조건매도).

## Tests

```bash
source .venv/bin/activate
PYTHONPATH=. pytest -q
```

