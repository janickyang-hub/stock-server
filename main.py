from flask import Flask, jsonify, request
import requests
import os
from datetime import datetime, timedelta
import pytz
import time

app = Flask(__name__)

KIS_APP_KEY    = "PSSfEHBwUldWZhyEBOcNFh3ykQGqEIhFzJ8M"
KIS_APP_SECRET = "SN/IHkdyzDE3YPbmWBziziNQ1QIw1qJD4fskpKXAHIimpHyvGhpTcJrpkUwSEU5kI9N9vGwTQ4m5DSyZrY2YhMXymFAt9Itkdcv412cnyrom/6MAS0Q1vLjOyBP9EUKw/rpcjxpb4IaXnJ3YstxticaeATcI4Kg9S6sWG5dqc9//A3bYTKI="
KIS_BASE_URL   = "https://openapi.koreainvestment.com:9443"
KRX_AUTH_KEY   = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE       = "https://data-dbg.krx.co.kr/svc/apis"

_token_cache = {"access_token": None, "expires_at": None}

def get_kis_token() -> str:
    now = datetime.now()
    if (_token_cache["access_token"] and
        _token_cache["expires_at"] and
        now < _token_cache["expires_at"]):
        return _token_cache["access_token"]
    res  = requests.post(f"{KIS_BASE_URL}/oauth2/tokenP",
                         json={"grant_type": "client_credentials",
                               "appkey": KIS_APP_KEY,
                               "appsecret": KIS_APP_SECRET}, timeout=10)
    token = res.json().get("access_token", "")
    if token:
        _token_cache["access_token"] = token
        _token_cache["expires_at"]   = now + timedelta(hours=23)
    return token

def kis_headers(tr_id: str) -> dict:
    return {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {get_kis_token()}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P"
    }

def krx_post(endpoint: str, params: dict) -> list:
    url     = f"{KRX_BASE}/{endpoint}"
    headers = {"AUTH_KEY": KRX_AUTH_KEY,
               "Content-Type": "application/json; charset=UTF-8"}
    try:
        res = requests.post(url, headers=headers, json=params, timeout=20)
        if res.status_code != 200:
            return []
        return res.json().get("OutBlock_1", [])
    except Exception as e:
        print(f"[ERROR] KRX: {e}")
        return []

def latest_biz_day() -> str:
    tz   = pytz.timezone("Asia/Seoul")
    now  = datetime.now(tz)
    date = now - timedelta(days=1) if now.hour < 16 else now
    for _ in range(7):
        if date.weekday() < 5:
            break
        date -= timedelta(days=1)
    return date.strftime("%Y%m%d")

def safe_float(val) -> float:
    try:
        v = str(val).replace(",", "").strip()
        return float(v) if v and v not in ("-", "N/A", "") else 0.0
    except:
        return 0.0

def to_short_code(isu_cd: str) -> str:
    code = isu_cd.strip()
    if len(code) == 12 and code.startswith("KR"):
        return code[3:9]
    return code

def cap_size(mkt_cap: float) -> str:
    if mkt_cap >= 1_000_000_000_000: return "large"
    if mkt_cap >= 300_000_000_000:   return "mid"
    return "small"

