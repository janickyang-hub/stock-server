from flask import Flask, jsonify
import requests, io, csv

app = Flask(__name__)

KRX_OTP_URL  = "https://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
KRX_DOWN_URL = "https://data.krx.co.kr/comm/fileDn/download_csv/download.cmd"
HEADERS = {
    "Referer":    "https://data.krx.co.kr",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

def get_krx_csv(params: dict) -> list[dict]:
    otp = requests.post(KRX_OTP_URL, data=params, headers=HEADERS, timeout=15).text.strip()
    res = requests.post(KRX_DOWN_URL, data={"code": otp}, headers=HEADERS, timeout=15)
    text = res.content.decode("euc-kr", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)

@app.route("/stocks")
def stocks():
    try:
        # 시세 데이터 (종가, 시가총액, 등락률)
        price_rows = get_krx_csv({
            "locale": "ko_KR", "mktId": "ALL", "trdDd": latest_biz_day(),
            "share": "1", "money": "1", "csvxls_isNo": "false",
            "name": "fileDown", "url": "dbms/MDC/STAT/standard/MDCSTAT01501"
        })

        # 투자지표 (PER, PBR, 배당수익률, EPS, BPS, 주당배당금)
        ind_rows = get_krx_csv({
            "locale": "ko_KR", "mktId": "ALL", "trdDd": latest_biz_day(),
            "money": "1", "csvxls_isNo": "false",
            "name": "fileDown", "url": "dbms/MDC/STAT/standard/MDCSTAT03501"
        })

        # 투자지표를 종목코드 기준 딕셔너리로 변환
        ind_map = {r.get("종목코드", ""): r for r in ind_rows}

        result = []
        for r in price_rows:
            code = r.get("종목코드", "").strip()
            if not code: continue
            ind  = ind_map.get(code, {})
            eps  = safe_float(ind.get("EPS", "0"))
            div  = safe_float(ind.get("주당배당금", "0"))

            result.append({
                "id":        code,
                "name":      r.get("종목명", "").strip(),
                "price":     safe_float(r.get("종가", "0")),
                "change":    safe_float(r.get("등락률", "0")),
                "marketCap": safe_float(r.get("시가총액", "0")),
                "per":       safe_float(ind.get("PER", "0")),
                "pbr":       safe_float(ind.get("PBR", "0")),
                "divYield":  safe_float(ind.get("배당수익률", "0")),
                "divAmount": int(div),
                "divPayout": round(div / eps * 100, 2) if eps > 0 else 0
            })

        # 시가총액 순 정렬 후 상위 2000개
        result.sort(key=lambda x: x["marketCap"], reverse=True)
        return jsonify(result[:2000])

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def safe_float(s: str) -> float:
    try: return float(str(s).replace(",", "").replace("-", "0") or "0")
    except: return 0.0

def latest_biz_day() -> str:
    from datetime import datetime, timedelta
    import pytz
    tz   = pytz.timezone("Asia/Seoul")
    now  = datetime.now(tz)
    date = now - timedelta(days=1) if now.hour < 16 else now
    for _ in range(7):
        if date.weekday() < 5: break  # 월~금
        date -= timedelta(days=1)
    return date.strftime("%Y%m%d")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
