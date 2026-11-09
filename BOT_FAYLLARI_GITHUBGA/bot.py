import urllib.request
import json
import time
import datetime
import os
import sys
import sqlite3
import http.server
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows UTF-8 console fix
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ==================== SOZLAMALAR ====================
TELEGRAM_BOT_TOKEN = "8804584400:AAETn8nmY1t8Q3mO1Wb4n1IIYaYN6DK5EH4"
TELEGRAM_CHAT_ID   = "8185913138"

INTERVAL = "2h"           # 2 soatlik taymfreym
FIBO_LOOKBACK = 80        # Oxirgi 80 ta yopilgan sham
CHECK_DELAY = 60          # Har 60 soniyada bozorni tekshirish
COOLDOWN_SECONDS = 7200   # Bir xil koin uchun 2 soat ichida qayta signal yubormaslik
MAX_WORKERS = 25          # Parallel tekshirish oqimlari

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYMBOLS_FILE = os.path.join(BASE_DIR, "all_valid_symbols.json")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
DB_PATH = os.path.join(BASE_DIR, "trades.db")

last_alerts = {}
bot_start_time = time.time()

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        uptime_mins = int((time.time() - bot_start_time) // 60)
        html = f"""<!DOCTYPE html>
<html>
<head><title>Antagravity Bot Status</title><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; padding: 25px; background: #0f172a; color: #f8fafc;">
    <h2>🚀 Antagravity Crypto Signal Bot (24/7)</h2>
    <p><b>Holat:</b> <span style="color: #4ade80; font-weight: bold;">ONLINE (FAOL)</span></p>
    <p><b>Kuzatilayotgan koinlar:</b> 279 ta (Binance, Gate.io, MEXC)</p>
    <p><b>Taymfreym:</b> 2h</p>
    <p><b>Strategiya:</b> RSI Div + MACD + Fibo 0.786</p>
    <p><b>Uptime:</b> {uptime_mins} daqiqa</p>
    <p><i>Server sog'lom va 24/7 rejimda ishlamoqda.</i></p>
</body>
</html>"""
        self.wfile.write(html.encode('utf-8'))
        
    def log_message(self, format, *args):
        pass

def start_http_server():
    try:
        port = int(os.environ.get("PORT", 8080))
        server = http.server.HTTPServer(('0.0.0.0', port), HealthHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        log(f"🌐 Cloud Health HTTP server ishga tushdi: port {port}")
    except Exception as e:
        log(f"HTTP server xatosi: {e}")

# ==================== LOG VA BAZA FUNKSIYALARI ====================
def log(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        market TEXT DEFAULT 'spot',
        entry_time TEXT NOT NULL,
        entry_price REAL NOT NULL,
        sl_price REAL NOT NULL,
        tp1_price REAL NOT NULL,
        tp2_price REAL NOT NULL,
        risk_pct REAL NOT NULL,
        reward1_pct REAL NOT NULL,
        reward2_pct REAL NOT NULL,
        swing_low REAL,
        swing_high REAL,
        status TEXT DEFAULT 'OPEN', -- 'OPEN', 'TP_HIT', 'SL_HIT'
        highest_reached REAL,
        lowest_reached REAL,
        max_gain_pct REAL DEFAULT 0.0,
        max_loss_pct REAL DEFAULT 0.0,
        exit_time TEXT,
        exit_price REAL,
        final_pnl_pct REAL DEFAULT 0.0,
        result TEXT DEFAULT 'PENDING' -- 'WIN', 'LOSS', 'PENDING'
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_type TEXT NOT NULL, -- 'DAILY', 'WEEKLY', 'MONTHLY'
        period_key TEXT NOT NULL,
        sent_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()

def get_tashkent_now():
    # UTC+5
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))

def load_symbols():
    try:
        with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Simvollarni yuklashda xatolik: {e}")
        return []

# ==================== TELEGRAMGA XABAR YUBORISH ====================
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return True
    except Exception as e:
        log(f"Telegramga yuborishda xatolik: {e}")
        return False

# ==================== BOZOR MA'LUMOTLARINI OLISH (BINANCE, GATE.IO, MEXC) ====================
def get_klines(item, interval="2h", limit=250):
    ex = item.get('exchange', 'binance')
    qsym = item.get('query_symbol', item['symbol'])
    
    try:
        if ex == 'binance':
            url = f"https://data-api.binance.vision/api/v3/klines?symbol={qsym}&interval={interval}&limit={limit}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                closed = data[:-1]
                if len(closed) < 80:
                    return None
                opens  = [float(c[1]) for c in closed]
                highs  = [float(c[2]) for c in closed]
                lows   = [float(c[3]) for c in closed]
                closes = [float(c[4]) for c in closed]
                return opens, highs, lows, closes
                
        elif ex == 'gateio':
            url = f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={qsym}&interval=2h&limit={limit}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                closed = [k for k in data if k[7] == 'true']
                if not closed:
                    closed = data[:-1]
                if len(closed) < 80:
                    return None
                # Gate: [t, quote_vol, close, high, low, open, ...]
                opens  = [float(k[5]) for k in closed]
                highs  = [float(k[3]) for k in closed]
                lows   = [float(k[4]) for k in closed]
                closes = [float(k[2]) for k in closed]
                return opens, highs, lows, closes
                
        elif ex == 'mexc':
            limit_60m = min(limit * 2, 500)
            url = f"https://api.mexc.co/api/v3/klines?symbol={qsym}&interval=60m&limit={limit_60m}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                closed_60m = data[:-1]
                opens, highs, lows, closes = [], [], [], []
                n = len(closed_60m)
                start_idx = n % 2
                for i in range(start_idx, n, 2):
                    c1 = closed_60m[i]
                    c2 = closed_60m[i+1]
                    o = float(c1[1])
                    h = max(float(c1[2]), float(c2[2]))
                    l = min(float(c1[3]), float(c2[3]))
                    c = float(c2[4])
                    opens.append(o)
                    highs.append(h)
                    lows.append(l)
                    closes.append(c)
                if len(closes) < 80:
                    return None
                return opens, highs, lows, closes
                
    except Exception:
        return None

def get_current_ticker(item):
    ex = item.get('exchange', 'binance')
    qsym = item.get('query_symbol', item['symbol'])
    try:
        if ex == 'binance':
            url = f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={qsym}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read().decode('utf-8'))
                return float(d['lastPrice']), float(d['highPrice']), float(d['lowPrice'])
        elif ex == 'gateio':
            url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={qsym}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read().decode('utf-8'))[0]
                return float(d['last']), float(d['high_24h']), float(d['low_24h'])
        elif ex == 'mexc':
            url = f"https://api.mexc.co/api/v3/ticker/24hr?symbol={qsym}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read().decode('utf-8'))
                return float(d['lastPrice']), float(d['highPrice']), float(d['lowPrice'])
    except Exception:
        return None, None, None

