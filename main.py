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

KIS_TOP_N        = 500
KST              = timezone(timedelta(hours=9))
CACHE_FILE       = "/tmp/stocks_cache.json"
DETAIL_CACHE_DIR = "/tmp/detail_cache"   # ✅ 종목별 상세 캐시 디렉토리
DETAIL_TOP_N     = 100                   # ✅ 초기 빌드 시 상세 데이터 저장 종목 수

# ✅ 서버 캐시 유효 시간 72시간
CACHE_TTL_HOURS = 72

os.makedirs(DETAIL_CACHE_DIR, exist_ok=True)

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
    for i in range(7):
        d = now - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        date_str = d.strftime("%Y%m%d")
        try:
            url     = f"{KRX_BASE}/sto/stk_bydd_trd"
            headers = {"AUTH_KEY": KRX_AUTH_KEY,
                       "Content-Type": "application/json; charset=UTF-8"}
            res     = requests.post(url, headers=headers,
                                    json={"basDd": date_str}, timeout=10)
            items   = res.json().get("OutBlock_1", [])
            if items:
                print(f"[DEBUG] KRX 유효 날짜: {date_str} ({len(items)}개)")
                return date_str
        except:
            continue
    return (now - timedelta(days=3)).strftime("%Y%m%d")

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

# =============================================
# ✅ 개선: 파일 캐시 — 날짜 대신 72시간 TTL 사용
# =============================================
def load_file_cache(allow_stale=False):
    """
    allow_stale=False: 72시간 이내 캐시만 반환
    allow_stale=True:  만료된 캐시도 반환 (오프라인/콜드스타트 폴백용)
    """
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)

        saved_at = cache.get("saved_at")  # ✅ 저장 시각(ISO) 추가
        data     = cache.get("data")

        if not data:
            return None

        if allow_stale:
            # 만료 여부 무관하게 반환 (오프라인 폴백)
            print(f"[CACHE] 스테일 캐시 사용 (저장: {saved_at}): {len(data)}개")
            return data

        if saved_at:
            saved_dt = datetime.fromisoformat(saved_at)
            age_hours = (datetime.now(KST) - saved_dt).total_seconds() / 3600
            if age_hours <= CACHE_TTL_HOURS:
                print(f"[CACHE] 파일 캐시 로드 ({age_hours:.1f}시간 전): {len(data)}개")
                return data
            else:
                print(f"[CACHE] 캐시 만료 ({age_hours:.1f}시간 전 저장)")
                return None
        else:
            # 구버전 캐시: date 필드만 있는 경우 → 오늘 날짜면 허용
            if cache.get("date") == today_str():
                return data
            return None

    except Exception as e:
        print(f"[CACHE] 파일 캐시 로드 실패: {e}")
    return None

def save_file_cache(data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "date":     today_str(),
                "saved_at": datetime.now(KST).isoformat(),  # ✅ 저장 시각 추가
                "data":     data
            }, f, ensure_ascii=False)
        print(f"[CACHE] 파일 캐시 저장: {len(data)}개")
    except Exception as e:
        print(f"[CACHE] 파일 캐시 저장 실패: {e}")

# =============================================
# ✅ 종목별 상세 캐시 저장/로드
# =============================================
def save_detail_cache(code, detail_data):
    """종목 상세 데이터를 /tmp/detail_cache/{code}.json 에 저장"""
    try:
        path = os.path.join(DETAIL_CACHE_DIR, f"{code}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "saved_at": datetime.now(KST).isoformat(),
                "detail":   detail_data
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"[DETAIL CACHE] 저장 실패 {code}: {e}")

