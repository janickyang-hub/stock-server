from flask import Flask, jsonify
import requests
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# KRX Open API 인증키 - 발급받은 키로 교체하세요
KRX_AUTH_KEY = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"

BASE_URL = "https://openapi.krx.co.kr/contents/OPP/USES/service"

HEADERS = {
    "AUTH_KEY": KRX_AUTH_KEY,
    "Content-Type": "application/json; charset=UTF-8"
}

def latest_biz_day():
    tz   = pytz.timezone("Asia/Seoul")
    now  = datetime.now(tz)
    date = now - timedelta(days=1) if now.hour < 16 else now
    for _ in range(7):
        if date.weekday() < 5:
            break
        date -= timedelta(days=1)
    return date.strftime("%Y%m%d")

def safe_float(val):
    try:
        return float(str(val).replace(",", "").strip() or "0")
    except:
        return 0.0

@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()

        # 1. 유가증권(KOSPI) 일별매매정보
        kospi  = fetch_stock_data("OPPUSES002_S3", base_date, "STK")
        # 2. 코스닥(KOSDAQ) 일별매매정보
        kosdaq = fetch_stock_data("OPPUSES002_S3", base_date, "KSQ")
        # 3. 전종목 투자지표 (PER, PBR, EPS, BPS, 배당수익률)
        indicators = fetch_indicator_data("OPPUSES002_S3", base_date)

        # 투자지표를 종목코드 딕셔너리로 변환
        ind_map = {item.get("ISU_SRT_CD", "").strip(): item for item in indicators}

        result = []
        for item in kospi + kosdaq:
            code = item.get("ISU_SRT_CD", "").strip()
            if not code:
                continue

            ind  = ind_map.get(code, {})
            eps  = safe_float(ind.get("EPS", 0))
            div  = safe_float(ind.get("DPS", 0))       # 주당배당금
            div_yield = safe_float(ind.get("DVD_YLD", 0))  # 배당수익률

            price     = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap   = safe_float(item.get("MKTCAP", 0))

            result.append({
                "id":        code,
                "name":      item.get("ISU_ABBRV", "").strip(),
                "price":     price,
                "change":    safe_float(item.get("FLUC_RT", 0)),
                "marketCap": mkt_cap,
                "per":       safe_float(ind.get("PER", 0)),
                "pbr":       safe_float(ind.get("PBR", 0)),
                "divYield":  div_yield,
                "divAmount": int(div),
                "divPayout": round(div / eps * 100, 2) if eps > 0 else 0
            })

        # 시가총액 기준 상위 2000개
        result.sort(key=lambda x: x["marketCap"], reverse=True)
        return jsonify(result[:2000])

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def fetch_stock_data(endpoint: str, base_date: str, market: str) -> list:
    """유가증권/코스닥 일별매매정보 조회"""
    url = f"{BASE_URL}/{endpoint}.cmd"
    params = {
        "bld":     "dbms/MDC/STAT/standard/MDCSTAT01501",
        "mktId":   market,
        "trdDd":   base_date,
        "share":   "1",
        "money":   "1",
    }
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=15)
        data = res.json()
        return data.get("output", data.get("OutBlock_1", []))
    except:
        return []

def fetch_indicator_data(endpoint: str, base_date: str) -> list:
    """전종목 투자지표(PER/PBR/배당) 조회"""
    url = f"{BASE_URL}/{endpoint}.cmd"
    params = {
        "bld":   "dbms/MDC/STAT/standard/MDCSTAT03501",
        "mktId": "ALL",
        "trdDd": base_date,
        "money": "1",
    }
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=15)
        data = res.json()
        return data.get("output", data.get("OutBlock_1", []))
    except:
        return []

if __name__ == "__main__":
   import os
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
