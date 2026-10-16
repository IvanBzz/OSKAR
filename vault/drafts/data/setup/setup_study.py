#!/usr/bin/env python
"""Сетап Ивана целиком: незакрытый FVG старшего ТФ как цель + признак разворота на младшем ТФ в его сторону.

План (объявлен до результатов): plan.md рядом. Коротко:
  LTF 1h (3 года) и 4h (вся история); HTF-гэпы 1d+1w (главное), 1w, 4h (вторично).
  Гэп-цель: ближайший живой (не закрытый полностью) гэп в сторону сделки на 1.5–8 ATR_LTF от close.
  Признаки: (a) доджи у 20-барного экстремума, (b) CHoCH по закрытию (фракталы k=2), (c) (a)+подтверждение,
  (d) (b), перед которым был (a). Вход по close сигнального бара, стоп за экстремум разворота ±0.1 ATR,
  тейк near / CE, тайм-аут 48 баров. Издержки как OSKAR risk/sizing.py, 1R = stop_loss_per_unit.
  Контроли: K1 — тот же признак без гэпа (синтетическая цель на том же расстоянии в ATR);
            K2 — гэп есть, вход в случайный бар ±100 баров без признака, стоп той же ширины в ATR.
  ДИ: кластерный бутстрап по UTC-дням (+ крупные кластеры 7д/30д на перекрытие сделок).

Данные — только локальный кеш (Binance USDⓈ-M, ../candles/cache и ../gaps/cache), сеть не нужна.
Запуск (python из .venv OSKAR):  python -I -B setup_study.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DRAFTS = HERE.parent
C_CANDLES = DRAFTS / "candles" / "cache"
C_GAPS = DRAFTS / "gaps" / "cache"
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "SUI"]

HOUR = 3_600_000
DAY = 24 * HOUR
WEEK = 7 * DAY
MONDAY0 = 4 * DAY  # 1970-01-05 — понедельник; недели Binance начинаются в понедельник 00:00 UTC
LTF_MS = {"1h": HOUR, "4h": 4 * HOUR}

# --- параметры (из plan.md)
ATR_N = 14
GAP_MIN_ATR = 0.1
D_MIN, D_MAX = 1.5, 8.0
K1_NOGAP_ATR = 10.0
LOOKBACK = 20
MOVE_ATR = 1.5
DOJI_BODY = 0.10
STOP_BUF_ATR = 0.1
HOLD = 48
WARMUP = 40
TAKER = 0.0005
SLIP = 0.0005
K1_DRAWS = 5
K2_DRAWS = 3
K2_WIN = 100
N_BOOT = 4000
SEED = 20261016
COARSE_DAYS = {"1h": 7, "4h": 30}
N_FAMILY = 16
BONF_Q = 100 * 0.05 / N_FAMILY / 2  # 0.156% хвост

HTF_SETS = {"DW": ("1d", "1w"), "W": ("1w",), "4h": ("4h",)}
SETS_FOR_LTF = {"1h": ("DW", "W", "4h"), "4h": ("DW", "W")}
SIGNALS = ("a", "b", "c", "d", "b_k3", "d_k3")
TPS = ("near", "ce")


# ----------------------------------------------------------------------------- data
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
    full = period // (4 * HOUR)
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
    """Бегущий экстремум с бара s, пока гэп жив (upper: max < thr; lower: min > thr)."""
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


def tf_targets(L: dict, gaps: pd.DataFrame, pre: dict | None) -> dict:
    """Для каждого бара LTF и направления: ближайший гэп в коридоре [D_MIN; D_MAX] ATR и мин. расстояние до любого живого.
    d=+1 (LONG) — медвежьи гэпы над ценой; d=-1 (SHORT) — бычьи под ценой."""
    ot, h, l, c, atr = L["ot"], L["h"], L["l"], L["c"], L["atr"]
    n = len(c)
    out = {d: {"best": np.full(n, np.inf), "near": np.full(n, np.nan), "ce": np.full(n, np.nan),
               "many": np.full(n, np.inf), "size": np.full(n, np.nan)} for d in (1, -1)}
    end_ms = ot[-1] + L["P"]
    for g in gaps.itertuples(index=False):
        if g.known >= end_ms:
            continue
        s = int(np.searchsorted(ot, g.known, "left"))
        if g.bull:  # под ценой, цель SHORT; заполнение — low <= bottom
            init = np.inf
            if pre is not None and g.known < pre["T0"]:
                a = int(np.searchsorted(pre["ot"], g.known, "left"))
                b = int(np.searchsorted(pre["ot"], pre["T0"], "left"))
                if b > a:
                    init = float(pre["l"][a:b].min())
                if init <= g.bottom:
                    continue
            run = alive_run(l, s, init, g.bottom, upper=False)
            if len(run) == 0:
                continue
            near = np.minimum(g.top, run)
            far = g.bottom
            sl_ = slice(s, s + len(run))
            dist = (c[sl_] - near) / atr[sl_]
            d = -1
        else:  # над ценой, цель LONG; заполнение — high >= top
            init = -np.inf
            if pre is not None and g.known < pre["T0"]:
                a = int(np.searchsorted(pre["ot"], g.known, "left"))
                b = int(np.searchsorted(pre["ot"], pre["T0"], "left"))
                if b > a:
                    init = float(pre["h"][a:b].max())
                if init >= g.top:
                    continue
            run = alive_run(h, s, init, g.top, upper=True)
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
            o["size"][pos] = (np.abs(far - near[better]) / atr[sl_][better])
    return out


def merge_targets(parts: dict, tfs: tuple) -> dict:
    out = {}
    for d in (1, -1):
        best = np.vstack([parts[tf][d]["best"] for tf in tfs])
        k = np.argmin(best, axis=0)
        cols = np.arange(best.shape[1])
        o = {"best": best[k, cols], "tfi": np.where(np.isfinite(best[k, cols]), k, -1)}
        for f in ("near", "ce", "size"):
            o[f] = np.vstack([parts[tf][d][f] for tf in tfs])[k, cols]
        o["many"] = np.min(np.vstack([parts[tf][d]["many"] for tf in tfs]), axis=0)
        o["tf_names"] = tfs
        out[d] = o
    return out


# ----------------------------------------------------------------------------- signals
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
    """CHoCH по закрытию. Возвращает маски long/short, экстремум разворота и индекс пробитого свинга."""
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
        j = t - 1 - k  # свинг, подтвержденный на закрытии t-1
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


def all_signals(L: dict) -> dict:
    """sig -> d -> (mask, extreme)"""
    o, h, l, c, atr = L["o"], L["h"], L["l"], L["c"], L["atr"]
    n = len(c)
    out = {}
    a_l, a_s = doji_sig(o, h, l, c, atr)
    out["a"] = {1: (a_l, l.copy()), -1: (a_s, h.copy())}
    c_l = np.zeros(n, bool)
    c_s = np.zeros(n, bool)
    c_l[1:] = a_l[:-1] & (c[1:] > c[:-1]) & (c[1:] > o[1:])
    c_s[1:] = a_s[:-1] & (c[1:] < c[:-1]) & (c[1:] < o[1:])
    e_l = np.full(n, np.nan)
    e_s = np.full(n, np.nan)
    e_l[1:] = np.minimum(l[:-1], l[1:])
    e_s[1:] = np.maximum(h[:-1], h[1:])
    out["c"] = {1: (c_l, e_l), -1: (c_s, e_s)}
    A_l = np.cumsum(a_l)
    A_s = np.cumsum(a_s)
    for k, sfx in ((2, ""), (3, "_k3")):
        b_l, b_s, x_l, x_s, j_l, j_s = choch_sig(h, l, c, k)
        out["b" + sfx] = {1: (b_l, x_l), -1: (b_s, x_s)}
        d_l = b_l.copy()
        d_s = b_s.copy()
        idx = np.nonzero(b_l)[0]
        d_l[idx] = (A_l[idx - 1] - A_l[j_l[idx]]) > 0
        idx = np.nonzero(b_s)[0]
        d_s[idx] = (A_s[idx - 1] - A_s[j_s[idx]]) > 0
        out["d" + sfx] = {1: (d_l, x_l), -1: (d_s, x_s)}
    return out


# ----------------------------------------------------------------------------- trade
def simulate(L: dict, idx, d, entry, sl, tp):
    """Вектор сделок. Возвращает dict с R_net, R_gross, исходом и смещением выхода."""
    o, h, l, c = L["o"], L["h"], L["l"], L["c"]
    offs = np.arange(1, HOLD + 1)
    J = idx[:, None] + offs
    HH, LL, OO = h[J], l[J], o[J]
    lg = (d > 0)[:, None]
    stop_m = np.where(lg, LL <= sl[:, None], HH >= sl[:, None])
    tp_m = np.where(lg, HH >= tp[:, None], LL <= tp[:, None])
    fs = np.where(stop_m.any(1), stop_m.argmax(1), HOLD)
    ft = np.where(tp_m.any(1), tp_m.argmax(1), HOLD)
    is_sl = (fs <= ft) & (fs < HOLD)
    is_tp = (ft < fs)
    rows = np.arange(len(idx))
    o_at = OO[rows, np.minimum(fs, HOLD - 1)]
    stop_px = np.where(d > 0, np.minimum(o_at, sl), np.maximum(o_at, sl))
    stop_exit = stop_px - d * SLIP * entry
    c_to = c[idx + HOLD]
    exit_px = np.where(is_sl, stop_exit, np.where(is_tp, tp, c_to))
    exit_noslip = np.where(is_sl, stop_px, np.where(is_tp, tp, c_to))
    pnl = d * (exit_px - entry) - TAKER * (entry + exit_px)
    sl_nom = sl - d * SLIP * entry
    risk = np.abs(entry - sl_nom) + TAKER * (entry + sl_nom)
    return {"R_net": pnl / risk, "R_gross": d * (exit_noslip - entry) / np.abs(entry - sl),
            "tp": is_tp.astype(float), "sl": is_sl.astype(float), "to": (~is_sl & ~is_tp).astype(float),
            "exit_off": np.where(is_sl, fs + 1, np.where(is_tp, ft + 1, HOLD))}


# ----------------------------------------------------------------------------- build
def load_symbol(ltf: str, s: str) -> tuple[dict, dict, dict]:
    df4 = load(C_CANDLES / f"{s}USDT_4h.csv")
    if ltf == "4h":
        dfl = df4.copy()
        pre = None
    else:
        dfl = load(C_CANDLES / f"{s}USDT_1h.csv")
        T0 = int(-(-dfl.open_time.iloc[0] // (4 * HOUR)) * (4 * HOUR))
        dfl = dfl[dfl.open_time >= T0].reset_index(drop=True)
        pre = {"T0": T0, "ot": df4.open_time.to_numpy(), "h": df4.high.to_numpy(float), "l": df4.low.to_numpy(float)}
    L = {k: dfl[c].to_numpy(float) for k, c in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))}
    L["ot"] = dfl.open_time.to_numpy()
    L["atr"], _ = wilder_atr(L["h"], L["l"], L["c"])
    L["P"] = LTF_MS[ltf]
    htf = {"1d": agg_htf(df4, DAY, 0), "1w": agg_htf(df4, WEEK, MONDAY0)}
    gaps = {"1d": find_gaps(htf["1d"], "1d", DAY), "1w": find_gaps(htf["1w"], "1w", WEEK)}
    if ltf == "1h":
        gaps["4h"] = find_gaps(df4.assign(period=4 * HOUR), "4h", 4 * HOUR)
    return L, gaps, pre


def run_ltf(ltf: str, rng: np.random.Generator, log: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    recs = []
    k1_pending = []  # (sym, set, sig, d, idx, entry, sl, atr)
    Ls = {}
    base = []
    for s in SYMBOLS:
        t0 = time.time()
        L, gaps, pre = load_symbol(ltf, s)
        Ls[s] = L
        n = len(L["c"])
        parts = {tf: tf_targets(L, g, pre) for tf, g in gaps.items()}
        sigs = all_signals(L)
        valid = np.zeros(n, bool)
        valid[WARMUP:n - HOLD] = True
        valid &= np.isfinite(L["atr"]) & (L["atr"] > 0)
        for set_name in SETS_FOR_LTF[ltf]:
            T = merge_targets(parts, HTF_SETS[set_name])
            for sig in SIGNALS:
                if set_name != "DW" and sig.endswith("_k3"):
                    continue
                for d in (1, -1):
                    mask, ext = sigs[sig][d]
                    m = mask & valid & np.isfinite(ext)
                    idx_all = np.nonzero(m)[0]
                    tt = T[d]
                    is_gap = np.isfinite(tt["best"][idx_all])
                    is_k1 = tt["many"][idx_all] > K1_NOGAP_ATR
                    base.append({"ltf": ltf, "set": set_name, "sig": sig, "d": d, "sym": s, "n_sig": len(idx_all),
                                 "n_gap": int(is_gap.sum()), "n_k1": int(is_k1.sum())})
                    atr = L["atr"]
                    # --- основные события
                    idx = idx_all[is_gap]
                    if len(idx):
                        entry = L["c"][idx]
                        sl = ext[idx] - d * STOP_BUF_ATR * atr[idx]
                        dd = np.full(len(idx), d)
                        stop_atr = np.abs(entry - sl) / atr[idx]
                        for tp_name in TPS:
                            tp = tt[tp_name][idx]
                            r = simulate(L, idx, dd, entry, sl, tp)
                            dist = (tp - entry) * d / atr[idx]
                            recs.append(pd.DataFrame({
                                "ltf": ltf, "set": set_name, "sig": sig, "tpk": tp_name, "grp": "main", "sym": s,
                                "d": d, "idx": idx, "t": L["ot"][idx], "stop_atr": stop_atr,
                                "stop_pct": np.abs(entry - sl) / entry * 100, "dist_tp": dist,
                                "dist_near": tt["best"][idx], "dist_ce": (tt["ce"][idx] - entry) * d / atr[idx],
                                "gap_tf": np.array(HTF_SETS[set_name])[tt["tfi"][idx]], **r}))
                        # --- K2: случайный бар ±K2_WIN с гэп-целью, без признака, стоп той же ширины в ATR
                        elig = np.isfinite(tt["best"]) & valid & ~mask
                        ks, par = [], []
                        for p, i in enumerate(idx):
                            lo, hi = max(0, i - K2_WIN), min(n, i + K2_WIN + 1)
                            cand = np.nonzero(elig[lo:hi])[0] + lo
                            cand = cand[cand != i]
                            if len(cand) == 0:
                                continue
                            ks.append(rng.choice(cand, K2_DRAWS, replace=True))
                            par.append(np.full(K2_DRAWS, p))
                        if ks:
                            k = np.concatenate(ks)
                            pp = np.concatenate(par)
                            ek = L["c"][k]
                            slk = ek - d * stop_atr[pp] * atr[k]
                            dk = np.full(len(k), d)
                            for tp_name in TPS:
                                r = simulate(L, k, dk, ek, slk, tt[tp_name][k])
                                df = pd.DataFrame({"pp": pp, **{x: r[x] for x in ("R_net", "R_gross", "tp", "sl", "to")}})
                                agg = df.groupby("pp").mean()
                                ip = agg.index.to_numpy()
                                recs.append(pd.DataFrame({
                                    "ltf": ltf, "set": set_name, "sig": sig, "tpk": tp_name, "grp": "K2", "sym": s,
                                    "d": d, "idx": idx[ip], "t": L["ot"][idx[ip]], "stop_atr": stop_atr[ip],
                                    **{x: agg[x].to_numpy() for x in ("R_net", "R_gross", "tp", "sl", "to")}}))
                    # --- K1 — отложить до сбора распределения расстояний
                    ik = idx_all[is_k1]
                    if len(ik):
                        sl1 = ext[ik] - d * STOP_BUF_ATR * atr[ik]
                        k1_pending.append((s, set_name, sig, d, ik, L["c"][ik], sl1, atr[ik]))
        log.append(f"{ltf} {s}: {n} bars, gaps " + ", ".join(f"{k}={len(v)}" for k, v in gaps.items()) +
                   f", {time.time() - t0:.1f}s")
        print(log[-1], flush=True)
    main = pd.concat([r for r in recs if r.grp.iloc[0] == "main"], ignore_index=True)
    # --- K1: синтетические цели из распределения настоящих расстояний (ячейка LTF × набор × признак × направление)
    for s, set_name, sig, d, ik, entry, sl1, atr in k1_pending:
        pool = main[(main.set == set_name) & (main.sig == sig) & (main.d == d) & (main.tpk == "near")]
        if len(pool) == 0:
            continue
        dn, dc = pool.dist_near.to_numpy(), pool.dist_ce.to_numpy()
        pick = rng.integers(0, len(pool), size=(len(ik), K1_DRAWS))
        L = Ls[s]
        idx_r = np.repeat(ik, K1_DRAWS)
        e_r = np.repeat(entry, K1_DRAWS)
        sl_r = np.repeat(sl1, K1_DRAWS)
        a_r = np.repeat(atr, K1_DRAWS)
        dd = np.full(len(idx_r), d)
        par = np.repeat(np.arange(len(ik)), K1_DRAWS)
        for tp_name, arr in (("near", dn), ("ce", dc)):
            tp = e_r + d * arr[pick.ravel()] * a_r
            r = simulate(L, idx_r, dd, e_r, sl_r, tp)
            df = pd.DataFrame({"pp": par, **{x: r[x] for x in ("R_net", "R_gross", "tp", "sl", "to")}}).groupby("pp").mean()
            recs.append(pd.DataFrame({
                "ltf": ltf, "set": set_name, "sig": sig, "tpk": tp_name, "grp": "K1", "sym": s, "d": d,
                "idx": ik, "t": L["ot"][ik], "stop_atr": np.abs(entry - sl1) / atr,
                **{x: df[x].to_numpy() for x in ("R_net", "R_gross", "tp", "sl", "to")}}))
    ev = pd.concat(recs, ignore_index=True)
    return ev, pd.DataFrame(base)


# ----------------------------------------------------------------------------- stats
class Boot:
    """Кластерный бутстрап: общие веса кластеров для всех групп одной ячейки."""

    def __init__(self, t_all: np.ndarray, block_days: int, rng):
        self.block = block_days * DAY
        self.k0 = int(t_all.min() // self.block)
        nc = int(t_all.max() // self.block) - self.k0 + 1
        self.nc = nc
        self.W = rng.multinomial(nc, np.full(nc, 1 / nc), size=N_BOOT).astype(np.float32)

    def means(self, t, v):
        ci = (t // self.block - self.k0).astype(int)
        sm = np.bincount(ci, weights=v, minlength=self.nc).astype(np.float32)
        cn = np.bincount(ci, minlength=self.nc).astype(np.float32)
        with np.errstate(invalid="ignore", divide="ignore"):
            return (self.W @ sm) / (self.W @ cn)


def pf(x):
    neg = -x[x < 0].sum()
    return np.inf if neg == 0 else x[x > 0].sum() / neg


def ci(bt, q=2.5):
    return np.nanpercentile(bt, [q, 100 - q])


def summarize_cell(ev, ltf, set_name, sig, tpk, boots, full=True):
    sel = (ev.ltf == ltf) & (ev.set == set_name) & (ev.sig == sig) & (ev.tpk == tpk)
    m = ev[sel & (ev.grp == "main")]
    k1 = ev[sel & (ev.grp == "K1")]
    k2 = ev[sel & (ev.grp == "K2")]
    row = {"ltf": ltf, "set": set_name, "sig": sig, "tp": tpk, "n": len(m), "n_k1": len(k1), "n_k2": len(k2)}
    if len(m) < 5:
        return row
    R = m.R_net.to_numpy()
    row.update(tp_rate=m.tp.mean(), sl_rate=m.sl.mean(), to_rate=m.to.mean(), R=R.mean(), R_gross=m.R_gross.mean(),
               PF=pf(R), stop_atr_med=m.stop_atr.median(), stop_pct_med=m.stop_pct.median(),
               dist_tp_med=m.dist_tp.median(), win_R=R[R > 0].mean() if (R > 0).any() else np.nan,
               loss_R=R[R < 0].mean() if (R < 0).any() else np.nan,
               R_k1=k1.R_net.mean() if len(k1) else np.nan, tp_k1=k1.tp.mean() if len(k1) else np.nan,
               R_k2=k2.R_net.mean() if len(k2) else np.nan, tp_k2=k2.tp.mean() if len(k2) else np.nan,
               PF_k1=pf(k1.R_net.to_numpy()) if len(k1) else np.nan)
    tm = m.t.to_numpy()
    for bname, B in boots.items():
        bm = B.means(tm, R)
        row[f"R_lo_{bname}"], row[f"R_hi_{bname}"] = ci(bm)
        row[f"R_lo3_{bname}"], row[f"R_hi3_{bname}"] = ci(bm, BONF_Q)
        for kn, kd in (("k1", k1), ("k2", k2)):
            if len(kd) < 5:
                continue
            diff = bm - B.means(kd.t.to_numpy(), kd.R_net.to_numpy())
            row[f"d{kn}"] = R.mean() - kd.R_net.mean()
            row[f"d{kn}_lo_{bname}"], row[f"d{kn}_hi_{bname}"] = ci(diff)
            row[f"d{kn}_lo3_{bname}"], row[f"d{kn}_hi3_{bname}"] = ci(diff, BONF_Q)
            tpd = B.means(tm, m.tp.to_numpy()) - B.means(kd.t.to_numpy(), kd.tp.to_numpy())
            row[f"dtp_{kn}_lo_{bname}"], row[f"dtp_{kn}_hi_{bname}"] = ci(tpd)
    tmid = np.median(tm)
    row["t_mid"] = tmid
    for hn, hm in (("h1", tm < tmid), ("h2", tm >= tmid)):
        row[f"R_{hn}"] = R[hm].mean() if hm.any() else np.nan
        row[f"n_{hn}"] = int(hm.sum())
        for kn, kd in (("k1", k1), ("k2", k2)):
            kh = kd.t < tmid if hn == "h1" else kd.t >= tmid
            row[f"R_{kn}_{hn}"] = kd.R_net[kh].mean() if kh.any() else np.nan
    if full:
        no = no_overlap(m)
        row.update(no)
    return row


def no_overlap(m: pd.DataFrame) -> dict:
    taken = []
    for s, g in m.groupby("sym"):
        g = g.sort_values("idx")
        last_exit = -1
        for r in g.itertuples(index=False):
            if r.idx >= last_exit:
                taken.append((r.t, r.R_net, r.tp, r.sl))
                last_exit = r.idx + r.exit_off
    if not taken:
        return {}
    tk = pd.DataFrame(taken, columns=["t", "R", "tp", "sl"]).sort_values("t")
    R = tk.R.to_numpy()
    eq = np.cumsum(R)
    dd = (np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq).max()
    streak = cur = 0
    for x in R:
        cur = cur + 1 if x < 0 else 0
        streak = max(streak, cur)
    months = (tk.t.max() - tk.t.min()) / (30.44 * DAY)
    return {"no_n": len(R), "no_R": R.mean(), "no_tp": tk.tp.mean(), "no_sl": tk.sl.mean(), "no_sum": R.sum(),
            "no_dd": dd, "no_streak": streak, "no_per_month": len(R) / months if months > 0 else np.nan,
            "no_PF": pf(R)}


# ----------------------------------------------------------------------------- data checks
def data_checks() -> list[str]:
    out = []
    for s in SYMBOLS:
        df4 = load(C_CANDLES / f"{s}USDT_4h.csv")
        d1 = agg_htf(df4, DAY, 0)
        w1 = agg_htf(df4, WEEK, MONDAY0)
        cd = load(C_GAPS / f"{s}USDT_1d.csv")
        cw = load(C_GAPS / f"{s}USDT_1w.csv")
        res = []
        for mine, ref, name in ((d1, cd, "1d"), (w1, cw, "1w")):
            j = mine.merge(ref, on="open_time", suffixes=("", "_r"))
            bad = np.zeros(len(j), bool)
            for f in ("open", "high", "low", "close"):
                bad |= ~np.isclose(j[f], j[f + "_r"], rtol=1e-9, atol=0)
            res.append(f"{name}: {len(mine)} бар (с {pd.Timestamp(mine.open_time.iloc[0], unit='ms'):%Y-%m-%d}), "
                       f"сверено {len(j)}, расхождений {int(bad.sum())}, неполных внутри {int((mine.nsub < mine.nsub.max()).sum())}")
        out.append(f"{s}: " + "; ".join(res))
    return out


# ----------------------------------------------------------------------------- main
def main() -> None:
    import pickle
    cache = HERE / "computed.pkl"
    if "--report" in sys.argv and cache.exists():  # только переписать отчет из сохраненного расчета
        with open(cache, "rb") as f:
            res, ev, base, slices, log = pickle.load(f)
        write_report(HERE / "results.md", res, ev, base, slices, log)
        print("report rewritten")
        return
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    log = data_checks()
    for x in log:
        print(x)
    evs, bases = [], []
    for ltf in ("1h", "4h"):
        e_, b_ = run_ltf(ltf, rng, log)
        evs.append(e_)
        bases.append(b_)
    base = pd.concat(bases, ignore_index=True)
    ev = pd.concat(evs, ignore_index=True)
    ev[ev.grp == "main"].to_csv(HERE / "events.csv.gz", index=False, compression="gzip", float_format="%.6g")
    rows = []
    boots = {}
    for ltf in ("1h", "4h"):
        t_all = ev[ev.ltf == ltf].t.to_numpy()
        boots[ltf] = {"day": Boot(t_all, 1, rng), "coarse": Boot(t_all, COARSE_DAYS[ltf], rng)}
        for set_name in SETS_FOR_LTF[ltf]:
            for sig in SIGNALS:
                if set_name != "DW" and sig.endswith("_k3"):
                    continue
                for tpk in TPS:
                    rows.append(summarize_cell(ev, ltf, set_name, sig, tpk, boots[ltf]))
                    print("cell", ltf, set_name, sig, tpk, flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(HERE / "results.csv", index=False, float_format="%.5g")
    slices = slice_tables(ev, boots)
    log.append(f"расчет {time.time() - t0:.0f} с")
    with open(cache, "wb") as f:
        pickle.dump((res, ev, base, slices, log), f)
    write_report(HERE / "results.md", res, ev, base, slices, log)
    print("ok", len(ev), "records,", len(res), "cells")


# ----------------------------------------------------------------------------- report
# Интерпретация написана ПОСЛЕ просмотра результатов (числа — из прогона с SEED=20261016).
NARRATIVE = """
### Вывод

