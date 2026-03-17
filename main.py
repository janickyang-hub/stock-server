from flask import Flask, jsonify
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
# 공공데이터포털 서비스키 (배당 데이터용)
FSC_SERVICE_KEY = "e0a1fb6fedf17f785d6b35276663fb0f47bb199d21038d494ea05b2250596a30"

_token_cache = {"access_token": None, "expires_at": None}

# =============================================
# KIS 토큰 발급
# =============================================
def get_kis_token() -> str:
    now = datetime.now()
    if (_token_cache["access_token"] and
        _token_cache["expires_at"] and
        now < _token_cache["expires_at"]):
        return _token_cache["access_token"]
    res   = requests.post(f"{KIS_BASE_URL}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": KIS_APP_KEY,
                                "appsecret": KIS_APP_SECRET}, timeout=10)
    token = res.json().get("access_token", "")
    if token:
        _token_cache["access_token"] = token
        _token_cache["expires_at"]   = now + timedelta(hours=23)
        print("[DEBUG] KIS 토큰 발급 성공")
    else:
        print(f"[ERROR] KIS 토큰 발급 실패: {res.text[:200]}")
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

# =============================================
# KRX Open API (시세 + 시가총액)
# =============================================
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

# =============================================
# 공공데이터포털 배당 API
# 전종목 배당정보를 연도별로 가져옴
# =============================================
def fetch_dividend_map(year: str) -> dict:
    """종목코드 → 배당정보 딕셔너리 반환"""
    div_map = {}
    try:
        url = "https://apis.data.go.kr/1160100/service/GetStocDiviInfoService/getDiviInfo"
        page = 1
        while True:
            params = {
                "serviceKey":  FSC_SERVICE_KEY,
                "numOfRows":   "1000",
                "pageNo":      str(page),
                "resultType":  "json",
                "basDt":       year,
            }
            res  = requests.get(url, params=params, timeout=20)
            data = res.json()
            items = (data.get("response", {})
                         .get("body", {})
                         .get("items", {})
                         .get("item", []))
            if not items:
                break
            for item in items:
                # 보통주만 (scrsItmsKcd: 0101)
                if item.get("scrsItmsKcd", "") != "0101":
                    continue
                isin = item.get("isinCd", "")
                if len(isin) == 12 and isin.startswith("KR"):
                    code = isin[3:9]  # ISIN → 단축코드
                    div_amt = safe_float(item.get("stckGenrDvdnAmt", 0))
                    div_rt  = safe_float(item.get("stckGenrCashDvdnRt", 0))
                    if div_amt > 0:
                        div_map[code] = {
                            "divAmount": int(div_amt),
                            "divRate":   div_rt,  # 배당률(%)
                        }
            total = int(data.get("response", {})
                            .get("body", {})
                            .get("totalCount", 0))
            if page * 1000 >= total:
                break
            page += 1
        print(f"[DEBUG] 배당 데이터: {len(div_map)}개 종목")
    except Exception as e:
        print(f"[ERROR] 배당 API: {e}")
    return div_map

# =============================================
# KIS: PER, PBR, EPS, BPS 조회
# =============================================
def kis_get_per_pbr(stock_code: str) -> dict:
    url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
    try:
        res    = requests.get(url, headers=kis_headers("FHKST01010100"),
                              params=params, timeout=10)
        output = res.json().get("output", {})
        return {
            "per": safe_float(output.get("per", 0)),
            "pbr": safe_float(output.get("pbr", 0)),
            "eps": safe_float(output.get("eps", 0)),
            "bps": safe_float(output.get("bps", 0)),
        }
    except Exception as e:
        print(f"[ERROR] KIS PER/PBR {stock_code}: {e}")
        return {"per": 0, "pbr": 0, "eps": 0, "bps": 0}

# =============================================
# 유틸리티
# =============================================
def latest_biz_day() -> str:
    tz   = pytz.timezone("Asia/Seoul")
    now  = datetime.now(tz)
    date = now - timedelta(days=1) if now.hour < 16 else now
    for _ in range(7):
        if date.weekday() < 5:
            break
        date -= timedelta(days=1)
    return date.strftime("%Y%m%d")

def current_year() -> str:
    return datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y")

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
# 메인 엔드포인트
# =============================================
@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()
        year      = current_year()
        print(f"[DEBUG] 기준일: {base_date}, 배당연도: {year}")

        # 1. KRX: 전종목 시세 (종가, 등락률, 시가총액)
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[DEBUG] KRX KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX 시세 API 응답 없음", "date": base_date}), 500

        # 2. 공공데이터포털: 전종목 배당 데이터 (1회 일괄 조회)
        div_map = fetch_dividend_map(str(int(year) - 1))

        # 3. 시가총액 기준 상위 2000개 선별
        all_items = []
        for item in kospi + kosdaq:
            isu_cd  = item.get("ISU_CD", "")
            name    = item.get("ISU_NM", "").strip()
            if not isu_cd or not name:
                continue
            short_cd = to_short_code(isu_cd)
            price    = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap  = safe_float(item.get("MKTCAP",     0))
            change   = safe_float(item.get("FLUC_RT",    0))
            all_items.append({
                "id": short_cd, "name": name,
                "price": price, "change": change,
                "marketCap": mkt_cap, "capSize": cap_size(mkt_cap),
            })

        all_items.sort(key=lambda x: x["marketCap"], reverse=True)
        top_items = all_items[:2000]

        # 4. KIS: 종목별 PER/PBR 조회 + 배당 데이터 결합
        result = []
        for i, item in enumerate(top_items):
            ind    = kis_get_per_pbr(item["id"])
            div    = div_map.get(item["id"], {})
            price  = item["price"]

            div_amt    = div.get("divAmount", 0)
            # 배당수익률 = 주당배당금 / 현재가 * 100
            div_yield  = round(div_amt / price * 100, 2) if price > 0 and div_amt > 0 else 0.0
            # 배당성향 = 주당배당금 / EPS * 100
            eps        = ind.get("eps", 0)
            div_payout = round(div_amt / eps * 100, 2) if eps > 0 and div_amt > 0 else 0.0

            result.append({
                "id":        item["id"],
                "name":      item["name"],
                "price":     price,
                "change":    item["change"],
                "marketCap": item["marketCap"],
                "capSize":   item["capSize"],
                "per":       ind["per"],
                "pbr":       ind["pbr"],
                "divYield":  div_yield,
                "divAmount": div_amt,
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

# =============================================
# 테스트 엔드포인트
# =============================================
@app.route("/test_kis")
def test_kis():
    token = get_kis_token()
    if not token:
        return jsonify({"error": "토큰 발급 실패"})
    detail = kis_get_per_pbr("005930")
    return jsonify({
        "token_ok":     True,
        "samsung_test": detail
    })

@app.route("/test_div")
def test_div():
    year    = str(int(current_year()) - 1)
    div_map = fetch_dividend_map(year)
    sample = dict(list(div_map.items())[:5])
    return jsonify({"year": year, "count": len(div_map), "sample": sample})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
