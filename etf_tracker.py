# -*- coding: utf-8 -*-
"""
ETF Holdings Tracker
--------------------
SOXX (iShares) / SOXQ (Invesco) / SMH (VanEck)의 공식 보유종목(holdings)
파일을 매일 내려받아, 종목별 비중(%)과 보유주식수를 시계열로 기록합니다.

결과 파일:
  data/history.csv   : 전체 시계열 (엑셀로도 열 수 있음)
  data/history.json  : 대시보드(index.html)가 읽는 파일
  data/latest.json   : 가장 최근 스냅샷

GitHub Actions가 이 스크립트를 매일 자동 실행합니다. (사람이 직접 실행할 필요 없음)
"""

import csv
import io
import json
import os
import re
import sys
import datetime

import requests
import pandas as pd

# ---------------------------------------------------------------
# 설정
# ---------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")
HISTORY_JSON = os.path.join(DATA_DIR, "history.json")
LATEST_JSON = os.path.join(DATA_DIR, "latest.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "text/csv,application/csv,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
}

# 운용사 공식 다운로드 주소 (1순위) + 예비 주소 (2순위: stockanalysis.com)
ISHARES_SOXX_CSV = (
    "https://www.ishares.com/us/products/239705/"
    "ishares-phlx-semiconductor-etf/1467271812596.ajax"
    "?fileType=csv&fileName=SOXX_holdings&dataType=fund"
)
INVESCO_SOXQ_CSV = (
    "https://www.invesco.com/us/financial-products/etfs/holdings/main/"
    "sitedetail/0?audienceType=Investor&action=download&ticker=SOXQ"
)
VANECK_SMH_PAGE = "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/holdings/"
BACKUP_URL = "https://stockanalysis.com/etf/{t}/holdings/"


def _get(url, timeout=60, referer=None):
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
        h["Sec-Fetch-Site"] = "same-origin"
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def _to_float(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("%", "").replace("$", "").strip()
    if s in ("", "-", "N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------
# 1) SOXX — iShares 공식 CSV
#    (iShares가 CSV 주소를 종종 바꾸므로, 제품 페이지에서
#     현재 유효한 CSV 링크를 매번 자동으로 찾아낸다)
# ---------------------------------------------------------------
ISHARES_PAGE = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf"


def _discover_ishares_csv():
    import html as _html
    page = _get(ISHARES_PAGE).text
    m = re.search(
        r'["\'](/us/products/239705/[^"\']*?\.ajax\?fileType=csv[^"\']*)["\']', page)
    if m:
        return "https://www.ishares.com" + _html.unescape(m.group(1))
    return None


def fetch_soxx():
    url = None
    try:
        url = _discover_ishares_csv()
    except Exception as e:
        print(f"[안내] iShares 페이지에서 CSV 링크 탐색 실패({e}) → 기본 주소 사용")
    text = _get(url or ISHARES_SOXX_CSV).text
    if "Ticker," not in text and url is None:
        raise ValueError("iShares CSV에서 holdings 표를 찾지 못함 (링크 탐색도 실패)")
    lines = text.splitlines()

    # 파일 상단 메타데이터에서 기준일 추출: 예) Fund Holdings as of,"Jul 06, 2026"
    as_of = None
    for ln in lines[:15]:
        if "holdings as of" in ln.lower():
            m = re.search(r'"?([A-Z][a-z]{2} \d{1,2}, \d{4})"?', ln)
            if m:
                as_of = datetime.datetime.strptime(m.group(1), "%b %d, %Y").date()
            break

    # 실제 표가 시작되는 줄(Ticker,Name,...) 찾기
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("Ticker,"):
            start = i
            break
    if start is None:
        raise ValueError("iShares CSV에서 holdings 표를 찾지 못함")

    df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "ticker": str(r["Ticker"]).strip(),
            "name": str(r["Name"]).strip(),
            "weight_pct": _to_float(r.get("Weight (%)")),
            "shares": _to_float(r.get("Quantity")),
            "market_value": _to_float(r.get("Market Value")),
        })
    return as_of, rows, "iShares (blackrock/ishares.com 공식 CSV)"


