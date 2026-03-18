from flask import Flask, jsonify
import requests
import os
from datetime import datetime, timedelta, timezone, date
import time
import threading

app = Flask(__name__)

KIS_APP_KEY     = "PSSfEHBwUldWZhyEBOcNFh3ykQGqEIhFzJ8M"
KIS_APP_SECRET  = "SN/IHkdyzDE3YPbmWBziziNQ1QIw1qJD4fskpKXAHIimpHyvGhpTcJrpkUwSEU5kI9N9vGwTQ4m5DSyZrY2YhMXymFAt9Itkdcv412cnyrom/6MAS0Q1vLjOyBP9EUKw/rpcjxpb4IaXnJ3YstxticaeATcI4Kg9S6sWG5dqc9//A3bYTKI="
KIS_BASE_URL    = "https://openapi.koreainvestment.com:9443"
KRX_AUTH_KEY    = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE        = "https://data-dbg.krx.co.kr/svc/apis"
FSC_SERVICE_KEY = "e0a1fb6fedf17f785d6b35276663fb0f47bb199d21038d494ea05b2250596a30"

# 한국 시간대 (pytz 없이 내장 모듈 사용)
KST = timezone(timedelta(hours=9))

_token_cache  = {"access_token": None, "expires_at": None}
_div_cache    = {"data": None, "date": None}
_stocks_cache = {
    "data":     None,
    "date":     None,
    "loading":  False,
    "error":    None,
    "progress": 0,
    "step":     "",
    "total":    0,
    "current":  0,
}

def now_kst() -> datetime:
    return datetime.now(KST)

def today_str() -> str:
    return now_kst().strftime("%Y%m%d")

def latest_biz_day() -> str:
    now  = now_kst()
    d    = now - timedelta(days=1) if now.hour < 16 else now
    for _ in range(7):
        if d.weekday() < 5:
            break
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

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

def fetch_dividend_map() -> dict:
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
            params = {
                "serviceKey": FSC_SERVICE_KEY,
                "numOfRows":  "1000",
                "pageNo":     str(page),
                "resultType": "json",
            }
            res  = requests.get(url, params=params, timeout=20)
            if res.status_code != 200:
                break
            data  = res.json()
            items = (data.get("response", {})
                         .get("body", {})
                         .get("items", {})
                         .get("item", []))
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
        }
    except:
        return {"per": 0, "pbr": 0, "eps": 0}

def build_stocks_data():
    _stocks_cache["loading"]  = True
    _stocks_cache["error"]    = None
    _stocks_cache["progress"] = 0
    _stocks_cache["current"]  = 0
    _stocks_cache["total"]    = 0

    try:
        today     = today_str()
        base_date = latest_biz_day()

        _stocks_cache["step"]     = "한국거래소 시세 조회 중..."
        _stocks_cache["progress"] = 5
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[BUILD] KRX KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        _stocks_cache["step"]     = "배당 정보 수집 중..."
        _stocks_cache["progress"] = 10
        div_map = fetch_dividend_map()
        _stocks_cache["progress"] = 30

        _stocks_cache["step"] = "종목 선별 중..."
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
        total     = len(top_items)
        _stocks_cache["total"]    = total
        _stocks_cache["progress"] = 35

        result = []
        for i, item in enumerate(top_items):
            _stocks_cache["step"]     = f"투자지표 조회 중... ({i+1}/{total})"
            _stocks_cache["current"]  = i + 1
            _stocks_cache["progress"] = 35 + int((i + 1) / total * 65)

            ind        = kis_get_per_pbr(item["id"])
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

            if (i + 1) % 18 == 0:
                time.sleep(1)
            if (i + 1) % 100 == 0:
                print(f"[BUILD] KIS 조회 {i+1}/{total}개 완료")

        _stocks_cache["data"]     = result
        _stocks_cache["date"]     = today
        _stocks_cache["loading"]  = False
        _stocks_cache["progress"] = 100
        _stocks_cache["step"]     = "완료"
        print(f"[BUILD] 완료! 총 {len(result)}개")

    except Exception as e:
        import traceback
        print(f"[BUILD ERROR] {traceback.format_exc()}")
        _stocks_cache["loading"] = False
        _stocks_cache["error"]   = str(e)
        _stocks_cache["step"]    = "오류 발생"

@app.route("/stocks")
def stocks():
    today = today_str()
    if _stocks_cache["data"] and _stocks_cache["date"] == today:
        return jsonify(_stocks_cache["data"])

    if _stocks_cache["loading"]:
        return jsonify({
            "status":   "loading",
            "progress": _stocks_cache["progress"],
            "step":     _stocks_cache["step"],
            "current":  _stocks_cache["current"],
            "total":    _stocks_cache["total"],
        }), 202

    _stocks_cache["loading"] = True
    threading.Thread(target=build_stocks_data, daemon=True).start()
    return jsonify({
        "status":   "loading",
        "progress": 0,
        "step":     "데이터 준비를 시작합니다...",
        "current":  0,
        "total":    0,
    }), 202

@app.route("/stocks/status")
def stocks_status():
    today = today_str()
    return jsonify({
        "cached":   _stocks_cache["data"] is not None and _stocks_cache["date"] == today,
        "loading":  _stocks_cache["loading"],
        "progress": _stocks_cache["progress"],
        "step":     _stocks_cache["step"],
        "current":  _stocks_cache["current"],
        "total":    _stocks_cache["total"],
        "count":    len(_stocks_cache["data"]) if _stocks_cache["data"] else 0,
        "error":    _stocks_cache["error"],
    })

@app.route("/test_div")
def test_div():
    _div_cache["data"] = None
    div_map = fetch_dividend_map()
    return jsonify({"count": len(div_map), "sample": dict(list(div_map.items())[:3])})

@app.route("/test_kis")
def test_kis():
    token = get_kis_token()
    if not token:
        return jsonify({"error": "토큰 발급 실패"})
    return jsonify({"token_ok": True, "samsung_test": kis_get_per_pbr("005930")})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

# 서버 시작 시 자동 로드 (loading 플래그 먼저 설정)
_stocks_cache["loading"] = True
threading.Thread(target=build_stocks_data, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