# ==================== INDIKATORLAR ====================
def calculate_tv_rsi(closes, length=14):
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    
    alpha = 1.0 / length
    avg_gain = [sum(gains[:length]) / length]
    avg_loss = [sum(losses[:length]) / length]
    
    for i in range(length, len(deltas)):
        avg_gain.append(alpha * gains[i] + (1 - alpha) * avg_gain[-1])
        avg_loss.append(alpha * losses[i] + (1 - alpha) * avg_loss[-1])
        
    rsi = [50.0] * length
    for g, l in zip(avg_gain, avg_loss):
        if l == 0:
            rsi.append(100.0)
        else:
            rs = g / l
            rsi.append(100.0 - (100.0 / (1.0 + rs)))
    return rsi

def calculate_ema(data, span):
    if not data:
        return []
    alpha = 2.0 / (span + 1.0)
    ema = [data[0]]
    for val in data[1:]:
        ema.append(alpha * val + (1.0 - alpha) * ema[-1])
    return ema

def calculate_macd(closes, fast=12, slow=26, signal_span=9):
    if len(closes) < slow + signal_span:
        return [], []
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = calculate_ema(macd_line, signal_span)
    return macd_line, signal_line

def check_bullish_divergence(lows, rsi, window=40):
    recent_lows = lows[-window:]
    recent_rsi = rsi[-window:]
    pivots = []
    for i in range(2, len(recent_lows) - 1):
        if recent_lows[i] <= recent_lows[i-1] and recent_lows[i] <= recent_lows[i-2] and \
           recent_lows[i] <= recent_lows[i+1]:
            pivots.append((i, recent_lows[i], recent_rsi[i]))
            
    if len(pivots) >= 2:
        p_prev = pivots[-2]
        p_curr = pivots[-1]
        price_lower = p_curr[1] <= p_prev[1] * 1.008
        rsi_higher  = p_curr[2] > p_prev[2]
        rsi_in_zone = p_curr[2] < 52
        
        if price_lower and rsi_higher and rsi_in_zone:
            return True, p_prev[1], p_curr[1], p_prev[2], p_curr[2]
    return False, 0, 0, 0, 0

