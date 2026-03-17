from flask import Flask, jsonify, request
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
        if res.status_code != 200:
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

# pykrx 진단
@app.route("/test_pykrx")
def test_pykrx():
    date = request.args.get("date", "20260313")
    result = {"date": date, "steps": []}

    # 1단계: import 테스트
    try:
        from pykrx import stock as pykrx_stock
        result["steps"].append("✅ pykrx import 성공")
    except Exception as e:
        result["steps"].append(f"❌ pykrx import 실패: {e}")
        return jsonify(result)

    # 2단계: 단일 종목 테스트 (삼성전자 005930)
    try:
        df_single = pykrx_stock.get_market_fundamental(date, date, "005930")
        result["steps"].append(f"✅ 단일종목 조회 성공: shape={df_single.shape} columns={list(df_single.columns)}")
        if not df_single.empty:
            result["single_sample"] = df_single.to_dict()
    except Exception as e:
        result["steps"].append(f"❌ 단일종목 조회 실패: {e}")

    # 3단계: 전종목 KOSPI 테스트
    try:
        df_all = pykrx_stock.get_market_fundamental(date, market="KOSPI")
        result["steps"].append(f"✅ 전종목 KOSPI 조회 성공: shape={df_all.shape}")
        if not df_all.empty:
            result["kospi_count"] = len(df_all)
            result["kospi_sample"] = df_all.head(2).to_dict()
    except Exception as e:
        result["steps"].append(f"❌ 전종목 KOSPI 조회 실패: {e}")

    return jsonify(result)

@app.route("/test_ind")
def test_ind():
    date = request.args.get("date", "20260313")
    try:
        from pykrx import stock as pykrx_stock
        df = pykrx_stock.get_market_fundamental(date, market="KOSPI")
        return jsonify({
            "date":       date,
            "shape":      str(df.shape),
            "columns":    list(df.columns),      # 실제 컬럼명 확인
            "index_name": str(df.index.name),    # 인덱스 이름
            "empty":      df.empty,
            "count":      len(df),
            "raw_sample": df.head(3).to_dict()   # 원본 데이터 구조 그대로
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()})


@app.route("/stocks")
def stocks():
    try:
        base_date = latest_biz_day()
        kospi  = krx_post("sto/stk_bydd_trd", {"basDd": base_date})
        kosdaq = krx_post("sto/ksq_bydd_trd", {"basDd": base_date})

        if not kospi and not kosdaq:
            return jsonify({"error": "KRX 시세 API 응답 없음", "date": base_date}), 500

        # pykrx 투자지표
        ind_map = {}
        try:
            from pykrx import stock as pykrx_stock
            for market in ["KOSPI", "KOSDAQ"]:
                df = pykrx_stock.get_market_fundamental(base_date, market=market)
                if not df.empty:
                    for ticker, row in df.iterrows():
                        ind_map[str(ticker)] = {
                            "PER": float(row.get("PER", 0) or 0),
                            "PBR": float(row.get("PBR", 0) or 0),
                            "EPS": float(row.get("EPS", 0) or 0),
                            "DPS": float(row.get("DPS", 0) or 0),
                            "DIV": float(row.get("DIV", 0) or 0),
                        }
        except Exception as e:
            print(f"[ERROR] pykrx: {e}")

        result = []
        for item in kospi + kosdaq:
            isu_cd = item.get("ISU_CD", "")
            name   = item.get("ISU_NM", "").strip()
            if not isu_cd or not name:
                continue

            short_cd   = to_short_code(isu_cd)
            price      = safe_float(item.get("TDD_CLSPRC", 0))
            mkt_cap    = safe_float(item.get("MKTCAP",     0))
            change     = safe_float(item.get("FLUC_RT",    0))
            ind        = ind_map.get(short_cd, {})
            eps        = ind.get("EPS", 0)
            dps        = ind.get("DPS", 0)

            result.append({
                "id":        short_cd,
                "name":      name,
                "price":     price,
                "change":    change,
                "marketCap": mkt_cap,
                "capSize":   cap_size(mkt_cap),
                "per":       ind.get("PER", 0),
                "pbr":       ind.get("PBR", 0),
                "divYield":  ind.get("DIV", 0),
                "divAmount": int(dps),
                "divPayout": round(dps / eps * 100, 2) if eps > 0 else 0.0
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