def load_detail_cache(code, allow_stale=False):
    """종목 상세 캐시 로드 — 당일 자정 이전 저장이면 유효"""
    try:
        path = os.path.join(DETAIL_CACHE_DIR, f"{code}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if allow_stale:
            return cache.get("detail")
        saved_at = cache.get("saved_at")
        if saved_at:
            saved_dt = datetime.fromisoformat(saved_at)
            # ✅ 오늘 자정(KST 00:00)이 기준 — 당일 저장이면 유효
            today_midnight = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
            if saved_dt >= today_midnight:
                return cache.get("detail")
        return None
    except Exception as e:
        print(f"[DETAIL CACHE] 로드 실패 {code}: {e}")
        return None

# =============================================
# 배당 API — cashDvdnPayDt 포함
# =============================================
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
                    code         = isin[3:9]
                    pay_dt       = item.get("cashDvdnPayDt", "")
                    if code not in div_map or dvdn_dt > div_map[code]["dvdnBasDt"]:
                        div_map[code] = {
                            "divAmount":     int(div_amt),
                            "dvdnBasDt":     dvdn_dt,
                            "cashDvdnPayDt": pay_dt,
                        }
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

# =============================================
# 빌드
# =============================================
def build_stocks_data():
    _build_status["loading"]  = True
    _build_status["error"]    = None
    _build_status["progress"] = 0
    _build_status["current"]  = 0
    _build_status["total"]    = 0

    try:
        base_date = latest_biz_day()

        _build_status["step"]     = "한국거래소 시세 조회 중..."
        _build_status["progress"] = 5
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[BUILD] KRX KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            raise Exception(f"KRX 데이터 없음 (날짜: {base_date})")

        _build_status["step"]     = "배당 정보 수집 중..."
        _build_status["progress"] = 10
        div_map = fetch_dividend_map()
        _build_status["progress"] = 30

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

        kis_map = {}
        for i, item in enumerate(top_items[:KIS_TOP_N]):
            _build_status["step"]    = f"투자지표 조회 중... ({i+1}/{KIS_TOP_N})"
            _build_status["current"] = i + 1
            _build_status["progress"] = 35 + int((i + 1) / KIS_TOP_N * 65)
            kis_map[item["id"]]      = kis_get_per_pbr(item["id"])
            if (i + 1) % 18 == 0:
                time.sleep(1)
            if (i + 1) % 100 == 0:
                print(f"[BUILD] KIS 조회 {i+1}/{KIS_TOP_N}개 완료")

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
                "id":            item["id"],
                "name":          item["name"],
                "price":         price,
                "change":        item["change"],
                "marketCap":     item["marketCap"],
                "capSize":       item["capSize"],
                "per":           ind["per"],
                "pbr":           ind["pbr"],
                "divYield":      div_yield,
                "divAmount":     div_amt,
                "divPayout":     div_payout,
                "cashDvdnPayDt": div.get("cashDvdnPayDt", ""),
            })

        save_file_cache(result)

        # ✅ 대형주 상위 DETAIL_TOP_N개 상세 데이터 백그라운드 저장
        large_caps = [r for r in result if r.get("capSize") == "large"][:DETAIL_TOP_N]
        print(f"[BUILD] 대형주 상세 데이터 사전 캐싱 시작: {len(large_caps)}개")
        _build_status["step"] = f"대형주 상세 데이터 저장 중..."
        saved_count = 0
        for i, item in enumerate(large_caps):
            code = item["id"]
            # 이미 당일 캐시가 있으면 스킵
            if load_detail_cache(code) is not None:
                saved_count += 1
                continue
            try:
                detail_data = {
                    "code":      code,
                    "financial": kis_get_financial(code),
                    "investor":  kis_get_investor(code),
                }
                save_detail_cache(code, detail_data)
                saved_count += 1
                if (i + 1) % 10 == 0:
                    print(f"[BUILD] 상세 캐시 {i+1}/{len(large_caps)}개 완료")
                # KIS API 호출 제한 대응 (재무 + 투자자 각 1회씩)
                if (i + 1) % 18 == 0:
                    time.sleep(1)
            except Exception as e:
                print(f"[BUILD] 상세 캐시 실패 {code}: {e}")
                continue

        _build_status["loading"]  = False
        _build_status["progress"] = 100
        _build_status["step"]     = "완료"
        print(f"[BUILD] 완료! 총 {len(result)}개 (상세 캐시 {saved_count}개)")

    except Exception as e:
        import traceback
        print(f"[BUILD ERROR] {traceback.format_exc()}")
        _build_status["loading"] = False
        _build_status["error"]   = str(e)
        _build_status["step"]    = "오류 발생"

# =============================================
# 매일 오전 7시 자동 빌드 스케줄러
# =============================================
def schedule_daily_build():
    while True:
        now  = now_kst()
        next_run = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait_sec = (next_run - now).total_seconds()
        print(f"[SCHEDULER] 다음 빌드: {next_run.strftime('%Y-%m-%d %H:%M KST')} ({int(wait_sec//3600)}시간 후)")
        time.sleep(wait_sec)
        print(f"[SCHEDULER] 오전 7시 자동 빌드 시작")
        build_stocks_data()

# =============================================
# 엔드포인트
# =============================================

# ✅ 추가: Warm-up ping 엔드포인트
@app.route("/ping")
def ping():
    """앱에서 주기적으로 호출하여 서버 슬립 방지"""
    return jsonify({"pong": True, "time": now_kst().isoformat()})

@app.route("/stocks")
def stocks():
    # 1. 유효한 캐시 확인 (72시간 이내)
    cached = load_file_cache(allow_stale=False)
    if cached:
        return jsonify(cached)

    # 2. 빌드 중이면 진행 상태 반환
    if _build_status["loading"]:
        return jsonify({
            "status":   "loading",
            "progress": _build_status["progress"],
            "step":     _build_status["step"],
            "current":  _build_status["current"],
            "total":    _build_status["total"],
        }), 202

    # 3. ✅ 개선: 만료된 캐시라도 즉시 반환하고 백그라운드에서 갱신
    stale = load_file_cache(allow_stale=True)
    if stale:
        print("[CACHE] 만료 캐시 즉시 반환 + 백그라운드 빌드 시작")
        _build_status["loading"] = True
        threading.Thread(target=build_stocks_data, daemon=True).start()
        return jsonify(stale)  # 즉시 응답!

    # 4. 캐시 없음 → 빌드 시작 후 202 반환
    _build_status["loading"] = True
    threading.Thread(target=build_stocks_data, daemon=True).start()
    return jsonify({
        "status": "loading", "progress": 0,
        "step": "데이터 준비를 시작합니다...", "current": 0, "total": 0,
    }), 202