def check_fibo_chronological(highs, lows, closes, opens, lookback=80):
    sub_highs = highs[-lookback:]
    sub_lows  = lows[-lookback:]
    
    min_low = min(sub_lows)
    low_idx = sub_lows.index(min_low)
    
    highs_after_low = sub_highs[low_idx:]
    if len(highs_after_low) < 5:
        return False, 0.0, 0.0, 0.0, 0, 0
        
    max_high = max(highs_after_low)
    high_idx = low_idx + highs_after_low.index(max_high)
    
    bars_ago_high = lookback - high_idx
    bars_ago_low  = lookback - low_idx
    
    if bars_ago_high < 2 or bars_ago_high > 35:
        return False, 0.0, 0.0, 0.0, 0, 0
        
    fib_range = max_high - min_low
    if fib_range <= 0 or (fib_range / max_high < 0.015):
        return False, 0.0, 0.0, 0.0, 0, 0
        
    fibo_0786 = max_high - (0.786 * fib_range)
    dist_pct = abs(closes[-1] - fibo_0786) / fibo_0786 * 100.0
    
    came_from_below = (lows[-1] <= fibo_0786 * 1.003 or closes[-2] < fibo_0786) and \
                      (closes[-1] >= fibo_0786 * 0.990) and \
                      (closes[-1] >= opens[-1] or closes[-1] > closes[-2])
                      
    if came_from_below and dist_pct <= 1.0:
        return True, fibo_0786, max_high, min_low, bars_ago_low, bars_ago_high
        
    return False, 0.0, max_high, min_low, bars_ago_low, bars_ago_high

# ==================== TP VA SL HISOB-KITOBLARI ====================
def calculate_trade_levels(entry_price, s_low, s_high):
    raw_sl = s_low * 0.995
    risk_diff = entry_price - raw_sl
    risk_pct = (risk_diff / entry_price) * 100.0
    
    if risk_pct < 1.0:
        risk_pct = 1.5
        sl_price = entry_price * (1 - risk_pct / 100.0)
        risk_diff = entry_price - sl_price
    elif risk_pct > 7.0:
        risk_pct = 5.0
        sl_price = entry_price * (1 - risk_pct / 100.0)
        risk_diff = entry_price - sl_price
    else:
        sl_price = raw_sl
        
    tp1_price = entry_price + (1.5 * risk_diff)
    reward1_pct = risk_pct * 1.5
    
    tp2_price = entry_price + (2.0 * risk_diff)
    reward2_pct = risk_pct * 2.0
    
    return sl_price, tp1_price, tp2_price, risk_pct, reward1_pct, reward2_pct

