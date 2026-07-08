# -*- coding: utf-8 -*-
"""
ETF 수급 텔레그램 알림
----------------------
etf_tracker.py 가 수집을 마친 뒤 실행되어, SOXX / SOXQ / SMH 의
관심종목(WATCH) 비중·주식수 변화와 비중 변동 상위 종목을
텔레그램으로 전송합니다.

- 새로 기록된 데이터가 있을 때만 전송합니다 (주말/휴장일에는 조용히 넘어감)
- 필요 Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  (DART 모니터링 시스템에 쓰던 봇 토큰/챗ID를 그대로 재사용 가능)
"""

import json
import os
import sys

import requests

# ── 설정 ─────────────────────────────────────────────
WATCH = ["MU", "AMD", "SNDK", "INTC", "AMAT"]   # 알림에 상세 표시할 관심종목
TOP_MOVERS = 3                 # ETF별 비중 변동 상위 몇 개를 보여줄지
# ─────────────────────────────────────────────────────

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY_JSON = os.path.join(BASE, "data", "history.json")
STATE_JSON = os.path.join(BASE, "data", "notify_state.json")

ETFS = ["SOXX", "SOXQ", "SMH"]


def dashboard_url():
    repo = os.getenv("GITHUB_REPOSITORY", "")  # 예: "jay/etf-tracker"
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return ""


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def num(x):
    try:
        v = float(x)
        return None if v != v else v  # NaN 방지
    except (TypeError, ValueError):
        return None


def arrow(v):
    if v is None:
        return ""
    return "▲" if v > 0 else ("▼" if v < 0 else "―")


def fmt_bp(v):
    return "신규" if v is None else f"{arrow(v)}{abs(v):,.1f}bp"


def fmt_shares(v):
    return "" if v is None else f" ({arrow(v)}{abs(v):,.0f}주)"


def build_message(history):
    """ETF별 최신일 vs 직전 기록일 비교 메시지 생성. (메시지, 최신일 dict) 반환"""
    lines = ["📊 <b>반도체 ETF 수급 리포트</b>", ""]
    latest_dates = {}
    has_content = False

    for etf in ETFS:
        rows = [r for r in history if r["etf"] == etf]
        dates = sorted({r["date"] for r in rows})
        if not dates:
            continue
        d1 = dates[-1]
        d0 = dates[-2] if len(dates) >= 2 else None
        latest_dates[etf] = d1
        cur = {r["ticker"]: r for r in rows if r["date"] == d1}
        prv = {r["ticker"]: r for r in rows if r["date"] == d0} if d0 else {}

        lines.append(f"■ <b>{etf}</b>  (기준일 {d1})")

        # 관심종목
        for t in WATCH:
            c = cur.get(t)
            if not c:
                lines.append(f"  {t}: 미보유")
                continue
            w1, w0 = num(c.get("weight_pct")), num(prv.get(t, {}).get("weight_pct"))
            s1, s0 = num(c.get("shares")), num(prv.get(t, {}).get("shares"))
            dbp = (w1 - w0) * 100 if (w1 is not None and w0 is not None) else None
            dsh = (s1 - s0) if (s1 is not None and s0 is not None) else None
            lines.append(
                f"  <code>{t:<5}</code> {w1:,.2f}%  {fmt_bp(dbp)}{fmt_shares(dsh)}"
            )
            has_content = True

        # 비중 변동 상위 (관심종목 제외)
        movers = []
        for t, c in cur.items():
            if t in WATCH or t not in prv:
                continue
            w1, w0 = num(c.get("weight_pct")), num(prv[t].get("weight_pct"))
            if w1 is None or w0 is None:
                continue
            movers.append((t, (w1 - w0) * 100))
        movers.sort(key=lambda x: abs(x[1]), reverse=True)
        if movers[:TOP_MOVERS]:
            mv = " · ".join(f"{t} {fmt_bp(d)}" for t, d in movers[:TOP_MOVERS])
            lines.append(f"  <i>변동상위: {mv}</i>")
        lines.append("")

    url = dashboard_url()
    if url:
        lines.append(f'📈 <a href="{url}">대시보드 보기</a>')
    lines.append("<i>출처: iShares·Invesco·VanEck 공식 일별 holdings</i>")
    return "\n".join(lines), latest_dates, has_content


def send(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[오류] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID Secrets가 없습니다.")
        sys.exit(1)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    j = r.json()
    if not j.get("ok"):
        print("[오류] 텔레그램 전송 실패:", json.dumps(j, ensure_ascii=False)[:300])
        sys.exit(1)
    print("[성공] 텔레그램 전송 완료")


def main():
    payload = load(HISTORY_JSON, None)
    if payload is None:
        print("[안내] history.json 이 아직 없습니다. 수집이 먼저 실행되어야 합니다.")
        return
    history = payload if isinstance(payload, list) else payload.get("rows", payload)

    msg, latest_dates, has_content = build_message(history)
    if not has_content:
        print("[안내] 표시할 데이터가 없어 전송하지 않습니다.")
        return

    # 새 데이터가 있을 때만 전송 (마지막 알림 기준일과 비교)
    state = load(STATE_JSON, {})
    if state.get("last_notified_dates") == latest_dates:
        print("[안내] 새로 기록된 기준일이 없어 전송 생략 (주말/휴장일)")
        return

    send(msg)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump({"last_notified_dates": latest_dates}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
