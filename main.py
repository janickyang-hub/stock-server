from flask import Flask, jsonify
import requests
import os
import json
from datetime import datetime, timedelta, timezone
import time
import threading

app = Flask(__name__)

KIS_APP_KEY     = "PSSfEHBwUldWZhyEBOcNFh3ykQGqEIhFzJ8M"
KIS_APP_SECRET  = "SN/IHkdyzDE3YPbmWBziziNQ1QIw1qJD4fskpKXAHIimpHyvGhpTcJrpkUwSEU5kI9N9vGwTQ4m5DSyZrY2YhMXymFAt9Itkdcv412cnyrom/6MAS0Q1vLjOyBP9EUKw/rpcjxpb4IaXnJ3YstxticaeATcI4Kg9S6sWG5dqc9//A3bYTKI="
KIS_BASE_URL    = "https://openapi.koreainvestment.com:9443"
KRX_AUTH_KEY    = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE        = "https://data-dbg.krx.co.kr/svc/apis"
FSC_SERVICE_KEY = "e0a1fb6fedf17f785d6b35276663fb0f47bb199d21038d494ea05b2250596a30"

KIS_TOP_N    = 500
KST          = timezone(timedelta(hours=9))
CACHE_FILE   = "/tmp/stocks_cache.json"   # 파일 캐시 경로

_token_cache  = {"access_token": None, "expires_at": None}
_div_cache    = {"data": None, "date": None}
_build_status = {
    "loading": False, "error": None,
    "progress": 0, "step": "", "total": 0, "current": 0,
}

def now_kst():
    return datetime.now(KST)

def today_str():
    return now_kst().strftime("%Y%m%d")

def latest_biz_day():
    now = now_kst()
    d   = now - timedelta(days=1) if now.hour < 16 else now
    for _ in range(7):
        if d.weekday() < 5:
            break
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

# =============================================
# 파일 캐시 (워커 재시작해도 유지)
# =============================================
def load_file_cache():
    """파일에서 캐시 로드. 오늘 날짜 데이터면 반환."""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("date") == today_str() and cache.get("data"):
            print(f"[CACHE] 파일 캐시 로드: {len(cache['data'])}개")
            return cache["data"]
    except Exception as e:
        print(f"[CACHE] 파일 캐시 로드 실패: {e}")
    return None