# MARK: - KIS API: 종목 투자지표 조회
# FHKST01010100: 주식현재가 시세 → PER, PBR, EPS, BPS
# FHKST01010200: 주식현재가 기본시세 → 배당수익률(dvdy_rate), 주당배당금(divi)
def kis_get_indicators(stock_code: str) -> dict:
    base_url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations"
    params   = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}

    # 1. PER, PBR, EPS, BPS
    try:
        r1     = requests.get(f"{base_url}/inquire-price",
                              headers=kis_headers("FHKST01010100"),
                              params=params, timeout=10)
        o1     = r1.json().get("output", {})
        per    = safe_float(o1.get("per", 0))
        pbr    = safe_float(o1.get("pbr", 0))
        eps    = safe_float(o1.get("eps", 0))
        bps    = safe_float(o1.get("bps", 0))
    except:
        per = pbr = eps = bps = 0.0

    # 2. 배당수익률, 주당배당금 (기본시세 API)
    try:
        r2       = requests.get(f"{base_url}/inquire-daily-price",
                                headers=kis_headers("FHKST01010400"),
                                params={**params,
                                        "FID_PERIOD_DIV_CODE": "D",
                                        "FID_ORG_ADJ_PRC":     "0"}, timeout=10)
        o2       = r2.json().get("output2", [{}])
        # 배당 정보는 종목 기본 정보 API에서 가져오기
        r3       = requests.get(f"{base_url}/inquire-price",
                                headers=kis_headers("FHKST01010100"),
                                params=params, timeout=10)
        o3       = r3.json().get("output", {})
        # 기본시세에서 배당수익률 필드 탐색
        div_yield = safe_float(o3.get("dvdy_rate",  # 배당수익률
                               o3.get("bps_dvdy_rate",
                               o3.get("stck_dvdy_rate", 0))))
        dps       = safe_float(o3.get("divi", o3.get("per_sto_divi_amt", 0)))
    except:
        div_yield = dps = 0.0

    # 배당성향 = DPS / EPS * 100
    div_payout = round(dps / eps * 100, 2) if eps > 0 and dps > 0 else 0.0

    return {
        "per":       per,
        "pbr":       pbr,
        "eps":       eps,
        "bps":       bps,
        "div_yield": div_yield,
        "dps":       dps,
        "div_payout": div_payout
    }

# MARK: - 배당 필드 탐색용 디버그 엔드포인트
@app.route("/test_div")
def test_div():
    """삼성전자 전체 응답에서 배당 관련 필드 확인"""
    token = get_kis_token()
    if not token:
        return jsonify({"error": "토큰 발급 실패"})

    results = {}
    base_url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations"
    params   = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"}

    # 여러 TR_ID 시도해서 배당 필드 탐색
    for tr_id, path, extra in [
        ("FHKST01010100", "inquire-price",          {}),
        ("FHKST01010200", "inquire-member",          {}),
        ("FHKST11300006", "inquire-daily-itemchartprice",
         {"FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
          "FID_INPUT_DATE_1": "20260101", "FID_INPUT_DATE_2": "20260317"}),
    ]:
        try:
            r = requests.get(f"{base_url}/{path}",
                             headers=kis_headers(tr_id),
                             params={**params, **extra}, timeout=10)
            data   = r.json()
            output = data.get("output", data.get("output1", {}))
            if isinstance(output, list):
                output = output[0] if output else {}
            # 배당 관련 키 필터링
            div_keys = {k: v for k, v in output.items()
                        if any(kw in k.lower()
                               for kw in ["div", "divi", "dvdy", "yield", "dps"])}
            results[tr_id] = {
                "div_keys":  div_keys,
                "all_keys":  list(output.keys()),
                "rt_cd":     data.get("rt_cd"),
                "msg":       data.get("msg1", "")
            }
        except Exception as e:
            results[tr_id] = {"error": str(e)}

    return jsonify(results)

@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()
        print(f"[DEBUG] 기준일: {base_date}")

        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[DEBUG] KRX KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX 시세 API 응답 없음", "date": base_date}), 500

        all_items = []
        for item in kospi + kosdaq:
            isu_cd  = item.get("ISU_CD", "")
            name    = item.get("ISU_NM", "").strip()
            if not isu_cd or not name:
                continue
            short_cd = to_short_code(isu_cd)
            price    = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap  = safe_float(item.get("MKTCAP", 0))
            change   = safe_float(item.get("FLUC_RT", 0))
            all_items.append({
                "id": short_cd, "name": name,
                "price": price, "change": change,
                "marketCap": mkt_cap, "capSize": cap_size(mkt_cap),
            })

        all_items.sort(key=lambda x: x["marketCap"], reverse=True)
        top_items = all_items[:2000]

        result = []
        for i, item in enumerate(top_items):
            ind = kis_get_indicators(item["id"])
            result.append({
                "id":        item["id"],
                "name":      item["name"],
                "price":     item["price"],
                "change":    item["change"],
                "marketCap": item["marketCap"],
                "capSize":   item["capSize"],
                "per":       ind["per"],
                "pbr":       ind["pbr"],
                "divYield":  ind["div_yield"],
                "divAmount": int(ind["dps"]),
                "divPayout": ind["div_payout"]
            })
            if (i + 1) % 18 == 0:
                time.sleep(1)

        print(f"[DEBUG] 최종: {len(result)}개")
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
