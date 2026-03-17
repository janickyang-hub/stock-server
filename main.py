from flask import Flask, jsonify
import requests
import os
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

KRX_AUTH_KEY = "C1421182F8FD42CA999E3F73D51D0DF2C3829272"
KRX_BASE     = "https://data-dbg.krx.co.kr/svc/apis"

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

# MARK: - 진단용 엔드포인트 (KRX 실제 응답 그대로 반환)
@app.route("/test")
def test():
    base_date = latest_biz_day()
    url       = f"{KRX_BASE}/sto/stk_bydd_trd"
    headers   = {
        "AUTH_KEY":     KRX_AUTH_KEY,
        "Content-Type": "application/json; charset=UTF-8"
    }
    try:
        res = requests.post(url, headers=headers,
                            json={"basDd": base_date}, timeout=20)
        return jsonify({
            "url":        url,
            "date":       base_date,
            "auth_key":   KRX_AUTH_KEY[:8] + "...",  # 앞 8자리만 표시
            "status":     res.status_code,
            "headers":    dict(res.headers),
            "body":       res.text[:1000]             # 응답 앞 1000자
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()

        headers = {
            "AUTH_KEY":     KRX_AUTH_KEY,
            "Content-Type": "application/json; charset=UTF-8"
        }

        kospi_res  = requests.post(f"{KRX_BASE}/sto/stk_bydd_trd",
                                   headers=headers,
                                   json={"basDd": base_date}, timeout=20)
        kosdaq_res = requests.post(f"{KRX_BASE}/sto/ksq_bydd_trd",
                                   headers=headers,
                                   json={"basDd": base_date}, timeout=20)

        kospi  = kospi_res.json().get("OutBlock_1",  []) if kospi_res.status_code  == 200 else []
        kosdaq = kosdaq_res.json().get("OutBlock_1", []) if kosdaq_res.status_code == 200 else []

        if not kospi and not kosdaq:
            return jsonify({
                "error":         "KRX API 응답 없음",
                "date":          base_date,
                "kospi_status":  kospi_res.status_code,
                "kospi_body":    kospi_res.text[:300],
                "kosdaq_status": kosdaq_res.status_code,
                "kosdaq_body":   kosdaq_res.text[:300],
            }), 500

        result = []
        for item in kospi + kosdaq:
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
                "per":       0.0,
                "pbr":       0.0,
                "divYield":  0.0,
                "divAmount": 0,
                "divPayout": 0.0
            })

        result.sort(key=lambda x: x["marketCap"], reverse=True)
        return jsonify(result[:2000])

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "date": latest_biz_day()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
