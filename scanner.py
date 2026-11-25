import os
import json
import requests
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

# ==========================================
# 1. KULLANICI AYARLARI
# ==========================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# Takip edilecek 7 parite (Görünen İsim : Yahoo Finance Sembolü)
SYMBOLS = {
    "XAU/USD": "GC=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "DXY":     "DX-Y.NYB",
    "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X",
    "USD/CAD": "USDCAD=X",
}

D1_SW_LEN  = 5      # 1D Ana Trend Pivot Uzunluğu
H4_EXT_LEN = 7      # 4H Ana Havuz Pivot Uzunluğu
H4_INT_LEN = 3      # 4H İç Likidite Pivot Uzunluğu
H1_EXT_LEN = 10     # 1H Ana Havuz Pivot Uzunluğu
H1_INT_LEN = 4      # 1H İç Likidite Pivot Uzunluğu
H1_MSS_LEN = 2      # 1H MSS Giriş Tetikleyici Pivot Uzunluğu
USE_KZ     = False  # Kill Zone filtresi (True yaparsan sadece Londra/NY seanslarında atar)
STATE_FILE = "state.json"

# ==========================================
# 2. YARDIMCI FONKSİYONLAR
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("HATA: TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik!")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    resp = requests.post(url, json=payload, timeout=15)
    if resp.ok:
        print("Telegram mesaji basariyla gonderildi.")
    else:
        print(f"Telegram API Hatasi ({resp.status_code}): {resp.text}")

def fmt_price(name, val):
    if val is None:
        return "-"
    if "JPY" in name or "DXY" in name:
        return f"{val:.3f}"
    if "XAU" in name:
        return f"{val:.2f}"
    return f"{val:.5f}"