# ---------------------------------------------------------------
# 2) SOXQ — Invesco 공식 CSV
# ---------------------------------------------------------------
def fetch_soxq():
    text = _get(
        INVESCO_SOXQ_CSV,
        referer="https://www.invesco.com/us/financial-products/etfs/product-detail"
                "?audienceType=Investor&ticker=SOXQ",
    ).text
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]

    def col(*cands):
        # 후보 순서를 우선시: 'Holding Ticker'가 'Fund Ticker'보다 먼저 매칭되도록
        for k in cands:
            for c in df.columns:
                if k.lower() in c.lower():
                    return c
        return None

    c_tkr = col("Holding Ticker", "Ticker")
    c_name = col("Name", "Security Name")
    c_wt = col("Weight", "% TNA", "PercentageOfFund")
    c_sh = col("Shares/Par", "Shares", "Par Value")
    c_mv = col("MarketValue", "Market Value")
    c_dt = col("Date")

    as_of = None
    if c_dt is not None and len(df) > 0:
        raw = str(df[c_dt].iloc[0]).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y"):
            try:
                as_of = datetime.datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue

    rows = []
    for _, r in df.iterrows():
        tkr = str(r.get(c_tkr, "")).strip()
        if not tkr or tkr.lower() == "nan":
            continue
        rows.append({
            "ticker": tkr,
            "name": str(r.get(c_name, "")).strip(),
            "weight_pct": _to_float(r.get(c_wt)),
            "shares": _to_float(r.get(c_sh)),
            "market_value": _to_float(r.get(c_mv)),
        })
    return as_of, rows, "Invesco (invesco.com 공식 CSV)"