**Подход Ивана в формализации плана не подтверждается.** Ни одна из 16 заранее объявленных ячеек
(1h/4h × 4 признака × 2 тейка) не дает средний R после комиссий > 0 с ДИ, отделенным от нуля.
Простые признаки — доджи у экстремума (a) и доджи + подтверждение (c) — убыточны: 1h −0.16…−0.22R,
4h −0.02…−0.13R. На 1h они по точечной оценке даже хуже контроля «тот же признак без гэпа» (K1):
−0.09…−0.10R, в пределах шума. Чистый CHoCH (b) — около
нуля: 1h −0.06…−0.07R, 4h 0.00R; он не лучше случайного входа рядом с тем же гэпом (K2).
Доля дошедших до гэпа после «признака разворота» почти та же, что у K1 и K2 (например, 1h (a): 11% против 12% и 14%).
Признак разворота не повышает шанс дойти до гэпа, а гэп не повышает отдачу от признака.

**Единственный кандидат** — (d) «доджи у экстремума, затем CHoCH в сторону гэпа». Это самый близкий к рисунку
Ивана вариант: остановка, затем слом структуры. Результат: +0.09…+0.15R, PF 1.23–1.35,
плюс в обеих половинах окна на обоих ТФ. Но сигналов мало: ~155 на ТФ (4h — ~2 сделки в месяц на 10 символов,
1h — ~4.5). ДИ накрывает 0. Перевес над K1 (тот же признак без гэпа) не доказан: на 1h +0.09…+0.14 при ДИ [−0.13; +0.38].
Перевес над K2 есть только на 1h (+0.20…+0.24, 95% ДИ > 0) и не выдерживает поправку на 16 ячеек.
Результат хрупкий. На 1h LONG −0.10R, весь плюс дают SHORT (+0.27…+0.33R) и 2025 год.
На 4h при фракталах k=3 вместо k=2 плюс исчезает (+0.00…+0.02R).
Это гипотеза для форвард-проверки, а не преимущество.

