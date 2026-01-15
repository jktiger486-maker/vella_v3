entryPrice: float | None = None

    remainingQty: float = 0.0

    slPrice: float | None = None

    tp1Filled: bool = False

    trailingActive: bool = False

    stopLine: float | None = None

    lastEntryCandleId: int | None = None

    regime: str = "OFF"

    exitReason: str | None = None





# ====================
# ============================================================

# [STATE IO]

# ============================================================

def load_state(cfg: CFG):

    if not os.path.exists(FIXED.STATE_FILE):

        return State()

    try:

        s = State(**json.load(open(FIXED.STATE_FILE)))

        if s.hasPosition and s.entryPrice:

            # CORE: reboot resume 시 slPrice 즉시 재계산

            s.slPrice = s.entryPrice * (1 + cfg.slPct / 100)

        return s

    except Exception as e:

        print("STATE_LOAD_FAIL:", e)

        return State()





def save_state_atomic(s: State):

    with tempfile.NamedTemporaryFile("w", delete=False) as f:

        json.dump(s.__dict__, f, indent=2)

        tmp = f.name

    os.replace(tmp, FIXED.STATE_FILE)





def reset_state(reason: str, last_candle_id=None):

    s = State(

        hasPosition=False,

        positionSide="SHORT",

        entryPrice=None,

        remainingQty=0.0,

        slPrice=None,

        tp1Filled=False,

        trailingActive=False,

        stopLine=None,

        lastEntryCandleId=last_candle_id,

        regime="OFF",

        exitReason=reason,

    )

    save_state_atomic(s)

    return s

# ============================================================

# [BINANCE REST]

# ============================================================

def klines(symbol, tf):

    try:

        r = requests.get(

            f"{FIXED.SPOT}/api/v3/klines",

            params=dict(symbol=symbol, interval=tf, limit=FIXED.KEEP),

            timeout=5,

        )

        r.raise_for_status()

        d = r.json()

        return d if len(d) >= 25 else None

    except Exception:

        return None


# ============================================================

# [BTC DAILY OPEN — 1D / KST 09:00 FIX]

# ============================================================

btc_daily_open_cache = None

btc_daily_open_anchor = None





def btc_daily_open_1d_cached(symbol):

    global btc_daily_open_cache, btc_daily_open_anchor

    now = datetime.now(timezone(timedelta(hours=9)))

    anchor = now.replace(hour=9, minute=0, second=0, microsecond=0)

    if now < anchor:

        anchor -= timedelta(days=1)



    if btc_daily_open_cache is None or btc_daily_open_anchor != anchor:

        r = requests.get(

            f"{FIXED.SPOT}/api/v3/klines",

            params=dict(symbol=symbol, interval="1d", limit=2),

            timeout=5,

        )

        r.raise_for_status()

        btc_daily_open_cache = float(r.json()[-1][1])

        btc_daily_open_anchor = anchor



    return btc_daily_open_cache



# ============================================================

# [FUTURES ORDER]

# ============================================================

