from flask import Flask, jsonify
import requests
import os
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

KRX_AUTH_KEY = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE     = "https://data-dbg.krx.co.kr/svc/apis"

def krx_get(endpoint: str, params: dict) -> list:
    """KRX Open API - GET 방식 (공식 호출 방식)"""
    url     = f"{KRX_BASE}/{endpoint}"
    headers = {"AUTH_KEY": KRX_AUTH_KEY}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=20)
        print(f"[DEBUG] GET {endpoint} status={res.status_code} len={len(res.text)}")
        if res.status_code != 200:
            print(f"[ERROR] body={res.text[:300]}")
            return []
        return res.json().get("OutBlock_1", [])
    except Exception as e:
        print(f"[ERROR] {endpoint}: {e}")
        return []

def krx_post(endpoint: str, params: dict) -> list:
    """KRX Open API - POST + JSON body 방식 (백업)"""
    url     = f"{KRX_BASE}/{endpoint}"
    headers = {
        "AUTH_KEY":     KRX_AUTH_KEY,
        "Content-Type": "application/json; charset=UTF-8"
    }
    try:
        res = requests.post(url, headers=headers, json=params, timeout=20)
        print(f"[DEBUG] POST {endpoint} status={res.status_code} len={len(res.text)}")
        if res.status_code != 200:
            print(f"[ERROR] body={res.text[:300]}")
            return []
        return res.json().get("OutBlock_1", [])
    except Exception as e:
        print(f"[ERROR] {endpoint}: {e}")
        return []

def krx_call(endpoint: str, params: dict) -> list:
    """GET 먼저 시도 → 실패시 POST 시도"""
    result = krx_get(endpoint, params)
    if not result:
        result = krx_post(endpoint, params)
    return result

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
        return float(v) if v and v != "-" else 0.0
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

@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()
        print(f"[DEBUG] 기준일: {base_date}")

        # 1. KOSPI 시세
        kospi  = krx_call("sto/stk_bydd_trd", {"basDd": base_date})
        # 2. KOSDAQ 시세
        kosdaq = krx_call("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[DEBUG] KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX 시세 API 응답 없음", "date": base_date}), 500

        # 3. PER/PBR/배당 투자지표 - 여러 엔드포인트 순서대로 시도
        ind_data = []
        for endpoint in [
            "sto/stk_isu_per_pbr",      # KOSPI 투자지표
            "sto/ksq_isu_per_pbr",      # KOSDAQ 투자지표
            "sto/per_pbr_bydd",         # 전종목 투자지표 후보1
            "equ/per_pbr_bydd",         # 전종목 투자지표 후보2
            "sto/stk_bydd_per_pbr",     # 후보3
        ]:
            result = krx_call(endpoint, {"basDd": base_date})
            if result:
                ind_data.extend(result)
                print(f"[DEBUG] 투자지표 성공: {endpoint} ({len(result)}개)")
                break

        print(f"[DEBUG] 투자지표 총={len(ind_data)}")

        # 투자지표 딕셔너리 (단축코드 기준)
        ind_map = {}
        for item in ind_data:
            code = (item.get("ISU_SRT_CD") or
                    to_short_code(item.get("ISU_CD", ""))).strip()
            if code:
                ind_map[code] = item

        result = []
        for item in kospi + kosdaq:
            isu_cd   = item.get("ISU_CD", "")
            name     = item.get("ISU_NM", "").strip()
            if not isu_cd or not name:
                continue

            short_cd  = to_short_code(isu_cd)
            price     = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap   = safe_float(item.get("MKTCAP",     0))
            change    = safe_float(item.get("FLUC_RT",    0))

            ind       = ind_map.get(short_cd, {})
            eps       = safe_float(ind.get("EPS",     0))
            dps       = safe_float(ind.get("DPS",     0))
            div_yld   = safe_float(ind.get("DVD_YLD", 0))
            per       = safe_float(ind.get("PER",     0))
            pbr       = safe_float(ind.get("PBR",     0))
            div_payout = round(dps / eps * 100, 2) if eps > 0 else 0.0

            result.append({
                "id":        short_cd,
                "name":      name,
                "price":     price,
                "change":    change,
                "marketCap": mkt_cap,
                "capSize":   cap_size(mkt_cap),
                "per":       per,
                "pbr":       pbr,
                "divYield":  div_yld,
                "divAmount": int(dps),
                "divPayout": div_payout
            })

        result.sort(key=lambda x: x["marketCap"], reverse=True)
        print(f"[DEBUG] 최종: {len(result)}개")
        return jsonify(result[:2000])

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ERROR] {tb}")
        return jsonify({"error": str(e), "trace": tb}), 500

@app.route("/test_ind")
def test_ind():
    """투자지표 API 응답 구조 확인용"""
    base_date = latest_biz_day()
    for endpoint in [
        "sto/stk_isu_per_pbr",
        "sto/ksq_isu_per_pbr",
        "sto/per_pbr_bydd",
        "equ/per_pbr_bydd",
        "sto/stk_bydd_per_pbr",
    ]:
        result = krx_call(endpoint, {"basDd": base_date})
        if result:
            return jsonify({
                "success_endpoint": endpoint,
                "date":    base_date,
                "count":   len(result),
                "keys":    list(result[0].keys()),
                "sample":  result[:2]
            })
    return jsonify({
        "error": "모든 엔드포인트 실패 - KRX 포털에서 투자지표 API 신청 필요",
        "date":  base_date,
        "tried": ["sto/stk_isu_per_pbr", "sto/ksq_isu_per_pbr",
                  "sto/per_pbr_bydd", "equ/per_pbr_bydd", "sto/stk_bydd_per_pbr"]
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
