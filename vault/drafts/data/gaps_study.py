#!/usr/bin/env python
"""Проверка утверждения «все гэпы рано или поздно закроются» на истории.

Гэп = FVG из 3 свечей, индекс i = третья (правая) свеча, гэп известен на закрытии i:
  бычий   low[i]  > high[i-2]  зона [high[i-2]; low[i]]  ближняя граница = low[i],  дальняя = high[i-2]
  медвежий high[i] < low[i-2]  зона [high[i]; low[i-2]]  ближняя граница = high[i], дальняя = low[i-2]
События ищутся с бара i+1 (без look-ahead): касание ближней границы, 50% (CE), полное заполнение.
Время = j - i в барах. Не случившееся к концу данных = цензура (Kaplan–Meier).

Базовые линии «магнитизма»:
  mirror — тот же бар i, уровень на ТОМ ЖЕ расстоянии от close[i], но с другой стороны;
  random — случайный бар k того же символа/ТФ из окна исследования, уровень в ту же сторону,
           что и гэп, на том же расстоянии в единицах ATR(14) (d/ATR[i] * ATR[k]).

Данные: Binance USDⓈ-M futures, публичные /fapi/v1/klines (без ключей), кеш в ./cache.
Запуск:  python -I -B gaps_study.py fetch   (скачать, ~170 запросов, ~2.5 req/s)
         python -I -B gaps_study.py analyze (посчитать из кеша -> results.csv, results.md)
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
TF_MIN = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
STUDY_DAYS = {"15m": 90, "1h": 365, "4h": 730, "1d": 1461, "1w": None}  # None = вся история
WARMUP = 400  # бар до окна: прогрев EMA200/ATR
GRID = (3, 12, 48, 200)
CAL = {"3ч": 180, "24ч": 1440, "7д": 10080, "30д": 43200}
ATR_FILTER = 0.1
N_RANDOM = 3
SEED = 20261016
LEVELS = ("touch", "ce", "full")


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
        for s in SYMBOLS:
            for tf, tfm in TF_MIN.items():
                p = CACHE / f"{s}USDT_{tf}.csv"
                if p.exists():
                    continue
                bar_ms = tfm * 60_000
                days = STUDY_DAYS[tf]
                start = 0 if days is None else now_ms - days * 86_400_000 - WARMUP * bar_ms
                rows: list = []
                while True:
                    r = cl.get(URL, params={"symbol": f"{s}USDT", "interval": tf, "startTime": start,
                                            "endTime": now_ms - 1, "limit": 1500})
                    nreq += 1
                    if r.status_code in (418, 429):
                        print("RATE LIMITED", r.status_code, r.text[:200]); sys.exit(2)
                    r.raise_for_status()
                    used = int(r.headers.get("x-mbx-used-weight-1m", "0"))
                    data = r.json()
                    time.sleep(0.4 if used < 1000 else 30)
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


def first_hit(i: int, levels: np.ndarray, side: int, h: np.ndarray, l: np.ndarray) -> np.ndarray:
    """Бары до первого касания уровней после бара i. side=-1: уровень ниже (low<=lvl), +1: выше. 0 = не было."""
    if i >= len(h) - 1:
        return np.zeros(len(levels), dtype=int)
    if side < 0:
        m = np.minimum.accumulate(l[i + 1:])
        pos = np.searchsorted(-m, -levels, side="left")
    else:
        m = np.maximum.accumulate(h[i + 1:])
        pos = np.searchsorted(m, levels, side="left")
    return np.where(pos < len(m), pos + 1, 0)


# ----------------------------------------------------------------------------- gaps
def build_records(now_ms: int) -> tuple[pd.DataFrame, dict]:
    rng = np.random.default_rng(SEED)
    recs = []
    spans = {}
    for tf, tfm in TF_MIN.items():
        for s in SYMBOLS:
            df = pd.read_csv(CACHE / f"{s}USDT_{tf}.csv")
            ot = df.open_time.to_numpy()
            h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
            n = len(c)
            atr = wilder_atr(h, l, c)
            ema = pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()
            ema[:199] = np.nan  # EMA200 без 200 бар истории — тренд неизвестен
            days = STUDY_DAYS[tf]
            s0 = 0 if days is None else int(np.searchsorted(ot, now_ms - days * 86_400_000))
            s0 = max(s0, 14)
            spans[(tf, s)] = (pd.Timestamp(ot[s0], unit="ms"), pd.Timestamp(ot[-1], unit="ms"), n - s0)
            idx = np.arange(s0, n)
            is_gap = (l[idx] > h[idx - 2]) | (h[idx] < l[idx - 2])
            for i in idx[is_gap]:
                bull = l[i] > h[i - 2]
                if bull:
                    top, bot, side = l[i], h[i - 2], -1
                    near, far = top, bot
                else:
                    top, bot, side = l[i - 2], h[i], +1
                    near, far = bot, top
                lv = np.array([near, (top + bot) / 2, far])
                d = np.abs(c[i] - lv)
                if np.isnan(ema[i]):
                    trend = "na"
                else:
                    trend = "with" if (bull and c[i] > ema[i]) or ((not bull) and c[i] < ema[i]) else "against"
                base = dict(tf=tf, sym=s, i=int(i), t=int(ot[i]), dir="bull" if bull else "bear",
                            size_atr=(top - bot) / atr[i], size_pct=(top - bot) / c[i] * 100,
                            d_ce_atr=d[1] / atr[i], trend=trend)
                L = n - 1 - i
                tg = first_hit(i, lv, side, h, l)
                recs.append({**base, "kind": "gap", "L": L, **dict(zip(LEVELS, tg))})
                tm = first_hit(i, c[i] - side * d, -side, h, l)
                recs.append({**base, "kind": "mirror", "L": L, **dict(zip(LEVELS, tm))})
                for _ in range(N_RANDOM):
                    k = int(rng.integers(s0, n))
                    lvk = c[k] + side * d / atr[i] * atr[k]
                    tr_ = first_hit(k, lvk, side, h, l)
                    recs.append({**base, "kind": "random", "L": n - 1 - k, **dict(zip(LEVELS, tr_))})
    return pd.DataFrame(recs), spans


# ----------------------------------------------------------------------------- Kaplan–Meier
def km(times: np.ndarray, events: np.ndarray):
    ts = np.sort(times)
    ev_t, d = np.unique(times[events], return_counts=True)
    if len(ev_t) == 0:
        return ev_t, np.array([]), ts[-1] if len(ts) else 0
    at_risk = len(times) - np.searchsorted(ts, ev_t, side="left")
    F = 1 - np.cumprod(1 - d / at_risk)
    return ev_t, F, ts[-1]


def km_at(ev_t, F, tmax, t):
    if t > tmax or t < 1:
        return np.nan
    k = np.searchsorted(ev_t, t, side="right") - 1
    return 0.0 if k < 0 else float(F[k])


def km_q(ev_t, F, q):
    ok = np.nonzero(F >= q)[0]
    return float(ev_t[ok[0]]) if len(ok) else np.nan


def summarize(g: pd.DataFrame, tfm: int) -> list[dict]:
    out = []
    for lvl in LEVELS:
        t = g[lvl].to_numpy()
        L = g["L"].to_numpy()
        ev = t > 0
        times = np.where(ev, t, L)
        ev_t, F, tmax = km(times, ev)
        row = {"level": lvl, "n": len(g)}
        for N in GRID:
            row[f"F{N}"] = km_at(ev_t, F, tmax, N)
        for name, mins in CAL.items():
            nb = mins / tfm
            row[f"cal_{name}"] = km_at(ev_t, F, tmax, int(nb)) if nb >= 1 and float(nb).is_integer() else np.nan
        row["filled_end_raw"] = ev.mean()
        row["open_end_raw"] = 1 - ev.mean()
        old = L >= 200
        row["n_old200"] = int(old.sum())
        row["open_end_old200"] = (1 - ev[old].mean()) if old.any() else np.nan
        row["km_F_end"] = float(F[-1]) if len(F) else 0.0
        row["km_median_bars"] = km_q(ev_t, F, 0.5)
        row["km_p80_bars"] = km_q(ev_t, F, 0.8)
        row["max_followup"] = int(tmax)
        out.append(row)
    return out


def run_analysis() -> None:
    now_ms = json.loads((CACHE / "meta.json").read_text())["now_ms"]
    rec, spans = build_records(now_ms)
    rec.to_csv(HERE / "gaps_records.csv.gz", index=False, compression="gzip")
    rows = []
    for tf, tfm in TF_MIN.items():
        for filt in ("atr>=0.1", "all"):
            d = rec[rec.tf == tf]
            if filt == "atr>=0.1":
                d = d[d.size_atr >= ATR_FILTER]
            gaps = d[d.kind == "gap"]
            q1, q2 = gaps.size_atr.quantile([1 / 3, 2 / 3])
            d = d.assign(size_t=np.where(d.size_atr <= q1, "T1_small", np.where(d.size_atr <= q2, "T2_mid", "T3_big")))
            segs = {"all": d, "trend=with": d[d.trend == "with"], "trend=against": d[d.trend == "against"],
                    "trend=na": d[d.trend == "na"], "dir=bull": d[d.dir == "bull"], "dir=bear": d[d.dir == "bear"],
                    "size=T1_small": d[d.size_t == "T1_small"], "size=T2_mid": d[d.size_t == "T2_mid"],
                    "size=T3_big": d[d.size_t == "T3_big"]}
            for s in SYMBOLS:
                segs[f"sym={s}"] = d[d.sym == s]
            for seg, sd in segs.items():
                for kind in ("gap", "mirror", "random"):
                    g = sd[sd.kind == kind]
                    if len(g) == 0:
                        continue
                    for r in summarize(g, tfm):
                        rows.append({"tf": tf, "flt": filt, "segment": seg, "kind": kind,
                                     "size_atr_q1": q1, "size_atr_q2": q2, **r})
    res = pd.DataFrame(rows)
    res.to_csv(HERE / "results.csv", index=False, float_format="%.4f")
    ci = baseline_ci(rec)
    ci.to_csv(HERE / "results_baseline_ci.csv", index=False, float_format="%.4f")
    write_md(res, rec, spans, ci)
    print("ok", len(rec), "records,", len(res), "summary rows")


# Кластеры по ВРЕМЕНИ (все символы вместе): крипта движется синхронно, гэпы соседних баров зависимы.
CLUSTER_DAYS = {"15m": 1, "1h": 7, "4h": 30, "1d": 91, "1w": 365}


def baseline_ci(rec: pd.DataFrame, n_boot: int = 2000) -> pd.DataFrame:
    """Разница P(уровень достигнут за N баров): гэп − контроль, кластерный бутстрап 95% ДИ.
    Берутся только наблюдения с полным follow-up >= N (без цензуры)."""
    rng = np.random.default_rng(SEED + 1)
    out = []
    for tf in TF_MIN:
        d = rec[(rec.tf == tf) & (rec.size_atr >= ATR_FILTER)]
        q1, q2 = d[d.kind == "gap"].size_atr.quantile([1 / 3, 2 / 3])
        d = d.assign(cl=d.t // (CLUSTER_DAYS[tf] * 86_400_000))
        segs = {"all": d, "trend=with": d[d.trend == "with"], "trend=against": d[d.trend == "against"],
                "size=T1_small": d[d.size_atr <= q1], "size=T3_big": d[d.size_atr > q2]}
        for seg, sd in segs.items():
            cl_vals, cl_idx = np.unique(sd.cl.to_numpy(), return_inverse=True)
            nc = len(cl_vals)
            if nc < 3:
                continue
            W = rng.multinomial(nc, np.full(nc, 1 / nc), size=n_boot)
            kinds = sd.kind.to_numpy()
            Ls = sd.L.to_numpy()
            for lvl in ("touch", "ce", "full"):
                tv = sd[lvl].to_numpy()
                for N in (3, 12, 48):
                    agg = {}
                    for kind in ("gap", "mirror", "random"):
                        m = (kinds == kind) & (Ls >= N)
                        hit = ((tv > 0) & (tv <= N) & m).astype(float)
                        agg[kind] = (np.bincount(cl_idx, weights=hit, minlength=nc),
                                     np.bincount(cl_idx, weights=m.astype(float), minlength=nc))
                    with np.errstate(invalid="ignore", divide="ignore"):
                        boot = {k: (W @ h) / (W @ n) for k, (h, n) in agg.items()}
                    pt = {k: h.sum() / n.sum() if n.sum() else np.nan for k, (h, n) in agg.items()}
                    row = {"tf": tf, "segment": seg, "level": lvl, "N": N, "clusters": nc,
                           "n_gap": int(agg["gap"][1].sum()), "p_gap": pt["gap"], "p_mirror": pt["mirror"],
                           "p_random": pt["random"]}
                    for ctrl in ("mirror", "random"):
                        diff = boot["gap"] - boot[ctrl]
                        row[f"d_{ctrl}"] = pt["gap"] - pt[ctrl]
                        row[f"d_{ctrl}_lo"], row[f"d_{ctrl}_hi"] = np.nanpercentile(diff, [2.5, 97.5])
                    out.append(row)
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------- report
def pct(x):
    return "—" if pd.isna(x) else f"{100 * x:.0f}%"


def human(bars, tf):
    if pd.isna(bars):
        return "не достигнута"
    m = bars * TF_MIN[tf]
    if m < 48 * 60:
        s = (f"{m / 60:.1f}ч".replace(".0ч", "ч") if m < 600 else f"{m / 60:.0f}ч") if m >= 60 else f"{m:.0f}м"
    elif m < 60 * 1440:
        s = f"{m / 1440:.0f}д"
    else:
        s = f"{m / 10080:.0f}нед"
    return f"{bars:.0f} ({s})"


def write_md(res: pd.DataFrame, rec: pd.DataFrame, spans: dict, ci: pd.DataFrame) -> None:
    L = []
    w = L.append
    sel = lambda tf, lvl, filt="atr>=0.1", seg="all", kind="gap": res[(res.tf == tf) & (res.level == lvl) & (res.flt == filt) & (res.segment == seg) & (res.kind == kind)].iloc[0]
    w("# Гэпы (FVG): закрываются ли и как быстро — результаты\n")
    w("Данные: Binance USDⓈ-M perpetual, 10 символов (BTC ETH SOL XRP DOGE BNB ADA AVAX LINK SUI), закрытые свечи. "
      "Доли — Kaplan–Meier (цензура = конец данных), пул по символам. Время — в барах того же ТФ от бара формирования i "
      "(гэп известен на закрытии i, поиск с i+1). Фильтр: размер зоны ≥ 0.1×ATR(14), если не сказано иное.\n")
    w("| ТФ | окно (UTC) | баров в окне (сумма) |\n|---|---|---|")
    for tf in TF_MIN:
        st = [spans[(tf, s)] for s in SYMBOLS]
        w(f"| {tf} | {min(x[0] for x in st):%Y-%m-%d} … {max(x[1] for x in st):%Y-%m-%d %H:%M} | {sum(x[2] for x in st)} |")
    w("")
    names = {"touch": "Касание ближней границы", "ce": "Заполнение 50% (CE)", "full": "Полное заполнение"}
    for lvl in ("full", "ce", "touch"):
        w(f"## {names[lvl]} — доля гэпов, закрытых в пределах N баров\n")
        w("| ТФ | n | ≤3 | ≤12 | ≤48 | ≤200 | закрыто к концу (сырое) | открыто к концу | открыто среди гэпов старше 200 баров (n) | медиана KM | P80 KM |")
        w("|---|---|---|---|---|---|---|---|---|---|---|")
        for tf in TF_MIN:
            r = sel(tf, lvl)
            w(f"| {tf} | {r.n} | {pct(r.F3)} | {pct(r.F12)} | {pct(r.F48)} | {pct(r.F200)} | {pct(r.filled_end_raw)} | "
              f"{pct(r.open_end_raw)} | {pct(r.open_end_old200)} ({r.n_old200}) | {human(r.km_median_bars, tf)} | {human(r.km_p80_bars, tf)} |")
        w("")
    w("## Календарное время: доля закрытых (KM)\n")
    w("| ТФ | уровень | ≤3ч | ≤24ч | ≤7д | ≤30д |\n|---|---|---|---|---|---|")
    for tf in TF_MIN:
        for lvl in ("ce", "full"):
            r = sel(tf, lvl)
            w(f"| {tf} | {lvl} | {pct(r['cal_3ч'])} | {pct(r['cal_24ч'])} | {pct(r['cal_7д'])} | {pct(r['cal_30д'])} |")
    w("")
    w("## Без фильтра микрогэпов (для сравнения)\n")
    w("| ТФ | n (все) | n (≥0.1 ATR) | CE ≤12 все / фильтр | полное ≤12 все / фильтр | полное открыто к концу все / фильтр |")
    w("|---|---|---|---|---|---|")
    for tf in TF_MIN:
        a, b = sel(tf, "ce", "all"), sel(tf, "ce")
        fa, fb = sel(tf, "full", "all"), sel(tf, "full")
        w(f"| {tf} | {a.n} | {b.n} | {pct(a.F12)} / {pct(b.F12)} | {pct(fa.F12)} / {pct(fb.F12)} | {pct(fa.open_end_raw)} / {pct(fb.open_end_raw)} |")
    w("")
    w("## По тренду EMA200 того же ТФ (гэп по тренду / против)\n")
    w("| ТФ | n по / против / нет EMA | CE ≤12 по / против | полное ≤12 по / против | полное ≤200 по / против | медиана полного по / против |")
    w("|---|---|---|---|---|---|")
    for tf in TF_MIN:
        cw, ca = sel(tf, "ce", seg="trend=with"), sel(tf, "ce", seg="trend=against")
        fw, fa = sel(tf, "full", seg="trend=with"), sel(tf, "full", seg="trend=against")
        na = res[(res.tf == tf) & (res.flt == "atr>=0.1") & (res.segment == "trend=na") & (res.kind == "gap") & (res.level == "full")]
        nna = int(na.n.iloc[0]) if len(na) else 0
        w(f"| {tf} | {cw.n} / {ca.n} / {nna} | {pct(cw.F12)} / {pct(ca.F12)} | {pct(fw.F12)} / {pct(fa.F12)} | {pct(fw.F200)} / {pct(fa.F200)} | "
          f"{human(fw.km_median_bars, tf)} / {human(fa.km_median_bars, tf)} |")
    w("")
    w("## По размеру (терцили size/ATR внутри ТФ)\n")
    w("| ТФ | границы терцилей, ×ATR | CE ≤12 мал / сред / круп | полное ≤12 мал / сред / круп | полное ≤200 мал / сред / круп | полное открыто к концу мал / сред / круп |")
    w("|---|---|---|---|---|---|")
    for tf in TF_MIN:
        c = [sel(tf, "ce", seg=f"size={t}") for t in ("T1_small", "T2_mid", "T3_big")]
        f = [sel(tf, "full", seg=f"size={t}") for t in ("T1_small", "T2_mid", "T3_big")]
        w(f"| {tf} | {c[0].size_atr_q1:.2f} / {c[0].size_atr_q2:.2f} | {' / '.join(pct(x.F12) for x in c)} | {' / '.join(pct(x.F12) for x in f)} | "
          f"{' / '.join(pct(x.F200) for x in f)} | {' / '.join(pct(x.open_end_raw) for x in f)} |")
    w("")
    w("## Магнит или случайное блуждание? Гэп vs контроль на том же расстоянии\n")
    w("mirror — тот же бар, уровень на том же расстоянии от close[i] с противоположной стороны; "
      "random — случайный бар, уровень в ту же сторону на том же расстоянии в ATR (×3 выборки на гэп).\n")
    w("| ТФ | уровень | ≤3 гэп / mirror / random | ≤12 гэп / mirror / random | ≤48 гэп / mirror / random | ≤200 гэп / mirror / random |")
    w("|---|---|---|---|---|---|")
    for tf in TF_MIN:
        for lvl in ("touch", "ce", "full"):
            k = [sel(tf, lvl, kind=x) for x in ("gap", "mirror", "random")]
            w(f"| {tf} | {lvl} | " + " | ".join(" / ".join(pct(x[f'F{N}']) for x in k) for N in GRID) + " |")
    w("")
    w("### Разница «гэп − контроль» с 95% ДИ (кластерный бутстрап по времени, 2000 повторов)\n")
    w("Только гэпы с полным follow-up ≥ N баров (без цензуры). Кластер: 15m — день, 1h — неделя, 4h — месяц, 1d — квартал, 1w — год. "
      "Положительная разница = гэп достигается ЧАЩЕ контроля (магнит); ДИ, накрывающий 0, = отличия нет.\n")
    w("| ТФ | уровень | N | n | P(гэп) | гэп − mirror [95% ДИ] | гэп − random [95% ДИ] |\n|---|---|---|---|---|---|---|")
    pp = lambda x: f"{100 * x:+.1f}"
    for _, r in ci[(ci.segment == "all") & (ci.level.isin(["ce", "full"])) & (ci.N.isin([3, 12, 48]))].iterrows():
        w(f"| {r.tf} | {r.level} | {r.N} | {r.n_gap} | {pct(r.p_gap)} | {pp(r.d_mirror)} [{pp(r.d_mirror_lo)}; {pp(r.d_mirror_hi)}] | "
          f"{pp(r.d_random)} [{pp(r.d_random_lo)}; {pp(r.d_random_hi)}] |")
    w("")
    w("### То же по сегментам: полное заполнение за 12 баров, гэп − mirror / гэп − random (п.п., [95% ДИ])\n")
    w("| ТФ | по тренду | против тренда | мелкие (T1) | крупные (T3) |\n|---|---|---|---|---|")
    for tf in TF_MIN:
        cells = []
        for seg in ("trend=with", "trend=against", "size=T1_small", "size=T3_big"):
            r = ci[(ci.tf == tf) & (ci.segment == seg) & (ci.level == "full") & (ci.N == 12)]
            if r.empty:
                cells.append("—")
                continue
            r = r.iloc[0]
            cells.append(f"{pp(r.d_mirror)} [{pp(r.d_mirror_lo)}; {pp(r.d_mirror_hi)}] / {pp(r.d_random)} [{pp(r.d_random_lo)}; {pp(r.d_random_hi)}]")
        w(f"| {tf} | " + " | ".join(cells) + " |")
    w("")
    w("### Где гэп относительно цены (медианы, ≥0.1 ATR)\n")
    w("| ТФ | размер зоны, ×ATR | размер зоны, % цены | расстояние close[i] → CE, ×ATR |\n|---|---|---|---|")
    gg = rec[(rec.kind == "gap") & (rec.size_atr >= ATR_FILTER)]
    for tf in TF_MIN:
        x = gg[gg.tf == tf]
        w(f"| {tf} | {x.size_atr.median():.2f} | {x.size_pct.median():.2f}% | {x.d_ce_atr.median():.2f} |")
    w("")
    w("## По символам: полное заполнение ≤12 баров / открыто к концу (≥0.1 ATR)\n")
    w("| символ | " + " | ".join(TF_MIN) + " |\n|---|" + "---|" * len(TF_MIN))
    for s in SYMBOLS:
        cells = []
        for tf in TF_MIN:
            r = res[(res.tf == tf) & (res.flt == "atr>=0.1") & (res.segment == f"sym={s}") & (res.kind == "gap") & (res.level == "full")]
            cells.append("—" if r.empty else f"{pct(r.F12.iloc[0])} / {pct(r.open_end_raw.iloc[0])}")
        w(f"| {s} | " + " | ".join(cells) + " |")
    w("")
    w("## По направлению гэпа (бычий / медвежий), полное заполнение\n")
    w("| ТФ | n бычьи / медвежьи | ≤12 | ≤200 | открыто к концу |\n|---|---|---|---|---|")
    for tf in TF_MIN:
        b, s_ = sel(tf, "full", seg="dir=bull"), sel(tf, "full", seg="dir=bear")
        w(f"| {tf} | {b.n} / {s_.n} | {pct(b.F12)} / {pct(s_.F12)} | {pct(b.F200)} / {pct(s_.F200)} | {pct(b.open_end_raw)} / {pct(s_.open_end_raw)} |")
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