def save_file_cache(data):
    """데이터를 파일에 저장."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today_str(), "data": data}, f, ensure_ascii=False)
        print(f"[CACHE] 파일 캐시 저장: {len(data)}개")
    except Exception as e:
        print(f"[CACHE] 파일 캐시 저장 실패: {e}")

def get_kis_token():
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
    return token

def kis_headers(tr_id):
    return {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {get_kis_token()}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P"
    }

def krx_post(endpoint, params):
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

def safe_float(val):
    try:
        v = str(val).replace(",", "").strip()
        return float(v) if v and v not in ("-", "N/A", "") else 0.0
    except:
        return 0.0

def to_short_code(isu_cd):
    code = isu_cd.strip()
    if len(code) == 12 and code.startswith("KR"):
        return code[3:9]
    return code

def cap_size(mkt_cap):
    if mkt_cap >= 1_000_000_000_000: return "large"
    if mkt_cap >= 300_000_000_000:   return "mid"
    return "small"

def fetch_dividend_map():
    today = today_str()
    if _div_cache["data"] is not None and _div_cache["date"] == today:
        return _div_cache["data"]

    cur_yr    = now_kst().year
    date_from = f"{cur_yr - 2}0101"
    date_to   = f"{cur_yr - 1}1231"
    div_map   = {}
    url       = "https://apis.data.go.kr/1160100/service/GetStocDiviInfoService/getDiviInfo"

    try:
        page = 1
        while True:
            params = {"serviceKey": FSC_SERVICE_KEY, "numOfRows": "1000",
                      "pageNo": str(page), "resultType": "json"}
            res  = requests.get(url, params=params, timeout=20)
            if res.status_code != 200:
                break
            data  = res.json()
            items = (data.get("response", {}).get("body", {})
                         .get("items", {}).get("item", []))
            if not items:
                break
            for item in items:
                if item.get("scrsItmsKcd", "") != "0101":
                    continue
                dvdn_dt = item.get("dvdnBasDt", "")
                if not (date_from <= dvdn_dt <= date_to):
                    continue
                isin    = item.get("isinCd", "")
                div_amt = safe_float(item.get("stckGenrDvdnAmt", 0))
                if len(isin) == 12 and isin.startswith("KR") and div_amt > 0:
                    code = isin[3:9]
                    if code not in div_map or dvdn_dt > div_map[code]["dvdnBasDt"]:
                        div_map[code] = {"divAmount": int(div_amt), "dvdnBasDt": dvdn_dt}
            total = int(data.get("response", {}).get("body", {}).get("totalCount", 0))
            print(f"[DEBUG] 배당 page={page}/{(total//1000)+1} 수집={len(div_map)}")
            if page * 1000 >= total:
                break
            page += 1
        print(f"[DEBUG] 배당 최종: {len(div_map)}개")
        _div_cache["data"] = div_map
        _div_cache["date"] = today
    except Exception as e:
        import traceback
        print(f"[ERROR] 배당: {traceback.format_exc()}")
    return div_map

def kis_get_per_pbr(stock_code):
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
        }
    except:
        return {"per": 0, "pbr": 0, "eps": 0}

def build_stocks_data():
    _build_status["loading"]  = True
    _build_status["error"]    = None
    _build_status["progress"] = 0
    _build_status["current"]  = 0
    _build_status["total"]    = 0

    try:
        today     = today_str()
        base_date = latest_biz_day()

        # 1단계: KRX 시세
        _build_status["step"]     = "한국거래소 시세 조회 중..."
        _build_status["progress"] = 5
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[BUILD] KRX KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        # 2단계: 배당
        _build_status["step"]     = "배당 정보 수집 중..."
        _build_status["progress"] = 10
        div_map = fetch_dividend_map()
        _build_status["progress"] = 30

        # 3단계: 종목 정렬
        _build_status["step"] = "종목 선별 중..."
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
        _build_status["total"]    = KIS_TOP_N
        _build_status["progress"] = 35

        # 4단계: KIS PER/PBR — 상위 500개만
        kis_map = {}
        for i, item in enumerate(top_items[:KIS_TOP_N]):
            _build_status["step"]     = f"투자지표 조회 중... ({i+1}/{KIS_TOP_N})"
            _build_status["current"]  = i + 1
            _build_status["progress"] = 35 + int((i + 1) / KIS_TOP_N * 65)
            kis_map[item["id"]]       = kis_get_per_pbr(item["id"])
            if (i + 1) % 18 == 0:
                time.sleep(1)
            if (i + 1) % 100 == 0:
                print(f"[BUILD] KIS 조회 {i+1}/{KIS_TOP_N}개 완료")

        # 5단계: 결합
        result = []
        for item in top_items:
            ind        = kis_map.get(item["id"], {"per": 0, "pbr": 0, "eps": 0})
            div        = div_map.get(item["id"], {})
            price      = item["price"]
            div_amt    = div.get("divAmount", 0)
            div_yield  = round(div_amt / price * 100, 2) if price > 0 and div_amt > 0 else 0.0
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

        # 파일 캐시에 저장 (워커 재시작 후에도 유지)
        save_file_cache(result)

        _build_status["loading"]  = False
        _build_status["progress"] = 100
        _build_status["step"]     = "완료"
        print(f"[BUILD] 완료! 총 {len(result)}개")

    except Exception as e:
        import traceback
        print(f"[BUILD ERROR] {traceback.format_exc()}")
        _build_status["loading"] = False
        _build_status["error"]   = str(e)
        _build_status["step"]    = "오류 발생"

@app.route("/stocks")
def stocks():
    # 1순위: 파일 캐시 (워커 재시작 후에도 유지)
    cached = load_file_cache()
    if cached:
        return jsonify(cached)

    # 2순위: 빌드 중
    if _build_status["loading"]:
        return jsonify({
            "status":   "loading",
            "progress": _build_status["progress"],
            "step":     _build_status["step"],
            "current":  _build_status["current"],
            "total":    _build_status["total"],
        }), 202

    # 3순위: 빌드 시작
    _build_status["loading"] = True
    threading.Thread(target=build_stocks_data, daemon=True).start()
    return jsonify({
        "status": "loading", "progress": 0,
        "step": "데이터 준비를 시작합니다...", "current": 0, "total": 0,
    }), 202

@app.route("/stocks/status")
def stocks_status():
    cached = load_file_cache()
    return jsonify({
        "cached":   cached is not None,
        "loading":  _build_status["loading"],
        "progress": _build_status["progress"],
        "step":     _build_status["step"],
        "current":  _build_status["current"],
        "total":    _build_status["total"],
        "count":    len(cached) if cached else 0,
        "error":    _build_status["error"],
    })

@app.route("/test_kis")
def test_kis():
    token = get_kis_token()
    if not token:
        return jsonify({"error": "토큰 발급 실패"})
    return jsonify({"token_ok": True, "samsung_test": kis_get_per_pbr("005930")})

@app.route("/test_div")
def test_div():
    _div_cache["data"] = None
    div_map = fetch_dividend_map()
    return jsonify({"count": len(div_map), "sample": dict(list(div_map.items())[:3])})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

# 서버 시작 시: 파일 캐시 없으면 백그라운드 빌드 시작
if not load_file_cache():
    print("[SERVER] 파일 캐시 없음 → 백그라운드 빌드 시작")
    _build_status["loading"] = True
    threading.Thread(target=build_stocks_data, daemon=True).start()
else:
    print("[SERVER] 파일 캐시 있음 → 즉시 서비스 가능")
    
@app.route("/debug/<stock_code>")
def debug_financial(stock_code):
    url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/income-statement"
    params = {
        "FID_DIV_CLS_CODE":      "0",   # 0: 연간, 1: 분기
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd":         stock_code,
    }
    res  = requests.get(url, headers=kis_headers("FHKST66430200"),
                        params=params, timeout=10)
    return jsonify(res.json())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# =============================================
# 종목 상세 API
# =============================================

def kis_get_financial(stock_code: str) -> dict:
    """3년간 연간 실적 (매출, 영업이익, 순이익, EPS) - FHKST66430200"""
    url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/income-statement"
    params = {
        "FID_DIV_CLS_CODE": "1",   # 1: 연간
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code,
    }
    try:
        res    = requests.get(url, headers=kis_headers("FHKST66430200"),
                              params=params, timeout=10)
        output = res.json().get("output", [])
        result = []
        for item in output[:3]:  # 최근 3년
            result.append({
                "stac_yymm":   item.get("stac_yymm",   ""),   # 결산년월
                "sale_account": safe_float(item.get("sale_account", 0)),  # 매출액
                "bsop_prti":   safe_float(item.get("bsop_prti",   0)),   # 영업이익
                "net_income":  safe_float(item.get("net_income",  0)),   # 순이익
                "eps":         safe_float(item.get("eps",         0)),   # EPS
            })
        return result
    except Exception as e:
        print(f"[ERROR] 재무 {stock_code}: {e}")
        return []

def kis_get_investor(stock_code: str) -> dict:
    """전일 투자자별 매매 수량 - FHKST01010900"""
    url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         stock_code,
    }
    try:
        res    = requests.get(url, headers=kis_headers("FHKST01010900"),
                              params=params, timeout=10)
        output = res.json().get("output", [])
        result = []
        for item in output[:1]:  # 전일 데이터
            result.append({
                "stck_bsop_date": item.get("stck_bsop_date", ""),  # 날짜
                "prsn_ntby_qty":  safe_float(item.get("prsn_ntby_qty", 0)),   # 개인 순매수
                "frgn_ntby_qty":  safe_float(item.get("frgn_ntby_qty", 0)),   # 외국인 순매수
                "orgn_ntby_qty":  safe_float(item.get("orgn_ntby_qty", 0)),   # 기관 순매수
            })
        return result
    except Exception as e:
        print(f"[ERROR] 투자자 {stock_code}: {e}")
        return []

@app.route("/stock/<stock_code>/detail")
def stock_detail(stock_code: str):
    try:
        financial = kis_get_financial(stock_code)
        investor  = kis_get_investor(stock_code)
        return jsonify({
            "code":      stock_code,
            "financial": financial,
            "investor":  investor,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
