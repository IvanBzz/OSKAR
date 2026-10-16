#!/usr/bin/env python
"""Доджи у локального экстремума как признак разворота — первый замер для OSKAR.

Вопрос: даёт ли доджи на 20-барном экстремуме после движения преимущество в сторону
разворота по сравнению с (а) обычной свечой у такого же экстремума и (б) случайной свечой.

Определения (бар i, всё известно на закрытии i, вход по close[i], без look-ahead):
  ATR      — Wilder ATR(14) на баре i.
  low-экстремум (вход LONG):  low[i]  <= min(low[i-20..i-1])  и  max(high[i-20..i-1]) - low[i]  >= 1.5*ATR[i]
  high-экстремум (вход SHORT): high[i] >= max(high[i-20..i-1]) и  high[i] - min(low[i-20..i-1]) >= 1.5*ATR[i]
  доджи@t  — |close-open| <= t * (high-low), t in {5%, 10%, 20%}; бары с high==low исключены.
  pta-доджи — определение pandas-ta/TA-Lib: тело < 10% от СРЕДНЕГО диапазона 10 баров (SMA вкл. текущий).
  обычная  — тот же экстремум, тело > 20% диапазона (не доджи ни при одном пороге).
  случайная — 3 случайных бара того же символа/ТФ в окне ±500 баров от события, то же направление.
  Чувствительность: движение до экстремума ≥ 6 ATR (1.5 ATR у 20-барного экстремума почти не режет);
  половины окна по времени; long/short; диапазон доджи ≥/< 1 ATR.

Исходы (d = +1 long / -1 short):
  r_H      = d*(close[i+H]-close[i])/ATR[i], H in {4,12,48} — доходность вперёд в ATR (gross).
  race     — «+1 ATR раньше -1 ATR» по high/low баров i+1..i+48; оба уровня в одном баре = проигрыш
             (консервативно, как ops/replay.py); ни один за 48 баров = «нет исхода».
  R_net    — сделка-скобка 1:1 (TP=+1 ATR, SL=-1 ATR, таймаут 48 баров по close) в R минус комиссия
             0.15% цены на круг: fee_R = 0.0015*close[i]/ATR[i].
ДИ: кластерный бутстрап по времени (все символы вместе; 15m — день, 1h — неделя, 4h — месяц), 2000 повторов.

Данные: Binance USDⓈ-M perpetual, публичные /fapi/v1/klines (без ключей), кеш в ./cache.
Запуск (python из .venv OSKAR):
  python -I -B doji_study.py fetch     скачать (~550 запросов, ~2.5 req/s)
  python -I -B doji_study.py analyze   посчитать -> results.md, results.csv, events.csv.gz
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "SUI"]
TF_MIN = {"15m": 15, "1h": 60, "4h": 240}
HIST_DAYS = {"15m": 365, "1h": 1095, "4h": None}  # None = вся история контракта

LOOKBACK = 20
MOVE_ATR = 1.5
MOVE_ATR_STRICT = 6.0  # 1.5 ATR почти не режет (у 20-барного экстремума медиана движения ~4.7 ATR)
HORIZONS = (4, 12, 48)
RACE_ATR = 1.0
RACE_MAX = 48
FEE_RT = 0.0015
DOJI_T = (0.05, 0.10, 0.20)
ORDINARY_MIN = 0.20
N_RANDOM = 3
RANDOM_WIN = 500
WARMUP = 40
CLUSTER_DAYS = {"15m": 1, "1h": 7, "4h": 30}
N_BOOT = 2000
SEED = 20261016


# ----------------------------------------------------------------------------- fetch
def fetch_all() -> None:
    import httpx

    CACHE.mkdir(exist_ok=True)
    meta_p = CACHE / "meta.json"
    if meta_p.exists():
        now_ms = json.loads(meta_p.read_text())["now_ms"]
    else:
        now_ms = int(time.time() * 1000)
        meta_p.write_text(json.dumps({"now_ms": now_ms, "utc": pd.Timestamp(now_ms, unit="ms").isoformat()}))
    nreq = 0
    with httpx.Client(timeout=30) as cl:
        for tf, tfm in TF_MIN.items():
            for s in SYMBOLS:
                p = CACHE / f"{s}USDT_{tf}.csv"
                if p.exists():
                    continue
                bar_ms = tfm * 60_000
                days = HIST_DAYS[tf]
                start = 0 if days is None else now_ms - days * 86_400_000 - WARMUP * bar_ms
                rows: list = []
                used = 0
                while True:
                    r = cl.get(URL, params={"symbol": f"{s}USDT", "interval": tf, "startTime": start,
                                            "endTime": now_ms - 1, "limit": 1500})
                    nreq += 1
                    if r.status_code in (418, 429):
                        print("RATE LIMITED", r.status_code, r.text[:200]); sys.exit(2)
                    r.raise_for_status()
                    used = int(r.headers.get("x-mbx-used-weight-1m", "0"))
                    data = r.json()
                    time.sleep(0.4 if used < 1500 else 30)
                    if not data:
                        break
                    rows += data
                    nxt = data[-1][0] + bar_ms
                    if len(data) < 1500 or nxt >= now_ms:
                        break
                    start = nxt
                df = pd.DataFrame([x[:7] for x in rows],
                                  columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
                df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
                df = df.drop_duplicates("open_time").sort_values("open_time")
                df = df[df.close_time < now_ms]  # только закрытые свечи
                df.to_csv(p, index=False)
                print(f"{s} {tf}: {len(df)} bars, weight1m={used}, req={nreq}", flush=True)


# ----------------------------------------------------------------------------- indicators
def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    tr = np.empty_like(c)
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr = np.full_like(c, np.nan)
    if len(c) >= n:
        atr[n - 1] = tr[:n].mean()
        for k in range(n, len(c)):
            atr[k] = (atr[k - 1] * (n - 1) + tr[k]) / n
    return atr


def outcomes(idx: np.ndarray, d: np.ndarray, h, l, c, atr) -> dict:
    """Исходы для баров idx в направлении d (+1/-1). Требует idx + RACE_MAX <= n-1."""
    out = {f"r{H}": d * (c[idx + H] - c[idx]) / atr[idx] for H in HORIZONS}
    offs = np.arange(1, RACE_MAX + 1)
    HH = h[idx[:, None] + offs]
    LL = l[idx[:, None] + offs]
    up = (c[idx] + RACE_ATR * atr[idx])[:, None]
    dn = (c[idx] - RACE_ATR * atr[idx])[:, None]
    hit_up, hit_dn = HH >= up, LL <= dn
    lng = (d > 0)[:, None]
    win_m = np.where(lng, hit_up, hit_dn)
    loss_m = np.where(lng, hit_dn, hit_up)
    fw = np.where(win_m.any(1), win_m.argmax(1), RACE_MAX)
    fl = np.where(loss_m.any(1), loss_m.argmax(1), RACE_MAX)
    win = fw < fl                       # тот же бар -> проигрыш (консервативно)
    loss = (fl <= fw) & (fl < RACE_MAX)
    none = ~win & ~loss
    timeout_r = d * (c[idx + RACE_MAX] - c[idx]) / atr[idx]
    R = np.where(win, 1.0, np.where(loss, -1.0, timeout_r / RACE_ATR))
    fee_R = FEE_RT * c[idx] / atr[idx]
    out.update(win=win.astype(float), loss=loss.astype(float), none=none.astype(float),
               R=R, fee_R=fee_R, R_net=R - fee_R)
    for H in HORIZONS:
        out[f"r{H}_net"] = out[f"r{H}"] - fee_R
    return out


# ----------------------------------------------------------------------------- events
def build_events(rng: np.random.Generator) -> pd.DataFrame:
    recs = []
    for tf in TF_MIN:
        for s in SYMBOLS:
            df = pd.read_csv(CACHE / f"{s}USDT_{tf}.csv")
            t = df.open_time.to_numpy()
            o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
            n = len(c)
            atr = wilder_atr(h, l, c)
            rg = h - l
            body = np.abs(c - o)
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(rg > 0, body / rg, np.nan)
            pta = body < 0.10 * pd.Series(rg).rolling(10).mean().to_numpy()
            pmin = pd.Series(l).shift(1).rolling(LOOKBACK).min().to_numpy()
            pmax = pd.Series(h).shift(1).rolling(LOOKBACK).max().to_numpy()
            ok = np.zeros(n, bool)
            ok[WARMUP:n - RACE_MAX] = True
            ok &= (rg > 0) & (atr > 0) & np.isfinite(pmin) & np.isfinite(pmax)
            new_low = l <= pmin
            new_high = h >= pmax
            mv_long = (pmax - l) / atr
            mv_short = (h - pmin) / atr
            is_long = ok & new_low & ~new_high & (mv_long >= MOVE_ATR)
            is_short = ok & new_high & ~new_low & (mv_short >= MOVE_ATR)
            for dirn, mask, mv in ((1, is_long, mv_long), (-1, is_short, mv_short)):
                idx = np.nonzero(mask)[0]
                if len(idx) == 0:
                    continue
                d = np.full(len(idx), dirn)
                res = outcomes(idx, d, h, l, c, atr)
                base = dict(tf=tf, sym=s, i=idx, t=t[idx], d=d, ratio=ratio[idx], pta=pta[idx],
                            # «цвет» относительно разворота: закрылась в сторону разворота?
                            rev_color=(dirn * (c[idx] - o[idx]) > 0),
                            rng_atr=rg[idx] / atr[idx], move_atr=mv[idx], atr_pct=atr[idx] / c[idx] * 100)
                recs.append(pd.DataFrame({**base, "kind": "ext", "parent_ratio": ratio[idx],
                                          "parent_move": mv[idx], "parent_pta": pta[idx], **res}))
                # случайные контроли: для каждого события N_RANDOM баров ±RANDOM_WIN, то же направление
                lo = np.maximum(idx - RANDOM_WIN, WARMUP)
                hi = np.minimum(idx + RANDOM_WIN, n - RACE_MAX - 1)
                rr = []
                for _ in range(N_RANDOM):
                    k = rng.integers(lo, hi + 1)
                    k = np.where(ok[k], k, idx)  # редкий невалидный бар -> заменить (почти не бывает)
                    rr.append(k)
                k = np.concatenate(rr)
                par = np.tile(np.arange(len(idx)), N_RANDOM)
                dk = np.full(len(k), dirn)
                resk = outcomes(k, dk, h, l, c, atr)
                recs.append(pd.DataFrame({
                    "tf": tf, "sym": s, "i": k, "t": t[idx][par],  # кластер — по времени родителя (парный дизайн)
                    "d": dk, "ratio": ratio[k], "pta": pta[k], "rev_color": (dirn * (c[k] - o[k]) > 0),
                    "rng_atr": rg[k] / atr[k], "move_atr": np.nan, "atr_pct": atr[k] / c[k] * 100,
                    "kind": "random", "parent_ratio": ratio[idx][par], "parent_move": mv[idx][par],
                    "parent_pta": pta[idx][par], **resk}))
    return pd.concat(recs, ignore_index=True)


# ----------------------------------------------------------------------------- bootstrap
METRICS = ["r4", "r12", "r48", "r48_net", "win", "loss", "none", "R", "R_net", "fee_R"]


def group_masks(sd: pd.DataFrame, strict: bool = False) -> dict[str, np.ndarray]:
    ext = (sd.kind == "ext").to_numpy()
    rnd = (sd.kind == "random").to_numpy()
    if strict:
        ext &= (sd.parent_move >= MOVE_ATR_STRICT).to_numpy()
        rnd &= (sd.parent_move >= MOVE_ATR_STRICT).to_numpy()
    r = sd.parent_ratio.to_numpy()
    g = {}
    for th in DOJI_T:
        g[f"doji{int(th * 100)}"] = ext & (r <= th)
        g[f"rand_doji{int(th * 100)}"] = rnd & (r <= th)
    g["doji_pta"] = ext & sd.parent_pta.to_numpy()
    g["rand_doji_pta"] = rnd & sd.parent_pta.to_numpy()
    g["ordinary"] = ext & (r > ORDINARY_MIN)
    g["ord_revcolor"] = g["ordinary"] & sd.rev_color.to_numpy()
    g["ord_trendcolor"] = g["ordinary"] & ~sd.rev_color.to_numpy()
    g["all_ext"] = ext
    g["random_all"] = rnd
    g["doji10_long"] = g["doji10"] & (sd.d > 0).to_numpy()
    g["doji10_short"] = g["doji10"] & (sd.d < 0).to_numpy()
    g["ord_long"] = g["ordinary"] & (sd.d > 0).to_numpy()
    g["ord_short"] = g["ordinary"] & (sd.d < 0).to_numpy()
    g["doji10_bigrange"] = g["doji10"] & (sd.rng_atr >= 1.0).to_numpy()
    g["doji10_smallrange"] = g["doji10"] & (sd.rng_atr < 1.0).to_numpy()
    return g


PAIRS = [("doji5", "ordinary"), ("doji10", "ordinary"), ("doji20", "ordinary"), ("doji_pta", "ordinary"),
         ("doji5", "rand_doji5"), ("doji10", "rand_doji10"), ("doji20", "rand_doji20"),
         ("doji_pta", "rand_doji_pta"), ("ordinary", "random_all"), ("doji10", "ord_revcolor"),
         ("doji10_long", "ord_long"), ("doji10_short", "ord_short")]


def boot_table(sd: pd.DataFrame, tf: str, label: str, rng: np.random.Generator, strict: bool = False) -> pd.DataFrame:
    cl = (sd.t.to_numpy() // (CLUSTER_DAYS[tf] * 86_400_000))
    cl_vals, cl_idx = np.unique(cl, return_inverse=True)
    nc = len(cl_vals)
    W = rng.multinomial(nc, np.full(nc, 1 / nc), size=N_BOOT).astype(float)
    masks = group_masks(sd, strict)
    agg = {}
    for gname, m in masks.items():
        cnt = np.bincount(cl_idx[m], minlength=nc).astype(float)
        nW = W @ cnt
        agg[gname] = {"n": int(m.sum()), "clusters": int((cnt > 0).sum())}
        for met in METRICS:
            v = sd[met].to_numpy()[m]
            sm = np.bincount(cl_idx[m], weights=v, minlength=nc)
            with np.errstate(invalid="ignore", divide="ignore"):
                agg[gname][met] = (v.mean() if len(v) else np.nan, (W @ sm) / nW)
    rows = []
    for gname, a in agg.items():
        row = {"tf": tf, "slice": label, "row": gname, "n": a["n"], "clusters": a["clusters"]}
        for met in METRICS:
            pt, bt = a[met]
            row[met] = pt
            row[f"{met}_lo"], row[f"{met}_hi"] = (np.nanpercentile(bt, [2.5, 97.5]) if a["n"] else (np.nan, np.nan))
        rows.append(row)
    for g1, g2 in PAIRS:
        a, b = agg[g1], agg[g2]
        row = {"tf": tf, "slice": label, "row": f"{g1} − {g2}", "n": a["n"], "clusters": a["clusters"]}
        for met in METRICS:
            row[met] = a[met][0] - b[met][0]
            diff = a[met][1] - b[met][1]
            row[f"{met}_lo"], row[f"{met}_hi"] = (np.nanpercentile(diff, [2.5, 97.5]) if a["n"] and b["n"] else (np.nan, np.nan))
        rows.append(row)
    return pd.DataFrame(rows)


def pta_check() -> str:
    """Сверка моего pta-доджи с pandas_ta.cdl_doji (если библиотека доступна)."""
    try:
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            import pandas_ta as ta
    except Exception as e:  # noqa: BLE001
        return f"pandas_ta недоступен ({type(e).__name__})"
    df = pd.read_csv(CACHE / "BTCUSDT_1h.csv")
    mine = (np.abs(df.close - df.open) < 0.10 * (df.high - df.low).rolling(10).mean()).to_numpy()
    lib = ta.cdl_doji(df.open, df.high, df.low, df.close)
    lib = (lib.fillna(0).to_numpy() > 0)
    m = np.arange(len(df)) >= 10
    return (f"pandas_ta {ta.version if hasattr(ta, 'version') else ''}: совпадение с cdl_doji на BTC 1h "
            f"{(mine[m] == lib[m]).mean() * 100:.2f}% баров; доля доджи: моя {mine[m].mean() * 100:.1f}%, "
            f"pandas-ta {lib[m].mean() * 100:.1f}%")


# ----------------------------------------------------------------------------- run
def run_analysis() -> None:
    rng = np.random.default_rng(SEED)
    ev = build_events(rng)
    # только события-экстремумы (случайные контроли воспроизводятся по SEED)
    ev[ev.kind == "ext"].to_csv(HERE / "events.csv.gz", index=False, compression="gzip", float_format="%.5g")
    tabs = []
    spans = {}
    for tf in TF_MIN:
        sd = ev[ev.tf == tf]
        spans[tf] = (pd.Timestamp(sd.t.min(), unit="ms"), pd.Timestamp(sd.t.max(), unit="ms"))
        tabs.append(boot_table(sd, tf, "all", rng))
        tabs.append(boot_table(sd, tf, f"move>={MOVE_ATR_STRICT}", rng, strict=True))
        tmid = np.median(sd[sd.kind == "ext"].t)
        spans[tf + "_mid"] = pd.Timestamp(tmid, unit="ms")
        tabs.append(boot_table(sd[sd.t < tmid], tf, "half1", rng))
        tabs.append(boot_table(sd[sd.t >= tmid], tf, "half2", rng))
    res = pd.concat(tabs, ignore_index=True)
    res.to_csv(HERE / "results.csv", index=False, float_format="%.4f")
    base = ev_base_rates(ev)
    write_md(res, ev, spans, base, pta_check())
    print("ok", len(ev), "records,", len(res), "rows")


def ev_base_rates(ev: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tf in TF_MIN:
        bars = sum(len(pd.read_csv(CACHE / f"{s}USDT_{tf}.csv", usecols=["open_time"])) for s in SYMBOLS)
        e = ev[(ev.tf == tf) & (ev.kind == "ext")]
        rows.append({"tf": tf, "bars": bars, "ext": len(e), "ext_share": len(e) / bars,
                     "doji5": (e.ratio <= 0.05).mean(), "doji10": (e.ratio <= 0.10).mean(),
                     "doji20": (e.ratio <= 0.20).mean(), "pta": e.pta.mean(),
                     "atr_pct_med": e.atr_pct.median(), "fee_R_med": e.fee_R.median(),
                     "move_med": e.move_atr.median(), "strict_share": (e.move_atr >= MOVE_ATR_STRICT).mean()})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- report
NAMES = {
    "doji5": "доджи ≤5%", "doji10": "доджи ≤10%", "doji20": "доджи ≤20%", "doji_pta": "доджи pandas-ta",
    "ordinary": "обычная (>20%)", "ord_revcolor": "обычная, закрылась в сторону разворота",
    "ord_trendcolor": "обычная, закрылась по движению", "random_all": "случайная свеча",
    "rand_doji10": "случайная (пары к доджи ≤10%)", "all_ext": "все свечи у экстремума",
    "rand_doji5": "случайная (пары к доджи ≤5%)", "rand_doji20": "случайная (пары к доджи ≤20%)",
    "rand_doji_pta": "случайная (пары к доджи pandas-ta)",
    "doji10_long": "доджи ≤10% у минимума → long", "doji10_short": "доджи ≤10% у максимума → short",
    "ord_long": "обычная у минимума → long", "ord_short": "обычная у максимума → short",
    "doji10_bigrange": "доджи ≤10%, диапазон ≥1 ATR", "doji10_smallrange": "доджи ≤10%, диапазон <1 ATR",
}


def fmt(r, met, pct=False, sign=False):
    """Точка [95% ДИ]. pct: доля в %, sign: разница в п.п."""
    if pd.isna(r[met]):
        return "—"
    if pct:
        f = "{:+.1f}" if sign else "{:.1f}"
        s = f.format(100 * r[met]) + ("п.п." if sign else "%")
        return f"{s} [{f.format(100 * r[met + '_lo'])}; {f.format(100 * r[met + '_hi'])}]"
    f = "{:+.2f}"
    return f"{f.format(r[met])} [{f.format(r[met + '_lo'])}; {f.format(r[met + '_hi'])}]"


def write_md(res, ev, spans, base, ptamsg) -> None:
    L = []
    w = L.append
    w("# Доджи у экстремума как признак разворота — первый замер\n")
    w("Данные: Binance USDⓈ-M perpetual (не BingX), 10 символов (BTC ETH SOL XRP DOGE BNB ADA AVAX LINK SUI), "
      "закрытые свечи. Вход по close сигнального бара, направление — в сторону разворота. "
      "ДИ 95% — кластерный бутстрап по времени (все символы вместе; кластер 15m=день, 1h=неделя, 4h=месяц), 2000 повторов. "
      "Комиссия 0.15% цены на круг.\n")
    w("**Заранее объявленный главный тест** (остальное — вторичное, ~150 интервалов: ~5% из них «значимы» случайно): "
      "15m (сигнальный ТФ OSKAR), доджи ≤10% − обычная свеча у экстремума, метрика — R скобки 1:1 ATR.\n")
    w(f"| ТФ | окно событий (UTC) | баров всего | событий-экстремумов | доля баров | доджи ≤5/10/20% среди них | pta-доджи | медиана ATR, % цены | медиана комиссии, в ATR | безубыточная доля побед скобки 1:1 | медиана движения, ATR | доля с движением ≥{MOVE_ATR_STRICT:g} ATR |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, b in base.iterrows():
        s0, s1 = spans[b.tf]
        w(f"| {b.tf} | {s0:%Y-%m-%d} … {s1:%Y-%m-%d} | {b.bars} | {b.ext} | {b.ext_share * 100:.1f}% | "
          f"{b.doji5 * 100:.1f} / {b.doji10 * 100:.1f} / {b.doji20 * 100:.1f}% | {b.pta * 100:.1f}% | {b.atr_pct_med:.2f}% | "
          f"{b.fee_R_med:.2f} | {(0.5 + b.fee_R_med / 2) * 100:.0f}% | {b.move_med:.1f} | {b.strict_share * 100:.0f}% |")
    w("")
    w("Экстремум срабатывает на ~каждом пятом баре: в тренде почти каждый бар — новый 20-барный минимум/максимум, "
      "а условие «движение ≥1.5 ATR» почти не режет. Поэтому есть разрез со строгим движением (раздел 5).\n")
    w(f"Сверка определения: {ptamsg}.\n")

    sel = lambda tf, sl, row: res[(res.tf == tf) & (res.slice == sl) & (res.row == row)].iloc[0]

    w("## 1. Группы: доходность вперёд (ATR, gross) и гонка «+1 ATR раньше −1 ATR»\n")
    w("Gross = до комиссий. «+1 раньше −1» = доля событий, где цена прошла +1 ATR в сторону разворота раньше, чем −1 ATR "
      "(касание обоих в одном баре = проигрыш, нет исхода за 48 баров = не выигрыш).\n")
    rows = ["doji5", "doji10", "doji20", "doji_pta", "ordinary", "ord_revcolor", "ord_trendcolor",
            "rand_doji10", "random_all"]
    for tf in TF_MIN:
        w(f"### {tf}\n")
        w("| группа | n | r4 | r12 | r48 | +1 раньше −1 | −1 раньше +1 | нет исхода |")
        w("|---|---|---|---|---|---|---|---|")
        for rw in rows:
            r = sel(tf, "all", rw)
            w(f"| {NAMES.get(rw, rw)} | {r.n} | {fmt(r, 'r4')} | {fmt(r, 'r12')} | {fmt(r, 'r48')} | "
              f"{fmt(r, 'win', True)} | {r.loss * 100:.1f}% | {r.none * 100:.1f}% |")
        w("")

    w("## 2. Сделка после комиссий: скобка 1:1 (TP +1 ATR / SL −1 ATR / таймаут 48 баров)\n")
    w("R_net = исход в R (1R = 1 ATR) минус комиссия 0.15% в R. Также r48_net — просто удержание 48 баров после комиссии.\n")
    w("| ТФ | группа | n | R gross | комиссия, R | R_net [95% ДИ] | r48_net [95% ДИ] |")
    w("|---|---|---|---|---|---|---|")
    for tf in TF_MIN:
        for rw in ["doji5", "doji10", "doji20", "doji_pta", "ordinary", "ord_revcolor", "rand_doji10", "random_all"]:
            r = sel(tf, "all", rw)
            w(f"| {tf} | {NAMES.get(rw, rw)} | {r.n} | {r.R:+.3f} | {r.fee_R:.3f} | {fmt(r, 'R_net')} | {fmt(r, 'r48_net')} |")
    w("")

    w("## 3. Главный вопрос: разница «доджи − контроль» (95% ДИ)\n")
    w("Положительная = доджи лучше контроля. ДИ, накрывающий 0, = различия не видно. "
      "Разница R_net почти равна разнице R gross: комиссия в обеих группах почти одинакова.\n")
    w("| ТФ | сравнение | n доджи | Δ r12, ATR | Δ r48, ATR | Δ «+1 раньше −1», п.п. | Δ R (скобка), R |")
    w("|---|---|---|---|---|---|---|")
    for tf in TF_MIN:
        for g1, g2 in PAIRS:
            r = sel(tf, "all", f"{g1} − {g2}")
            w(f"| {tf} | {NAMES.get(g1, g1)} − {NAMES.get(g2, g2)} | {r.n} | {fmt(r, 'r12')} | {fmt(r, 'r48')} | "
              f"{fmt(r, 'win', True, True)} | {fmt(r, 'R')} |")
    w("")

    w("## 4. Устойчивость во времени: первая и вторая половина окна (доджи ≤10%)\n")
    w("| ТФ | половина | граница | n доджи | доджи: +1 раньше −1 | доджи: R_net | Δ R vs обычная | Δ R vs случайная |")
    w("|---|---|---|---|---|---|---|---|")
    for tf in TF_MIN:
        for h_ in ("half1", "half2"):
            r = sel(tf, h_, "doji10")
            d1 = sel(tf, h_, "doji10 − ordinary")
            d2 = sel(tf, h_, "doji10 − rand_doji10")
            w(f"| {tf} | {h_} | {spans[tf + '_mid']:%Y-%m-%d} | {r.n} | {fmt(r, 'win', True)} | {fmt(r, 'R_net')} | "
              f"{fmt(d1, 'R')} | {fmt(d2, 'R')} |")
    w("")

    w(f"## 5. Чувствительность: сильное движение до экстремума (≥{MOVE_ATR_STRICT} ATR)\n")
    w("| ТФ | группа / сравнение | n | +1 раньше −1 | R_net | Δ R |")
    w("|---|---|---|---|---|---|")
    sl = f"move>={MOVE_ATR_STRICT}"
    for tf in TF_MIN:
        for rw in ["doji10", "ordinary", "rand_doji10"]:
            r = sel(tf, sl, rw)
            w(f"| {tf} | {NAMES.get(rw, rw)} | {r.n} | {fmt(r, 'win', True)} | {fmt(r, 'R_net')} | |")
        for g1, g2 in (("doji10", "ordinary"), ("doji10", "rand_doji10")):
            r = sel(tf, sl, f"{g1} − {g2}")
            w(f"| {tf} | {NAMES[g1]} − {NAMES.get(g2, g2)} | {r.n} | {fmt(r, 'win', True, True)} | | {fmt(r, 'R')} |")
    w("")

    w("## 6. Разрезы доджи ≤10%: направление и размер свечи\n")
    w("| ТФ | группа | n | +1 раньше −1 | R gross | R_net |")
    w("|---|---|---|---|---|---|")
    for tf in TF_MIN:
        for rw in ["doji10_long", "ord_long", "doji10_short", "ord_short", "doji10_bigrange", "doji10_smallrange"]:
            r = sel(tf, "all", rw)
            w(f"| {tf} | {NAMES[rw]} | {r.n} | {fmt(r, 'win', True)} | {r.R:+.3f} | {fmt(r, 'R_net')} |")
    w("")
    (HERE / "results.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    if cmd == "fetch":
        fetch_all()
    elif cmd == "analyze":
        run_analysis()
    else:
        print(__doc__)
