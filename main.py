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
    url  = f"{KIS_BASE_URL}/oauth2/tokenP"
    body = {"grant_type": "client_credentials",
            "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET}
    res  = requests.post(url, json=body, timeout=10)
    data = res.json()
    token = data.get("access_token", "")
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
        print(f"[ERROR] KRX {endpoint}: {e}")
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

# MARK: - KIS 주식현재가 시세 (PER, PBR, 배당수익률)
def kis_get_stock_detail(stock_code: str) -> dict:
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code
    }
    try:
        res    = requests.get(url, headers=kis_headers("FHKST01010100"),
                              params=params, timeout=10)
        output = res.json().get("output", {})
        return {
            "per":       safe_float(output.get("per",       0)),
            "pbr":       safe_float(output.get("pbr",       0)),
            "eps":       safe_float(output.get("eps",       0)),
            "bps":       safe_float(output.get("bps",       0)),
            "div_yield": safe_float(output.get("dvdy_rate", 0)),  # 배당수익률
            "dps":       safe_float(output.get("divi",      0)),  # 주당배당금
        }
    except Exception as e:
        print(f"[ERROR] KIS {stock_code}: {e}")
        return {"per": 0, "pbr": 0, "eps": 0, "bps": 0, "div_yield": 0, "dps": 0}

# MARK: - 전체 응답 필드 확인용
@app.route("/test_kis")
def test_kis():
    try:
        token = get_kis_token()
        if not token:
            return jsonify({"error": "토큰 발급 실패"})

        # 삼성전자 전체 응답 필드 확인
        url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"}
        res    = requests.get(url, headers=kis_headers("FHKST01010100"),
                              params=params, timeout=10)
        data   = res.json()
        output = data.get("output", {})

        # 배당/PER/PBR 관련 필드만 필터링
        finance_fields = {k: v for k, v in output.items()
                          if any(kw in k.lower() for kw in
                                 ["per", "pbr", "eps", "bps", "div", "divi",
                                  "dvdy", "yield", "rate", "stac"])}

        return jsonify({
            "token_ok":       True,
            "finance_fields": finance_fields,   # 재무 관련 필드 전체
            "all_keys":       list(output.keys())  # 전체 키 목록
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

# MARK: - 메인 엔드포인트
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
            isu_cd   = item.get("ISU_CD", "")
            name     = item.get("ISU_NM", "").strip()
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
            detail     = kis_get_stock_detail(item["id"])
            eps        = detail.get("eps", 0)
            dps        = detail.get("dps", 0)
            div_yield  = detail.get("div_yield", 0)
            div_payout = round(dps / eps * 100, 2) if eps > 0 else 0.0

            result.append({
                "id":        item["id"],
                "name":      item["name"],
                "price":     item["price"],
                "change":    item["change"],
                "marketCap": item["marketCap"],
                "capSize":   item["capSize"],
                "per":       detail.get("per", 0),
                "pbr":       detail.get("pbr", 0),
                "divYield":  div_yield,
                "divAmount": int(dps),
                "divPayout": div_payout
            })

            # KIS API 초당 20회 제한 방지
            if (i + 1) % 18 == 0:
                time.sleep(1)

        print(f"[DEBUG] 최종: {len(result)}개")
        return jsonify(result)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ERROR] {tb}")
        return jsonify({"error": str(e), "trace": tb}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