@app.route("/stocks/status")
def stocks_status():
    cached = load_file_cache(allow_stale=False)
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

def kis_get_financial(stock_code):
    url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/income-statement"
    params = {"FID_DIV_CLS_CODE": "0", "fid_cond_mrkt_div_code": "J",
              "fid_input_iscd": stock_code}
    try:
        res    = requests.get(url, headers=kis_headers("FHKST66430200"),
                              params=params, timeout=10)
        output = res.json().get("output", [])
        result = []
        for item in output[:3]:
            result.append({
                "stac_yymm":      item.get("stac_yymm", ""),
                "sale_account":   safe_float(item.get("sale_account",   0)),
                "bsop_prti":      safe_float(item.get("bsop_prti",      0)),
                "net_income":     safe_float(item.get("thtr_ntin",      0)),
                "sale_totl_prfi": safe_float(item.get("sale_totl_prfi", 0)),
                "eps":            0.0,
            })
        return result
    except Exception as e:
        print(f"[ERROR] 재무 {stock_code}: {e}")
        return []

# ✅ 수정: 최근 유효 영업일 자동 탐색 (0 데이터 스킵)
def kis_get_investor(stock_code):
    now = now_kst()

    for days_back in range(1, 8):
        prev = now - timedelta(days=days_back)
        if prev.weekday() >= 5:  # 주말 스킵
            continue
        prev_date = prev.strftime("%Y%m%d")

        url    = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         stock_code,
            "FID_INPUT_DATE_1":       prev_date,
        }
        try:
            res    = requests.get(url, headers=kis_headers("FHKST01010900"),
                                  params=params, timeout=10)
            output = res.json().get("output", [])

            # ✅ output 전체를 순회하며 빈 값이 아닌 첫 번째 항목 탐색
            # (output[0]은 항상 오늘 날짜이며 값이 빈 문자열""로 채워짐)
            valid = None
            for row in output:
                if any(str(row.get(k, "")).strip() not in ("", "0")
                       for k in ["prsn_ntby_qty", "frgn_ntby_qty", "orgn_ntby_qty"]):
                    valid = row
                    break

            if valid:
                print(f"[DEBUG] 투자자 {stock_code} 유효 날짜: {valid.get('stck_bsop_date', prev_date)}")
                return [{
                    "stck_bsop_date": valid.get("stck_bsop_date", prev_date),
                    "prsn_ntby_qty":  safe_float(valid.get("prsn_ntby_qty", 0)),
                    "frgn_ntby_qty":  safe_float(valid.get("frgn_ntby_qty", 0)),
                    "orgn_ntby_qty":  safe_float(valid.get("orgn_ntby_qty", 0)),
                }]
            else:
                print(f"[DEBUG] 투자자 {stock_code} {prev_date} 전체 output 빈 값, 이전 날짜 시도")

        except Exception as e:
            print(f"[ERROR] 투자자 {stock_code} ({prev_date}): {e}")
            continue

    print(f"[WARN] 투자자 {stock_code} 유효 데이터 없음")
    return []

@app.route("/stock/<stock_code>/detail")
def stock_detail(stock_code):
    try:
        # ✅ 당일 캐시 확인 → 있으면 즉시 반환 (서버 슬립과 무관)
        cached = load_detail_cache(stock_code)
        if cached:
            print(f"[DETAIL] 캐시 반환: {stock_code}")
            return jsonify(cached)

        # 캐시 없으면 실시간 조회 후 저장
        detail_data = {
            "code":      stock_code,
            "financial": kis_get_financial(stock_code),
            "investor":  kis_get_investor(stock_code),
        }
        save_detail_cache(stock_code, detail_data)
        return jsonify(detail_data)
    except Exception as e:
        import traceback
        # ✅ 실패 시 만료 캐시라도 반환
        stale = load_detail_cache(stock_code, allow_stale=True)
        if stale:
            print(f"[DETAIL] 실패, 만료 캐시 반환: {stock_code}")
            return jsonify(stale)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

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

# 서버 시작 시: 캐시 없으면 빌드, 스케줄러 항상 실행
if not load_file_cache(allow_stale=True):
    print("[SERVER] 파일 캐시 없음 → 즉시 빌드 시작")
    _build_status["loading"] = True
    threading.Thread(target=build_stocks_data, daemon=True).start()
else:
    print("[SERVER] 파일 캐시 있음 → 즉시 서비스 가능")

threading.Thread(target=schedule_daily_build, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
