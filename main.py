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
    """표준코드(KR7005930003) → 단축코드(005930)"""
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

        # 스펙 Spec-2: 유가증권 일별매매정보
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        # 스펙 Spec-3: 코스닥 일별매매정보
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})

        print(f"[DEBUG] KOSPI={len(kospi)} KOSDAQ={len(kosdaq)}")

        if not kospi and not kosdaq:
            return jsonify({
                "error": "KRX API 응답 없음",
                "date":  base_date
            }), 500

        result = []
        for item in kospi + kosdaq:
            # Spec-2/3 응답 필드 기준
            isu_cd = item.get("ISU_CD", "")
            name   = item.get("ISU_NM", "").strip()
            if not isu_cd or not name:
                continue

            short_cd = to_short_code(isu_cd)
            price    = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap  = safe_float(item.get("MKTCAP",     0))
            change   = safe_float(item.get("FLUC_RT",    0))

            result.append({
                "id":        short_cd,
                "name":      name,
                "price":     price,
                "change":    change,
                "marketCap": mkt_cap,
                "capSize":   cap_size(mkt_cap),
                "per":       0.0,   # 투자지표 스펙 확보 후 추가 예정
                "pbr":       0.0,
                "divYield":  0.0,
                "divAmount": 0,
                "divPayout": 0.0
            })

        result.sort(key=lambda x: x["marketCap"], reverse=True)
        print(f"[DEBUG] 최종: {len(result)}개")
        return jsonify(result[:2000])

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