# ---------------------------------------------------------------
# 3) SMH — VanEck 페이지에 내장된 JSON 데이터에서 추출
#    (페이지가 자바스크립트 렌더링이라 HTML 표가 없고,
#     대신 페이지 소스 안 JSON 블록에 전체 보유 데이터가 들어 있음)
# ---------------------------------------------------------------
def _hunt_holdings_in_json(obj, depth=0):
    """중첩 JSON 안에서 '보유종목 리스트'처럼 생긴 구조를 재귀 탐색"""
    if depth > 12:
        return None
    if isinstance(obj, list) and len(obj) >= 10 and all(isinstance(x, dict) for x in obj[:5]):
        keys = set().union(*(set(x.keys()) for x in obj[:5]))
        kl = {k.lower() for k in keys}
        has_tkr = any(("ticker" in k or "symbol" in k) for k in kl)
        has_wt = any(("weight" in k or "netassets" in k or "percent" in k) for k in kl)
        if has_tkr and has_wt:
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _hunt_holdings_in_json(v, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _hunt_holdings_in_json(v, depth + 1)
            if r is not None:
                return r
    return None


def _key_like(d, *cands):
    for k in d.keys():
        for c in cands:
            if c in k.lower():
                return k
    return None


def fetch_smh():
    html = _get(VANECK_SMH_PAGE).text

    # 1차: 페이지에 내장된 JSON (__NEXT_DATA__ 등 스크립트 블록)
    for m in re.finditer(r'<script[^>]*>\s*({.*?})\s*</script>', html, re.DOTALL):
        blob = m.group(1)
        if '"ticker"' not in blob.lower() and '"symbol"' not in blob.lower():
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        found = _hunt_holdings_in_json(data)
        if found:
            sample = found[0]
            k_tkr = _key_like(sample, "ticker", "symbol")
            k_name = _key_like(sample, "name", "holding", "security")
            k_wt = _key_like(sample, "weight", "netassets", "percent")
            k_sh = _key_like(sample, "shares", "quantity")
            k_mv = _key_like(sample, "marketvalue", "market_value")
            rows = []
            for rec in found:
                tkr = str(rec.get(k_tkr, "")).strip()
                if not tkr:
                    continue
                rows.append({
                    "ticker": tkr,
                    "name": str(rec.get(k_name, "")).strip() if k_name else "",
                    "weight_pct": _to_float(rec.get(k_wt)),
                    "shares": _to_float(rec.get(k_sh)) if k_sh else None,
                    "market_value": _to_float(rec.get(k_mv)) if k_mv else None,
                })
            if rows:
                return None, rows, "VanEck (vaneck.com 공식 페이지 내장 데이터)"

    # 2차: 혹시 서버 렌더링된 HTML 표가 있는 경우
    tables = pd.read_html(io.StringIO(html))
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        if any("ticker" in c or "symbol" in c for c in cols) and any("weight" in c or "%" in c for c in cols):
            df.columns = [str(c) for c in df.columns]

            def col(*cands):
                for k in cands:
                    for c in df.columns:
                        if k.lower() in c.lower():
                            return c
                return None

            c_tkr = col("ticker", "symbol")
            c_name = col("name", "holding")
            c_wt = col("weight", "% of net", "%")
            c_sh = col("shares", "quantity")
            c_mv = col("market value")

            rows = []
            for _, r in df.iterrows():
                tkr = str(r.get(c_tkr, "")).strip()
                if not tkr or tkr.lower() == "nan":
                    continue
                rows.append({
                    "ticker": tkr,
                    "name": str(r.get(c_name, "")).strip() if c_name else "",
                    "weight_pct": _to_float(r.get(c_wt)),
                    "shares": _to_float(r.get(c_sh)) if c_sh else None,
                    "market_value": _to_float(r.get(c_mv)) if c_mv else None,
                })
            if rows:
                return None, rows, "VanEck (vaneck.com 공식 페이지)"
    raise ValueError("VanEck 페이지에서 holdings 표를 찾지 못함")


# ---------------------------------------------------------------
# 백업: stockanalysis.com (운용사 EOD 데이터를 재게시하는 애그리게이터)
# ---------------------------------------------------------------
def fetch_backup(etf):
    html = _get(BACKUP_URL.format(t=etf.lower())).text
    tables = pd.read_html(io.StringIO(html))
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        if any("symbol" in c for c in cols) and any("weight" in c or "%" in c for c in cols):
            df.columns = [str(c) for c in df.columns]

            def col(*cands):
                for k in cands:
                    for c in df.columns:
                        if k.lower() in c.lower():
                            return c
                return None

            c_tkr = col("symbol")
            c_name = col("name")
            c_wt = col("weight", "%")
            c_sh = col("shares")

            rows = []
            for _, r in df.iterrows():
                tkr = str(r.get(c_tkr, "")).strip()
                if not tkr or tkr.lower() in ("nan", ""):
                    continue
                rows.append({
                    "ticker": tkr,
                    "name": str(r.get(c_name, "")).strip() if c_name else "",
                    "weight_pct": _to_float(r.get(c_wt)),
                    "shares": _to_float(r.get(c_sh)) if c_sh else None,
                    "market_value": None,
                })
            if rows:
                return None, rows, "stockanalysis.com (백업 소스, 운용사 EOD 데이터 재게시)"
    raise ValueError("백업 소스에서도 표를 찾지 못함")


# ---------------------------------------------------------------
# 기록/저장
# ---------------------------------------------------------------
FIELDS = ["date", "etf", "ticker", "name", "weight_pct", "shares",
          "market_value", "source", "fetched_at_utc"]


def load_history():
    if not os.path.exists(HISTORY_CSV):
        return []
    with open(HISTORY_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    history = load_history()
    existing_keys = {(h["etf"], h["date"]) for h in history}
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    today = datetime.date.today()

    fetchers = {"SOXX": fetch_soxx, "SOXQ": fetch_soxq, "SMH": fetch_smh}
    errors = []

    for etf, fn in fetchers.items():
        as_of, rows, source = None, None, None
        try:
            as_of, rows, source = fn()
        except Exception as e:
            print(f"[경고] {etf} 공식 소스 실패: {e} → 백업 소스 시도")
            try:
                as_of, rows, source = fetch_backup(etf)
            except Exception as e2:
                errors.append(f"{etf}: 공식({e}) / 백업({e2}) 모두 실패")
                continue

        # 기준일이 파일에 없으면 실행일 기준(전일 종가 데이터라 하루 차이가 날 수 있음)
        date_str = (as_of or today).isoformat()

        if (etf, date_str) in existing_keys:
            print(f"[안내] {etf} {date_str} 데이터는 이미 기록되어 있어 건너뜀 (주말/휴장일)")
            continue

        for r in rows:
            history.append({
                "date": date_str, "etf": etf,
                "ticker": r["ticker"], "name": r["name"],
                "weight_pct": r["weight_pct"], "shares": r["shares"],
                "market_value": r["market_value"],
                "source": source, "fetched_at_utc": fetched_at,
            })
        print(f"[성공] {etf} {date_str} — {len(rows)}개 종목 기록 (출처: {source})")

    # 저장 (CSV)
    history.sort(key=lambda h: (h["date"], h["etf"], str(h["ticker"])))
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for h in history:
            w.writerow({k: h.get(k, "") for k in FIELDS})

    # 저장 (대시보드용 JSON)
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

    # 최신 스냅샷
    latest = {}
    for h in history:
        etf = h["etf"]
        if etf not in latest or h["date"] > latest[etf]["date"]:
            latest[etf] = {"date": h["date"], "rows": []}
    for h in history:
        etf = h["etf"]
        if h["date"] == latest[etf]["date"]:
            latest[etf]["rows"].append(h)
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_utc": fetched_at, "etfs": latest}, f, ensure_ascii=False)

    if errors:
        print("\n".join("[오류] " + e for e in errors))
        # 세 개 전부 실패했을 때만 워크플로를 실패 처리
        if len(errors) == len(fetchers):
            sys.exit(1)

    print("완료.")


if __name__ == "__main__":
    main()