class Futures:

    def __init__(self):

        self.key = os.getenv("BINANCE_API_KEY")

        sec = os.getenv("BINANCE_API_SECRET")

        if not self.key or not sec:

            raise RuntimeError("BINANCE_API_KEY / SECRET not set")

        self.secret = sec.encode()

        self._load_lot_rules()



    def _load_lot_rules(self):

        r = requests.get(f"{FIXED.FUTURES}/fapi/v1/exchangeInfo", timeout=8)

        r.raise_for_status()

        sym = next(s for s in r.json()["symbols"] if s["symbol"] == FIXED.TRADE_SYMBOL)

        lot = next(f for f in sym["filters"] if f["filterType"] == "LOT_SIZE")

        self.step_size = Decimal(lot["stepSize"])

        self.min_qty = Decimal(lot["minQty"])



    def _round_down(self, q: Decimal) -> Decimal:

        return (q / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size



    def _normalize_qty(self, qty: float) -> str:

        q = self._round_down(Decimal(str(qty)))

        if q < self.min_qty:

            raise RuntimeError("QTY_TOO_SMALL")

        dec = len(format(self.step_size, "f").split(".")[1].rstrip("0"))

        return f"{q:.{dec}f}"



    def order(self, side, qty, reduce=False):

        if qty <= 0:

            return 0.0



        qty_str = self._normalize_qty(qty)



        p = dict(

            symbol=FIXED.TRADE_SYMBOL,

            side=side,

            type="MARKET",

            quantity=qty_str,

            reduceOnly=reduce,

            timestamp=int(time.time() * 1000),

        )



        q = urlencode(p)

        p["signature"] = hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()

        r = requests.post(

            f"{FIXED.FUTURES}/fapi/v1/order",

            headers={"X-MBX-APIKEY": self.key},

            params=p,

            timeout=8,

        )

        r.raise_for_status()

        return float(qty_str)



# ============================================================

# [INDICATOR]

# ============================================================

def ema_series(vals, n=9):

    if len(vals) < n:

        raise ValueError("EMA series length < n")



    sma = sum(vals[:n]) / n

    e = sma

    k = 2 / (n + 1)

    for v in vals[n:]:

        e = v * k + e * (1 - k)

    return e


# ============================================================

# [ENGINE INIT]

# ============================================================

cfg = CFG()

state = load_state(cfg)

fx = Futures()



boot_skip_entry = state.hasPosition  # CORE: reboot resume 직후 ENTRY 1 cycle skip

prev_regime = state.regime

just_exited = False





def data_fail(kl, stale_ms):

    if not kl or len(kl) < 2:

        return True

    try:

        # CORE: 완료봉 기준으로 stale 판정 (진행중 봉 제외)

        close_time = int(kl[-2][6])

    except Exception:

        return True

    return int(time.time() * 1000) - close_time > stale_ms


# ============================================================

# [CYCLE]

# ============================================================

def cycle():

    global state, boot_skip_entry, prev_regime, just_exited

    just_exited = False



    btc = klines(FIXED.BTC_SYMBOL, FIXED.TF_BTC)

    sui5 = klines(FIXED.TRADE_SYMBOL, FIXED.TF_ENTRY)

    sui3 = klines(FIXED.TRADE_SYMBOL, FIXED.TF_EXIT)



    # === EXIT CHAIN #1 : BTC_DATA_FAIL (최우선) ===

    if data_fail(btc, FIXED.BTC_STALE_MS):

        state.regime = "OFF"

        prev_regime = "OFF"


        if state.hasPosition and state.remainingQty > 0:

            fx.order("BUY", state.remainingQty, True)

            state = reset_state("BTC_DATA_FAIL", state.lastEntryCandleId)

            just_exited = True

            return



        save_state_atomic(state)

        return


    # === REGIME (BTC Direction Only) ===

    try:

        btc_close = float(btc[-2][4])  # 완료봉 close

        btc_ema9 = ema_series([float(x[4]) for x in btc[:-1]], 9)  # 진행중 제외

        btc_open = btc_daily_open_1d_cached(FIXED.BTC_SYMBOL)

    except Exception as e:

        # ERROR 0% 운용: BTC 지표/캐시 계산 실패도 BTC_DATA_FAIL로 동일 처리

        print("BTC_REGIME_CALC_FAIL:", e)

        state.regime = "OFF"

        prev_regime = "OFF"


        if state.hasPosition and state.remainingQty > 0:

            fx.order("BUY", state.remainingQty, True)

            state = reset_state("BTC_DATA_FAIL", state.lastEntryCandleId)

            just_exited = True

            return



        save_state_atomic(state)

        return



    state.regime = "ON" if (btc_close < btc_open and btc_close < btc_ema9) else "OFF"


    # === EXIT CHAIN #2 : REGIME EXIT ===

    # CORE(X-001): Regime OFF "전환" 시 보유 포지션 전량 즉시 EXIT

    if state.hasPosition and prev_regime == "ON" and state.regime == "OFF":

        fx.order("BUY", state.remainingQty, True)

        state = reset_state("REGIME_EXIT", state.lastEntryCandleId)

        prev_regime = "OFF"

        just_exited = True

        return

    # CORE: 동기화

    prev_regime = state.regime





    # === SUI DATA FAIL ===

    # CORE(C-003): 정보 없음 = 진입 차단 (포지션 보유 중이면 판단 중단 / HOLD)

    if data_fail(sui5, FIXED.SUI_STALE_MS) or data_fail(sui3, FIXED.SUI_STALE_MS):

        return

    # ============================================================

    # [EXIT CHAIN #3~#5] (포지션 보유 시)

    # ============================================================

    if state.hasPosition:

        price = float(sui3[-1][4])  # 현재 tick price (진행중 봉 가능)



        # CORE: 상태 방어 (ERROR 0% 운용)

        # - 상태 파일/재부팅/예외 복구 과정에서 entryPrice/slPrice가 None이면 즉시 안전 리턴

        if state.entryPrice is None or state.entryPrice <= 0 or state.slPrice is None:

            just_exited = True

            return


        # === EXIT CHAIN #3 : SL EXIT (SHORT, realtime price allowed) ===

        # CORE: SL은 진행 중 봉 price 기준 허용 (현재 tick price 사용)

        if price >= state.slPrice:

            fx.order("BUY", state.remainingQty, True)

            state = reset_state("SL_EXIT", state.lastEntryCandleId)

            just_exited = True

            return



        pnl = (state.entryPrice - price) / state.entryPrice * 100



        # === EXIT CHAIN #4 : TP1 PARTIAL EXIT ===

        if cfg.tp1Enabled and (not state.tp1Filled) and pnl >= cfg.tp1Pct:

            qty = state.remainingQty * cfg.tp1SplitPct

            if qty <= 0:

                return



            try:

                fx.order("BUY", qty, True)

            except Exception:

                # ERROR 0%: 주문 실패 시 상태 변경 금지

                return



            state.remainingQty -= qty

            state.tp1Filled = True


            # CORE: TP1 체결 직후 trailing 전환

            state.trailingActive = (cfg.tpTrailingEnabled and cfg.tp1Enabled)

            state.stopLine = None  # CORE: trailingActive 전환 시 stopLine 명시적 초기화



            save_state_atomic(state)

            return







        # === EXIT CHAIN #5 : TRAILING EXIT ===

        if state.trailingActive:

            N = int(cfg.trailingSensitivity)



            # CORE: 3m 완료봉 기준 최근 N봉 최저가(진행중 봉 제외)


            if N <= 0:

                return

            if len(sui3) < N + 2:   # (진행중 1) + (완료봉 N) + (여유 1)

                return



            current_price = float(sui3[-1][4])  # 진행중 3m 봉 close (CORE의 "현재봉 종가"로 사용)

            closed = sui3[:-1]                  # 완료봉만

            lows = [float(x[3]) for x in closed[-N:]]

            stop_line = min(lows)



            # CORE(X-006): 현재봉 종가 > 최근 N봉 최저가 → 잔여 전량 EXIT

            if current_price > stop_line:

                fx.order("BUY", state.remainingQty, True)

                state = reset_state("TRAILING_EXIT", state.lastEntryCandleId)


                just_exited = True

                return



            # stopLine은 trailingActive=true 동안만 유효, 변경 시에만 저장

            if state.stopLine != stop_line:

                state.stopLine = stop_line

                save_state_atomic(state)



        return  # CORE: 포지션 보유 시 이 cycle에서 ENTRY 평가 금지




    # ============================================================

    # [ENTRY] (무포지션 시)

    # ============================================================

    # === BOOT SKIP (CORE: reboot resume 직후 ENTRY 1 cycle skip) ===

    if boot_skip_entry:

        boot_skip_entry = False

        return



    # CORE: EXIT 발생한 동일 cycle에서는 ENTRY 평가 금지

    if just_exited:

        return



    # CORE: regimeFilterEnabled=true 이면 Regime=ON 에서만 ENTRY 평가

    if cfg.regimeFilterEnabled and state.regime != "ON":

        return

    if (not sui5) or len(sui5) < 25:

        return



    prev2 = sui5[-3]  # 완료봉-2

    prev1 = sui5[-2]  # 완료봉-1 (직전 완료봉)

    current_id = int(sui5[-1][6])  # 현재 5m 봉 ID (소비봉 기준)



    # CORE: 소비봉 1회 제한 (동일 5m 봉 중복 진입 금지)

    if state.lastEntryCandleId == current_id:

        return



    closes = [float(x[4]) for x in sui5[:-1]]  # 진행중 제외

    ema9_prev = ema_series(closes, 9)



    body_top_prev2 = max(float(prev2[1]), float(prev2[4]))

    tol = ema9_prev * (cfg.ema9EntryTolerance / 100.0)

    # ============================================================

    # CORE: Mother Trigger (WINDOW MEMORY PATCH)

    # - 최근 N봉 중 1회라도 발생하면 "숏 가능 구간" 유지

    # ============================================================

    MOTHER_WINDOW = 7





    mother_hit = False



    for i in range(2, 2 + MOTHER_WINDOW):

        if len(sui5) < i + 1:

            break



        p2 = sui5[-(i + 1)]

        p1 = sui5[-i]


        body_top = max(float(p2[1]), float(p2[4]))

        ema_ok = abs(body_top - ema9_prev) <= tol



        direction_ok = float(p1[4]) < ema9_prev

        direction_relax = abs(float(p1[4]) - ema9_prev) <= tol



        if ema_ok and (direction_ok or direction_relax):

            mother_hit = True

            break



    if cfg.entryFilterEnabled and not mother_hit:

        return


    high_prev = float(prev1[2])

    low_prev = float(prev1[3])

    close_prev = float(prev1[4])

    range_pct = (high_prev - low_prev) / close_prev * 100



    if len(sui5) < 21:

        return



    vols = [float(x[5]) for x in sui5[-21:-1]]  # 완료봉 20개

    vol_ma20 = sum(vols) / 20

    vol_ratio = float(prev1[5]) / vol_ma20 if vol_ma20 > 0 else 0



    if cfg.volatilityFilterEnabled and range_pct < cfg.volatilityMinPct:                                                        ty and

        return


    if cfg.volumeFilterEnabled and vol_ratio < cfg.volumeSpikeRatio:

        return



    price = float(sui5[-1][4])  # 현재 tick price (진행중 5m)



    # SAFE PATCH: WS 지연 대비 EMA 기준 ±ε 허용 (0.02%)

    ema_eps = ema9_prev * 0.0002

    if abs(price - ema9_prev) > (tol + ema_eps):

        return



    qty = fx.order("SELL", cfg.investUSDT / price)



    if qty <= 0:

        return



    state = State(

        hasPosition=True,

        positionSide="SHORT",

        entryPrice=price,

        remainingQty=qty,

        slPrice=price * (1 + cfg.slPct / 100),

        tp1Filled=False,

        trailingActive=(cfg.tpTrailingEnabled and (not cfg.tp1Enabled)),  # CORE: tp1Enabled=false면 진입 직후 trailing

        stopLine=None,

        regime=state.regime,

        lastEntryCandleId=current_id,

    )

    save_state_atomic(state)


# ============================================================

# [MAIN LOOP]

# ============================================================

print("VELLA V3 APP START")

while True:

    try:

        cycle()



    except Exception as e:

        try:

            # 포지션 보유 중 예외 → FAIL-SAFE: 전량 EXIT 시도 후 상태 리셋(소비봉 유지)

            if state.hasPosition and state.remainingQty > 0:

                fx.order("BUY", state.remainingQty, True)

                state = reset_state("ENGINE_EXCEPTION", state.lastEntryCandleId)


            else:

                # 무포지션 예외 → Regime OFF 강제 + 소비봉 유지 (상태만 기록)

                state.regime = "OFF"

                prev_regime = "OFF"

                state.exitReason = "ENGINE_EXCEPTION"

                save_state_atomic(state)



        except Exception:

            # 예외 처리 중 예외까지 터져도 메인 루프는 계속 돌아가야 한다

            pass



        print("ERROR:", e)



    time.sleep(FIXED.LOOP_SEC)