**Цена ошибки.** Без наложения сделок (10 символов, 1R на сделку):
- самый простой вариант (доджи у экстремума, 1h): 81% стопов, серия до 37 убытков подряд, просадка ~320R за 3 года;
- (d) на 4h: 38% стопов, серия до 4 убытков, просадка 5–6R за 6 лет — но ~1.9 сделки в месяц на 10 символов.

Издержки съедают до ~0.14R на сделку на 1h (доджи, стоп ~0.9% цены), 0.05–0.08R на остальных признаках 1h
и 0.02–0.07R на 4h.
Если сетап и торговать, то на 4h, не на 1h.

**Попутное наблюдение (не проверялось как гипотеза).** На 4h CHoCH к стороне, где гэпа нет (K1), дает
+0.09…+0.13R. Это лучше, чем к гэпу: ΔK1 = −0.09…−0.12, у CE ДИ кр. < 0 без поправки.
Возможное объяснение: отсутствие гэпа над ценой означает, что старший ТФ растет. Тогда K1 — вход по старшему тренду,
а «разворот к гэпу» — против него. Направление старшего ТФ стоит проверить отдельно, с заранее объявленным планом.

### Проверки корректности

- Гэп-цель (живость, частичное заполнение, near/CE, выбор ближайшего в коридоре) пересчитана перебором
  для 3600 случайных пар бар × направление (1h и 4h; BTC, SOL, SUI): 1816 с целью, расхождений 0.
