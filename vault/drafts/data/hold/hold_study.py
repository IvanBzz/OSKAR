#!/usr/bin/env python
"""hold_study.py — «куда пойдёт цена»: достижение HTF-гэпа (1d/1w) после разворотного сигнала 4h
БЕЗ стопа и БЕЗ тайм-аута 48 баров (держим до цели или до 90 дней). План — plan.md, части 2 и 2a.

Определения гэпа, целей и сигналов скопированы из setup_study.py (ночной тест), чтобы сравнение было честным.

Запуск (OSKAR/.venv):
  python -I -B hold_study.py --universe hold [--procs 5] [--limit N] [--symbols BTC,ETH] [--no-fetch]
  python -I -B hold_study.py                 # 10 монет (candles/cache 4h)

Сеть: только https://fapi.binance.com/fapi/v1/fundingRate для символов без файла фандинга в hold/cache,
≤ 0.7 запроса/с, все вызовы и разбор JSON — в try, ошибки — в лог. Сейчас файлы есть у всех 547 → сеть не нужна.
Не трогаются: 127.0.0.1:8321, BingX, OSKAR (только чтение констант издержек, продублированы ниже).
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent  # drafts/hold
DRAFTS = HERE.parent
C_CANDLES = DRAFTS / "candles" / "cache"
C_GAPS = DRAFTS / "gaps" / "cache"
C_HOLD = HERE / "cache"
UNIVERSE_CSV = HERE / "universe.csv"
SYMBOLS10 = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "SUI"]
MAJORS = {"BTC", "ETH"}

HOUR = 3_600_000
DAY = 24 * HOUR
WEEK = 7 * DAY
MONDAY0 = 4 * DAY  # 1970-01-05 — понедельник
P4H = 4 * HOUR

# --- параметры (как setup_study.py)
ATR_N = 14
GAP_MIN_ATR = 0.1
D_MIN, D_MAX = 1.5, 8.0
K1_NOGAP_ATR = 10.0
LOOKBACK = 20
MOVE_ATR = 1.5
DOJI_BODY = 0.10
WARMUP = 40
TAKER = 0.0005  # OSKAR risk/sizing.py TAKER_FEE_PCT
SLIP = 0.0005   # OSKAR risk/sizing.py STOP_SLIPPAGE_PCT

# --- параметры hold-исследования (plan.md, части 2 и 2a)
HORIZONS = (7, 14, 30, 60, 90)
HMAX_DAYS = 90
HMAX = HMAX_DAYS * 6  # 540 баров 4h
MMR = 0.005           # поддерживающая маржа, грубо
LEVS = (1, 2, 3, 5, 10, 20)
EMA_1D = 50
EMA_1W = 20
K1_DRAWS = 3
K2_DRAWS = 1
K2_WIN = 100
N_BOOT = 2000
SEED = 20261016
COARSE_WEEKS = 13
SIGNALS = ("b", "d")
TARGETS = ("near", "ce", "far")
CHUNK = 1500
FR_PAUSE = 1.5  # с между запросами фандинга (≈0.67 запр/с)

SIG_NAME = {"b": "(b) CHoCH по закрытию", "d": "(d) доджи у экстремума → CHoCH"}
TGT_NAME = {"near": "ближняя граница", "ce": "CE (середина)", "far": "полное закрытие"}
POP_NAME = {"all": "с делистнутыми", "live": "без делистнутых"}


# ============================================================================ данные
def load(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p)
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def wilder_atr(h, l, c, n=ATR_N):
    tr = np.empty_like(c)
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr = np.full_like(c, np.nan)
    if len(c) >= n:
        atr[n - 1] = tr[:n].mean()
        for k in range(n, len(c)):
            atr[k] = (atr[k - 1] * (n - 1) + tr[k]) / n
    return atr, tr


def agg_htf(df4: pd.DataFrame, period: int, offset: int) -> pd.DataFrame:
    """Агрегация 4h -> 1d/1w. Отбрасываются неполные первый и последний бары."""
    key = (df4.open_time.to_numpy() - offset) // period
    g = df4.groupby(key, sort=True)
    out = pd.DataFrame({"open_time": g.open_time.first().index.to_numpy() * period + offset,
                        "open": g.open.first().to_numpy(), "high": g.high.max().to_numpy(),
                        "low": g.low.min().to_numpy(), "close": g.close.last().to_numpy(),
                        "nsub": g.size().to_numpy()})
    full = period // P4H
    lo, hi = 0, len(out)
    if out.nsub.iloc[0] < full:
        lo = 1
    if out.nsub.iloc[-1] < full:
        hi -= 1
    out = out.iloc[lo:hi].reset_index(drop=True)
    out["period"] = period
    return out


def find_gaps(htf: pd.DataFrame, tf: str, period: int) -> pd.DataFrame:
    h, l, c = (htf[k].to_numpy(float) for k in ("high", "low", "close"))
    atr, tr = wilder_atr(h, l, c)
    atr_f = np.where(np.isnan(atr), np.cumsum(tr) / np.arange(1, len(tr) + 1), atr)
    i = np.arange(2, len(c))
    bull = l[i] > h[i - 2]
    bear = h[i] < l[i - 2]
    rows = []
    for m, is_bull in ((bull, True), (bear, False)):
        ii = i[m]
        bot = np.where(is_bull, h[ii - 2], h[ii])
        top = np.where(is_bull, l[ii], l[ii - 2])
        ok = (top - bot) >= GAP_MIN_ATR * atr_f[ii]
        ii, bot, top = ii[ok], bot[ok], top[ok]
        rows.append(pd.DataFrame({"tf": tf, "known": htf.open_time.to_numpy()[ii] + period, "bull": is_bull,
                                  "bottom": bot, "top": top, "size_htf_atr": (top - bot) / atr_f[ii]}))
    return pd.concat(rows, ignore_index=True).sort_values("known").reset_index(drop=True)


def alive_run(arr: np.ndarray, s: int, init: float, thr: float, upper: bool) -> np.ndarray:
    n = len(arr)
    w = 256
    while True:
        e = min(n, s + w)
        seg = arr[s:e]
        if upper:
            run = np.maximum.accumulate(np.maximum(seg, init))
            f = int(np.searchsorted(run, thr, "left"))
        else:
            run = np.minimum.accumulate(np.minimum(seg, init))
            f = int(np.searchsorted(-run, -thr, "left"))
        if f < len(run) or e == n:
            return run[:f]
        w *= 8


def tf_targets(L: dict, gaps: pd.DataFrame) -> dict:
    """Для каждого бара 4h и направления: ближайший живой гэп в коридоре [D_MIN; D_MAX] ATR (near, ce, far)
    и мин. расстояние до любого живого гэпа (many). d=+1 LONG — медвежьи гэпы над ценой; d=-1 SHORT — бычьи под ценой."""
    ot, h, l, c, atr = L["ot"], L["h"], L["l"], L["c"], L["atr"]
    n = len(c)
    out = {d: {"best": np.full(n, np.inf), "near": np.full(n, np.nan), "ce": np.full(n, np.nan),
               "far": np.full(n, np.nan), "many": np.full(n, np.inf)} for d in (1, -1)}
    end_ms = ot[-1] + P4H
    for g in gaps.itertuples(index=False):
        if g.known >= end_ms:
            continue
        s = int(np.searchsorted(ot, g.known, "left"))
        if g.bull:
            run = alive_run(l, s, np.inf, g.bottom, upper=False)
            if len(run) == 0:
                continue
            near = np.minimum(g.top, run)
            far = g.bottom
            sl_ = slice(s, s + len(run))
            dist = (c[sl_] - near) / atr[sl_]
            d = -1
        else:
            run = alive_run(h, s, -np.inf, g.top, upper=True)
            if len(run) == 0:
                continue
            near = np.maximum(g.bottom, run)
            far = g.top
            sl_ = slice(s, s + len(run))
            dist = (near - c[sl_]) / atr[sl_]
            d = 1
        o = out[d]
        o["many"][sl_] = np.fmin(o["many"][sl_], dist)
        better = (dist >= D_MIN) & (dist <= D_MAX) & (dist < o["best"][sl_])
        if better.any():
            pos = np.nonzero(better)[0] + s
            o["best"][pos] = dist[better]
            o["near"][pos] = near[better]
            o["ce"][pos] = (near[better] + far) / 2
            o["far"][pos] = far
    return out


def merge_targets(parts: dict, tfs: tuple) -> dict:
    out = {}
    for d in (1, -1):
        best = np.vstack([parts[tf][d]["best"] for tf in tfs])
        k = np.argmin(best, axis=0)
        cols = np.arange(best.shape[1])
        o = {"best": best[k, cols], "tfi": np.where(np.isfinite(best[k, cols]), k, -1)}
        for f in ("near", "ce", "far"):
            o[f] = np.vstack([parts[tf][d][f] for tf in tfs])[k, cols]
        o["many"] = np.min(np.vstack([parts[tf][d]["many"] for tf in tfs]), axis=0)
        out[d] = o
    return out


# ============================================================================ сигналы (копия setup_study.py)
def doji_sig(o, h, l, c, atr):
    rg = h - l
    body = np.abs(c - o)
    pmin = pd.Series(l).shift(1).rolling(LOOKBACK).min().to_numpy()
    pmax = pd.Series(h).shift(1).rolling(LOOKBACK).max().to_numpy()
    with np.errstate(invalid="ignore"):
        ok = (rg > 0) & (atr > 0) & np.isfinite(pmin) & np.isfinite(pmax)
        doji = body <= DOJI_BODY * rg
        new_low = l <= pmin
        new_high = h >= pmax
        lg = ok & doji & new_low & ~new_high & ((pmax - l) / atr >= MOVE_ATR)
        sh = ok & doji & new_high & ~new_low & ((h - pmin) / atr >= MOVE_ATR)
    return lg, sh


def fractals(h, l, k):
    lmax = pd.Series(h).shift(1).rolling(k).max().to_numpy()
    rmax = pd.Series(h[::-1]).shift(1).rolling(k).max().to_numpy()[::-1]
    lmin = pd.Series(l).shift(1).rolling(k).min().to_numpy()
    rmin = pd.Series(l[::-1]).shift(1).rolling(k).min().to_numpy()[::-1]
    with np.errstate(invalid="ignore"):
        sh = (h > lmax) & (h >= rmax)
        sl = (l < lmin) & (l <= rmin)
    return sh, sl


def choch_sig(h, l, c, k):
    n = len(c)
    sh, sl = fractals(h, l, k)
    lg = np.zeros(n, bool)
    st = np.zeros(n, bool)
    ext_l = np.full(n, np.nan)
    ext_s = np.full(n, np.nan)
    jref_l = np.full(n, -1)
    jref_s = np.full(n, -1)
    SH: list = []
    SL: list = []
    used_h = used_l = False
    for t in range(1, n):
        j = t - 1 - k
        if j >= k:
            if sh[j]:
                SH.append((h[j], j))
                SH = SH[-2:]
                used_h = False
            if sl[j]:
                SL.append((l[j], j))
                SL = SL[-2:]
                used_l = False
        if len(SH) == 2 and len(SL) == 2:
            if (not used_h) and SH[1][0] < SH[0][0] and SL[1][0] < SL[0][0] and c[t] > SH[1][0] >= c[t - 1]:
                lg[t] = True
                jj = SH[1][1]
                ext_l[t] = l[jj + 1:t + 1].min()
                jref_l[t] = jj
                used_h = True
            if (not used_l) and SL[1][0] > SL[0][0] and SH[1][0] > SH[0][0] and c[t] < SL[1][0] <= c[t - 1]:
                st[t] = True
                jj = SL[1][1]
                ext_s[t] = h[jj + 1:t + 1].max()
                jref_s[t] = jj
                used_l = True
    return lg, st, ext_l, ext_s, jref_l, jref_s


def signals_bd(L: dict) -> dict:
    """sig -> d -> mask ((b) CHoCH k=2, (d) доджи → CHoCH)."""
    o, h, l, c, atr = L["o"], L["h"], L["l"], L["c"], L["atr"]
    a_l, a_s = doji_sig(o, h, l, c, atr)
    A_l = np.cumsum(a_l)
    A_s = np.cumsum(a_s)
    b_l, b_s, _, _, j_l, j_s = choch_sig(h, l, c, 2)
    d_l = b_l.copy()
    d_s = b_s.copy()
    idx = np.nonzero(b_l)[0]
    d_l[idx] = (A_l[idx - 1] - A_l[j_l[idx]]) > 0
    idx = np.nonzero(b_s)[0]
    d_s[idx] = (A_s[idx - 1] - A_s[j_s[idx]]) > 0
    return {"b": {1: b_l, -1: b_s}, "d": {1: d_l, -1: d_s}}


# ============================================================================ фандинг
def now_ms_snapshot() -> int:
    p = C_HOLD / "meta.json"
    if p.exists():
        return int(json.loads(p.read_text())["now_ms"])
    return int(time.time() * 1000)


def fetch_funding(symbols: list[str], log: list[str]) -> None:
    """Докачка /fapi/v1/fundingRate в hold/cache/{SYM}USDT_funding.csv.gz (формат build_dataset.py). ≤0.7 запр/с.
    Все сетевые вызовы и разбор JSON — внутри try; любая ошибка → лог, символ пропускается, скрипт не падает."""
    todo = []
    for s in symbols:
        sym = s if s.endswith("USDT") else s + "USDT"
        if not (C_HOLD / f"{sym}_funding.csv.gz").exists():
            todo.append(sym)
    if not todo:
        log.append("фандинг: файлы есть у всех символов, сеть не нужна")
        return
    try:
        import httpx
    except Exception as e:  # noqa: BLE001
        log.append(f"фандинг: httpx недоступен ({e!r}), докачка пропущена")
        return
    C_HOLD.mkdir(exist_ok=True)
    now_ms = now_ms_snapshot()
    jl = HERE / "fetch_log_funding_study.jsonl"
    nreq = 0
    try:
        cl = httpx.Client(timeout=30, base_url="https://fapi.binance.com")
    except Exception as e:  # noqa: BLE001
        log.append(f"фандинг: клиент не создан ({e!r})")
        return
    with cl:
        for sym in todo:
            p = C_HOLD / f"{sym}_funding.csv.gz"
            start, rows, err, n0, t0, nerr = 1, [], None, nreq, time.monotonic(), 0
            while True:
                time.sleep(FR_PAUSE)
                try:
                    r = cl.get("/fapi/v1/fundingRate", params={"symbol": sym, "startTime": start, "limit": 1000})
                    nreq += 1
                    status = r.status_code
                    if status == 418:
                        log.append(f"funding {sym}: 418 BAN — остановка докачки")
                        return
                    if status == 429:
                        log.append(f"funding {sym}: 429; sleep 65")
                        time.sleep(65)
                        continue
                    if status != 200:
                        err = f"http {status} {r.text[:120]}"
                        break
                    data = r.json()
                except Exception as e:  # noqa: BLE001 — обрыв ответа, битый JSON, таймаут
                    nerr += 1
                    log.append(f"funding {sym}: ошибка запроса/разбора {e!r}; попытка {nerr}/5, sleep 10")
                    if nerr >= 5:
                        err = f"net/parse errors x5: {e!r}"
                        break
                    time.sleep(10)
                    continue
                if isinstance(data, dict):
                    err = f"code={data.get('code')} msg={data.get('msg')}"
                    break
                if not isinstance(data, list):
                    err = f"unexpected payload {type(data).__name__}"
                    break
                if not data:
                    break
                rows += data
                if len(data) < 1000:
                    break
                try:
                    start = int(data[-1]["fundingTime"]) + 1
                except Exception as e:  # noqa: BLE001
                    err = f"bad row {e!r}"
                    break
            info = dict(symbol=sym, tf="funding", nreq=nreq - n0, sec=round(time.monotonic() - t0, 1), n_raw=len(rows))
            if err:
                info["error"] = err
                log.append(f"funding {sym}: SKIP {err}")
            else:
                try:
                    df = pd.DataFrame([(int(x["fundingTime"]), float(x["fundingRate"]),
                                        float(x["markPrice"]) if x.get("markPrice") not in (None, "") else np.nan)
                                       for x in rows], columns=["funding_time", "funding_rate", "mark_price"])
                    df = df.drop_duplicates("funding_time").sort_values("funding_time")
                    df = df[df.funding_time < now_ms].reset_index(drop=True)
                    info["n_saved"] = len(df)
                    with gzip.open(p, "wt", encoding="utf-8", newline="") as f:
                        df.to_csv(f, index=False)
                    log.append(f"funding {sym}: n={len(df)} req={info['nreq']} {info['sec']}s")
                except Exception as e:  # noqa: BLE001
                    info["error"] = f"save {e!r}"
                    log.append(f"funding {sym}: SKIP save {e!r}")
            try:
                with open(jl, "a", encoding="utf-8") as f:
                    f.write(json.dumps(info) + "\n")
            except Exception:  # noqa: BLE001
                pass
            print(log[-1], flush=True)
    log.append(f"funding: всего запросов {nreq}")


def load_funding(sym: str) -> pd.DataFrame | None:
    p = C_HOLD / f"{sym}_funding.csv.gz"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p).dropna(subset=["funding_rate"])
    except Exception:  # noqa: BLE001
        return None
    return df.drop_duplicates("funding_time").sort_values("funding_time").reset_index(drop=True)


def avg_funding() -> pd.DataFrame:
    """Средний по датасету ряд ставок: среднее по символам в часовом слоте (fallback для символов без файла)."""
    parts = []
    for p in sorted(C_HOLD.glob("*_funding.csv.gz")):
        df = load_funding(p.name[:-len("_funding.csv.gz")])
        if df is not None and len(df):
            parts.append(pd.DataFrame({"slot": df.funding_time // HOUR, "funding_rate": df.funding_rate}))
    if not parts:
        return pd.DataFrame({"funding_time": [], "funding_rate": []})
    g = pd.concat(parts).groupby("slot").funding_rate.mean()
    return pd.DataFrame({"funding_time": g.index.to_numpy() * HOUR, "funding_rate": g.to_numpy()})


def funding_cum(L: dict, fund: pd.DataFrame) -> np.ndarray:
    """C[i] = Σ ставок с funding_time ≤ close_time бара i. Стоимость удержания t..t+k для LONG = C[t+k] − C[t]."""
    ft = fund.funding_time.to_numpy(np.int64)
    cs = np.concatenate([[0.0], np.cumsum(fund.funding_rate.to_numpy(float))])
    pos = np.searchsorted(ft, L["ot"] + P4H, "right")
    return cs[pos]


# ============================================================================ сканирование вперёд
def scan(L: dict, idx: np.ndarray, d: np.ndarray, entry: np.ndarray, targets: dict, cumf: np.ndarray) -> dict:
    """Без стопа и тайм-аута: первый бар касания цели (1..HMAX, 0 = нет), MAE до касания, MAE за HMAX, фандинг.
    Усечённые события (делистинг, < HMAX баров впереди): индексы за концом данных прижимаются к последнему бару —
    повтор последнего бара новых касаний не создаёт и MAE не меняет; «потеря 100 %» ставится в event_frame."""
    h, l = L["h"], L["l"]
    last = len(h) - 1
    n_ev = len(idx)
    out = {"mae90_px": np.full(n_ev, np.nan)}
    for name in targets:
        out[f"off_{name}"] = np.zeros(n_ev, int)
        out[f"mae_{name}_px"] = np.full(n_ev, np.nan)
        out[f"fund_{name}"] = np.full(n_ev, np.nan)
    out["fund30"] = d * (cumf[np.minimum(idx + 180, last)] - cumf[idx]) * 100
    out["fund90"] = d * (cumf[np.minimum(idx + HMAX, last)] - cumf[idx]) * 100
    offs = np.arange(1, HMAX + 1)
    for a in range(0, n_ev, CHUNK):
        sl = slice(a, a + CHUNK)
        ii, dd, ee = idx[sl], d[sl], entry[sl]
        J = np.minimum(ii[:, None] + offs, last)
        HH, LL = h[J], l[J]
        lg = (dd > 0)[:, None]
        adv = np.where(lg, ee[:, None] - np.minimum.accumulate(LL, 1), np.maximum.accumulate(HH, 1) - ee[:, None])
        rows = np.arange(len(ii))
        out["mae90_px"][sl] = adv[:, -1]
        for name, tp in targets.items():
            tpc = tp[sl][:, None]
            hit = np.where(lg, HH >= tpc, LL <= tpc)
            any_ = hit.any(1)
            k = hit.argmax(1)
            off = np.where(any_, k + 1, 0)
            out[f"off_{name}"][sl] = off
            out[f"mae_{name}_px"][sl] = np.where(any_, adv[rows, k], np.nan)
            fc = dd * (cumf[np.minimum(ii + off, last)] - cumf[ii]) * 100
            out[f"fund_{name}"][sl] = np.where(any_, fc, np.nan)
    return out


def sim_stop(L: dict, idx, d, entry, sl, tp, cumf) -> dict:
    """Сделка со стопом (на P95 MAE), целью и выходом по close t+HMAX без касаний. Издержки как sizing.py.
    Усечённые события без касаний закрываются по close последнего бара данных (plan.md 2a)."""
    o, h, l, c = L["o"], L["h"], L["l"], L["c"]
    last = len(c) - 1
    n_ev = len(idx)
    res = {k: np.full(n_ev, np.nan) for k in ("R_net", "tp", "sl", "to", "exit_off", "fund_R")}
    offs = np.arange(1, HMAX + 1)
    for a in range(0, n_ev, CHUNK):
        s_ = slice(a, a + CHUNK)
        ii, dd, ee, ss, tt = idx[s_], d[s_], entry[s_], sl[s_], tp[s_]
        J = np.minimum(ii[:, None] + offs, last)
        HH, LL, OO = h[J], l[J], o[J]
        lg = (dd > 0)[:, None]
        stop_m = np.where(lg, LL <= ss[:, None], HH >= ss[:, None])
        tp_m = np.where(lg, HH >= tt[:, None], LL <= tt[:, None])
        fs = np.where(stop_m.any(1), stop_m.argmax(1), HMAX)
        ft = np.where(tp_m.any(1), tp_m.argmax(1), HMAX)
        is_sl = (fs <= ft) & (fs < HMAX)
        is_tp = ft < fs
        rows = np.arange(len(ii))
        o_at = OO[rows, np.minimum(fs, HMAX - 1)]
        stop_px = np.where(dd > 0, np.minimum(o_at, ss), np.maximum(o_at, ss))
        stop_exit = stop_px - dd * SLIP * ee
        c_to = c[np.minimum(ii + HMAX, last)]
        exit_px = np.where(is_sl, stop_exit, np.where(is_tp, tt, c_to))
        pnl = dd * (exit_px - ee) - TAKER * (ee + exit_px)
        sl_nom = ss - dd * SLIP * ee
        risk = np.abs(ee - sl_nom) + TAKER * (ee + sl_nom)
        exit_off = np.where(is_sl, fs + 1, np.where(is_tp, ft + 1, HMAX))
        fund_frac = dd * (cumf[np.minimum(ii + exit_off, last)] - cumf[ii])  # доля notional
        res["R_net"][s_] = pnl / risk
        res["tp"][s_] = is_tp
        res["sl"][s_] = is_sl
        res["to"][s_] = ~is_sl & ~is_tp
        res["exit_off"][s_] = exit_off
        res["fund_R"][s_] = fund_frac * ee / risk
    return res


# ============================================================================ сборка событий
def universe(kind: str) -> tuple[list[str], dict]:
    """Список символов (без USDT) и метаданные: delisted, top50, status, delist_date."""
    if kind == "hold":
        u = pd.read_csv(UNIVERSE_CSV)
        u = u[u.bars_4h.fillna(0) > 0]
        syms, meta = [], {}
        for r in u.itertuples(index=False):
            sym = str(r.symbol)
            if not (C_HOLD / f"{sym}_4h.csv.gz").exists():
                continue
            s = sym[:-4] if sym.endswith("USDT") else sym
            syms.append(s)
            meta[s] = {"delisted": int(r.delisted) == 1, "top50": int(r.top50) == 1, "status": str(r.status),
                       "delist_date": "" if pd.isna(r.delist_date) else str(r.delist_date)}
        return sorted(syms), meta
    meta = {s: {"delisted": False, "top50": True, "status": "TRADING", "delist_date": ""} for s in SYMBOLS10}
    return SYMBOLS10, meta


def load_symbol(s: str, meta_s: dict) -> dict:
    sym = s + "USDT"
    p = C_HOLD / f"{sym}_4h.csv.gz"
    if not p.exists():
        p = C_CANDLES / f"{sym}_4h.csv"
    df4 = load(p)[["open_time", "open", "high", "low", "close", "volume", "close_time"]]
    L = {k: df4[c].to_numpy(float) for k, c in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))}
    L["ot"] = df4.open_time.to_numpy(np.int64)
    L["atr"], _ = wilder_atr(L["h"], L["l"], L["c"])
    L["sym"] = s
    L["delisted"] = bool(meta_s["delisted"])
    L["top50"] = bool(meta_s["top50"])
    n = len(L["ot"])
    htf = {"1d": agg_htf(df4, DAY, 0), "1w": agg_htf(df4, WEEK, MONDAY0)}
    L["gaps"] = {"1d": find_gaps(htf["1d"], "1d", DAY), "1w": find_gaps(htf["1w"], "1w", WEEK)}
    # тренд 1d: close последнего закрытого дня выше EMA50(1d); тренд 1w: close последней закрытой недели выше EMA20(1w)
    for tf, period, span, key in (("1d", DAY, EMA_1D, "trend_up"), ("1w", WEEK, EMA_1W, "trend_up_w")):
        hb = htf[tf]
        tr = np.full(n, np.nan)
        if len(hb) >= span:
            ema = hb.close.ewm(span=span, adjust=False).mean().to_numpy()
            up = hb.close.to_numpy() > ema
            j = np.searchsorted(hb.open_time.to_numpy() + period, L["ot"], "right") - 1
            ok = j >= span - 1
            tr[ok] = up[j[ok]].astype(float)
        L[key] = tr
    L["htf"] = htf
    return L


def event_frame(L: dict, grp: str, sig: str, d: int, idx: np.ndarray, entry: np.ndarray, tg: dict,
                gap_tf: np.ndarray, w: np.ndarray, parent: np.ndarray, cumf: np.ndarray) -> pd.DataFrame:
    atr = L["atr"][idx]
    n = len(L["c"])
    sc = scan(L, idx, np.full(len(idx), d), entry, tg, cumf)
    t = L["ot"][idx]
    trunc = idx > n - 1 - HMAX  # впереди меньше HMAX баров (только делистнутые символы)
    df = pd.DataFrame({
        "grp": grp, "sig": sig, "d": d, "sym": L["sym"], "major": L["sym"] in MAJORS, "top50": L["top50"],
        "delisted": L["delisted"], "trunc": trunc, "idx": idx, "t": t,
        "year": pd.to_datetime(t, unit="ms").year, "week": (t - MONDAY0) // WEEK, "w": w, "parent": parent,
        "gap_tf": gap_tf, "entry": entry, "atr": atr,
        "dist_near": (tg["near"] - entry) * d / atr, "dist_ce": (tg["ce"] - entry) * d / atr,
        "dist_far": (tg["far"] - entry) * d / atr, "size_atr": np.abs(tg["far"] - tg["near"]) / atr,
        "trend_up": L["trend_up"][idx], "trend_up_w": L["trend_up_w"][idx],
    })
    df["trend_with"] = np.where(np.isnan(df.trend_up), np.nan, (df.trend_up == (d > 0)).astype(float))
    df["trend_with_w"] = np.where(np.isnan(df.trend_up_w), np.nan, (df.trend_up_w == (d > 0)).astype(float))
    for name in TARGETS:
        df[f"off_{name}"] = sc[f"off_{name}"]
        df[f"mae_{name}_atr"] = sc[f"mae_{name}_px"] / atr
        df[f"mae_{name}_pct"] = sc[f"mae_{name}_px"] / entry * 100
        df[f"fund_{name}"] = sc[f"fund_{name}"]
    lost = trunc & (sc["off_ce"] == 0)  # делистинг до прихода в CE → позиция потеряна (MAE = 100 %)
    df["lost"] = lost
    df["mae90_atr"] = np.where(lost, entry / atr, sc["mae90_px"] / atr)
    df["mae90_pct"] = np.where(lost, 100.0, sc["mae90_px"] / entry * 100)
    df["mae_hold_ce_pct"] = np.where(sc["off_ce"] > 0, df.mae_ce_pct, df.mae90_pct)
    df["fund30"] = sc["fund30"]
    df["fund90"] = sc["fund90"]
    return df


def work(args: tuple) -> tuple:
    """Один символ: загрузка, гэпы, цели, сигналы, события main и K2; K1 — отложенно (нужен общий пул)."""
    s, meta_s, seed, favg = args
    t0 = time.time()
    rng = np.random.default_rng(seed)
    L = load_symbol(s, meta_s)
    fund = load_funding(s + "USDT")
    if fund is None or len(fund) == 0:
        fund = favg
        L["fund_src"] = "avg" if (favg is not None and len(favg)) else "none"
    else:
        L["fund_src"] = "own"
    cumf = funding_cum(L, fund) if (fund is not None and len(fund)) else np.zeros(len(L["ot"]))
    L["cumf"] = cumf
    n = len(L["c"])
    parts = {tf: tf_targets(L, g) for tf, g in L["gaps"].items()}
    T = merge_targets(parts, ("1d", "1w"))
    sigs = signals_bd(L)
    valid = np.zeros(n, bool)
    valid[WARMUP:(n if L["delisted"] else n - HMAX)] = True  # делистнутые: хвост < HMAX включён (trunc)
    valid &= np.isfinite(L["atr"]) & (L["atr"] > 0)
    recs, base, k1_pending = [], [], []
    for sig in SIGNALS:
        for d in (1, -1):
            mask = sigs[sig][d] & valid
            idx_all = np.nonzero(mask)[0]
            tt = T[d]
            is_gap = np.isfinite(tt["best"][idx_all])
            is_k1 = tt["many"][idx_all] > K1_NOGAP_ATR
            base.append({"sig": sig, "d": d, "sym": s, "delisted": L["delisted"], "n_sig": len(idx_all),
                         "n_gap": int(is_gap.sum()), "n_k1": int(is_k1.sum()), "bars": n})
            idx = idx_all[is_gap]
            if len(idx):
                entry = L["c"][idx]
                tg = {k: tt[k][idx] for k in TARGETS}
                gtf = np.array(["1d", "1w"])[tt["tfi"][idx]]
                recs.append(event_frame(L, "main", sig, d, idx, entry, tg, gtf, np.ones(len(idx)),
                                        np.arange(len(idx)), cumf))
                # K2: гэп есть, вход в случайный бар ±K2_WIN без сигнала этого вида
                elig = np.isfinite(tt["best"]) & valid & ~sigs[sig][d]
                ks, par = [], []
                for p, i in enumerate(idx):
                    lo, hi = max(0, i - K2_WIN), min(n, i + K2_WIN + 1)
                    cand = np.nonzero(elig[lo:hi])[0] + lo
                    cand = cand[cand != i]
                    if len(cand):
                        ks.append(rng.choice(cand, K2_DRAWS, replace=True))
                        par.append(np.full(K2_DRAWS, p))
                if ks:
                    k = np.concatenate(ks)
                    pp = np.concatenate(par)
                    tgk = {q: tt[q][k] for q in TARGETS}
                    gtk = np.array(["1d", "1w"])[tt["tfi"][k]]
                    recs.append(event_frame(L, "K2", sig, d, k, L["c"][k], tgk, gtk,
                                            np.full(len(k), 1.0 / K2_DRAWS), pp, cumf))
            ik = idx_all[is_k1]
            if len(ik):
                k1_pending.append((s, sig, d, ik))
    line = (f"{s}: {n} баров 4h ({pd.to_datetime(L['ot'][0], unit='ms').date()}…"
            f"{pd.to_datetime(L['ot'][-1], unit='ms').date()}){' DELISTED' if L['delisted'] else ''}, "
            f"гэпы 1d={len(L['gaps']['1d'])} 1w={len(L['gaps']['1w'])}, фандинг={L['fund_src']}, {time.time() - t0:.1f}s")
    L["n_gaps"] = {tf: len(g) for tf, g in L["gaps"].items()}
    L["missing_4h"] = int(((np.diff(L["ot"]) // P4H) - 1).sum())
    L.pop("gaps", None)
    if s != "BTC":
        L.pop("htf", None)
    return s, L, recs, base, k1_pending, line


def build(symbols: list[str], meta: dict, log: list[str], procs: int) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    favg = None
    missing = [s for s in symbols if not (C_HOLD / f"{s}USDT_funding.csv.gz").exists()]
    if missing:
        favg = avg_funding()
        log.append(f"фандинг: нет файла у {len(missing)} символов → средний ряд по датасету ({len(favg)} слотов)")
    tasks = [(s, meta[s], SEED + 1000 + i, favg) for i, s in enumerate(symbols)]
    Ls: dict = {}
    recs: list[pd.DataFrame] = []
    base: list[dict] = []
    k1_pending: list = []
    n_fund_own = 0
    lines: list[str] = []

    def take(res):
        nonlocal n_fund_own
        s, L, r, b, k, line = res
        Ls[s] = L
        recs.extend(r)
        base.extend(b)
        k1_pending.extend(k)
        n_fund_own += L["fund_src"] == "own"
        lines.append(line)
        print(f"[{len(lines)}/{len(symbols)}] {line}", flush=True)

    if procs > 1:
        with ProcessPoolExecutor(max_workers=procs) as ex:
            for res in ex.map(work, tasks, chunksize=2):
                take(res)
    else:
        for t in tasks:
            take(work(t))
    (HERE / "hold_study.log").write_text("\n".join(lines), encoding="utf-8")
    main = pd.concat([r for r in recs if r.grp.iloc[0] == "main"], ignore_index=True)
    # K1: синтетические цели — тройка расстояний случайного настоящего события той же ячейки (sig × d), пул по всем символам
    rng = np.random.default_rng(SEED)
    for s, sig, d, ik in k1_pending:
        pool = main[(main.sig == sig) & (main.d == d)]
        if len(pool) == 0:
            continue
        L = Ls[s]
        pick = rng.integers(0, len(pool), size=len(ik) * K1_DRAWS)
        idx_r = np.repeat(ik, K1_DRAWS)
        par = np.repeat(np.arange(len(ik)), K1_DRAWS)
        e_r = L["c"][idx_r]
        a_r = L["atr"][idx_r]
        tg = {q: e_r + d * pool[f"dist_{q}"].to_numpy()[pick] * a_r for q in TARGETS}
        recs.append(event_frame(L, "K1", sig, d, idx_r, e_r, tg, np.full(len(idx_r), "none"),
                                np.full(len(idx_r), 1.0 / K1_DRAWS), par, L["cumf"]))
    ev = pd.concat(recs, ignore_index=True)
    log.append(f"фандинг: собственный ряд у {n_fund_own}/{len(symbols)} символов")
    return ev, Ls, pd.DataFrame(base)


# ============================================================================ статистика
def wq(x, w, q):
    x = np.asarray(x, float)
    w = np.asarray(w, float)
    m = np.isfinite(x) & (w > 0)
    if m.sum() == 0:
        return np.full(np.shape(q), np.nan) if np.ndim(q) else np.nan
    x, w = x[m], w[m]
    o = np.argsort(x)
    x, w = x[o], w[o]
    cw = (np.cumsum(w) - 0.5 * w) / w.sum()
    return np.interp(q, cw, x)


def wmean(x, w):
    x = np.asarray(x, float)
    w = np.asarray(w, float)
    m = np.isfinite(x)
    return float((x[m] * w[m]).sum() / w[m].sum()) if w[m].sum() > 0 else np.nan


class Boot:
    """Кластерный бутстрап: кластер = неделя (или COARSE_WEEKS недель), общие веса для всех групп."""

    def __init__(self, clusters: np.ndarray, rng: np.random.Generator, n_boot: int = N_BOOT):
        uniq, self.inv = np.unique(clusters, return_inverse=True)
        self.nc = len(uniq)
        self.W = rng.multinomial(self.nc, np.full(self.nc, 1.0 / self.nc), size=n_boot).astype(float)

    def dist(self, x: np.ndarray, w: np.ndarray, m: np.ndarray) -> np.ndarray:
        mm = m & np.isfinite(x)
        S = np.bincount(self.inv[mm], weights=(x * w)[mm], minlength=self.nc)
        N = np.bincount(self.inv[mm], weights=w[mm], minlength=self.nc)
        with np.errstate(invalid="ignore", divide="ignore"):
            return (self.W @ S) / (self.W @ N)


def ci(arr, q=2.5):
    a = arr[np.isfinite(arr)]
    if len(a) == 0:
        return (np.nan, np.nan)
    return (float(np.percentile(a, q)), float(np.percentile(a, 100 - q)))


def reach(ev: pd.DataFrame, name: str, days: int) -> np.ndarray:
    off = ev[f"off_{name}"].to_numpy()
    return ((off > 0) & (off <= days * 6)).astype(float)


def cell_rows(e: pd.DataFrame, B_week: Boot, B_quarter: Boot, sig: str, pop: str) -> list[dict]:
    """Строки results.csv по ячейке (pop, sig): группы main/K1/K2 × цели × горизонты + время, MAE, плечо,
    ликвидации, фандинг; затем разности main − K1 и main − K2 с ДИ."""
    rows = []
    boots = {"week": B_week, "quarter": B_quarter}
    for grp in ("main", "K1", "K2"):
        g = e[e.grp == grp]
        w = g.w.to_numpy()
        r = {"pop": pop, "sig": sig, "grp": grp, "n": float(w.sum()), "n_rows": len(g),
             "n_trunc": float(w[g.trunc.to_numpy()].sum()), "n_lost": float(w[g.lost.to_numpy()].sum())}
        for name in TARGETS:
            for H in HORIZONS:
                r[f"reach_{name}_{H}"] = wmean(reach(g, name, H), w) * 100
            off = g[f"off_{name}"].to_numpy()
            hit = off > 0
            days = off[hit] / 6
            r[f"t50_{name}"], r[f"t80_{name}"] = wq(days, w[hit], [0.5, 0.8]) if hit.any() else (np.nan, np.nan)
            for unit in ("atr", "pct"):
                mae = g[f"mae_{name}_{unit}"].to_numpy()
                q50, q80, q95 = wq(mae[hit], w[hit], [0.5, 0.8, 0.95]) if hit.any() else (np.nan,) * 3
                r[f"mae_{name}_{unit}_p50"], r[f"mae_{name}_{unit}_p80"], r[f"mae_{name}_{unit}_p95"] = q50, q80, q95
            fc = g[f"fund_{name}"].to_numpy()
            r[f"fund_{name}_mean"] = wmean(fc[hit], w[hit]) if hit.any() else np.nan
            r[f"fund_{name}_p50"] = wq(fc[hit], w[hit], 0.5) if hit.any() else np.nan
        nh = g.off_ce.to_numpy() == 0
        m90a, m90p = g.mae90_atr.to_numpy(), g.mae90_pct.to_numpy()
        r["mae90_nohit_atr_p50"], r["mae90_nohit_atr_p80"] = wq(m90a[nh], w[nh], [0.5, 0.8]) if nh.any() else (np.nan, np.nan)
        r["mae90_nohit_pct_p50"], r["mae90_nohit_pct_p80"] = wq(m90p[nh], w[nh], [0.5, 0.8]) if nh.any() else (np.nan, np.nan)
        mh = g.mae_hold_ce_pct.to_numpy()
        r["mae_hold_ce_pct_p50"], r["mae_hold_ce_pct_p80"], r["mae_hold_ce_pct_p95"] = wq(mh, w, [0.5, 0.8, 0.95])
        for q in ("p50", "p80", "p95"):
            mp = r[f"mae_ce_pct_{q}"]
            r[f"lev_{q}"] = 1.0 / (mp / 100 + MMR) if np.isfinite(mp) else np.nan
        r["lev95_hold"] = 1.0 / (r["mae_hold_ce_pct_p95"] / 100 + MMR) if np.isfinite(r["mae_hold_ce_pct_p95"]) else np.nan
        mce = g.mae_ce_pct.to_numpy()
        for Lv in LEVS:
            thr = 100.0 / Lv - MMR * 100
            liq_before = np.isfinite(mce) & (mce >= thr)
            for H in (30, 90):
                ok = (reach(g, "ce", H) > 0) & ~liq_before
                r[f"ce{H}_noliq_L{Lv}"] = wmean(ok.astype(float), w) * 100
            r[f"liq90_L{Lv}"] = wmean((mh >= thr).astype(float), w) * 100
        r["fund30_mean"] = wmean(g.fund30.to_numpy(), w)
        r["fund30_p50"] = wq(g.fund30.to_numpy(), w, 0.5)
        r["fund90_mean"] = wmean(g.fund90.to_numpy(), w)
        r["dist_near_p50"] = wq(g.dist_near.to_numpy(), w, 0.5)
        r["dist_ce_p50"] = wq(g.dist_ce.to_numpy(), w, 0.5)
        r["dist_far_p50"] = wq(g.dist_far.to_numpy(), w, 0.5)
        r["size_atr_p50"] = wq(g.size_atr.to_numpy(), w, 0.5)
        rows.append(r)
    # разности main − K1 / main − K2 с ДИ (недельные и квартальные кластеры)
    m_main = (e.grp == "main").to_numpy()
    m_k1 = (e.grp == "K1").to_numpy()
    m_k2 = (e.grp == "K2").to_numpy()
    w = e.w.to_numpy()
    rd = {"pop": pop, "sig": sig, "grp": "diff_main_K1", "n": float(w[m_main].sum()), "n_rows": int(m_main.sum())}
    rd2 = {"pop": pop, "sig": sig, "grp": "diff_main_K2", "n": float(w[m_main].sum()), "n_rows": int(m_main.sum())}
    for name in TARGETS:
        for H in HORIZONS:
            x = reach(e, name, H) * 100
            for mk, rr in ((m_k1, rd), (m_k2, rd2)):
                rr[f"reach_{name}_{H}"] = wmean(x[m_main], w[m_main]) - wmean(x[mk], w[mk])
                for bname, B in boots.items():
                    dd = B.dist(x, w, m_main) - B.dist(x, w, mk)
                    lo, hi = ci(dd)
                    rr[f"reach_{name}_{H}_{bname}_lo"], rr[f"reach_{name}_{H}_{bname}_hi"] = lo, hi
                    if H == 30 and name == "ce":
                        lo2, hi2 = ci(dd, 1.25)
                        rr[f"reach_{name}_{H}_{bname}_lo975"], rr[f"reach_{name}_{H}_{bname}_hi975"] = lo2, hi2
    if m_main.sum() == 0:
        rows += [rd, rd2]
        return rows
    tmed = np.median(e.t.to_numpy()[m_main])
    for half, mh_ in (("h1", e.t.to_numpy() <= tmed), ("h2", e.t.to_numpy() > tmed)):
        x = reach(e, "ce", 30) * 100
        a, b = m_main & mh_, m_k1 & mh_
        rd[f"ce30_{half}_main"] = wmean(x[a], w[a])
        rd[f"ce30_{half}_K1"] = wmean(x[b], w[b])
        rd[f"ce30_{half}_diff"] = rd[f"ce30_{half}_main"] - rd[f"ce30_{half}_K1"]
        dd = B_week.dist(x, w, a) - B_week.dist(x, w, b)
        rd[f"ce30_{half}_lo"], rd[f"ce30_{half}_hi"] = ci(dd)
        rd[f"ce30_{half}_n"] = float(w[a].sum())
    rd["t_median"] = str(pd.to_datetime(tmed, unit="ms").date())
    rows += [rd, rd2]
    return rows


def stop_rows(ev: pd.DataFrame, Ls: dict) -> tuple[list[dict], pd.DataFrame]:
    """R при стопе на P95 MAE (по достигшим CE в ячейке), цель near/CE, без тайм-аута до HMAX."""
    rows = []
    trades = []
    for sig in SIGNALS:
        for grp in ("main", "K1"):
            g = ev[(ev.sig == sig) & (ev.grp == grp)]
            if len(g) == 0:
                continue
            hit = g.off_ce.to_numpy() > 0
            p95 = float(wq(g.mae_ce_atr.to_numpy()[hit], g.w.to_numpy()[hit], 0.95)) if hit.any() else np.nan
            if not np.isfinite(p95):
                continue
            for tname in ("near", "ce"):
                parts = []
                for s, gs in g.groupby("sym"):
                    L = Ls[s]
                    idx = gs.idx.to_numpy()
                    d = gs.d.to_numpy()
                    entry = gs.entry.to_numpy()
                    atr = gs.atr.to_numpy()
                    sl = entry - d * p95 * atr
                    tp = entry + d * gs[f"dist_{tname}"].to_numpy() * atr
                    r = sim_stop(L, idx, d, entry, sl, tp, L["cumf"])
                    parts.append(pd.DataFrame({"sig": sig, "grp": grp, "tgt": tname, "sym": s, "t": gs.t.to_numpy(),
                                               "w": gs.w.to_numpy(), "d": d, "delisted": gs.delisted.to_numpy(), **r}))
                tr = pd.concat(parts, ignore_index=True)
                trades.append(tr)
                w = tr.w.to_numpy()
                R = tr.R_net.to_numpy()
                Rf = R - tr.fund_R.to_numpy()
                pos = (R > 0)
                pf = float((R[pos] * w[pos]).sum() / max(1e-12, -(R[~pos] * w[~pos]).sum()))
                rows.append({"sig": sig, "grp": grp, "tgt": tname, "stop_atr_p95": p95, "n": float(w.sum()),
                             "tp": wmean(tr.tp, w) * 100, "sl": wmean(tr.sl, w) * 100, "to": wmean(tr.to, w) * 100,
                             "R_net": wmean(R, w), "R_net_fund": wmean(Rf, w), "PF": pf,
                             "fund_R_mean": wmean(tr.fund_R, w), "exit_days_p50": wq(tr.exit_off / 6, w, 0.5)})
    return rows, (pd.concat(trades, ignore_index=True) if trades else pd.DataFrame())


# (col, заголовок, сопоставление K1): False — весь K1 ячейки; True — K1 с тем же значением col;
# имя колонки — K1 с тем же значением этой колонки (значение берётся после « | » в составном ключе).
# Разрезы с пометкой «пост-хок» добавлены после просмотра первых чисел (отступление от plan.md): у 1w-гэпов и крупных гэпов
# CE дальше от входа (медиана 5.9 ATR против 4.1 у 1d и 4.3 у K1), поэтому сравнение со всем K1 несправедливо.
DIST_EDGES = (1.5, 2.5, 3.5, 4.5, 6.0, np.inf)
SLICES = [("gap_tf", "ТФ гэпа", False), ("dir", "направление", True), ("trend", "тренд 1d", True),
          ("trend_w", "тренд 1w", True), ("trend_both", "тренд 1d+1w", True),
          ("size_bin", "размер гэпа (ATR 4h)", False), ("year", "год", True),
          ("cap", "BTC/ETH / топ-50 / остальные", True), ("delisted_s", "символ делистнут", True),
          ("dist_bin", "расстояние до CE, ATR (пост-хок)", True),
          ("tf_dist", "ТФ гэпа | расстояние до CE (пост-хок, K1 той же дистанции)", "dist_bin"),
          ("size_dist", "размер гэпа | расстояние до CE (пост-хок, K1 той же дистанции)", "dist_bin"),
          ("sym", "символ", True)]


def add_slice_cols(ev: pd.DataFrame) -> pd.DataFrame:
    main_all = ev[ev.grp == "main"]
    ter = np.quantile(main_all.size_atr.to_numpy(), [1 / 3, 2 / 3])
    ev = ev.copy()
    ev["size_bin"] = np.where(ev.size_atr < ter[0], f"1: <{ter[0]:.2f}",
                              np.where(ev.size_atr < ter[1], f"2: {ter[0]:.2f}–{ter[1]:.2f}", f"3: ≥{ter[1]:.2f}"))
    labels = [f"{i + 1}: {a:g}–{b:g}" if np.isfinite(b) else f"{i + 1}: ≥{a:g}" for i, (a, b) in enumerate(zip(DIST_EDGES[:-1], DIST_EDGES[1:]))]
    k = np.clip(np.searchsorted(np.array(DIST_EDGES[1:-1]), ev.dist_ce.to_numpy(), "right"), 0, len(labels) - 1)
    ev["dist_bin"] = np.array(labels)[k]
    ev["tf_dist"] = ev.gap_tf.astype(str) + " | " + ev.dist_bin
    ev["size_dist"] = ev.size_bin.str[:1] + " | " + ev.dist_bin
    ev["dir"] = np.where(ev.d > 0, "long", "short")
    ev["trend"] = np.where(np.isnan(ev.trend_with), "n/a", np.where(ev.trend_with > 0, "по тренду", "против"))
    ev["trend_w"] = np.where(np.isnan(ev.trend_with_w), "n/a", np.where(ev.trend_with_w > 0, "по тренду", "против"))
    both = np.where(np.isnan(ev.trend_with) | np.isnan(ev.trend_with_w), "n/a",
                    np.where((ev.trend_with > 0) & (ev.trend_with_w > 0), "оба по тренду",
                             np.where((ev.trend_with == 0) & (ev.trend_with_w == 0), "оба против", "разнобой")))
    ev["trend_both"] = both
    ev["cap"] = np.where(ev.major, "1: BTC/ETH", np.where(ev.top50, "2: топ-50", "3: остальные"))
    ev["delisted_s"] = np.where(ev.delisted, "да", "нет")
    return ev


def slice_rows(ev: pd.DataFrame, boots_week: dict, pop: str) -> list[dict]:
    """Разрезы: доля достижения CE за 30/90 дней, main vs K1 (K1 — тот же разрез, где он определён)."""
    rows = []
    for sig in SIGNALS:
        e = ev[ev.sig == sig]
        w = e.w.to_numpy()
        B = boots_week[sig]
        for col, title, k1_match in SLICES:
            vals = sorted(e.loc[e.grp == "main", col].dropna().unique(), key=str)
            for v in vals:
                m_main = ((e.grp == "main") & (e[col] == v)).to_numpy()
                if k1_match is True:
                    k1_sel = e[col] == v
                elif isinstance(k1_match, str):
                    k1_sel = e[k1_match] == str(v).split(" | ")[-1]
                else:
                    k1_sel = True
                m_k1 = ((e.grp == "K1") & k1_sel).to_numpy()
                r = {"pop": pop, "sig": sig, "slice": title, "value": str(v), "n_main": float(w[m_main].sum()),
                     "n_K1": float(w[m_k1].sum())}
                for H in (30, 90):
                    x = reach(e, "ce", H) * 100
                    r[f"ce{H}_main"] = wmean(x[m_main], w[m_main])
                    r[f"ce{H}_K1"] = wmean(x[m_k1], w[m_k1])
                    r[f"ce{H}_diff"] = r[f"ce{H}_main"] - r[f"ce{H}_K1"]
                    dd = B.dist(x, w, m_main) - B.dist(x, w, m_k1)
                    r[f"ce{H}_lo"], r[f"ce{H}_hi"] = ci(dd)
                    xn = reach(e, "near", H) * 100
                    r[f"near{H}_main"] = wmean(xn[m_main], w[m_main])
                    r[f"near{H}_K1"] = wmean(xn[m_k1], w[m_k1])
                hit = m_main & (e.off_ce.to_numpy() > 0)
                r["mae_ce_pct_p95"] = wq(e.mae_ce_pct.to_numpy()[hit], w[hit], 0.95) if hit.any() else np.nan
                r["t50_ce"] = wq(e.off_ce.to_numpy()[hit] / 6, w[hit], 0.5) if hit.any() else np.nan
                mh = e.mae_hold_ce_pct.to_numpy()
                for Lv in (3, 5):
                    thr = 100.0 / Lv - MMR * 100
                    r[f"liq90_L{Lv}_main"] = wmean((mh[m_main] >= thr).astype(float), w[m_main]) * 100
                rows.append(r)
    return rows


# ============================================================================ проверки данных
def data_checks(Ls: dict) -> list[str]:
    out = []
    p = C_GAPS / "BTCUSDT_1w.csv"
    if "BTC" in Ls and "htf" in Ls["BTC"] and p.exists():
        nat = load(p)
        agg = Ls["BTC"]["htf"]["1w"]
        m = agg.merge(nat, on="open_time", suffixes=("_a", "_n"))
        bad = 0
        for k in ("open", "high", "low", "close"):
            bad += int((np.abs(m[f"{k}_a"] - m[f"{k}_n"]) / m[f"{k}_n"] > 1e-9).sum())
        out.append(f"сверка 1w BTC: агрегат 4h vs родной 1w Binance (gaps/cache), общих недель {len(m)}, расхождений OHLC {bad}")
    p = C_GAPS / "BTCUSDT_1d.csv"
    if "BTC" in Ls and "htf" in Ls["BTC"] and p.exists():
        nat = load(p)
        agg = Ls["BTC"]["htf"]["1d"]
        m = agg.merge(nat, on="open_time", suffixes=("_a", "_n"))
        bad = int((np.abs(m.close_a - m.close_n) / m.close_n > 1e-9).sum())
        out.append(f"сверка 1d BTC: агрегат 4h vs родной 1d, общих дней {len(m)}, расхождений close {bad}")
    miss = {s: L["missing_4h"] for s, L in Ls.items()}
    tot = sum(miss.values())
    nz = {s: v for s, v in miss.items() if v > 0}
    out.append(f"пропущенных 4h-баров внутри рядов: {tot} у {len(nz)} символов из {len(Ls)}"
               + (f" ({', '.join(f'{s}:{v}' for s, v in sorted(nz.items(), key=lambda kv: -kv[1])[:10])})" if nz else ""))
    out.append(f"фандинг: own={sum(L['fund_src'] == 'own' for L in Ls.values())}, "
               f"avg={sum(L['fund_src'] == 'avg' for L in Ls.values())}, none={sum(L['fund_src'] == 'none' for L in Ls.values())}")
    return out


# ============================================================================ отчёт
def f1(x, nd=1):
    return "—" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


def fs(x, nd=1):
    return "—" if x is None or not np.isfinite(x) else f"{x:+.{nd}f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def verdict(rd: dict) -> tuple[str, list[str]]:
    notes = []
    diff = rd["reach_ce_30"]
    lo_w, lo_q = rd["reach_ce_30_week_lo"], rd["reach_ce_30_quarter_lo"]
    lo_w2, lo_q2 = rd["reach_ce_30_week_lo975"], rd["reach_ce_30_quarter_lo975"]
    halves = rd["ce30_h1_diff"] > 0 and rd["ce30_h2_diff"] > 0
    notes.append(f"Δ CE30 = {diff:+.1f} п.п.; 95% ДИ недели [{rd['reach_ce_30_week_lo']:+.1f}; {rd['reach_ce_30_week_hi']:+.1f}], "
                 f"кварталы [{rd['reach_ce_30_quarter_lo']:+.1f}; {rd['reach_ce_30_quarter_hi']:+.1f}]; "
                 f"97.5%: недели от {lo_w2:+.1f}, кварталы от {lo_q2:+.1f}")
    notes.append(f"половины периода: {rd['ce30_h1_diff']:+.1f} / {rd['ce30_h2_diff']:+.1f} п.п. (граница {rd['t_median']})")
    sig95 = min(lo_w, lo_q) > 0
    sig975 = min(lo_w2, lo_q2) > 0
    if diff >= 10 and sig95 and halves:
        return ("ПОДТВЕРЖДЕНО" if sig975 else "НАМЁК (95%, не 97.5%)"), notes
    if diff > 0 and sig95:
        return ("СЛАБЫЙ ЭФФЕКТ (< 10 п.п.)" + ("" if halves else ", не в обеих половинах")), notes
    return "НЕ ПОДТВЕРЖДЕНО", notes


def write_report(res: pd.DataFrame, srows: pd.DataFrame, slc: pd.DataFrame, base: pd.DataFrame, ev: pd.DataFrame,
                 log: list[str], checks: list[str], symbols: list[str], meta: dict, t_run: float) -> str:
    L = []
    n_del = sum(meta[s]["delisted"] for s in symbols)
    main = ev[ev.grp == "main"]
    R = lambda pop, sig, grp: res[(res["pop"] == pop) & (res.sig == sig) & (res.grp == grp)].iloc[0]  # noqa: E731
    L.append("# «Куда пойдёт цена»: достижение HTF-гэпа после разворотного сигнала 4h, без стопа и тайм-аута — универсум 547\n")
    L.append(f"Расчёт {pd.Timestamp.now('UTC').strftime('%Y-%m-%d %H:%M')} UTC, hold_study.py, {t_run / 60:.1f} мин. "
             f"План — plan.md (части 2 и 2a, записаны до расчёта).")
    L.append(f"Символов {len(symbols)} (делистнутых {n_del}), 4h-баров {int(base.groupby('sym').bars.first().sum()):,}. "
             f"Горизонт удержания до {HMAX_DAYS} дней; у живых символов события без полных {HMAX} баров впереди исключены; "
             f"у делистнутых — включены с усечением (делистинг до CE = потеря позиции, MAE 100 %).")
    L.append(f"Событий main (сигнал + гэп впереди): {len(main):,}, из них усечённых {int(main.trunc.sum()):,}, "
             f"потерянных при делистинге {int(main.lost.sum()):,}; K1 (сигнал без гэпа) {int((ev.grp == 'K1').sum() / K1_DRAWS):,} событий × {K1_DRAWS}; "
             f"K2 {int((ev.grp == 'K2').sum()):,}. Период сигналов {pd.to_datetime(main.t.min(), unit='ms').date()} … "
             f"{pd.to_datetime(main.t.max(), unit='ms').date()}.\n")
    # --- вердикт
    L.append("## Вердикт по заранее объявленному критерию (CE за 30 дней, main − K1 ≥ 10 п.п., ДИ вне нуля, обе половины периода)\n")
    L.append("Население для вердикта — «с делистнутыми» (plan.md 2a). Вариант «без делистнутых» — рядом, в вердикте не участвует.\n")
    vrows = []
    for pop in ("all", "live"):
        for sig in SIGNALS:
            rd = R(pop, sig, "diff_main_K1").to_dict()
            v, notes = verdict(rd)
            vrows.append([POP_NAME[pop], SIG_NAME[sig], f"**{v}**" if pop == "all" else v, "; ".join(notes)])
    L.append(md_table(["Население", "Ячейка", "Вердикт", "Основание"], vrows) + "\n")
    # --- 1. доли достижения
    L.append("## 1. Доля событий, после которых цена дошла до цели (%, без стопа), население «с делистнутыми»\n")
    L.append("main — сигнал + гэп 1d/1w впереди; K1 — тот же сигнал без гэпа (≤10 ATR), цель на том же расстоянии (синтетика); "
             "K2 — гэп впереди, вход в случайный бар ±100 без сигнала. Δ — main − K1 [95% ДИ, кластер = неделя].\n")
    for sig in SIGNALS:
        r = {g: R("all", sig, g) for g in ("main", "K1", "K2", "diff_main_K1")}
        L.append(f"### {SIG_NAME[sig]} — n: main {r['main'].n:.0f}, K1 {r['K1'].n:.0f} (×{K1_DRAWS} розыгрыша), K2 {r['K2'].n:.0f}\n")
        rows = []
        for name in TARGETS:
            for H in HORIZONS:
                k = f"reach_{name}_{H}"
                rows.append([TGT_NAME[name], H, f1(r["main"][k]), f1(r["K1"][k]), f1(r["K2"][k]),
                             f"{fs(r['diff_main_K1'][k])} [{fs(r['diff_main_K1'][k + '_week_lo'])}; {fs(r['diff_main_K1'][k + '_week_hi'])}]"])
        L.append(md_table(["Цель", "дней", "main", "K1", "K2", "Δ main−K1 [ДИ]"], rows) + "\n")
        L.append(f"Медианные расстояния (ATR 4h) main: near {f1(r['main'].dist_near_p50, 2)}, CE {f1(r['main'].dist_ce_p50, 2)}, "
                 f"far {f1(r['main'].dist_far_p50, 2)}; K1: near {f1(r['K1'].dist_near_p50, 2)}, CE {f1(r['K1'].dist_ce_p50, 2)}.\n")
    L.append("### С делистнутыми vs без (CE и near, main и K1)\n")
    rows = []
    for sig in SIGNALS:
        for pop in ("all", "live"):
            rm, rk, rd = R(pop, sig, "main"), R(pop, sig, "K1"), R(pop, sig, "diff_main_K1")
            rows.append([SIG_NAME[sig], POP_NAME[pop], f"{rm.n:.0f}", f1(rm.reach_near_30), f1(rm.reach_ce_30), f1(rm.reach_ce_90),
                         f1(rk.reach_ce_30), f1(rk.reach_ce_90),
                         f"{fs(rd.reach_ce_30)} [{fs(rd.reach_ce_30_week_lo)}; {fs(rd.reach_ce_30_week_hi)}]",
                         f"{fs(rd.reach_ce_90)} [{fs(rd.reach_ce_90_week_lo)}; {fs(rd.reach_ce_90_week_hi)}]"])
    L.append(md_table(["Ячейка", "население", "n main", "near30 main", "CE30 main", "CE90 main", "CE30 K1", "CE90 K1",
                       "Δ CE30 [ДИ]", "Δ CE90 [ДИ]"], rows) + "\n")
    # --- 2. время
    L.append("## 2. Время до цели (дни; среди достигших за 90 дней — условные оценки, недостигшие цензурированы)\n")
    rows = []
    for sig in SIGNALS:
        for grp in ("main", "K1", "K2"):
            r = R("all", sig, grp)
            rows.append([SIG_NAME[sig], grp] + [f"{f1(r[f't50_{n}'])} / {f1(r[f't80_{n}'])}" for n in TARGETS])
    L.append(md_table(["Ячейка", "группа", "near P50/P80", "CE P50/P80", "full P50/P80"], rows) + "\n")
    # --- 3. MAE и плечо
    L.append("## 3. Цена удержания: MAE против входа до прихода в цель (достигшие), плечо выживания\n")
    L.append(f"Плечо L_max = 1/(MAE% + {MMR * 100:.1f}%) — изолированная маржа, грубо, без комиссий/фандинга; "
             "для альтов MMR выше → меньше. «Недостигшие» — MAE за 90 дней у тех, кто CE не дошёл (делистинг = 100 %). "
             "«Удержание до CE» — MAE до касания у достигших, MAE90 у недостигших: то, что реально переживает позиция.\n")
    rows = []
    for pop in ("all", "live"):
        for sig in SIGNALS:
            for grp in ("main", "K1"):
                r = R(pop, sig, grp)
                rows.append([POP_NAME[pop], SIG_NAME[sig], grp,
                             f"{f1(r.mae_near_atr_p50, 2)} / {f1(r.mae_near_atr_p80, 2)} / {f1(r.mae_near_atr_p95, 2)}",
                             f"{f1(r.mae_ce_atr_p50, 2)} / {f1(r.mae_ce_atr_p80, 2)} / {f1(r.mae_ce_atr_p95, 2)}",
                             f"{f1(r.mae_ce_pct_p50)} / {f1(r.mae_ce_pct_p80)} / {f1(r.mae_ce_pct_p95)}",
                             f"{f1(r.lev_p50)} / {f1(r.lev_p80)} / {f1(r.lev_p95)}",
                             f"{f1(r.mae90_nohit_pct_p50)} / {f1(r.mae90_nohit_pct_p80)}",
                             f"{f1(r.mae_hold_ce_pct_p50)} / {f1(r.mae_hold_ce_pct_p80)} / {f1(r.mae_hold_ce_pct_p95)}",
                             f1(r.lev95_hold, 2)])
    L.append(md_table(["население", "Ячейка", "группа", "MAE→near, ATR P50/80/95", "MAE→CE, ATR P50/80/95", "MAE→CE, % P50/80/95",
                       "L_max (достигшие) P50/80/95", "недостигшие: MAE90 % P50/80", "удержание до CE: MAE % P50/80/95",
                       "L «додержать 95 %»"], rows) + "\n")
    # --- 3b. ликвидации
    L.append("## 3b. «Додержать без ликвидации»: доля событий, где CE достигнут ДО ликвидации при плече L, и доля ликвидаций за 90 дней\n")
    L.append(f"Изолированная маржа, MMR {MMR * 100:.1f}% → ликвидация при ходе против ≥ 100/L − {MMR * 100:.1f}%. "
             "Удержание до CE или до 90 дней; делистинг до CE = ликвидация. Комиссии и фандинг в порог не включены (реально хуже).\n")
    for pop in ("all", "live"):
        L.append(f"### Население «{POP_NAME[pop]}»\n")
        rows = []
        for sig in SIGNALS:
            for grp in ("main", "K1"):
                r = R(pop, sig, grp)
                rows.append([SIG_NAME[sig], grp, f1(r.reach_ce_30), f1(r.reach_ce_90)]
                            + [f"{f1(r[f'ce30_noliq_L{Lv}'])} / {f1(r[f'ce90_noliq_L{Lv}'])} / {f1(r[f'liq90_L{Lv}'])}" for Lv in LEVS])
        L.append(md_table(["Ячейка", "группа", "CE30 без плеча", "CE90 без плеча"]
                          + [f"L={Lv}: CE30 / CE90 / ликв.90" for Lv in LEVS], rows) + "\n")
    # --- 4. стоп на P95
    L.append("## 4. Если всё же ставить стоп на P95 MAE (ячейка, достигшие CE) — R после комиссий, без тайм-аута (население «с делистнутыми»)\n")
    L.append("1R = ход до стопа + проскальзывание 0.05% + taker 0.05% × 2 (как OSKAR risk/sizing.py). "
             "P95 взят по той же выборке (in-sample) — оценка оптимистична. R_net+фандинг — с вычетом фандинга за время сделки. "
             "Усечённые события без касаний закрыты по последнему close данных.\n")
    rows = []
    for _, r in srows.iterrows():
        rows.append([SIG_NAME[r.sig], r.grp, TGT_NAME[r.tgt], f1(r.stop_atr_p95, 2), f"{r.n:.0f}", f1(r.tp), f1(r.sl), f1(r.to),
                     fs(r.R_net, 3), fs(r.R_net_fund, 3), f1(r.PF, 2), f1(r.exit_days_p50)])
    L.append(md_table(["Ячейка", "группа", "цель", "стоп, ATR", "n", "TP %", "SL %", "90д %", "R_net", "R_net+фандинг", "PF", "выход, дней P50"], rows) + "\n")
    # --- 5. фандинг
    L.append("## 5. Фандинг за время удержания (% от notional, знак «+» = платим), население «с делистнутыми»\n")
    rows = []
    for sig in SIGNALS:
        for grp in ("main", "K1"):
            r = R("all", sig, grp)
            rows.append([SIG_NAME[sig], grp, f"{fs(r.fund_near_mean, 3)} / {fs(r.fund_near_p50, 3)}",
                         f"{fs(r.fund_ce_mean, 3)} / {fs(r.fund_ce_p50, 3)}", f"{fs(r.fund30_mean, 3)} / {fs(r.fund30_p50, 3)}",
                         fs(r.fund90_mean, 3)])
    L.append(md_table(["Ячейка", "группа", "до near: mean / P50", "до CE: mean / P50", "30 дней: mean / P50", "90 дней: mean"], rows) + "\n")
    # --- 6. разрезы
    L.append("## 6. Разрезы (население «с делистнутыми»): доля достижения CE за 30 и 90 дней, main vs K1 (Δ с 95% ДИ, недельные кластеры)\n")
    L.append("Для разрезов «ТФ гэпа» и «размер гэпа» K1 не имеет такого признака — сравнение со всем K1 ячейки. "
             "Разрезы с пометкой «пост-хок» добавлены после просмотра первых чисел (отступление от plan.md): у 1w-гэпов и крупных гэпов "
             "CE дальше от входа (медиана 5.9 ATR против 4.1 у 1d и 4.3 у K1), поэтому там K1 сопоставлен по той же дистанции до CE. "
             "Много сравнений → часть «значимых» клеток случайна. Разрез по символам — в results_slices.csv (здесь топ-12 по n).\n")
    for sig in SIGNALS:
        L.append(f"### {SIG_NAME[sig]}\n")
        rows = []
        ss = slc[(slc.sig == sig) & (slc["pop"] == "all")]
        for _, r in ss[ss["slice"] != "символ"].iterrows():
            rows.append([r["slice"], r.value, f"{r.n_main:.0f}", f1(r.ce30_main), f1(r.ce30_K1),
                         f"{fs(r.ce30_diff)} [{fs(r.ce30_lo)}; {fs(r.ce30_hi)}]", f1(r.ce90_main), f1(r.ce90_K1),
                         f"{fs(r.ce90_diff)} [{fs(r.ce90_lo)}; {fs(r.ce90_hi)}]", f1(r.mae_ce_pct_p95), f1(r.t50_ce), f1(r.liq90_L3_main)])
        top = ss[ss["slice"] == "символ"].sort_values("n_main", ascending=False).head(12)
        for _, r in top.iterrows():
            rows.append([r["slice"], r.value, f"{r.n_main:.0f}", f1(r.ce30_main), f1(r.ce30_K1),
                         f"{fs(r.ce30_diff)} [{fs(r.ce30_lo)}; {fs(r.ce30_hi)}]", f1(r.ce90_main), f1(r.ce90_K1),
                         f"{fs(r.ce90_diff)} [{fs(r.ce90_lo)}; {fs(r.ce90_hi)}]", f1(r.mae_ce_pct_p95), f1(r.t50_ce), f1(r.liq90_L3_main)])
        L.append(md_table(["разрез", "значение", "n main", "CE30 main", "CE30 K1", "Δ30 [ДИ]", "CE90 main", "CE90 K1", "Δ90 [ДИ]",
                           "MAE→CE % P95", "t50 CE, дн", "ликв.90 при 3×, %"], rows) + "\n")
    # --- 7. половины
    L.append("## 7. Половины периода (CE за 30 дней, main − K1)\n")
    rows = []
    for pop in ("all", "live"):
        for sig in SIGNALS:
            rd = R(pop, sig, "diff_main_K1")
            for h in ("h1", "h2"):
                rows.append([POP_NAME[pop], SIG_NAME[sig], "до" if h == "h1" else "после", rd.t_median, f"{rd[f'ce30_{h}_n']:.0f}",
                             f1(rd[f"ce30_{h}_main"]), f1(rd[f"ce30_{h}_K1"]),
                             f"{fs(rd[f'ce30_{h}_diff'])} [{fs(rd[f'ce30_{h}_lo'])}; {fs(rd[f'ce30_{h}_hi'])}]"])
    L.append(md_table(["население", "Ячейка", "половина", "граница", "n main", "main", "K1", "Δ [ДИ]"], rows) + "\n")
    # --- 8. база сигналов
    L.append("## 8. База сигналов (сигналов 4h / с гэпом впереди = main / без гэпа ≤10 ATR = K1)\n")
    b = base.groupby(["sig", "delisted"], sort=False)[["n_sig", "n_gap", "n_k1"]].sum().reset_index()
    rows = [[SIG_NAME[r.sig], "делистнутые" if r.delisted else "живые", f"{r.n_sig:,}", f"{r.n_gap:,}", f"{r.n_k1:,}"] for r in b.itertuples()]
    L.append(md_table(["сигнал", "символы", "сигналов", "с гэпом (main)", "без гэпа (K1)"], rows) + "\n")
    bs = base.groupby("sym")[["n_sig", "n_gap"]].sum().sort_values("n_gap", ascending=False).head(10)
    L.append("Топ-10 символов по числу main-событий: " + ", ".join(f"{s} {int(r.n_gap)}" for s, r in bs.iterrows()) + ".\n")
    # --- 9. данные
    L.append("## 9. Данные, проверки, лог\n")
    L += [f"- {c}" for c in checks]
    L += [f"- {x}" for x in log]
    L.append("- Пер-символьный лог — hold_study.log; события — events.csv.gz; результаты — results.csv, results_slices.csv, results_stop.csv.")
    L.append("")
    L.append("## 10. Ограничения (честно)\n")
    L += [
        "- Делистнутые: «потеря 100 %» при делистинге до прихода в CE — допущение по заданию; реально Binance закрывает позиции по"
        " settlement-цене (убыток обычно меньше 100 %, но крах перед делистингом в данных есть). 5 делистнутых без данных и символы,"
        " не попавшие ни в exchangeInfo, ни в vision-листинг, не учтены — остаток ошибки выжившего неоценим.",
        "- Переименования (MATIC→POL и т.п.) не склеены; каждый тикер — отдельная история.",
        "- Цели фиксированы на момент сигнала; «полное закрытие» = дальняя граница гэпа на момент сигнала.",
        "- MAE включает бар касания цели (консервативно: внутрибарный порядок high/low неизвестен); фандинг и комиссии в MAE не входят.",
        "- Плечо выживания — грубая формула изолированной маржи с MMR 0.5%; у Binance MMR тирами (BTC 0.4%… альты 1–2.5%+), реальное L_max ниже.",
        "- Стоп на P95 MAE — in-sample (P95 из тех же событий); честная оценка R будет хуже.",
        "- Абсолютные доли достижения на растущем рынке 2020–2025 смещены (лонги к верхним гэпам выигрывают от дрейфа) — поэтому главный"
        " контроль K1 с теми же расстояниями и тем же сигналом, а не абсолютные доли.",
        "- События сильно пересекаются (90 дней удержания, сигналы кластеризуются, 547 символов коррелируют) — недельные ДИ узковаты;"
        " квартальные кластеры даны рядом и входят в критерий.",
        "- Топ-50 определён по объёму за последние 365 дней (только живые) — это признак «сегодня», а не на момент сигнала (look-ahead в разрезе, не в сигналах).",
        "- 1h-сигналы, k=3, (a)/(c) не считались (план: только (b) и (d) на 4h).",
    ]
    return "\n".join(L)


# ============================================================================ main
def main() -> None:
    t_run = time.time()
    args = sys.argv[1:]

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default

    kind = "hold" if opt("--universe") == "hold" else "ten"
    no_fetch = "--no-fetch" in args
    procs = int(opt("--procs", "1"))
    symbols, meta = universe(kind)
    if opt("--symbols"):
        want = opt("--symbols").split(",")
        symbols = [s for s in symbols if s in want]
    if opt("--limit"):
        symbols = symbols[:int(opt("--limit"))]
    if not symbols:
        print("нет символов с 4h", file=sys.stderr)
        sys.exit(1)
    log: list[str] = []
    if not no_fetch:
        try:
            fetch_funding([s + "USDT" for s in symbols], log)
        except Exception as e:  # noqa: BLE001
            log.append(f"фандинг: докачка прервана {e!r}; продолжаем с тем, что есть")
    ev, Ls, base = build(symbols, meta, log, procs)
    print(f"events: {len(ev)} rows; main {int((ev.grp == 'main').sum())}, K1 rows {int((ev.grp == 'K1').sum())}, "
          f"K2 {int((ev.grp == 'K2').sum())}; trunc {int(ev.trunc.sum())}, lost {int(ev.lost.sum())}", flush=True)
    ev = add_slice_cols(ev)
    ev.to_csv(HERE / "events.csv.gz", index=False, compression="gzip")
    pops = {"all": ev, "live": ev[~ev.delisted]}
    rows, srows_all, slc_rows = [], [], []
    for pop, evp in pops.items():
        boots_week = {}
        for sig in SIGNALS:
            e = evp[evp.sig == sig].reset_index(drop=True)
            B_week = Boot(e.week.to_numpy(), np.random.default_rng(SEED + 1))
            B_quarter = Boot(e.week.to_numpy() // COARSE_WEEKS, np.random.default_rng(SEED + 2))
            boots_week[sig] = B_week
            rows += cell_rows(e, B_week, B_quarter, sig, pop)
            print(f"stats {pop}/{sig} done", flush=True)
        slc_rows += slice_rows(evp.reset_index(drop=True), boots_week, pop)
    res = pd.DataFrame(rows)
    res.to_csv(HERE / "results.csv", index=False)
    srows, trades = stop_rows(ev, Ls)
    srows = pd.DataFrame(srows)
    srows.to_csv(HERE / "results_stop.csv", index=False)
    if len(trades):
        trades.to_csv(HERE / "trades_stop.csv.gz", index=False, compression="gzip")
    slc = pd.DataFrame(slc_rows)
    slc.to_csv(HERE / "results_slices.csv", index=False)
    checks = data_checks(Ls)
    md = write_report(res, srows, slc, base, ev, log, checks, symbols, meta, time.time() - t_run)
    (HERE / "results.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
