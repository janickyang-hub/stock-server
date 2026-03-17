from flask import Flask, jsonify
import requests
import os
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

KRX_AUTH_KEY = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE     = "https://data-dbg.krx.co.kr/svc/apis"

def krx_post(endpoint: str, params: dict) -> list:
    url     = f"{KRX_BASE}/{endpoint}"
    headers = {
        "AUTH_KEY":     KRX_AUTH_KEY,
        "Content-Type": "application/json; charset=UTF-8"
    }
    try:
        res = requests.post(url, headers=headers, json=params, timeout=20)
        print(f"[DEBUG] {endpoint} status={res.status_code} len={len(res.text)}")
        if res.status_code != 200:
            print(f"[ERROR] body={res.text[:300]}")
            return []
        return res.json().get("OutBlock_1", [])
    except Exception as e:
        print(f"[ERROR] {endpoint}: {e}")
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

        # 1. KOSPI 시세 (Spec-2: sto/stk_bydd_trd)
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        # 2. KOSDAQ 시세 (Spec-3: sto/ksq_bydd_trd)
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})
        print(f"[DEBUG] KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX 시세 API 응답 없음", "date": base_date}), 500

        # 3. KOSPI 투자지표 PER/PBR/배당 (sto/stk_isu_per_pbr)
        ind_kospi  = krx_post("sto/stk_isu_per_pbr", {"basDd": base_date})
        # 4. KOSDAQ 투자지표 PER/PBR/배당 (sto/ksq_isu_per_pbr)
        ind_kosdaq = krx_post("sto/ksq_isu_per_pbr", {"basDd": base_date})
        print(f"[DEBUG] 투자지표 KOSPI={len(ind_kospi)} KOSDAQ={len(ind_kosdaq)}")

        # 투자지표 딕셔너리 생성 (ISU_SRT_CD 또는 ISU_CD 기준)
        ind_map = {}
        for item in ind_kospi + ind_kosdaq:
            # 투자지표 API는 ISU_SRT_CD(단축코드) 제공
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

            short_cd = to_short_code(isu_cd)
            price    = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap  = safe_float(item.get("MKTCAP",     0))
            change   = safe_float(item.get("FLUC_RT",    0))

            ind      = ind_map.get(short_cd, {})
            eps      = safe_float(ind.get("EPS", 0))
            dps      = safe_float(ind.get("DPS", 0))   # 주당배당금
            div_yld  = safe_float(ind.get("DVD_YLD", 0))  # 배당수익률(%)
            per      = safe_float(ind.get("PER", 0))
            pbr      = safe_float(ind.get("PBR", 0))
            # 배당성향 = 주당배당금 / EPS * 100
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

# 투자지표 API 응답 구조 확인용
@app.route("/test_ind")
def test_ind():
    base_date = latest_biz_day()
    ind = krx_post("sto/stk_isu_per_pbr", {"basDd": base_date})
    return jsonify({
        "date":    base_date,
        "count":   len(ind),
        "sample":  ind[:3] if ind else [],
        "keys":    list(ind[0].keys()) if ind else []
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