- Симуляция сделки (стоп/тейк/тайм-аут, гэп через стоп, издержки, R) перепроверена циклом на 400 случайных событиях: 0 расхождений.
- Нет заглядывания вперед: сигналы, пересчитанные на префиксе данных [0..t], совпадают с полными на 330 барах
  (380 сигналов): 0 расхождений.
- 1d/1w из 4h сверены с klines Binance 1d/1w (кеш gaps): 0 расхождений по 1d. По 1w — 2 бара в 2019 (BTC 2019-09-23, ETH 2019-12-09).
  У них битые open/high/low в самом weekly-kline Binance; агрегат из 4h правдоподобнее.

### Отступления от плана

- Бутстрап — 4000 повторов вместо 2000, чтобы точнее оценить хвосты 99.7% ДИ (Бонферрони). Правило вердикта не менялось.
- Первый запуск упал на технической ошибке склейки таблиц (pandas attrs) до подсчета любых метрик. Исправлено без изменения логики.
- «Издержки в R» показаны как разница «R до изд.» − R. Знаменатели немного разные (|entry−SL| против 1R с издержками),
  поэтому разница приблизительная.

### Ограничения

- «Признаки разворота» формализованы нами: доджи, CHoCH, их комбинации. Дискреционное чтение Ивана —
  контекст, объем, снятие ликвидности, «как на рисунке» — правила могут не ловить. Отрицательный результат относится к этим правилам.
