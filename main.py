from flask import Flask, jsonify, request
import requests
import os
from datetime import datetime, timedelta
import pytz
import json

app = Flask(__name__)

# =============================================
# 설정값 - 여기에 발급받은 키를 입력하세요
# =============================================
KIS_APP_KEY    = "PSSfEHBwUldWZhyEBOcNFh3ykQGqEIhFzJ8M"
KIS_APP_SECRET = "SN/IHkdyzDE3YPbmWBziziNQ1QIw1qJD4fskpKXAHIimpHyvGhpTcJrpkUwSEU5kI9N9vGwTQ4m5DSyZrY2YhMXymFAt9Itkdcv412cnyrom/6MAS0Q1vLjOyBP9EUKw/rpcjxpb4IaXnJ3YstxticaeATcI4Kg9S6sWG5dqc9//A3bYTKI="
KIS_BASE_URL   = "https://openapi.koreainvestment.com:9443"

# KRX Open API 인증키 (기존 시세용)
KRX_AUTH_KEY = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE     = "https://data-dbg.krx.co.kr/svc/apis"

# 토큰 캐시 (서버 메모리에 저장, 재시작 시 재발급)
_token_cache = {"access_token": None, "expires_at": None}

# =============================================
# KIS 접근토큰 발급 및 캐시
# =============================================
def get_kis_token() -> str:
    """KIS 접근토큰 발급 (24시간 유효, 캐시 사용)"""
    now = datetime.now()
    if (_token_cache["access_token"] and
        _token_cache["expires_at"] and
        now < _token_cache["expires_at"]):
        return _token_cache["access_token"]

    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     KIS_APP_KEY,
        "appsecret":  KIS_APP_SECRET
    }
    res = requests.post(url, json=body, timeout=10)
    data = res.json()
    token = data.get("access_token", "")
    if token:
        _token_cache["access_token"] = token
        _token_cache["expires_at"]   = now + timedelta(hours=23)
        print(f"[DEBUG] KIS 토큰 발급 성공")
    else:
        print(f"[ERROR] KIS 토큰 발급 실패: {data}")
    return token

# =============================================
# KIS API 공통 헤더
# =============================================
def kis_headers(tr_id: str) -> dict:
    return {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {get_kis_token()}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P"
    }

# =============================================
# KRX Open API (시세 + 시가총액)
# =============================================
def krx_post(endpoint: str, params: dict) -> list:
    url     = f"{KRX_BASE}/{endpoint}"
    headers = {
        "AUTH_KEY":     KRX_AUTH_KEY,
        "Content-Type": "application/json; charset=UTF-8"
    }
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

# =============================================
# KIS: 종목 현재가 시세 조회 (PER, PBR, 배당 포함)
# FHKST01010100 - 주식현재가 시세
# =============================================
def kis_get_stock_detail(stock_code: str) -> dict:
    """KIS API로 단일 종목 PER/PBR/배당수익률 조회"""
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code
    }
    try:
        res = requests.get(url,
                           headers=kis_headers("FHKST01010100"),
                           params=params,
                           timeout=10)
        data = res.json()
        output = data.get("output", {})
        return {
            "per":      safe_float(output.get("per",      0)),
            "pbr":      safe_float(output.get("pbr",      0)),
            "div_yield": safe_float(output.get("dvdy_rate", 0)),  # 배당수익률
            "dps":      safe_float(output.get("divi",      0)),   # 주당배당금
        }
    except Exception as e:
        print(f"[ERROR] KIS 종목 상세 {stock_code}: {e}")
        return {"per": 0, "pbr": 0, "div_yield": 0, "dps": 0}

# =============================================
# KIS: 업종별 전종목 시세 조회
# FHPST01710000 - 업종별 주가 조회
# =============================================
def kis_get_market_data(market_code: str) -> list:
    """KIS API로 시장 전종목 기본 데이터 조회"""
    # market_code: "0001" = KOSPI, "1001" = KOSDAQ
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    results = []

    # KIS는 전종목 일괄 조회 API가 제한적이므로
    # KRX에서 종목 리스트를 가져온 후 KIS에서 투자지표를 보완하는 방식 사용
    return results

# =============================================
# 메인 엔드포인트
# =============================================
@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()
        print(f"[DEBUG] 기준일: {base_date}")

        # 1. KRX에서 전종목 시세 (종가, 시가총액, 등락률)
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[DEBUG] KRX KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX 시세 API 응답 없음", "date": base_date}), 500

        # 2. 시가총액 기준 상위 종목 선별 (KIS API 호출 수 제한)
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
                "id":        short_cd,
                "name":      name,
                "price":     price,
                "change":    change,
                "marketCap": mkt_cap,
                "capSize":   cap_size(mkt_cap),
            })

        # 시가총액 순 정렬 후 상위 2000개
        all_items.sort(key=lambda x: x["marketCap"], reverse=True)
        top_items = all_items[:2000]

        # 3. KIS API로 PER/PBR/배당수익률 조회
        # KIS API는 초당 20회 제한이 있으므로 배치로 처리
        import time
        result = []
        for i, item in enumerate(top_items):
            detail = kis_get_stock_detail(item["id"])

            eps = detail.get("dps", 0)
            dps = detail.get("dps", 0)
            div_yield = detail.get("div_yield", 0)
            per = detail.get("per", 0)
            pbr = detail.get("pbr", 0)

            result.append({
                "id":        item["id"],
                "name":      item["name"],
                "price":     item["price"],
                "change":    item["change"],
                "marketCap": item["marketCap"],
                "capSize":   item["capSize"],
                "per":       per,
                "pbr":       pbr,
                "divYield":  div_yield,
                "divAmount": int(dps),
                "divPayout": 0  # KIS API에서 배당성향은 별도 계산 필요
            })

            # KIS API 호출 제한 방지 (초당 20회)
            if (i + 1) % 18 == 0:
                time.sleep(1)

        print(f"[DEBUG] 최종: {len(result)}개")
        return jsonify(result)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ERROR] {tb}")
        return jsonify({"error": str(e), "trace": tb}), 500

# =============================================
# KIS 토큰 테스트
# =============================================
@app.route("/test_kis")
def test_kis():
    try:
        token = get_kis_token()
        if not token:
            return jsonify({"error": "토큰 발급 실패 - App Key/Secret 확인 필요"})

        # 삼성전자(005930) 테스트 조회
        detail = kis_get_stock_detail("005930")
        return jsonify({
            "token_ok":   True,
            "token_preview": token[:20] + "...",
            "samsung_test": detail
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
