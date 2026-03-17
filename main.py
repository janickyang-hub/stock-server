from flask import Flask, jsonify
import requests
import os
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

KRX_AUTH_KEY = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"

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

def krx_get(api_id: str, params: dict) -> list:
    """KRX Open API 호출 - AUTH_KEY는 헤더로 전달"""
    url = "https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S3.cmd"
    headers = {
        "AUTH_KEY": KRX_AUTH_KEY,
    }
    payload = {"bld": api_id, **params}
    try:
        res  = requests.post(url, headers=headers, data=payload, timeout=20)
        print(f"[DEBUG] {api_id} status={res.status_code} body={res.text[:300]}")
        data = res.json()
        # KRX 응답은 OutBlock_1 또는 output 키에 배열이 들어있음
        for key in ("OutBlock_1", "output", "data", "result"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []
    except Exception as e:
        print(f"[ERROR] krx_get {api_id}: {e}")
        return []

@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()
        print(f"[DEBUG] 기준일: {base_date}")

        # KOSPI 일별매매정보
        kospi = krx_get("dbms/MDC/STAT/standard/MDCSTAT01501", {
            "mktId": "STK", "trdDd": base_date, "share": "1", "money": "1"
        })
        print(f"[DEBUG] KOSPI: {len(kospi)}개")

        # KOSDAQ 일별매매정보
        kosdaq = krx_get("dbms/MDC/STAT/standard/MDCSTAT01501", {
            "mktId": "KSQ", "trdDd": base_date, "share": "1", "money": "1"
        })
        print(f"[DEBUG] KOSDAQ: {len(kosdaq)}개")

        # 전종목 투자지표 (PER, PBR, EPS, BPS, 배당수익률)
        indicators = krx_get("dbms/MDC/STAT/standard/MDCSTAT03501", {
            "mktId": "ALL", "trdDd": base_date, "money": "1"
        })
        print(f"[DEBUG] 투자지표: {len(indicators)}개")

        if not kospi and not kosdaq:
            # 응답 키 확인용 원본 출력
            url  = "https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S3.cmd"
            hdrs = {"AUTH_KEY": KRX_AUTH_KEY}
            test = requests.post(url, headers=hdrs,
                                 data={"bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
                                       "mktId": "STK", "trdDd": base_date,
                                       "share": "1", "money": "1"}, timeout=20)
            return jsonify({
                "error": "KRX API 응답 없음",
                "date":  base_date,
                "status": test.status_code,
                "raw":   test.text[:500]   # 실제 응답 앞부분 확인용
            }), 500

        ind_map = {}
        for item in indicators:
            code = (item.get("ISU_SRT_CD") or item.get("shortCode") or "").strip()
            if code:
                ind_map[code] = item

        result = []
        for item in kospi + kosdaq:
            code = (item.get("ISU_SRT_CD") or item.get("shortCode") or "").strip()
            if not code:
                continue
            ind       = ind_map.get(code, {})
            eps       = safe_float(ind.get("EPS") or ind.get("eps") or 0)
            div       = safe_float(ind.get("DPS") or ind.get("dps") or 0)
            div_yield = safe_float(ind.get("DVD_YLD") or ind.get("dvdYld") or 0)
            price     = safe_float(item.get("TDD_CLSPRC") or item.get("closePrice") or 0)
            mkt_cap   = safe_float(item.get("MKTCAP") or item.get("marketCap") or 0)
            change    = safe_float(item.get("FLUC_RT") or item.get("fluctRt") or 0)
            name      = (item.get("ISU_ABBRV") or item.get("itemName") or "").strip()

            result.append({
                "id":        code,
                "name":      name,
                "price":     price,
                "change":    change,
                "marketCap": mkt_cap,
                "per":       safe_float(ind.get("PER") or ind.get("per") or 0),
                "pbr":       safe_float(ind.get("PBR") or ind.get("pbr") or 0),
                "divYield":  div_yield,
                "divAmount": int(div),
                "divPayout": round(div / eps * 100, 2) if eps > 0 else 0
            })

        result.sort(key=lambda x: x["marketCap"], reverse=True)
        print(f"[DEBUG] 최종: {len(result)}개")
        return jsonify(result[:2000])

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ERROR] {tb}")
        return jsonify({"error": str(e), "trace": tb}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    try:
        base_date = latest_biz_day()
        print(f"[DEBUG] 기준일: {base_date}")

        kospi = fetch_stock_data("OPPUSES002_S3", base_date, "STK")
        print(f"[DEBUG] KOSPI 종목수: {len(kospi)}")

        kosdaq = fetch_stock_data("OPPUSES002_S3", base_date, "KSQ")
        print(f"[DEBUG] KOSDAQ 종목수: {len(kosdaq)}")

        indicators = fetch_indicator_data("OPPUSES002_S3", base_date)
        print(f"[DEBUG] 투자지표 종목수: {len(indicators)}")

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX API 응답 없음", "date": base_date}), 500

        ind_map = {item.get("ISU_SRT_CD", "").strip(): item for item in indicators}

        result = []
        for item in kospi + kosdaq:
            code = item.get("ISU_SRT_CD", "").strip()
            if not code:
                continue
            ind       = ind_map.get(code, {})
            eps       = safe_float(ind.get("EPS", 0))
            div       = safe_float(ind.get("DPS", 0))
            div_yield = safe_float(ind.get("DVD_YLD", 0))
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

        print(f"[DEBUG] 최종 종목수: {len(result)}")
        result.sort(key=lambda x: x["marketCap"], reverse=True)
        return jsonify(result[:2000])

    except Exception as e:
        import traceback
        print(f"[ERROR] {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

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