# ==================== YANGI SIGNAL KELGANDA JURNALGA YOZISH ====================
def register_new_signal(symbol, exchange, entry_price, rsi_val, fib_val, p_prev_l, p_curr_l, p_prev_r, p_curr_r, s_high, s_low, bars_low, bars_high):
    sl_price, tp1_price, tp2_price, risk_pct, reward1_pct, reward2_pct = calculate_trade_levels(entry_price, s_low, s_high)
    now_str = get_tashkent_now().strftime("%Y-%m-%d %H:%M:%S")
    market_str = exchange.upper()
    
    # Bazaga saqlash
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO trades (
        symbol, market, entry_time, entry_price, sl_price, tp1_price, tp2_price,
        risk_pct, reward1_pct, reward2_pct, swing_low, swing_high, status,
        highest_reached, lowest_reached, result
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 'PENDING')
    """, (
        symbol, market_str, now_str, entry_price, sl_price, tp1_price, tp2_price,
        risk_pct, reward1_pct, reward2_pct, s_low, s_high, entry_price, entry_price
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    
    log(f"📝 JURNALGA KIRITILDI [#{trade_id}]: {symbol} ({market_str}) | Entry={entry_price} | SL={sl_price:.4f} (-{risk_pct:.2f}%) | TP={tp2_price:.4f} (+{reward2_pct:.2f}%)")
    
    # Telegramga to'liq signal xabari
    msg = (
        f"🚨 <b>ТРЕЙДИНГ СИГНАЛ #{trade_id} (Antagravity)</b>\n\n"
        f"📈 <b>Актив:</b> #{symbol} (<b>{market_str}</b>)\n"
        f"⏱ <b>Таймфрейм:</b> {INTERVAL} (yopilgan sham bo'yicha)\n"
        f"💲 <b>Кириш нархи (Entry):</b> <code>{entry_price}</code>\n\n"
        f"🎯 <b>Take Profit (1:2 R:R):</b> <code>{tp2_price:.4f}</code> (<b>+{reward2_pct:.2f}%</b>) 🟢\n"
        f"🛑 <b>Stop Loss (SL):</b> <code>{sl_price:.4f}</code> (<b>-{risk_pct:.2f}%</b>) 🔴\n"
        f"⚖️ <b>Risk / Reward:</b> 1 : 2.0\n\n"
        f"📊 <b>Техник таҳлил шартлари:</b>\n"
        f"• <b>RSI Дивергенция (Bullish):</b> ✅\n"
        f"   └ <i>Нарх:</i> {p_prev_l:.4f} ➔ {p_curr_l:.4f}\n"
        f"   └ <i>RSI ({rsi_val:.1f}):</i> {p_prev_r:.1f} ➔ {p_curr_r:.1f}\n"
        f"• <b>MACD:</b> Bullish kesishuv ✅\n"
        f"• <b>Фибоначчи 0.786 (Low ➔ High):</b> ✅\n"
        f"   └ <i>Low:</i> {s_low:.4f} ({bars_low} sham oldin)\n"
        f"   └ <i>High:</i> {s_high:.4f} ({bars_high} sham oldin)\n"
        f"   └ <i>0.786 darajasi:</i> {fib_val:.4f}\n\n"
        f"📋 <i>Сигнал журналга киритилди ва 24/7 назоратга олинди!</i>"
    )
    send_telegram(msg)

# ==================== OCHIQ SAVDOLARNI TEKSHIRISH (TP/SL NAZORATI) ====================
def monitor_open_trades():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    open_trades = cur.fetchall()
    conn.close()
    
    if not open_trades:
        return
        
    symbols_list = load_symbols()
    sym_map = {s['symbol']: s for s in symbols_list}
    
    for t in open_trades:
        trade_id = t['id']
        symbol = t['symbol']
        market_str = t['market'] or 'BINANCE'
        entry_p = t['entry_price']
        sl_p = t['sl_price']
        tp_p = t['tp2_price']
        risk_pct = t['risk_pct']
        reward_pct = t['reward2_pct']
        entry_time_str = t['entry_time']
        
        # Simvol obyektini olish
        item = sym_map.get(symbol, {
            'symbol': symbol,
            'exchange': market_str.lower(),
            'query_symbol': symbol
        })
        
        last_p, curr_high, curr_low = get_current_ticker(item)
        if not last_p:
            continue
            
        now_dt = get_tashkent_now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            entry_dt = datetime.datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
            duration = now_dt - entry_dt
            dur_hours = int(duration.total_seconds() // 3600)
            dur_mins = int((duration.total_seconds() % 3600) // 60)
            dur_str = f"{dur_hours} soat {dur_mins} daqiqa"
        except Exception:
            dur_str = "Noma'lum"
            
        new_highest = max(t['highest_reached'] or entry_p, last_p, curr_high or last_p)
        new_lowest = min(t['lowest_reached'] or entry_p, last_p, curr_low or last_p)
        max_gain = ((new_highest - entry_p) / entry_p) * 100.0
        max_loss = ((new_lowest - entry_p) / entry_p) * 100.0
        
        # 1. TAKE PROFIT URILDIMI?
        if (curr_high and curr_high >= tp_p) or (last_p >= tp_p):
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
            UPDATE trades SET 
                status = 'TP_HIT', result = 'WIN', exit_time = ?, 
                exit_price = ?, final_pnl_pct = ?, highest_reached = ?, 
                lowest_reached = ?, max_gain_pct = ?, max_loss_pct = ?
            WHERE id = ?
            """, (now_str, tp_p, reward_pct, new_highest, new_lowest, max_gain, max_loss, trade_id))
            conn.commit()
            conn.close()
            
            log(f"🎯 WIN: #{trade_id} {symbol} ({market_str}) TAKE PROFIT urildi! (+{reward_pct:.2f}%)")
            
            msg = (
                f"🎯 <b>TAKE PROFIT URILDI! (+{reward_pct:.2f}%)</b> 🎉\n\n"
                f"📈 <b>Актив:</b> #{symbol} ({market_str} - Сигнал #{trade_id})\n"
                f"💲 <b>Кириш нархи:</b> {entry_p}\n"
                f"🏁 <b>Чиқиш нархи:</b> {tp_p:.4f}\n"
                f"💰 <b>Соф фойда:</b> <b>+{reward_pct:.2f}%</b> (R:R 1:2.0)\n"
                f"⏱ <b>Савдо давомийлиги:</b> {dur_str}\n"
                f"✅ <b>Натижа:</b> ҒАЛАБА (WIN) 🏆"
            )
            send_telegram(msg)
            continue
            
        # 2. STOP LOSS URILDIMI?
        elif (curr_low and curr_low <= sl_p) or (last_p <= sl_p):
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
            UPDATE trades SET 
                status = 'SL_HIT', result = 'LOSS', exit_time = ?, 
                exit_price = ?, final_pnl_pct = ?, highest_reached = ?, 
                lowest_reached = ?, max_gain_pct = ?, max_loss_pct = ?
            WHERE id = ?
            """, (now_str, sl_p, -risk_pct, new_highest, new_lowest, max_gain, max_loss, trade_id))
            conn.commit()
            conn.close()
            
            log(f"🛑 LOSS: #{trade_id} {symbol} ({market_str}) STOP LOSS urildi! (-{risk_pct:.2f}%)")
            
            msg = (
                f"🛑 <b>STOP LOSS URILDI! (-{risk_pct:.2f}%)</b> ⚠️\n\n"
                f"📉 <b>Актив:</b> #{symbol} ({market_str} - Сигнал #{trade_id})\n"
                f"💲 <b>Кириш нархи:</b> {entry_p}\n"
                f"🏁 <b>Чиқиш нархи:</b> {sl_p:.4f}\n"
                f"⚠️ <b>Зарар:</b> <b>-{risk_pct:.2f}%</b>\n"
                f"⏱ <b>Савдо давомийлиги:</b> {dur_str}\n"
                f"❌ <b>Натижа:</b> МАҒЛУБИЯТ (LOSS)"
            )
            send_telegram(msg)
            continue
            
        else:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
            UPDATE trades SET 
                highest_reached = ?, lowest_reached = ?, 
                max_gain_pct = ?, max_loss_pct = ?
            WHERE id = ?
            """, (new_highest, new_lowest, max_gain, max_loss, trade_id))
            conn.commit()
            conn.close()