def fetch_ohlc(ticker, period, interval):
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty and ticker == "DX-Y.NYB":
        df = yf.download("DX=F", period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    # Henüz kapanmamış canlı mumu çıkar (Sadece kapanmış mumlarda sinyal üret)
    return df.iloc[:-1] if len(df) > 1 else df

def in_kill_zone(ts):
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ny_time = ts.astimezone(ZoneInfo("America/New_York"))
    hour = ny_time.hour
    return (2 <= hour < 5) or (7 <= hour < 10)

def is_pivot_high(highs, idx, left, right):
    p = idx - right
    if p < left or idx >= len(highs):
        return False
    val = highs[p]
    return all(val >= highs[k] for k in range(p - left, p)) and all(val > highs[k] for k in range(p + 1, idx + 1))

def is_pivot_low(lows, idx, left, right):
    p = idx - right
    if p < left or idx >= len(lows):
        return False
    val = lows[p]
    return all(val <= lows[k] for k in range(p - left, p)) and all(val < lows[k] for k in range(p + 1, idx + 1))

# ==========================================
# 3. TEK ZAMAN DİLİMİ HAVUZ TARAMA MOTORU (4H & 1H)
# ==========================================
def scan_timeframe_pools(df, ext_len, int_len, d1_trend):
    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values

    son_tepe, son_tepe_idx, ust_sweep_ok = None, -1, False
    son_dip, son_dip_idx, alt_sweep_ok   = None, -1, False

    ic_tepe, ic_ust_sweep, ic_ust_broken = None, False, False
    ic_dip, ic_alt_sweep, ic_alt_broken  = None, False, False

    sweep_just_happened = False
    sweep_source_type   = ""
    sweep_source_price  = None

    for i in range(len(df)):
        prev_ust_sw    = ust_sweep_ok
        prev_alt_sw    = alt_sweep_ok
        prev_ic_ust_sw = ic_ust_sweep
        prev_ic_alt_sw = ic_alt_sweep

        # Dış (Ana) Likidite Havuzları
        if is_pivot_high(highs, i, ext_len, ext_len):
            son_tepe     = highs[i - ext_len]
            son_tepe_idx = i - ext_len
            ust_sweep_ok = False

        if is_pivot_low(lows, i, ext_len, ext_len):
            son_dip     = lows[i - ext_len]
            son_dip_idx = i - ext_len
            alt_sweep_ok = False

        lik_alindi_alt = son_dip is not None and lows[i] < son_dip and closes[i] > son_dip
        lik_alindi_ust = son_tepe is not None and highs[i] > son_tepe and closes[i] < son_tepe

        if lik_alindi_ust:
            ust_sweep_ok = True
        if lik_alindi_alt:
            alt_sweep_ok = True

        # İç Likidite Havuzları
        if is_pivot_high(highs, i, int_len, int_len):
            cand_h = highs[i - int_len]
            if son_tepe is None or cand_h < son_tepe:
                ic_tepe       = cand_h
                ic_ust_sweep  = False
                ic_ust_broken = False

        if is_pivot_low(lows, i, int_len, int_len):
            cand_l = lows[i - int_len]
            if son_dip is None or cand_l > son_dip:
                ic_dip        = cand_l
                ic_alt_sweep  = False
                ic_alt_broken = False

        ic_alindi_ust = ic_tepe is not None and not ic_ust_broken and highs[i] > ic_tepe and closes[i] < ic_tepe
        ic_alindi_alt = ic_dip is not None and not ic_alt_broken and lows[i] < ic_dip and closes[i] > ic_dip

        if ic_alindi_ust:
            ic_ust_sweep = True
        elif ic_tepe is not None and closes[i] > ic_tepe and not ic_ust_sweep:
            ic_ust_broken = True

        if ic_alindi_alt:
            ic_alt_sweep = True
        elif ic_dip is not None and closes[i] < ic_dip and not ic_alt_sweep:
            ic_alt_broken = True

        # Son kapanan mumda 1D yönüne uygun yeni bir likidite alımı oldu mu?
        if i == len(df) - 1:
            is_bearish = (d1_trend == -1) or (d1_trend == 0 and son_tepe_idx > son_dip_idx)
            if is_bearish:
                if lik_alindi_ust and not prev_ust_sw:
                    sweep_just_happened = True
                    sweep_source_type   = "Ana Üst Havuz"
                    sweep_source_price  = son_tepe
                elif ic_alindi_ust and not prev_ic_ust_sw:
                    sweep_just_happened = True
                    sweep_source_type   = "İç Likidite (IDM)"
                    sweep_source_price  = ic_tepe
            else:
                if lik_alindi_alt and not prev_alt_sw:
                    sweep_just_happened = True
                    sweep_source_type   = "Ana Alt Havuz"
                    sweep_source_price  = son_dip
                elif ic_alindi_alt and not prev_ic_alt_sw:
                    sweep_just_happened = True
                    sweep_source_type   = "İç Likidite (IDM)"
                    sweep_source_price  = ic_dip

    is_bearish   = (d1_trend == -1) or (d1_trend == 0 and son_tepe_idx > son_dip_idx)
    ext_pb_sweep = ust_sweep_ok if is_bearish else alt_sweep_ok
    int_pb_sweep = (ic_tepe is not None and ic_ust_sweep) if is_bearish else (ic_dip is not None and ic_alt_sweep)
    any_pb_sweep = ext_pb_sweep or int_pb_sweep
    target_price = son_dip if is_bearish else son_tepe

    return {
        "is_bearish": is_bearish,
        "sweep_just_happened": sweep_just_happened,
        "sweep_source_type": sweep_source_type,
        "sweep_source_price": sweep_source_price,
        "any_pb_sweep": any_pb_sweep,
        "ext_pb_sweep": ext_pb_sweep,
        "target_price": target_price,
        "last_ts": str(df.index[-1]),
    }

# ==========================================
# 4. ANA PARİTE TARAMA AKIŞI (1D -> 4H -> 1H)
# ==========================================
def analyze_symbol(name, ticker, state, status_report):
    df_1d = fetch_ohlc(ticker, period="1y", interval="1d")
    df_1h = fetch_ohlc(ticker, period="60d", interval="1h")

    if len(df_1d) < 20 or len(df_1h) < 45:
        print(f"{name}: Yetersiz veri (1D: {len(df_1d)}, 1H: {len(df_1h)})")
        status_report.append(f"• <b>{name}:</b> Veri alınamadı")
        return

    df_4h = df_1h.resample("4h").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last"
    }).dropna()

    if len(df_4h) < 25:
        return

    # --- A. 1D ANA YÖN HESABI ---
    d1_highs  = df_1d["High"].values
    d1_lows   = df_1d["Low"].values
    d1_closes = df_1d["Close"].values
    l_ph, l_pl, d1_trend = None, None, 0

    for i in range(len(df_1d)):
        if is_pivot_high(d1_highs, i, D1_SW_LEN, D1_SW_LEN):
            l_ph = d1_highs[i - D1_SW_LEN]
        if is_pivot_low(d1_lows, i, D1_SW_LEN, D1_SW_LEN):
            l_pl = d1_lows[i - D1_SW_LEN]
        if l_ph is not None and d1_closes[i] > l_ph:
            d1_trend = 1
        elif l_pl is not None and d1_closes[i] < l_pl:
            d1_trend = -1

    d1_yon_txt = "BULLISH (ALIŞ) 🟢" if d1_trend == 1 else "BEARISH (SATIŞ) 🔻" if d1_trend == -1 else "NÖTR (YATAY) ⚪"
    current_prc = fmt_price(name, df_1h["Close"].values[-1])
    status_report.append(f"• <b>{name}</b> ({current_prc}): {d1_yon_txt}")
    print(f"{name} tarandi -> 1D Yon: {d1_yon_txt}")

    # --- B. 4H VE 1H HAVUZ TARAMALARI ---
    res_4h = scan_timeframe_pools(df_4h, H4_EXT_LEN, H4_INT_LEN, d1_trend)
    res_1h = scan_timeframe_pools(df_1h, H1_EXT_LEN, H1_INT_LEN, d1_trend)

    # --- C. 1H MSS GİRİŞ KIRILIMI ---
    h1_highs  = df_1h["High"].values
    h1_lows   = df_1h["Low"].values
    h1_closes = df_1h["Close"].values
    rec_ph, rec_pl = None, None
    h1_bull_mss, h1_bear_mss = False, False

    for i in range(1, len(df_1h)):
        if is_pivot_high(h1_highs, i, H1_MSS_LEN, H1_MSS_LEN):
            rec_ph = h1_highs[i - H1_MSS_LEN]
        if is_pivot_low(h1_lows, i, H1_MSS_LEN, H1_MSS_LEN):
            rec_pl = h1_lows[i - H1_MSS_LEN]

        if i == len(df_1h) - 1:
            h1_bull_mss = rec_ph is not None and h1_closes[i] > rec_ph and h1_closes[i - 1] <= rec_ph
            h1_bear_mss = rec_pl is not None and h1_closes[i] < rec_pl and h1_closes[i - 1] >= rec_pl

    kz_ok = (not USE_KZ) or in_kill_zone(df_1h.index[-1])

    # --- D. 1. AŞAMA BİLDİRİMLERİ (4H veya 1H Likidite Temizlendi) ---
    for tf_name, res in [("4H", res_4h), ("1H", res_1h)]:
        st1_key = f"{name}_{tf_name}_STAGE1"
        if res["sweep_just_happened"] and state.get(st1_key) != res["last_ts"]:
            state[st1_key] = res["last_ts"]
            sw_prc = fmt_price(name, res["sweep_source_price"])
            msg1 = (
                f"🟡 <b>1. AŞAMA: {tf_name} LİKİDİTE TEMİZLENDİ ✓</b>\n"
                f"<b>Parite:</b> {name}\n"
                f"<b>1D Ana Yön:</b> {d1_yon_txt}\n"
                f"<b>Zaman Dilimi:</b> {tf_name}\n"
                f"<b>Alınan Havuz:</b> {res['sweep_source_type']} ({sw_prc})\n"
                f"<i>1H MSS kırılımı bekleniyor...</i>"
            )
            send_telegram(msg1)

    # --- E. 2. AŞAMA BİLDİRİMLERİ (TAM ONAY: 1D Yön + 4H/1H Sweep + 1H MSS) ---
    for tf_name, res in [("4H", res_4h), ("1H", res_1h)]:
        is_bear     = res["is_bearish"]
        ltf_mss_dir = h1_bear_mss if is_bear else h1_bull_mss
        st2_key     = f"{name}_{tf_name}_STAGE2"

        if res["any_pb_sweep"] and kz_ok and ltf_mss_dir and state.get(st2_key) != res_1h["last_ts"]:
            state[st2_key] = res_1h["last_ts"]
            aktif_havuz = f"{tf_name} Ana Havuz" if res["ext_pb_sweep"] else f"{tf_name} İç Likidite"
            hedef_fiyat = fmt_price(name, res["target_price"])
            islem_yonu  = "SAT (SHORT) 🔻" if is_bear else "AL (LONG) 🟢"
            msg2 = (
                f"🚀 <b>2. AŞAMA: TAM ONAY (GİRİŞ SİNYALİ)</b>\n"
                f"<b>Parite:</b> {name}\n"
                f"<b>1D Ana Yön:</b> {d1_yon_txt}\n"
                f"<b>İşlem Yönü:</b> {islem_yonu}\n"
                f"<b>Tetikleyici:</b> {aktif_havuz} Sweep + 1H MSS ✓\n"
                f"<b>Güncel Fiyat:</b> {current_prc}\n"
                f"<b>{tf_name} Hedef Havuz:</b> {hedef_fiyat}"
            )
            send_telegram(msg2)

if __name__ == "__main__":
    current_state = load_state()
    status_report = []

    for sym_name, yf_ticker in SYMBOLS.items():
        try:
            analyze_symbol(sym_name, yf_ticker, current_state, status_report)
        except Exception as e:
            print(f"{sym_name} taranirken hata olustu: {e}")
            status_report.append(f"• <b>{sym_name}:</b> Tarama hatası")

    save_state(current_state)

    # Manuel olarak "Run workflow" basıldığında Telegram'a test & durum özeti atar
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        ozet = "\n".join(status_report)
        test_msg = (
            f"✅ <b>SMC LİKİDİTE BOTU AKTİF (TEST MESAJI)</b>\n"
            f"<i>Telegram bağlantısı doğrulandı. 10 dakikada bir tarama aktif.</i>\n\n"
            f"📊 <b>Güncel 1D Ana Yön Özeti:</b>\n{ozet}"
        )
        send_telegram(test_msg)
