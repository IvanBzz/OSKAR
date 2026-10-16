#!/usr/bin/env python
"""Фильтр по волатильности (features.atr_pct) на counterfactuals OSKAR. План — plan.md (пред-регистрация).

Стадии (каждая идемпотентна, кеш/снимок не перезаписываются):
  snapshot — журнал (sqlite, mode=ro) -> dataset.csv (заморозка: демон живой, БД меняется)
  fetch    — Binance USDⓈ-M публичные /fapi/v1/klines 1m на окно [floor15(ts)-30м; ts+12ч+2м], кеш ./cache
  analyze  — R до/после комиссий в вариантах J_all / J_ontime / B_all / B_ontime -> results.md, trades.csv

Запуск: <python> -I -B vol_study.py snapshot|fetch|analyze
Никаких обращений к BingX и к 127.0.0.1:8321.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
DATASET = HERE / "dataset.csv"
DB_URI = "file:C:/Users/Ivan/Desktop/Projects/Trading/OSKAR/data/oskar.db?mode=ro"
BN = "https://fapi.binance.com"

HORIZON_MS = 12 * 3600 * 1000          # EXPIRED_FLAT_BARS=48 x 15m (core/cycle.py:26)
LATE_MS = 24 * 3600 * 1000             # «дорешён позже суток»
TAKER = 0.0005                         # risk/sizing.py TAKER_FEE_PCT=0.05 за сторону
SLIP = 0.0005                          # risk/sizing.py STOP_SLIPPAGE_PCT=0.05 от entry
PRICE_TOL = 0.02                       # BingX vs Binance после множителя 10^k
NREP = 10_000
SEED = 20261016
BLK4_MS = 4 * 3600 * 1000
GRID_PCT = (50, 60, 66.667, 75, 80, 90)
# момент снимка (лог stage snapshot): 2026-10-16T05:44:02Z. Строки с ts+12ч > SNAP_MS — горизонт
# не прошёл, в выборку попали только быстро дорешённые (TP/SL) — цензура; их соседи висят без исхода.
SNAP_MS = 1792129442000


def iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


# ----------------------------------------------------------------------------- snapshot
def snapshot() -> None:
    if DATASET.exists():
        print("dataset.csv уже есть — снимок заморожен"); return
    con = sqlite3.connect(DB_URI, uri=True)
    rows = con.execute(
        "SELECT id, decision_id, cohort, candidate_json, entry_price, sl_price, tp_price, "
        "hypothetical_outcome, resolved_at, created_at FROM counterfactuals ORDER BY id").fetchall()
    out = []
    for (rid, did, cohort, cj, e, sl, tp, oc, res_at, cr_at) in rows:
        c = json.loads(cj)
        mkt = next(o["price"] for o in c["entry_options"] if o["kind"] == "market")
        kind = next((o["kind"] for o in c["entry_options"] if abs(o["price"] - e) < 1e-12), "unknown")
        out.append(dict(
            id=rid, decision_id=did, cohort=cohort, symbol=c["symbol"], direction=c["direction"],
            setup_type=c.get("setup_type"), score=c.get("score"), timeframe=c.get("timeframe"),
            ts=c["ts"], ts_ms=iso_ms(c["ts"]), created_at=cr_at, resolved_at=res_at,
            outcome=oc, entry=e, sl=sl, tp=tp, market_px=mkt, entry_kind=kind,
            atr_pct=c["features"]["atr_pct"], trend_mtf=c["features"].get("trend_mtf"),
        ))
    df = pd.DataFrame(out)
    df.to_csv(DATASET, index=False)
    print(f"snapshot: {len(df)} rows at {datetime.now(timezone.utc).isoformat()}")


# ----------------------------------------------------------------------------- fetch
def bn_candidates(symbol: str) -> list[str]:
    base = symbol.split("/")[0]
    c = [f"{base}USDT", f"1000{base}USDT", f"1000000{base}USDT"]
    if base.startswith("1000"):
        c.append(f"{base[4:]}USDT")
    return c


def win(ts_ms: int) -> tuple[int, int]:
    f15 = ts_ms - ts_ms % 900_000
    return f15 - 30 * 60_000, ts_ms + HORIZON_MS + 2 * 60_000


def fetch() -> None:
    import httpx

    CACHE.mkdir(exist_ok=True)
    df = pd.read_csv(DATASET)
    df = df[df.outcome.notna()]
    ex_p = CACHE / "exchangeInfo_symbols.json"
    with httpx.Client(timeout=30) as cl:
        if not ex_p.exists():
            r = cl.get(f"{BN}/fapi/v1/exchangeInfo"); r.raise_for_status()
            syms = {s["symbol"]: s["status"] for s in r.json()["symbols"]
                    if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL"}
            ex_p.write_text(json.dumps(syms))
            time.sleep(1)
        syms = json.loads(ex_p.read_text())
        nreq = 0
        for _, row in df.iterrows():
            cands = [s for s in bn_candidates(row.symbol) if s in syms]
            if not cands:
                continue
            s0, s1 = win(int(row.ts_ms))
            for bs in cands:
                p = CACHE / f"{bs}_{s0}.csv"
                if p.exists():
                    continue
                r = cl.get(f"{BN}/fapi/v1/klines", params={"symbol": bs, "interval": "1m",
                                                          "startTime": s0, "endTime": s1, "limit": 1000})
                nreq += 1
                if r.status_code in (418, 429):
                    print("RATE LIMITED", r.status_code, r.text[:200]); sys.exit(2)
                used = int(r.headers.get("x-mbx-used-weight-1m", "0"))
                if r.status_code == 400:
                    pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "close_time"]).to_csv(p, index=False)
                    print(bs, "400", r.text[:120]); time.sleep(0.4); continue
                r.raise_for_status()
                d = pd.DataFrame([x[:7] for x in r.json()],
                                 columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
                d = d.drop(columns="volume")
                d.to_csv(p, index=False)
                if nreq % 20 == 0:
                    print(f"req={nreq} weight1m={used}", flush=True)
                time.sleep(0.4 if used < 1500 else 30)
    print("fetch done, requests:", nreq)


# ----------------------------------------------------------------------------- outcomes
def r_tp(e, tp, D):
    return abs(tp - e) / D, (abs(tp - e) - TAKER * (e + tp)) / D


def r_sl(e, sl, D):
    x = sl - SLIP * e if sl < e else sl + SLIP * e
    return -1.0, -(abs(e - x) + TAKER * (e + x)) / D


def r_exit(e, px, D, sgn):
    return sgn * (px - e) / D, (sgn * (px - e) - TAKER * (e + px)) / D


def load_path(row, syms) -> dict:
    """Binance-путь в ценах BingX. status: ok | no_symbol | mismatch | partial."""
    s0, _ = win(int(row.ts_ms))
    best = None
    for bs in bn_candidates(row.symbol):
        if bs not in syms:
            continue
        p = CACHE / f"{bs}_{s0}.csv"
        if not p.exists():
            continue
        d = pd.read_csv(p)
        if d.empty:
            continue
        f15 = int(row.ts_ms) - int(row.ts_ms) % 900_000
        refs = []
        for bound in (f15, f15 - 900_000):  # закрытие последней закрытой 15м свечи (или предыдущей)
            m = d[d.close_time == bound - 1]
            if len(m):
                refs.append(float(m.close.iloc[0]))
        for ref in refs:
            r = row.market_px / ref
            mult = 10.0 ** round(math.log10(r))
            dev = abs(r / mult - 1)
            if best is None or dev < best["dev"]:
                best = dict(bs=bs, d=d, k=r, mult=mult, dev=dev)
    if best is None:
        return dict(status="no_symbol")
    if best["dev"] > PRICE_TOL:
        return dict(status="mismatch", dev=best["dev"], bs=best["bs"])
    d = best["d"]
    t0 = int(row.ts_ms)
    path = d[(d.open_time > t0) & (d.open_time <= t0 + HORIZON_MS)]
    if len(path) < 700 or int(path.open_time.iloc[-1]) < t0 + HORIZON_MS - 120_000:
        return dict(status="partial", n=len(path), bs=best["bs"])
    k = best["k"]
    return dict(status="ok", bs=best["bs"], dev=best["dev"], k=k,
                hi=path.high.to_numpy(float) * k, lo=path.low.to_numpy(float) * k,
                p12=float(path.close.iloc[-1]) * k)


def resolve_b(row, P) -> tuple[str, float | None, float | None]:
    """Логика текущего резолвера (cycle.py:246-280) на 1m: лимит должен быть достигнут, SL раньше TP."""
    e, sl, tp = row.entry, row.sl, row.tp
    D = abs(e - sl)
    sgn = 1 if row.direction == "long" else -1
    entered = row.entry_kind == "market"
    for hi, lo in zip(P["hi"], P["lo"]):
        if not entered:
            if not (lo <= e if sgn > 0 else hi >= e):
                continue
            entered = True
        slh = lo <= sl if sgn > 0 else hi >= sl
        tph = hi >= tp if sgn > 0 else lo <= tp
        if slh:
            return ("sl", *r_sl(e, sl, D))
        if tph:
            return ("tp", *r_tp(e, tp, D))
    if not entered:
        return ("not_entered", None, None)
    return ("horizon", *r_exit(e, P["p12"], D, sgn))


def touched(row, P) -> bool:
    if row.entry_kind == "market":
        return True
    return bool((P["lo"] <= row.entry).any() if row.direction == "long" else (P["hi"] >= row.entry).any())


def build(df: pd.DataFrame, syms: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    J, B = [], []
    for _, row in df.iterrows():
        P = load_path(row, syms)
        D = abs(row.entry - row.sl)
        sgn = 1 if row.direction == "long" else -1
        base = dict(id=row.id, ts_ms=int(row.ts_ms), atr=row.atr_pct, cohort=row.cohort,
                    direction=row.direction, late=bool(row.late), entry_kind=row.entry_kind,
                    sl_pct=D / row.entry * 100, sl_atr=D / (row.atr_pct / 100 * row.entry),
                    journal=row.outcome, bn=P["status"], bn_sym=P.get("bs"), bn_dev=P.get("dev"))
        # --- J: журнал + переоценка expired_flat
        if row.outcome == "tp_hit":
            J.append({**base, "used": "tp", **dict(zip(("rg", "rn"), r_tp(row.entry, row.tp, D)))})
        elif row.outcome == "sl_hit":
            J.append({**base, "used": "sl", **dict(zip(("rg", "rn"), r_sl(row.entry, row.sl, D)))})
        elif P["status"] != "ok":
            J.append({**base, "used": "no_data", "rg": np.nan, "rn": np.nan})
        elif not touched(row, P):
            J.append({**base, "used": "not_entered", "rg": np.nan, "rn": np.nan})
        else:
            J.append({**base, "used": "horizon", **dict(zip(("rg", "rn"), r_exit(row.entry, P["p12"], D, sgn)))})
        # --- B: полное переразрешение по Binance
        if P["status"] != "ok":
            B.append({**base, "used": "no_data", "rg": np.nan, "rn": np.nan})
        else:
            u, rg, rn = resolve_b(row, P)
            B.append({**base, "used": u, "rg": np.nan if rg is None else rg, "rn": np.nan if rn is None else rn})
    return pd.DataFrame(J), pd.DataFrame(B)


# ----------------------------------------------------------------------------- bootstrap
def boot_w(cid: np.ndarray, nrep: int = NREP) -> np.ndarray:
    """Матрица весов строк (nrep x n): кластерный бутстреп = мультиномиальные кратности кластеров."""
    rng = np.random.default_rng(SEED)
    u, inv = np.unique(cid, return_inverse=True)
    cnt = rng.multinomial(len(u), np.full(len(u), 1 / len(u)), size=nrep)
    return cnt[:, inv].astype(float)


def wmean(W, y, m=None):
    if m is not None:
        W = W * m
    den = W.sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, (W * y).sum(1) / den, np.nan)


def wslope(W, x, y):
    n, sx, sy = W.sum(1), (W * x).sum(1), (W * y).sum(1)
    sxx, sxy = (W * x * x).sum(1), (W * x * y).sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (n * sxy - sx * sy) / (n * sxx - sx * sx)


def ci(v):
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return (np.nan, np.nan, np.nan)
    lo, hi = np.percentile(v, [2.5, 97.5])
    return lo, hi, float((v <= 0).mean())


def fci(t):
    return "—" if not np.isfinite(t[0]) else f"[{t[0]:+.2f}; {t[1]:+.2f}]"


def day_id(ts):
    return (np.asarray(ts) // 86_400_000).astype(np.int64)


def blk_id(ts):
    return (np.asarray(ts) // BLK4_MS).astype(np.int64)


def dlab(ts) -> pd.Series:
    return pd.Series(pd.to_datetime(np.asarray(ts), unit="ms")).dt.strftime("%d.%m")


def days_sorted(ts) -> list[str]:
    d = np.unique(day_id(ts))
    return [pd.to_datetime(int(x) * 86_400_000, unit="ms").strftime("%d.%m") for x in d]


def slope_point(x, y):
    return float(np.polyfit(x, y, 1)[0]) if len(x) > 2 else np.nan


# ----------------------------------------------------------------------------- report helpers
def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if (isinstance(v, float) and np.isnan(v)) else str(v) for v in r) + " |")
    return "\n".join(lines)


def terc_label(a, q1, q2):
    return np.where(a <= q1, "низкая", np.where(a <= q2, "средняя", "высокая"))


TERC = ("низкая", "средняя", "высокая")


def tercile_table(t: pd.DataFrame) -> pd.DataFrame:
    out = []
    Wd = boot_w(day_id(t.ts_ms))
    for g in TERC + ("все",):
        m = (t.terc == g).to_numpy() if g != "все" else np.ones(len(t), bool)
        s = t[m]
        c = ci(wmean(Wd, t.rn.to_numpy(), m.astype(float)))
        out.append({
            "ATR%": g, "N": len(s),
            "TP/SL/гор.": f"{(s.used == 'tp').sum()}/{(s.used == 'sl').sum()}/{(s.used == 'horizon').sum()}",
            "atr% медиана": f"{s.atr.median():.2f}",
            "R до ком.": f"{s.rg.mean():+.2f}", "R после ком.": f"{s.rn.mean():+.2f}",
            "95% CI (сутки)": fci(c),
            "стоп % медиана": f"{s.sl_pct.median():.2f}", "стоп в ATR медиана": f"{s.sl_atr.median():.2f}",
            "издержки, R медиана": f"{(s.rg - s.rn).median():.2f}",
            "сумма R после ком.": f"{s.rn.sum():+.1f}",
        })
    return pd.DataFrame(out)


def diff_row(t: pd.DataFrame, name: str) -> dict:
    lo = (t.terc == "низкая").to_numpy().astype(float)
    hi = (t.terc == "высокая").to_numpy().astype(float)
    y, yg = t.rn.to_numpy(), t.rg.to_numpy()
    res = {"вариант": name, "N низк/выс": f"{int(lo.sum())}/{int(hi.sum())}",
           "Δ до ком.": f"{yg[lo > 0].mean() - yg[hi > 0].mean():+.2f}",
           "Δ после ком.": f"{y[lo > 0].mean() - y[hi > 0].mean():+.2f}",
           "кластеров сутки/4ч": f"{len(np.unique(day_id(t.ts_ms)))}/{len(np.unique(blk_id(t.ts_ms)))}"}
    for lab, cid in (("сутки", day_id(t.ts_ms)), ("4ч", blk_id(t.ts_ms))):
        W = boot_w(cid)
        c = ci(wmean(W, y, lo) - wmean(W, y, hi))
        res[f"95% CI {lab}"] = fci(c)
        res[f"P(Δ≤0) {lab}"] = f"{c[2]:.3f}"
    return res


def slope_row(t: pd.DataFrame, name: str) -> dict:
    x, y = t.atr.to_numpy(), t.rn.to_numpy()
    d = day_id(t.ts_ms)
    xd = x - pd.Series(x).groupby(d).transform("mean").to_numpy()
    yd = y - pd.Series(y).groupby(d).transform("mean").to_numpy()
    res = {"вариант": name, "N": len(t), "b (R на 1 п.п. ATR%)": f"{slope_point(x, y):+.2f}"}
    for lab, cid in (("сутки", d), ("4ч", blk_id(t.ts_ms))):
        res[f"CI {lab}"] = fci(ci(wslope(boot_w(cid), x, y)))
    bfe = float((xd * yd).sum() / (xd * xd).sum())
    res["b с FE дня"] = f"{bfe:+.2f}"
    for lab, cid in (("сутки", d), ("4ч", blk_id(t.ts_ms))):
        W = boot_w(cid)
        with np.errstate(invalid="ignore", divide="ignore"):
            res[f"CI FE {lab}"] = fci(ci((W * xd * yd).sum(1) / (W * xd * xd).sum(1)))
    return res


def split_test(t: pd.DataFrame, name: str, grid: bool = False) -> dict:
    t = t.sort_values("ts_ms").reset_index(drop=True)
    n1 = len(t) // 2
    a, b = t.iloc[:n1], t.iloc[n1:]
    if not grid:
        T = float(np.percentile(a.atr, 66.667)); pick = "P66.7"
    else:
        best = None
        for pc in GRID_PCT:
            T_ = float(np.percentile(a.atr, pc))
            kept = a[a.atr <= T_]
            if len(kept) >= 20 and (best is None or kept.rn.mean() > best[0]):
                best = (kept.rn.mean(), T_, pc)
        _, T, pc = best; pick = f"P{pc:g}"
    keep_a, keep_b = (a.atr <= T).to_numpy(), (b.atr <= T).to_numpy()
    y = b.rn.to_numpy()
    res = {"вариант": name, "порог T": f"{pick}={T:.2f}%",
           "1-я пол. до": f"{pd.to_datetime(a.ts_ms.max(), unit='ms'):%d.%m %H:%M}",
           "N1/N2": f"{len(a)}/{len(b)}",
           "1-я: R ≤T / >T": f"{a.rn[keep_a].mean():+.2f} / {a.rn[~keep_a].mean():+.2f}",
           "2-я: R ≤T / >T": f"{y[keep_b].mean():+.2f} / {y[~keep_b].mean():+.2f}",
           "2-я: >T": f"{(~keep_b).sum()} из {len(b)}",
           "Δ2": f"{y[keep_b].mean() - y[~keep_b].mean():+.2f}" if (~keep_b).any() and keep_b.any() else "—",
           "кл. 4ч/сутки": f"{len(np.unique(blk_id(b.ts_ms)))}/{len(np.unique(day_id(b.ts_ms)))}"}
    for lab, cid in (("4ч", blk_id(b.ts_ms)), ("сутки", day_id(b.ts_ms))):
        W = boot_w(cid)
        c = ci(wmean(W, y, keep_b.astype(float)) - wmean(W, y, (~keep_b).astype(float)))
        res[f"CI Δ2 {lab}"] = fci(c)
    Wb = boot_w(blk_id(b.ts_ms))
    res["2-я: R все → с фильтром"] = f"{y.mean():+.2f} → {y[keep_b].mean():+.2f}"
    res["CI улучш. 4ч"] = fci(ci(wmean(Wb, y, keep_b.astype(float)) - wmean(Wb, y)))
    return res


# ----------------------------------------------------------------------------- analyze
def analyze() -> None:
    df = pd.read_csv(DATASET)
    syms = json.loads((CACHE / "exchangeInfo_symbols.json").read_text())
    df["late"] = [bool(isinstance(r, str) and iso_ms(r) - iso_ms(c) > LATE_MS)
                  for r, c in zip(df.resolved_at, df.created_at)]
    res = df[df.outcome.notna()].copy()
    q1, q2 = np.percentile(res.atr_pct, [33.333, 66.667])
    J, B = build(res, syms)
    for t in (J, B):
        t["terc"] = terc_label(t.atr.to_numpy(), q1, q2)
    J.assign(variant="J").to_csv(HERE / "trades.csv", index=False)
    B.assign(variant="B").to_csv(HERE / "trades.csv", index=False, mode="a", header=False)

    for t in (J, B):
        t["full_h"] = (t.ts_ms + HORIZON_MS) <= SNAP_MS
    V = {
        "J_ontime (основной)": J[(J.used.isin(["tp", "sl", "horizon"])) & (~J.late)],
        "J_all": J[J.used.isin(["tp", "sl", "horizon"])],
        "B_all": B[B.used.isin(["tp", "sl", "horizon"])],
        "B_ontime": B[(B.used.isin(["tp", "sl", "horizon"])) & (~B.late)],
        # пост-хок (после 1-го прогона): без цензурированных строк с непрошедшим горизонтом
        "J_ontime_full*": J[(J.used.isin(["tp", "sl", "horizon"])) & (~J.late) & J.full_h],
    }
    L = []
    P = L.append
    # --- данные
    P("## 1. Данные\n")
    P(f"- Снимок журнала: {len(df)} строк; с исходом {len(res)}; висят (горизонт не прошёл) {df.outcome.isna().sum()}; "
      f"дорешены позже 24 ч: {int(res.late.sum())} (по дням создания: "
      + ", ".join(f"{k} — {v}" for k, v in pd.to_datetime(res[res.late].ts_ms, unit='ms').dt.strftime('%d.%m').value_counts().sort_index().items()) + ").")
    P(f"- Границы терцилей ATR% (по {len(res)} дорешённым): ≤{q1:.3f}% низкая, ≤{q2:.3f}% средняя, выше — высокая.")
    bst = B.bn.value_counts().to_dict()
    P(f"- Binance 1m: ok {bst.get('ok', 0)}, нет символа {bst.get('no_symbol', 0)}, цена не сходится {bst.get('mismatch', 0)}, "
      f"неполные свечи {bst.get('partial', 0)}. Отклонение BingX/Binance на закрытии 15м (после множителя): медиана "
      f"{B[B.bn == 'ok'].bn_dev.median() * 100:.3f}%, 95-й перц. {np.nanpercentile(B[B.bn == 'ok'].bn_dev, 95) * 100:.3f}%.")
    nos = sorted(set(res[B.bn.to_numpy() == 'no_symbol'].symbol.str.split('/').str[0]))
    P(f"- Нет символа на Binance USDⓈ-M: {', '.join(nos)}.")
    P(f"- Неполные свечи = горизонт 12 ч ещё не прошёл к моменту снимка (05:44 UTC 16.10): "
      f"{int((~J.full_h).sum())} строк 16.10, все дорешены как TP/SL быстро "
      f"({int(((~J.full_h) & (J.journal == 'sl_hit')).sum())} SL / {int(((~J.full_h) & (J.journal == 'tp_hit')).sum())} TP), "
      f"их {int(df.outcome.isna().sum())} соседей того же окна висят без исхода → смещение к быстрым исходам. "
      f"В J они есть (журнал), в B — нет; вариант J_ontime_full* их исключает.")
    P(f"- J: использовано {J.used.isin(['tp','sl','horizon']).sum()} (TP {int((J.used=='tp').sum())}, SL {int((J.used=='sl').sum())}, "
      f"expired→цена 12ч {int((J.used=='horizon').sum())}); expired без данных {int((J.used=='no_data').sum())}, "
      f"лимит не достигнут {int((J.used=='not_entered').sum())}.")
    P(f"- B: использовано {B.used.isin(['tp','sl','horizon']).sum()} (TP {int((B.used=='tp').sum())}, SL {int((B.used=='sl').sum())}, "
      f"горизонт {int((B.used=='horizon').sum())}); лимит не достигнут {int((B.used=='not_entered').sum())}, без данных {int((B.used=='no_data').sum())}.")
    vc = dlab(V["J_all"].ts_ms).value_counts()
    P("- Сделок по дням (J_all): " + ", ".join(f"{k} — {vc.get(k, 0)}" for k in days_sorted(V["J_all"].ts_ms)) + ".\n")

    P("### Согласие журнала и переразрешения по Binance (строки с данными Binance)\n")
    ok = B[B.bn == "ok"]
    for lab, m in (("в срок (≤24 ч)", ~ok.late), ("поздние (>24 ч)", ok.late)):
        ct = pd.crosstab(ok[m].journal, ok[m].used)
        ct.index.name = f"журнал \\ Binance, {lab}"
        P(md_table(ct.reset_index())); P("")
        agree = ((ok[m].journal == "tp_hit") & (ok[m].used == "tp")) | ((ok[m].journal == "sl_hit") & (ok[m].used == "sl")) | \
                ((ok[m].journal == "expired_flat") & ok[m].used.isin(["horizon", "not_entered"]))
        P(f"Совпадение класса исхода: {agree.mean() * 100:.0f}% из {int(m.sum())}.\n")

    # --- воспроизведение
    P("## 2. Воспроизведение старой цифры (журнал, только TP/SL, без комиссий, iid)\n")
    r0 = J[J.used.isin(["tp", "sl"])].copy()
    a1, a2 = np.percentile(r0.atr, [33.333, 66.667])
    r0["t0"] = terc_label(r0.atr.to_numpy(), a1, a2)
    Wi = boot_w(np.arange(len(r0)))
    rows = []
    for g in TERC:
        m = (r0.t0 == g).to_numpy().astype(float)
        rows.append({"ATR%": g, "N": int(m.sum()), "R до ком.": f"{r0.rg[m > 0].mean():+.2f}",
                     "95% CI iid": fci(ci(wmean(Wi, r0.rg.to_numpy(), m))),
                     "R после ком.": f"{r0.rn[m > 0].mean():+.2f}",
                     "95% CI iid, после": fci(ci(wmean(Wi, r0.rn.to_numpy(), m)))})
    P(md_table(pd.DataFrame(rows)))
    lo_, hi_ = (r0.t0 == "низкая").to_numpy().astype(float), (r0.t0 == "высокая").to_numpy().astype(float)
    P(f"\nN={len(r0)}. Δ низкая−высокая до комиссий {r0.rg[lo_ > 0].mean() - r0.rg[hi_ > 0].mean():+.2f} "
      f"{fci(ci(wmean(Wi, r0.rg.to_numpy(), lo_) - wmean(Wi, r0.rg.to_numpy(), hi_)))} (iid); тот же Δ с бутстрепом по суткам "
      f"{fci(ci(wmean(boot_w(day_id(r0.ts_ms)), r0.rg.to_numpy(), lo_) - wmean(boot_w(day_id(r0.ts_ms)), r0.rg.to_numpy(), hi_)))}.\n")

    # --- терцили по вариантам
    P("## 3. Терцили ATR% (R после комиссий, CI — бутстреп по суткам)\n")
    for name, t in V.items():
        P(f"### {name}\n"); P(md_table(tercile_table(t))); P("")
    P("### Разница «низкая − высокая»\n")
    P(md_table(pd.DataFrame([diff_row(t, n) for n, t in V.items()]))); P("")

    # --- регрессия
    P("## 4. Непрерывная зависимость: R после комиссий ~ a + b·atr_pct\n")
    P(md_table(pd.DataFrame([slope_row(t, n) for n, t in V.items()])))
    P("\nb < 0 означает: выше ATR% — хуже R. FE дня = сравнение внутри одного дня.\n")

    # --- когорты и направления
    P("## 5. Когорты и направления (R после комиссий, N в скобках; описательно)\n")
    for name in ("J_ontime (основной)", "B_all"):
        t = V[name]
        for by in ("cohort", "direction"):
            g = t.groupby([by, "terc"]).rn.agg(["mean", "size"]).reset_index()
            piv = {}
            for _, r in g.iterrows():
                piv.setdefault(r[by], {})[r.terc] = f"{r['mean']:+.2f} ({int(r['size'])})"
            tot = t.groupby(by).rn.agg(["mean", "size"])
            rows = [{by: k, **{c: v.get(c, "—") for c in TERC},
                     "все": f"{tot.loc[k, 'mean']:+.2f} ({int(tot.loc[k, 'size'])})"} for k, v in piv.items()]
            P(f"**{name}, {by}**\n"); P(md_table(pd.DataFrame(rows))); P("")

    P("### Подгруппы: Δ «низкая − высокая» и наклон b (J_ontime; CI — блоки 4 ч)\n")
    t = V["J_ontime (основной)"]
    rows = []
    for lab, m in [("все", np.ones(len(t), bool))] + \
                  [(f"cohort={c}", (t.cohort == c).to_numpy()) for c in ("risk_rejected", "scout_pass", "shadow")] + \
                  [(f"direction={d}", (t.direction == d).to_numpy()) for d in ("long", "short")]:
        s = t[m]
        lo = (s.terc == "низкая").to_numpy().astype(float); hi = (s.terc == "высокая").to_numpy().astype(float)
        y, x = s.rn.to_numpy(), s.atr.to_numpy()
        W = boot_w(blk_id(s.ts_ms))
        rows.append({"подгруппа": lab, "N (низк/выс)": f"{len(s)} ({int(lo.sum())}/{int(hi.sum())})",
                     "Δ": f"{y[lo > 0].mean() - y[hi > 0].mean():+.2f}" if lo.sum() and hi.sum() else "—",
                     "CI Δ": fci(ci(wmean(W, y, lo) - wmean(W, y, hi))) if lo.sum() >= 5 and hi.sum() >= 5 else "N<5",
                     "b": f"{slope_point(x, y):+.2f}", "CI b": fci(ci(wslope(W, x, y))) if len(s) >= 20 else "N<20"})
    P(md_table(pd.DataFrame(rows))); P("")

    # --- временное разбиение
    P("## 6. Временное разбиение: порог по 1-й половине, проверка на 2-й\n")
    P("Основной (пред-регистрирован): T = P66.7 atr_pct 1-й половины.\n")
    P(md_table(pd.DataFrame([split_test(t, n) for n, t in V.items()]))); P("")
    P("Вторичный: T по сетке перцентилей {50,60,66.7,75,80,90} 1-й половины (max R оставленных, ≥20).\n")
    P(md_table(pd.DataFrame([split_test(t, n, grid=True) for n, t in V.items()]))); P("")

    # --- leave-one-day-out
    P("## 7. Leave-one-day-out: Δ «низкая − высокая» после комиссий без одного дня\n")
    P("Ячейка: Δ без этого дня (в скобках — сколько сделок выброшено).\n")
    rows = []
    alldays = days_sorted(V["J_all"].ts_ms)
    for name in ("J_ontime (основной)", "J_all", "B_all", "J_ontime_full*"):
        t = V[name]
        d = dlab(t.ts_ms).to_numpy()
        lo_all = t.rn[t.terc == 'низкая'].mean() - t.rn[t.terc == 'высокая'].mean()
        r = {"вариант": name, "все дни": f"{lo_all:+.2f}"}
        for day in alldays:
            n = int((d == day).sum())
            if n == 0:
                r[day] = "—"; continue
            s = t[d != day]
            r[day] = f"{s.rn[s.terc == 'низкая'].mean() - s.rn[s.terc == 'высокая'].mean():+.2f} ({n})"
        rows.append(r)
    P(md_table(pd.DataFrame(rows))); P("")

    # --- механизм: состав терцилей по дням
    P("## 8. Состав терцилей по дням (J_all) и средний R дня\n")
    t = V["J_all"]
    dd = dlab(t.ts_ms).to_numpy()
    rows = []
    for day in alldays:
        s = t[dd == day]
        rows.append({"день": day, **{g: int((s.terc == g).sum()) for g in TERC},
                     "медиана atr%": f"{s.atr.median():.2f}", "R после ком.": f"{s.rn.mean():+.2f}",
                     **{f"R {g}": (f"{s.rn[s.terc == g].mean():+.2f}" if (s.terc == g).any() else "—") for g in TERC}})
    P(md_table(pd.DataFrame(rows))); P("")

    # --- разведка (не пред-регистрировано): стоп в ATR
    P("## 9. Разведка (не в плане, пост-хок): стоп в единицах ATR как альтернатива\n")
    P("В высокой терцили стоп уже относительно ATR (см. «стоп в ATR медиана»). Проверка: R ~ atr% + стоп/ATR "
      "(МНК, CI — блоки 4 ч), и R по терцилям стоп/ATR. Только для гипотез на будущее.\n")
    rows = []
    for name in ("J_ontime (основной)", "B_all"):
        t = V[name]
        x1, x2, y = t.atr.to_numpy(), t.sl_atr.to_numpy(), t.rn.to_numpy()
        X = np.column_stack([np.ones(len(t)), x1, x2])
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        W = boot_w(blk_id(t.ts_ms), 2000)
        bs = []
        for w in W:
            sw = np.sqrt(w)
            bs.append(np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)[0])
        bs = np.array(bs)
        q = np.percentile(t.sl_atr, [33.333, 66.667])
        lab = np.where(t.sl_atr <= q[0], "узкий", np.where(t.sl_atr <= q[1], "средний", "широкий"))
        g = t.groupby(lab).rn.agg(["mean", "size"])
        rows.append({"вариант": name, "N": len(t), "corr(atr%, стоп/ATR)": f"{np.corrcoef(x1, x2)[0, 1]:+.2f}",
                     "b atr%": f"{beta[1]:+.2f} {fci(ci(bs[:, 1]))}", "b стоп/ATR": f"{beta[2]:+.2f} {fci(ci(bs[:, 2]))}",
                     "R по стоп/ATR узк/ср/шир": " / ".join(f"{g.loc[k, 'mean']:+.2f} ({int(g.loc[k, 'size'])})" for k in ("узкий", "средний", "широкий")),
                     "границы стоп/ATR": f"{q[0]:.2f} / {q[1]:.2f}"})
    P(md_table(pd.DataFrame(rows))); P("")

    body = "\n".join(L)
    out = HERE / "results.md"
    marker = "<!-- AUTO-TABLES: ниже генерирует vol_study.py analyze -->"
    head = out.read_text(encoding="utf-8").split(marker)[0] if out.exists() else "# Результаты: фильтр по волатильности\n\n"
    out.write_text(head + marker + "\n\n" + body + "\n", encoding="utf-8")
    print(body)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    {"snapshot": snapshot, "fetch": fetch, "analyze": analyze}[sys.argv[1]]()