# ==================== AVTOMATIK DAVRIY HISOBOTLAR ====================
def check_and_send_scheduled_reports():
    now_uz = get_tashkent_now()
    today_str = now_uz.strftime("%Y-%m-%d")
    hour = now_uz.hour
    minute = now_uz.minute
    weekday = now_uz.weekday() # 0=Du, 6=Yak
    
    conn = get_db()
    cur = conn.cursor()
    
    # ---------------- 1. KUNLIK HISOBOT (Har kuni soat 23:00 da) ----------------
    if hour == 23 and minute >= 0:
        cur.execute("SELECT id FROM reports WHERE report_type = 'DAILY' AND period_key = ?", (today_str,))
        if not cur.fetchone():
            send_daily_report(today_str)
            cur.execute("INSERT INTO reports (report_type, period_key, sent_at) VALUES ('DAILY', ?, ?)", 
                        (today_str, now_uz.strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            
    # ---------------- 2. HAFTALIK HISOBOT (Har Yakshanba soat 23:05 da) ----------------
    if weekday == 6 and hour == 23 and minute >= 5:
        week_key = now_uz.strftime("%Y-W%W")
        cur.execute("SELECT id FROM reports WHERE report_type = 'WEEKLY' AND period_key = ?", (week_key,))
        if not cur.fetchone():
            send_weekly_report(week_key)
            cur.execute("INSERT INTO reports (report_type, period_key, sent_at) VALUES ('WEEKLY', ?, ?)", 
                        (week_key, now_uz.strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            
    # ---------------- 3. OYLIK HISOBOT (Har oyning 1-sanasida soat 00:05 da) ----------------
    if now_uz.day == 1 and hour == 0 and minute >= 5:
        prev_month = (now_uz.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        cur.execute("SELECT id FROM reports WHERE report_type = 'MONTHLY' AND period_key = ?", (prev_month,))
        if not cur.fetchone():
            send_monthly_report(prev_month)
            cur.execute("INSERT INTO reports (report_type, period_key, sent_at) VALUES ('MONTHLY', ?, ?)", 
                        (prev_month, now_uz.strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            
    conn.close()

def send_daily_report(date_str):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM trades WHERE entry_time LIKE ? OR exit_time LIKE ?", (f"{date_str}%", f"{date_str}%"))
    today_trades = cur.fetchall()
    
    cur.execute("SELECT * FROM trades WHERE exit_time LIKE ? AND result = 'WIN'", (f"{date_str}%",))
    wins = cur.fetchall()
    
    cur.execute("SELECT * FROM trades WHERE exit_time LIKE ? AND result = 'LOSS'", (f"{date_str}%",))
    losses = cur.fetchall()
    
    cur.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    open_trades = cur.fetchall()
    
    cur.execute("SELECT COUNT(*), SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END), SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END), SUM(final_pnl_pct) FROM trades WHERE result != 'PENDING'")
    total_closed, total_wins, total_losses, total_pnl = cur.fetchone()
    conn.close()
    
    total_closed = total_closed or 0
    total_wins = total_wins or 0
    total_losses = total_losses or 0
    total_pnl = total_pnl or 0.0
    all_time_winrate = (total_wins / total_closed * 100.0) if total_closed > 0 else 0.0
    
    today_closed = len(wins) + len(losses)
    today_winrate = (len(wins) / today_closed * 100.0) if today_closed > 0 else 0.0
    today_pnl = sum([t['final_pnl_pct'] for t in wins]) + sum([t['final_pnl_pct'] for t in losses])
    
    trade_lines = []
    for t in wins:
        trade_lines.append(f"• #{t['symbol']} ({t['market']}) ➔ <b>TP URILDI (+{t['final_pnl_pct']:.2f}%)</b> ✅")
    for t in losses:
        trade_lines.append(f"• #{t['symbol']} ({t['market']}) ➔ <b>SL URILDI ({t['final_pnl_pct']:.2f}%)</b> ❌")
    for t in open_trades:
        trade_lines.append(f"• #{t['symbol']} ({t['market']}) ➔ <i>Hozir ochiq (Max: +{t['max_gain_pct']:.1f}%)</i> ⏳")
        
    trades_block = "\n".join(trade_lines) if trade_lines else "<i>Bugun yangi yopilgan savdo bo'lmadi.</i>"
    
    msg = (
        f"📊 <b>КУНЛИК САВДО ҲИСОБОТИ ({date_str})</b>\n\n"
        f"📅 <b>Бугунги кўрсаткичлар:</b>\n"
        f"• <b>Янги сигналлар:</b> {len(today_trades)} та\n"
        f"• <b>Ютуқ (TP):</b> {len(wins)} та ✅\n"
        f"• <b>Зарар (SL):</b> {len(losses)} та ❌\n"
        f"• <b>Ҳозирда очиқ:</b> {len(open_trades)} та ⏳\n"
        f"• <b>Бугунги Win Rate:</b> <b>{today_winrate:.1f}%</b>\n"
        f"• <b>Бугунги соф PnL:</b> <b>{today_pnl:+.2f}%</b>\n\n"
        f"📋 <b>Савдолар ҳаракати:</b>\n{trades_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>УМУМИЙ ЖАМҒАРМА (ALL-TIME):</b>\n"
        f"• Жами ёпилган савдолар: <b>{total_closed} та</b>\n"
        f"• Умумий Win Rate: <b>{all_time_winrate:.1f}%</b>\n"
        f"• Умумий соф фойда: <b>{total_pnl:+.2f}%</b>"
    )
    send_telegram(msg)
    log(f"Кунлик ҳисобот юборилди: {date_str} (WinRate: {today_winrate:.1f}%, PnL: {today_pnl:+.2f}%)")

def send_weekly_report(week_key):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE datetime(exit_time) >= datetime('now', '-7 days') AND result != 'PENDING'")
    closed = cur.fetchall()
    conn.close()
    
    total = len(closed)
    wins = [t for t in closed if t['result'] == 'WIN']
    losses = [t for t in closed if t['result'] == 'LOSS']
    winrate = (len(wins) / total * 100.0) if total > 0 else 0.0
    pnl = sum([t['final_pnl_pct'] for t in closed])
    
    best = max(closed, key=lambda x: x['final_pnl_pct']) if closed else None
    worst = min(closed, key=lambda x: x['final_pnl_pct']) if closed else None
    
    best_str = f"#{best['symbol']} (+{best['final_pnl_pct']:.2f}%)" if best else "Yo'q"
    worst_str = f"#{worst['symbol']} ({worst['final_pnl_pct']:.2f}%)" if worst else "Yo'q"
    
    msg = (
        f"👑 <b>ҲАФТАЛИК СИГНАЛЛАР ҲИСОБОТИ ({week_key})</b>\n\n"
        f"📅 <b>Охирги 7 кунлик натижалар:</b>\n"
        f"• <b>Жами ёпилган савдоlar:</b> {total} та\n"
        f"• <b>Take Profit (Ғалаба):</b> {len(wins)} та ✅\n"
        f"• <b>Stop Loss (Мағлубият):</b> {len(losses)} та ❌\n\n"
        f"🎯 <b>Ҳафталик Win Rate:</b> <b>{winrate:.1f}%</b>\n"
        f"💰 <b>Ҳафталик соф PnL:</b> <b>{pnl:+.2f}%</b> 🚀\n\n"
        f"🌟 <b>Энг яхши савдо:</b> {best_str}\n"
        f"📉 <b>Энг катта зарар:</b> {worst_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Ҳафталик мониторинг муваффақиятли якунланди!</i>"
    )
    send_telegram(msg)
    log(f"Ҳафталик ҳисобот юборилди: {week_key}")

def send_monthly_report(month_str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE exit_time LIKE ? AND result != 'PENDING'", (f"{month_str}%",))
    closed = cur.fetchall()
    conn.close()
    
    total = len(closed)
    wins = [t for t in closed if t['result'] == 'WIN']
    losses = [t for t in closed if t['result'] == 'LOSS']
    winrate = (len(wins) / total * 100.0) if total > 0 else 0.0
    pnl = sum([t['final_pnl_pct'] for t in closed])
    
    msg = (
        f"🏛 <b>ОЙЛИК БОШ ҲИСОБОТ ({month_str})</b>\n\n"
        f"📅 <b>30 кунлик тўлиқ аудит:</b>\n"
        f"• <b>Жами савдолар:</b> {total} та\n"
        f"• <b>Ютуқли (TP):</b> {len(wins)} та ✅\n"
        f"• <b>Зарарли (SL):</b> {len(losses)} та ❌\n\n"
        f"🏆 <b>ОЙЛИК WIN RATE:</b> <b>{winrate:.1f}%</b>\n"
        f"💎 <b>ОЙЛИК СОФ ФОЙДА (PnL):</b> <b>{pnl:+.2f}%</b> 🚀\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Барча маълумотлар базада сақланмоқда!</i>"
    )
    send_telegram(msg)
    log(f"Ойлик ҳисобот юборилди: {month_str}")

# ==================== BOZORNI SKAN QILISH ====================
def analyze_symbol(item):
    symbol = item["symbol"]
    exchange = item.get("exchange", "binance")
    
    data = get_klines(item, INTERVAL, 250)
    if not data:
        return None
    opens, highs, lows, closes = data
    
    # 1. MACD Bullish kesishuv
    macd_line, signal_line = calculate_macd(closes)
    if not macd_line or len(macd_line) < 3:
        return None
    macd_bull = macd_line[-1] > signal_line[-1] and (
        macd_line[-2] <= signal_line[-2] or (len(macd_line) >= 3 and macd_line[-3] <= signal_line[-3])
    )
    if not macd_bull:
        return None
        
    # 2. RSI Bullish Divergensiya (TradingView Wilder's RMA)
    rsi_vals = calculate_tv_rsi(closes)
    if not rsi_vals or len(rsi_vals) < 40 or rsi_vals[-1] >= 52:
        return None
    is_div, p_prev_l, p_curr_l, p_prev_r, p_curr_r = check_bullish_divergence(lows, rsi_vals, window=40)
    if not is_div:
        return None
        
    # 3. Aniq Fibo 0.786 (Low ➔ High, yangi to'lqin)
    is_fibo, fib_lvl, s_high, s_low, bars_low, bars_high = check_fibo_chronological(highs, lows, closes, opens, FIBO_LOOKBACK)
    if not is_fibo:
        return None
        
    return {
        "symbol": symbol,
        "exchange": exchange,
        "price": closes[-1],
        "rsi": rsi_vals[-1],
        "fib": fib_lvl,
        "s_high": s_high,
        "s_low": s_low,
        "bars_low": bars_low,
        "bars_high": bars_high,
        "p_prev_l": p_prev_l,
        "p_curr_l": p_curr_l,
        "p_prev_r": p_prev_r,
        "p_curr_r": p_curr_r
    }

def scan_all():
    symbols_list = load_symbols()
    if not symbols_list:
        return
        
    now = time.time()
    signals_found = 0
    start_t = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {executor.submit(analyze_symbol, item): item for item in symbols_list}
        for future in as_completed(future_to_item):
            try:
                res = future.result()
                if res:
                    sym = res["symbol"]
                    last_t = last_alerts.get(sym, 0)
                    if now - last_t >= COOLDOWN_SECONDS:
                        signals_found += 1
                        register_new_signal(
                            sym, res["exchange"], res["price"], res["rsi"], res["fib"],
                            res["p_prev_l"], res["p_curr_l"], res["p_prev_r"], res["p_curr_r"],
                            res["s_high"], res["s_low"], res["bars_low"], res["bars_high"]
                        )
                        last_alerts[sym] = now
            except Exception:
                pass
                
    duration = time.time() - start_t
    log(f"Skan yakunlandi ({len(symbols_list)} ta koin {INTERVAL} taymfreymda {duration:.1f}s da tekshirildi, {signals_found} ta yangi signal).")

# ==================== ASOSIY SIKL ====================
def main():
    init_db()
    start_http_server()
    symbols_list = load_symbols()
    by_ex = {}
    for s in symbols_list:
        by_ex[s.get('exchange', 'binance')] = by_ex.get(s.get('exchange', 'binance'), 0) + 1
        
    log("🚀 Antagravity Multi-Market Crypto Signal & Win-Rate Journal Bot ishga tushdi!")
    log(f"Jami kuzatilayotgan koinlar: {len(symbols_list)} ta (Binance: {by_ex.get('binance',0)}, Gate.io: {by_ex.get('gateio',0)}, MEXC Alpha: {by_ex.get('mexc',0)})")
    log(f"Taymfreym: {INTERVAL} | Fibo davri: oxirgi {FIBO_LOOKBACK} ta sham | Oraliq tekshirish: {CHECK_DELAY}s")
    
    now_str = get_tashkent_now().strftime("%Y-%m-%d %H:%M:%S")
    start_msg = (
        f"🤖 <b>Antagravity Multi-Market Savdo Jurnali va Bot Ishga Tushdi!</b>\n\n"
        f"⏱ <b>Boshlanish vaqti:</b> {now_str}\n"
        f"📊 <b>Kuzatuvda:</b> <b>{len(symbols_list)} ta koin (100% to'liq ro'yxat)</b>\n"
        f"   └ <i>Binance:</i> {by_ex.get('binance',0)} ta\n"
        f"   └ <i>Gate.io:</i> {by_ex.get('gateio',0)} ta\n"
        f"   └ <i>MEXC (Alpha):</i> {by_ex.get('mexc',0)} ta\n\n"
        f"🎯 <b>Strategiya:</b> RSI Bullish Div + MACD + Fibo 0.786 ({INTERVAL})\n"
        f"⚖️ <b>Boshqaruv:</b> Avtomatik TP (1:2 R:R) va SL monitoringi\n"
        f"📈 <b>Hisobotlar:</b> Kunlik (23:00), Haftalik (Yakshanba 23:05), Oylik\n\n"
        f"<i>Barcha 278 ta koin bo'yicha signallar 24/7 qamrab olindi!</i>"
    )
    send_telegram(start_msg)
    
    while True:
        try:
            # 1. Yangi signallarni skan qilish
            scan_all()
            
            # 2. Ochiq savdolarni tekshirish (TP yoki SL urildimi?)
            monitor_open_trades()
            
            # 3. Kunlik / Haftalik / Oylik hisobot vaqti kelganini tekshirish
            check_and_send_scheduled_reports()
            
        except Exception as e:
            log(f"Asosiy siklda xatolik: {e}")
            
        time.sleep(CHECK_DELAY)

if __name__ == "__main__":
    main()