- Тейк «полное закрытие» и ведение позиции «по тренду до гэпа» (трейлинг, частичные тейки) не проверялись.
  Тайм-аут 48 баров — 2 суток на 1h, 8 суток на 4h — отрезает долгие ходы к недельным гэпам.
  Тезис «на недельном гэпе забрать гигантское движение» этой постановкой не проверен.
  При этом цель — ближайший гэп в 1.5–8 ATR младшего ТФ, а недельные гэпы среди целей — меньшинство (см. 4б).
- Внутри бара при касании и стопа, и тейка считается стоп (консервативно). Фандинг не учтен.
- 10 крупных перпов, которые живы сегодня (ошибка выжившего). 1h — только 3 года, SUI — с 2023.
- Сигналы (d) редки (n≈155 на ТФ, K1 для 4h (d) — 45 событий). Мощности не хватает, чтобы отличить +0.1R от нуля.
- K1 — не чистый контроль «та же ситуация без гэпа»: отсутствие гэпа связано с трендом старшего ТФ.
- Вторичных ячеек 32. Отдельные «значимые» клетки в них ожидаемы случайно (~5%).
"""

SIG_NAME = {"a": "(a) доджи у 20-бар. экстремума", "b": "(b) CHoCH по закрытию", "c": "(c) доджи + подтверждение",
            "d": "(d) доджи, затем CHoCH", "b_k3": "(b) CHoCH, фракталы k=3", "d_k3": "(d) доджи+CHoCH, k=3"}
TP_NAME = {"near": "ближняя граница", "ce": "CE (середина)"}


def f2(x, nd=2, sign=True):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def pc(x):
    return "—" if x is None or not np.isfinite(x) else f"{100 * x:.0f}%"


def cii(r, pfx, bname, nd=2):
    lo, hi = r.get(f"{pfx}_lo_{bname}", np.nan), r.get(f"{pfx}_hi_{bname}", np.nan)
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return "—"
    return f"[{lo:+.{nd}f}; {hi:+.{nd}f}]"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def cell_verdict(r) -> tuple[str, dict]:
    g = lambda k: r.get(k, np.nan)  # noqa: E731
    pos = lambda *ks: all(np.isfinite(g(k)) and g(k) > 0 for k in ks)  # noqa: E731
    p = {
        "p1": pos("R", "R_lo_day", "R_lo_coarse"),
        "p2": pos("dk1", "dk1_lo_day", "dk1_lo_coarse"),
        "p3": pos("dk2", "dk2_lo_day", "dk2_lo_coarse"),
        "p4": pos("R_h1", "R_h2"),
        "b1": pos("R_lo3_day", "R_lo3_coarse"),
        "b2": pos("dk1_lo3_day", "dk1_lo3_coarse"),
        "b3": pos("dk2_lo3_day", "dk2_lo3_coarse"),
    }
    if p["p1"] and p["p2"] and p["p3"] and p["p4"] and p["b1"] and p["b2"] and p["b3"]:
        v = "ПОДТВ."
    elif p["p1"] and p["p2"] and p["p3"] and p["p4"]:
        v = "намек"
    elif p["p1"]:
        v = "частично"
    else:
        v = "нет"
    return v, p


def write_report(path: Path, res: pd.DataFrame, ev: pd.DataFrame, base: pd.DataFrame, slices: dict, log: list) -> None:
    L = []
    w = L.append
    w("# Сетап Ивана «разворот к гэпу старшего ТФ» — результаты\n")
    w("План с гипотезой, определениями и правилом вердикта объявлен до расчета: `plan.md`. Скрипт: `setup_study.py`.")
    w("Данные: Binance USDⓈ-M perpetual, 10 символов (BTC ETH SOL XRP DOGE BNB ADA AVAX LINK SUI), закрытые бары, "
      "локальный кеш. LTF 1h — с 2023-10-15 (3 года), LTF 4h — вся история контрактов (с 2019-09 у BTC, SUI с 2023-05). "
      "1d/1w — агрегация 4h. Издержки как OSKAR `risk/sizing.py`: taker 0.05% каждая нога + 0.05% проскальзывания "
      "на стопе; 1R = `stop_loss_per_unit` (полный стоп = −1.00R). Все R ниже — **после издержек**, если не сказано иное.\n")
    w("Обозначения: TP/SL/ТО — доли исходов (тейк / стоп / тайм-аут 48 баров). R — средний R_net. "
      "ДИ — 95% кластерный бутстрап по UTC-дням; «ДИ кр.» — крупные кластеры (7 дн. для 1h, 30 дн. для 4h). "
      "K1 — тот же признак, гэпа в эту сторону нет (цель — уровень на том же расстоянии в ATR). "
      "K2 — гэп есть, вход в случайный бар ±100 баров без признака, стоп той же ширины в ATR. "
      "ΔK = R − R_контроля (ДИ кр. — парный кластерный бутстрап).\n")

    main = res[(res.set == "DW") & res.sig.isin(["a", "b", "c", "d"])].copy()
    verdicts = {}
    for _, r in main.iterrows():
        verdicts[(r.ltf, r.sig, r.tp)] = cell_verdict(r.to_dict())

    # ---- главный итог
    vs = [v for v, _ in verdicts.values()]
    if "ПОДТВ." in vs:
        overall = "ПОДТВЕРЖДЕН (хотя бы одна ячейка проходит 1–4 с Бонферрони)"
    elif "намек" in vs:
        overall = "НАМЕК (1–4 только на 95%, без поправки на 16 ячеек)"
    elif "частично" in vs:
        overall = "ЧАСТИЧНО (R_net > 0 с ДИ есть, но нет перевеса над контролем)"
    else:
        overall = "НЕ ПОДТВЕРЖДЕН (ни одна из 16 ячеек главного семейства)"
    w(f"## Вердикт по правилу плана: **{overall}**\n")
    cnt = pd.Series(vs).value_counts().to_dict()
    w("Ячейки главного семейства (16): " + ", ".join(f"{k} — {v}" for k, v in cnt.items()) + ".\n")
    w(NARRATIVE)

    # ---- главная таблица
    w("## 1. Главное семейство: гэп-цель 1d+1w (ближайший живой в 1.5–8 ATR), long+short\n")
    rows = []
    for _, r in main.iterrows():
        r = r.to_dict()
        v, _ = verdicts[(r["ltf"], r["sig"], r["tp"])]
        rows.append([r["ltf"], SIG_NAME[r["sig"]], TP_NAME[r["tp"]], r["n"],
                     f"{pc(r.get('tp_rate'))}/{pc(r.get('sl_rate'))}/{pc(r.get('to_rate'))}",
                     f"**{f2(r.get('R', np.nan))}**", cii(r, "R", "day"), cii(r, "R", "coarse"),
                     f2(r.get("PF", np.nan), 2, False), f2(r.get("R_gross", np.nan)),
                     f2(r.get("stop_atr_med", np.nan), 2, False), f2(r.get("dist_tp_med", np.nan), 1, False), v])
    w(md_table(["LTF", "признак", "тейк", "n", "TP/SL/ТО", "R", "ДИ", "ДИ кр.", "PF", "R до изд.",
                "стоп, ATR (мед.)", "до тейка, ATR (мед.)", "вердикт"], rows))
    w("")
    w("### 1б. Против контролей\n")
    rows = []
    for _, r in main.iterrows():
        r = r.to_dict()
        rows.append([r["ltf"], r["sig"], r["tp"], f2(r.get("R", np.nan)),
                     f"{f2(r.get('R_k1', np.nan))} (n={r.get('n_k1', 0)}, TP {pc(r.get('tp_k1', np.nan))})",
                     f2(r.get("dk1", np.nan)), cii(r, "dk1", "coarse"),
                     f"{f2(r.get('R_k2', np.nan))} (TP {pc(r.get('tp_k2', np.nan))})",
                     f2(r.get("dk2", np.nan)), cii(r, "dk2", "coarse"),
                     f"{pc(r.get('tp_rate', np.nan))} vs {pc(r.get('tp_k1', np.nan))} / {pc(r.get('tp_k2', np.nan))}"])
    w(md_table(["LTF", "приз.", "тейк", "R", "K1: R", "ΔK1", "ДИ кр.", "K2: R", "ΔK2", "ДИ кр.",
                "TP: сетап vs K1/K2"], rows))
    w("")
    w("### 1в. Условия вердикта по ячейкам (1: R>0 с ДИ; 2: >K1; 3: >K2; 4: обе половины; B: Бонферрони 99.7%)\n")
    rows = []
    for _, r in main.iterrows():
        r = r.to_dict()
        v, p = verdicts[(r["ltf"], r["sig"], r["tp"])]
        yn = lambda b: "да" if b else "нет"  # noqa: E731
        rows.append([r["ltf"], r["sig"], r["tp"], yn(p["p1"]), yn(p["p2"]), yn(p["p3"]), yn(p["p4"]),
                     f"{yn(p['b1'])}/{yn(p['b2'])}/{yn(p['b3'])}",
                     f"{f2(r.get('R_h1', np.nan))} / {f2(r.get('R_h2', np.nan))}",
                     f"{f2(r.get('R_k1_h1', np.nan))} / {f2(r.get('R_k1_h2', np.nan))}",
                     f"{f2(r.get('R_k2_h1', np.nan))} / {f2(r.get('R_k2_h2', np.nan))}", v])
    w(md_table(["LTF", "приз.", "тейк", "1", "2", "3", "4", "B (1/2/3)", "R: 1-я / 2-я половина",
                "K1 по половинам", "K2 по половинам", "вердикт"], rows))
    w("")

    # ---- без наложения
    w("## 2. Реальная торговля без наложения сделок (по символу — новый сигнал только после выхода)\n")
    rows = []
    for _, r in main.iterrows():
        r = r.to_dict()
        rows.append([r["ltf"], r["sig"], r["tp"], int(r.get("no_n", 0) or 0), f2(r.get("no_per_month", np.nan), 1, False),
                     f2(r.get("no_R", np.nan)), f2(r.get("no_PF", np.nan), 2, False), pc(r.get("no_tp", np.nan)),
                     pc(r.get("no_sl", np.nan)), f2(r.get("loss_R", np.nan)), f2(r.get("win_R", np.nan)),
                     int(r.get("no_streak", 0) or 0), f2(r.get("no_dd", np.nan), 1, False), f2(r.get("no_sum", np.nan), 1)])
    w(md_table(["LTF", "приз.", "тейк", "сделок", "в мес. (10 симв.)", "R", "PF", "TP", "стопов",
                "ср. убыток, R*", "ср. прибыль, R*", "макс. серия убытков", "макс. просадка, R", "Σ R"], rows))
    w("\n*средний убыток/прибыль — по всем событиям ячейки (с наложением).\n")

    # ---- вторичное
    w("## 3. Вторичное (описательно, в вердикт не входит)\n")
    sec = res[~((res.set == "DW") & res.sig.isin(["a", "b", "c", "d"]))]
    rows = []
    for _, r in sec.iterrows():
        r = r.to_dict()
        if r["n"] < 5:
            rows.append([r["ltf"], r["set"], r["sig"], r["tp"], r["n"]] + ["—"] * 7)
            continue
        rows.append([r["ltf"], {"DW": "1d+1w", "W": "1w", "4h": "4h"}[r["set"]], r["sig"], r["tp"], r["n"],
                     pc(r.get("tp_rate", np.nan)), f2(r.get("R", np.nan)), cii(r, "R", "coarse"),
                     f2(r.get("PF", np.nan), 2, False), f"{f2(r.get('dk1', np.nan))} {cii(r, 'dk1', 'coarse')}",
                     f"{f2(r.get('dk2', np.nan))} {cii(r, 'dk2', 'coarse')}",
                     f"{f2(r.get('R_h1', np.nan))} / {f2(r.get('R_h2', np.nan))}"])
    w(md_table(["LTF", "гэпы", "приз.", "тейк", "n", "TP", "R", "ДИ кр.", "PF", "ΔK1 [ДИ кр.]", "ΔK2 [ДИ кр.]",
                "половины"], rows))
    w("")

    # ---- разрезы
    w("## 4. Разрезы главного семейства\n")
    d = slices["dir_ctrl"]
    w("### 4а. По направлению (R; в скобках доля TP)\n")
    rows = []
    for (ltf, sig, tpk), g in d.groupby(["ltf", "sig", "tpk"]):
        row = [ltf, sig, tpk]
        for dd in (1, -1):
            for grp in ("main", "K1", "K2"):
                x = g[(g.d == dd) & (g.grp == grp)]
                row.append("—" if x.empty else f"{f2(x.R.iloc[0])} ({pc(x.tp.iloc[0])}, n={x.n.iloc[0]})")
        rows.append(row)
    w(md_table(["LTF", "приз.", "тейк", "LONG сетап", "LONG K1", "LONG K2", "SHORT сетап", "SHORT K1", "SHORT K2"], rows))
    w("")
    w("### 4б. По ТФ гэпа-цели (сетап)\n")
    g = slices["gaptf"]
    rows = [[r.ltf, r.sig, r.tpk, r.gap_tf, r.n, f2(r.R), pc(r.tp)] for r in g.itertuples()]
    w(md_table(["LTF", "приз.", "тейк", "гэп", "n", "R", "TP"], rows))
    w("")
    for key, title in (("year", "### 4в. По годам (R, n)"), ("sym", "### 4г. По символам (R, n)")):
        w(title + "\n")
        t = slices[key]
        col = "year" if key == "year" else "sym"
        for ltf in ("1h", "4h"):
            for tpk in TPS:
                x = t[(t.ltf == ltf) & (t.tpk == tpk)]
                if x.empty:
                    continue
                cols = sorted(x[col].unique(), key=lambda v: (SYMBOLS.index(v) if key == "sym" else v))
                rows = []
                for sig in ("a", "b", "c", "d"):
                    for grp in ("main", "K1", "K2"):
                        y = x[(x.sig == sig) & (x.grp == grp)].set_index(col)
                        if y.empty:
                            continue
                        rows.append([sig, {"main": "сетап", "K1": "K1", "K2": "K2"}[grp]] +
                                    [f"{f2(y.R[c_])} ({y.n[c_]})" if c_ in y.index else "—" for c_ in cols])
                w(f"LTF {ltf}, тейк {TP_NAME[tpk]}:\n")
                w(md_table(["приз.", "группа"] + [str(c_) for c_ in cols], rows))
                w("")

    # ---- счетчики
    w("## 5. Счетчики сигналов (главный набор 1d+1w; сумма по символам)\n")
    b = base[base.set == "DW"].groupby(["ltf", "sig", "d"])[["n_sig", "n_gap", "n_k1"]].sum().reset_index()
    rows = [[r.ltf, r.sig, "LONG" if r.d > 0 else "SHORT", r.n_sig, r.n_gap, f"{100 * r.n_gap / max(r.n_sig, 1):.0f}%", r.n_k1]
            for r in b.itertuples()]
    w(md_table(["LTF", "приз.", "напр.", "всего сигналов", "с гэп-целью 1.5–8 ATR", "доля", "K1 (нет гэпа ≤10 ATR)"], rows))
    w("")
    w("## 6. Журнал данных\n")
    w("```")
    for x in log:
        w(x)
    w("```")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def slice_tables(ev, boots) -> dict:
    """Разрезы главного семейства: направление, ТФ гэпа-цели, годы, символы."""
    m = ev[(ev.set == "DW") & ev.sig.isin(["a", "b", "c", "d"])].copy()
    m["year"] = pd.to_datetime(m.t, unit="ms").dt.year
    out = {}
    g = m[m.grp == "main"]
    out["dir"] = g.groupby(["ltf", "sig", "tpk", "d"]).agg(n=("R_net", "size"), R=("R_net", "mean"), tp=("tp", "mean")).reset_index()
    out["gaptf"] = g.groupby(["ltf", "sig", "tpk", "gap_tf"]).agg(n=("R_net", "size"), R=("R_net", "mean"), tp=("tp", "mean")).reset_index()
    out["year"] = m.groupby(["ltf", "sig", "tpk", "grp", "year"]).agg(n=("R_net", "size"), R=("R_net", "mean")).reset_index()
    out["sym"] = m.groupby(["ltf", "sig", "tpk", "grp", "sym"]).agg(n=("R_net", "size"), R=("R_net", "mean")).reset_index()
    # K1/K2 по направлению — для сравнения
    out["dir_ctrl"] = m.groupby(["ltf", "sig", "tpk", "grp", "d"]).agg(n=("R_net", "size"), R=("R_net", "mean"), tp=("tp", "mean")).reset_index()
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
