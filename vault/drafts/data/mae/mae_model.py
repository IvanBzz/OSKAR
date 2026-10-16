#!/usr/bin/env python
"""MAE-модель «как не уйти в ликвидацию» — план в plan.md рядом (записан до расчёта).

Запуск (временный venv hold/.venv, OSKAR не трогается):
    python -I -B mae_model.py            # полный расчёт
    python -I -B mae_model.py --quick    # отладка: 2 символа, мало итераций
    python -I -B mae_model.py --rebuild  # пересобрать dataset.pkl

Шаги: 1) датасет: 4h из drafts/candles/cache (10 монет, вся история) + фандинг cache/ -> dataset.pkl;
      2) walk-forward по годам (2021..2026): B0 «ATR×k», B1 HAR-RV, M1 линейная квантильная, M2 HGBR-квантили;
      3) сигналы (b)/(d) setup_study, вторичный таргет MAE_до_цели на S1, плечо/выживаемость, тест направления;
      4) results.csv + tables.md (таблицы для results.md).
Функции гэпов/сигналов/ATR импортируются из drafts/setup/setup_study.py без изменений.
"""
from __future__ import annotations

import importlib.util
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import QuantileRegressor
from sklearn.metrics import roc_auc_score

HERE = Path(__file__).resolve().parent
DRAFTS = HERE.parent.parent
C_CANDLES = DRAFTS / "candles" / "cache"
CACHE = HERE / "cache"
_spec = importlib.util.spec_from_file_location("setup_study", DRAFTS / "setup" / "setup_study.py")
SS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SS)

QUICK = "--quick" in sys.argv
SYMBOLS = SS.SYMBOLS[:2] if QUICK else SS.SYMBOLS
HOUR, DAY, WEEK = SS.HOUR, SS.DAY, SS.WEEK
BAR = 4 * HOUR
HS = (42, 180)
QS = (0.80, 0.95)
WARM = 200
TEST_YEARS = list(range(2021, 2027))
MMR = 0.005          # поддерживающая маржа, первый тир (приближение)
FEES = 0.001         # две taker-ноги по 0.05 % (OSKAR risk/sizing.py TAKER_FEE_PCT)
LMAX = 20
FIXED_L = (1, 2, 3, 5, 10)
LEV_MODELS = ("B0", "M2", "M2es")
SEED = 20261016
M1_MAX = 20_000
ITERS = 40 if QUICK else 400
ITERS_CLF = 30 if QUICK else 300

BASE_FEATS = ["d", "atr_pct_4h", "atr_pct_1d", "rv_1d", "rv_3d", "rv_7d", "rv_ratio", "pos20_d", "pos50_d",
              "trend_1d_d", "trend_1w_d", "gap_dist_atr", "gap_corr", "gap_size_atr", "gap_tf", "dow", "hour",
              "fund_last_d", "fund_3d_d", "btc_atr_pct", "btc_rv_7d", "log_age"]
# ОТСТУПЛЕНИЕ ОТ ПЛАНА (после просмотра результатов, см. diag.py и results.md): заявленный M2 (400 итераций,
# с log_age) переобучается и недокрывает P95 (77–92 %). M2es — тот же бустинг, но (а) без log_age (прокси
# календарного времени монеты, не экстраполируется в новый год) и (б) число итераций выбирается по pinball
# на последних 15 % обучения (временная валидация, тест не используется), затем переобучение на всём обучении.
POSTHOC_DROP = ["log_age"]
ES_VAL_FRAC = 0.15


def feats_for(H: int) -> list[str]:
    return BASE_FEATS + [f"range{H}_atr"]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ----------------------------------------------------------------------------- датасет
def fwd_roll(x: np.ndarray, H: int, fn: str) -> np.ndarray:
    """Агрегат по барам t+1..t+H (rolling(H) на будущем окне)."""
    s = pd.Series(x).rolling(H)
    r = s.std(ddof=0) if fn == "std" else getattr(s, fn)()
    return r.shift(-H).to_numpy()


def htf_align(df4: pd.DataFrame, period: int, offset: int, ema_n: int, ot4: np.ndarray):
    """ATR14 и EMA закрытий старшего ТФ (агрегат из 4h), последний бар, закрывшийся не позже close 4h-бара."""
    htf = SS.agg_htf(df4, period, offset)
    h, l, c = (htf[k].to_numpy(float) for k in ("high", "low", "close"))
    atr, _ = SS.wilder_atr(h, l, c)
    ema = pd.Series(c).ewm(span=ema_n, adjust=False).mean().to_numpy()
    closes = htf.open_time.to_numpy() + period
    j = np.searchsorted(closes, ot4 + BAR, "right") - 1
    ok = j >= 0
    jj = np.maximum(j, 0)
    return np.where(ok, atr[jj], np.nan), np.where(ok, ema[jj], np.nan)


def build_symbol(s: str, btc: pd.DataFrame | None) -> pd.DataFrame:
    L, gaps, _ = SS.load_symbol("4h", s)
    df4 = SS.load(C_CANDLES / f"{s}USDT_4h.csv")
    o, h, l, c, ot, atr = L["o"], L["h"], L["l"], L["c"], L["ot"], L["atr"]
    n = len(c)
    ct = ot + BAR
    parts = {tf: SS.tf_targets(L, g, None) for tf, g in gaps.items()}
    T = SS.merge_targets(parts, ("1d", "1w"))
    sigs = SS.all_signals(L)
    r = np.r_[np.nan, np.diff(np.log(c))]
    rv = {k: pd.Series(r).rolling(w).std(ddof=0).to_numpy() * 100 for k, w in (("1d", 6), ("3d", 18), ("7d", 42))}
    atr_1d, ema_1d = htf_align(df4, DAY, 0, 20, ot)
    atr_1w, ema_1w = htf_align(df4, WEEK, SS.MONDAY0, 10, ot)
    roll = {w: (pd.Series(l).rolling(w).min().to_numpy(), pd.Series(h).rolling(w).max().to_numpy()) for w in (20, 50)}
    rng_h = {H: (pd.Series(h).rolling(H).max().to_numpy() - pd.Series(l).rolling(H).min().to_numpy()) / atr for H in HS}
    # фандинг
    f = pd.read_csv(CACHE / f"{s}USDT_funding.csv.gz")
    ft = f.funding_time.to_numpy()
    fr = f.funding_rate.to_numpy(float)
    cs = np.r_[0.0, np.cumsum(fr)]
    j = np.searchsorted(ft, ct, "right") - 1          # последнее начисление не позже close бара
    okf = j >= 0
    jj = np.maximum(j, 0)
    fund_last = np.where(okf, fr[jj], np.nan) * 100
    j0 = np.maximum(j - 8, 0)
    fund_3d = np.where(okf, (cs[jj + 1] - cs[j0]) / np.maximum(j + 1 - j0, 1), np.nan) * 100
    cumf = {}
    for H in HS:
        je = np.searchsorted(ft, ct + H * BAR, "right") - 1
        cumf[H] = cs[je + 1] - cs[j + 1]              # начисления в (close_t; close_{t+H}], доля notional
    fmin = {H: fwd_roll(l, H, "min") for H in HS}
    fmax = {H: fwd_roll(h, H, "max") for H in HS}
    rvf = {H: fwd_roll(r, H, "std") * 100 for H in HS}
    cH = {H: pd.Series(c).shift(-H).to_numpy() for H in HS}
    atr_pct = atr / c * 100
    valid_base = np.zeros(n, bool)
    valid_base[WARM:] = True
    valid_base &= np.isfinite(atr) & (atr > 0)
    if btc is not None:
        b = btc.set_index("ot").reindex(ot)
        btc_atr_pct, btc_rv_7d = b.atr_pct_4h.to_numpy(), b.rv_7d.to_numpy()
    else:
        btc_atr_pct, btc_rv_7d = atr_pct, rv["7d"]
    rows = []
    for d in (1, -1):
        tt = T[d]
        best = tt["best"]
        has = np.isfinite(best)
        df = pd.DataFrame({
            "sym": s, "ot": ot, "d": d, "atr_pct": atr_pct, "close": c,
            "atr_pct_4h": atr_pct, "atr_pct_1d": atr_1d / c * 100,
            "rv_1d": rv["1d"], "rv_3d": rv["3d"], "rv_7d": rv["7d"],
            "rv_ratio": np.where(rv["7d"] > 0, rv["1d"] / rv["7d"], np.nan),
            "trend_1d_d": d * (c - ema_1d) / atr_1d, "trend_1w_d": d * (c - ema_1w) / atr_1w,
            "gap_dist_atr": np.where(np.isfinite(tt["many"]), tt["many"], np.nan),
            "gap_corr": has.astype(float), "gap_size_atr": np.where(has, tt["size"], np.nan),
            "gap_tf": np.where(has, tt["tfi"] + 1, 0).astype(float),
            "dow": ((ot // DAY) + 3) % 7, "hour": (ot % DAY) // HOUR,
            "fund_last_d": d * fund_last, "fund_3d_d": d * fund_3d,
            "btc_atr_pct": btc_atr_pct, "btc_rv_7d": btc_rv_7d,
            "log_age": np.log1p((ot - ot[0]) / DAY),
            "sig_b": sigs["b"][d][0] & has, "sig_d": sigs["d"][d][0] & has,
        })
        for w in (20, 50):
            mn, mx = roll[w]
            p = np.where(mx > mn, (c - mn) / (mx - mn), np.nan)
            df[f"pos{w}_d"] = p if d > 0 else 1 - p
        for H in HS:
            mae = (c - fmin[H]) / atr if d > 0 else (fmax[H] - c) / atr
            df[f"mae{H}"] = np.maximum(mae, 0)
            df[f"rvf{H}"] = rvf[H]
            df[f"up{H}"] = np.where(np.isfinite(cH[H]), (cH[H] > c).astype(float), np.nan)
            df[f"cumf{H}"] = d * cumf[H]
            valid = valid_base & (np.arange(n) + H < n)
            df[f"valid{H}"] = valid
            df[f"range{H}_atr"] = rng_h[H]
            maetp = np.full(n, np.nan)
            reached = np.full(n, np.nan)
            idx = np.nonzero(has & valid)[0]
            if len(idx):
                J = idx[:, None] + np.arange(1, H + 1)
                HH, LL = h[J], l[J]
                near = tt["near"][idx][:, None]
                if d > 0:
                    hit = HH >= near
                    adv = (c[idx][:, None] - LL) / atr[idx][:, None]
                else:
                    hit = LL <= near
                    adv = (HH - c[idx][:, None]) / atr[idx][:, None]
                first = np.where(hit.any(1), hit.argmax(1), H - 1)
                adv = np.where(np.arange(H)[None, :] <= first[:, None], adv, -np.inf)
                maetp[idx] = np.maximum(adv.max(1), 0)
                reached[idx] = hit.any(1).astype(float)
            df[f"maetp{H}"] = maetp
            df[f"reached{H}"] = reached
        rows.append(df)
    out = pd.concat(rows, ignore_index=True)
    out["year"] = pd.to_datetime(out.ot, unit="ms").dt.year
    return out


def build_dataset() -> pd.DataFrame:
    btc_df = build_symbol("BTC", None)
    btc = btc_df[btc_df.d == 1][["ot", "atr_pct_4h", "rv_7d"]].reset_index(drop=True)
    parts = [btc_df]
    log(f"BTC: {len(btc_df) // 2} bars")
    for s in SYMBOLS:
        if s == "BTC":
            continue
        t0 = time.time()
        parts.append(build_symbol(s, btc))
        log(f"{s}: {len(parts[-1]) // 2} bars, {time.time() - t0:.1f}s")
    D = pd.concat(parts, ignore_index=True)
    return D


# ----------------------------------------------------------------------------- модели
def pinball(y: np.ndarray, qh: np.ndarray, q: float) -> float:
    u = y - qh
    return float(np.mean(np.maximum(q * u, (q - 1) * u)))


class Prep:
    """Импутация медианой обучения + флаги пропусков + стандартизация (для линейной модели)."""

    def fit(self, X: pd.DataFrame):
        self.med = X.median()
        self.miss_cols = [c for c in X.columns if X[c].isna().any()]
        Z = self._fill(X)
        self.mu, self.sd = Z.mean(), Z.std().replace(0, 1)
        return self

    def _fill(self, X):
        Z = X.fillna(self.med)
        for c in self.miss_cols:
            Z[c + "_na"] = X[c].isna().astype(float)
        return Z

    def transform(self, X: pd.DataFrame) -> np.ndarray:
        Z = self._fill(X)
        return ((Z - self.mu) / self.sd).fillna(0).to_numpy()


def har_fit_predict(tr: pd.DataFrame, te: pd.DataFrame, H: int, q: float, ytr: np.ndarray):
    """HAR-RV: OLS log RV_fwd_H на log rv_1d/3d/7d; масштаб в ATR; k_q — квантиль отношения на обучении."""
    def X(df):
        return np.column_stack([np.ones(len(df)), np.log(df.rv_1d.to_numpy()), np.log(df.rv_3d.to_numpy()),
                                np.log(df.rv_7d.to_numpy())])
    ok_tr = (tr.rv_1d > 0) & (tr.rv_3d > 0) & (tr.rv_7d > 0) & (tr[f"rvf{H}"] > 0)
    ok_tr &= np.isfinite(tr.rv_1d) & np.isfinite(tr.rv_3d) & np.isfinite(tr.rv_7d)
    Xtr = X(tr[ok_tr])
    beta, *_ = np.linalg.lstsq(Xtr, np.log(tr.loc[ok_tr, f"rvf{H}"].to_numpy()), rcond=None)
    s_tr = np.exp(Xtr @ beta) * np.sqrt(H) / tr.loc[ok_tr, "atr_pct"].to_numpy()
    k = float(np.quantile(ytr[ok_tr.to_numpy()] / s_tr, q))
    ok_te = ((te.rv_1d > 0) & (te.rv_3d > 0) & (te.rv_7d > 0)).to_numpy()
    out = np.full(len(te), np.nan)
    if ok_te.any():
        s_te = np.exp(X(te[ok_te]) @ beta) * np.sqrt(H) / te.loc[ok_te, "atr_pct"].to_numpy()
        out[ok_te] = k * s_te
    return out, beta, k


def fit_models(tr: pd.DataFrame, te: pd.DataFrame, H: int, target: str, models: tuple, rng) -> dict:
    """Возвращает model -> q -> q̂ на te."""
    feats = feats_for(H)
    ytr = tr[target].to_numpy()
    Xtr, Xte = tr[feats], te[feats]
    out: dict = {m: {} for m in models}
    b0 = {q: float(np.quantile(ytr, q)) for q in QS}
    for q in QS:
        out["B0"][q] = np.full(len(te), b0[q])
    if "B1" in models:
        for q in QS:
            p, beta, k = har_fit_predict(tr, te, H, q, ytr)
            out["B1"][q] = np.where(np.isfinite(p), p, b0[q])
            out["B1"].setdefault("meta", {})[q] = {"beta": beta.round(4).tolist(), "k": round(k, 3)}
    if "M1" in models:
        prep = Prep().fit(Xtr)
        sub = rng.choice(len(tr), size=min(M1_MAX, len(tr)), replace=False)
        Ztr, Zte = prep.transform(Xtr.iloc[sub]), prep.transform(Xte)
        for q in QS:
            # highs-ipm: 8 с на 20 тыс. строк против 39 с у highs (bench.py), коэффициенты близки
            m = QuantileRegressor(quantile=q, alpha=0.0, solver="highs-ipm").fit(Ztr, ytr[sub])
            out["M1"][q] = m.predict(Zte)
    def hgbr(q, it):
        return HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=it, learning_rate=0.05,
                                             max_leaf_nodes=15, min_samples_leaf=300, l2_regularization=1.0,
                                             early_stopping=False, random_state=SEED)
    if "M2" in models:
        for q in QS:
            out["M2"][q] = hgbr(q, ITERS).fit(Xtr, ytr).predict(Xte)
    if "M2es" in models:
        fe = [f for f in feats if f not in POSTHOC_DROP]
        order = np.argsort(tr.ot.to_numpy(), kind="stable")
        cut = int(len(order) * (1 - ES_VAL_FRAC))
        A, B = order[:cut], order[cut:]
        XA, XB, Xe = Xtr[fe].iloc[A], Xtr[fe].iloc[B], Xte[fe]
        for q in QS:
            m = hgbr(q, ITERS).fit(XA, ytr[A])
            best_i, best = 1, np.inf
            for i, p in enumerate(m.staged_predict(XB), 1):
                pl = pinball(ytr[B], p, q)
                if pl < best:
                    best, best_i = pl, i
            out["M2es"][q] = hgbr(q, best_i).fit(Xtr[fe], ytr).predict(Xe)
            out["M2es"].setdefault("meta", {})[q] = best_i
    return out


def year_bounds(Y: int) -> tuple[int, int]:
    a = int(pd.Timestamp(f"{Y}-01-01", tz="UTC").value // 10**6)
    b = int(pd.Timestamp(f"{Y + 1}-01-01", tz="UTC").value // 10**6)
    return a, b


def walk_forward(D: pd.DataFrame, H: int, target: str, mask: np.ndarray, models: tuple, tag: str, rng,
                 res: list, keep_cols: list) -> pd.DataFrame:
    """Walk-forward по годам; строки метрик -> res; возвращает тестовые строки с q̂ всех моделей."""
    preds = []
    base = mask & D[f"valid{H}"].to_numpy() & np.isfinite(D[target].to_numpy())
    for Y in TEST_YEARS:
        a, b = year_bounds(Y)
        ot = D.ot.to_numpy()
        tr_m = base & (ot + (H + 1) * BAR <= a)
        te_m = base & (ot >= a) & (ot < b)
        if tr_m.sum() < 1000 or te_m.sum() < 50:
            log(f"[{tag} H={H}] {Y}: skip (train={tr_m.sum()}, test={te_m.sum()})")
            continue
        tr, te = D[tr_m], D[te_m]
        t0 = time.time()
        out = fit_models(tr, te, H, target, models, rng)
        yte = te[target].to_numpy()
        P = te[keep_cols].copy()
        P["y"] = yte
        P["H"], P["year"], P["tag"] = H, Y, tag
        for m in models:
            for q in QS:
                qh = out[m][q]
                P[f"{m}_{q}"] = qh
                res.append(dict(tag=tag, H=H, target=target, year=Y, model=m, q=q, n_train=len(tr), n_test=len(te),
                                coverage=float(np.mean(yte <= qh)), pinball=pinball(yte, qh, q),
                                mean_qhat=float(np.mean(qh))))
        meta = out.get("B1", {}).get("meta")
        es = out.get("M2es", {}).get("meta")
        log(f"[{tag} H={H}] {Y}: train={len(tr)} test={len(te)} {time.time() - t0:.0f}s" +
            (f" HAR95 beta={meta[0.95]['beta']} k={meta[0.95]['k']}" if meta else "") +
            (f" M2es iters={es}" if es else ""))
        preds.append(P)
    return pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()


def add_pooled(res: pd.DataFrame, P: pd.DataFrame, tag: str, models: tuple) -> pd.DataFrame:
    rows = []
    for H in HS:
        PH = P[(P.H == H) & (P.tag == tag)]
        if len(PH) == 0:
            continue
        for m in models:
            for q in QS:
                qh = PH[f"{m}_{q}"].to_numpy()
                rows.append(dict(tag=tag, H=H, target=PH.target.iloc[0] if "target" in PH else "", year="all", model=m,
                                 q=q, n_train=np.nan, n_test=len(PH), coverage=float(np.mean(PH.y <= qh)),
                                 pinball=pinball(PH.y.to_numpy(), qh, q), mean_qhat=float(np.mean(qh))))
    return pd.concat([res, pd.DataFrame(rows)], ignore_index=True)


# ----------------------------------------------------------------------------- отчёт
def md_table(header, rows) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def f2(x, nd=2):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{nd}f}"


def pc(x, nd=1):
    return "—" if x is None or not np.isfinite(x) else f"{100 * x:.{nd}f}%"


def main() -> None:
    t_all = time.time()
    rng = np.random.default_rng(SEED)
    dpath = HERE / ("dataset_quick.pkl" if QUICK else "dataset.pkl")
    if dpath.exists() and "--rebuild" not in sys.argv:
        D = pd.read_pickle(dpath)
        log(f"dataset loaded: {len(D)} rows")
    else:
        D = build_dataset()
        D.to_pickle(dpath)
        log(f"dataset built: {len(D)} rows, {time.time() - t_all:.0f}s")
    L: list[str] = []
    res: list = []
    all_mask = np.ones(len(D), bool)
    models = ("B0", "B1", "M1", "M2", "M2es")
    keep = ["sym", "ot", "d", "atr_pct", "sig_b", "sig_d", "gap_corr"]

    # --- 0. описательно: таргет
    L.append("## Т1. Распределение MAE (в ATR сигнального бара), все бары S0\n")
    rows = []
    for H in HS:
        v = D[D[f"valid{H}"]]
        for d, name in ((1, "лонг"), (-1, "шорт")):
            y = v[v.d == d][f"mae{H}"]
            rows.append([H, f"{H * 4 / 24:.0f} дн.", name, len(y), f2(y.quantile(.5)), f2(y.quantile(.8)), f2(y.quantile(.95)),
                         f2(y.quantile(.99)), pc((y == 0).mean())])
    L.append(md_table(["H, баров", "≈ дней", "d", "n", "P50", "P80", "P95", "P99", "MAE = 0"], rows))
    k42 = D[D.valid42].mae42.quantile(.95)
    k180 = D[D.valid180].mae180.quantile(.95)
    L.append(f"\nПроверка sqrt(H)-масштаба: P95(180)/P95(42) = {k180 / k42:.2f} против sqrt(180/42) = {np.sqrt(180 / 42):.2f}; "
             f"P50: {D[D.valid180].mae180.quantile(.5) / D[D.valid42].mae42.quantile(.5):.2f}.\n")
    rows = []
    for Y in sorted(D.year.unique()):
        r = [Y]
        for H in HS:
            y = D[(D.year == Y) & D[f"valid{H}"]][f"mae{H}"]
            r += [len(y), f2(y.quantile(.5)), f2(y.quantile(.95))] if len(y) else ["", "", ""]
        rows.append(r)
    L.append(md_table(["Год", "n (H=42)", "P50", "P95", "n (H=180)", "P50", "P95"], rows))
    L.append("")
    rows = []
    for s in SYMBOLS:
        r = [s]
        for H in HS:
            y = D[(D.sym == s) & D[f"valid{H}"]][f"mae{H}"]
            ap = D[(D.sym == s) & D[f"valid{H}"]].atr_pct
            r += [f2(y.quantile(.95)), f2((y * ap / 100).quantile(.95) * 100, 1) + "%"]
        rows.append(r)
    L.append(md_table(["Символ", "P95 MAE42, ATR", "P95 MAE42, % цены", "P95 MAE180, ATR", "P95 MAE180, % цены"], rows))
    L.append("")

    # --- 1. главный тест: S0, таргет mae_H
    P_main = []
    for H in HS:
        P_main.append(walk_forward(D, H, f"mae{H}", all_mask, models, "S0", rng, res, keep))
    P_main = pd.concat(P_main, ignore_index=True)
    P_main["target"] = "mae"
    R = pd.DataFrame(res)
    R = add_pooled(R, P_main, "S0", models)

    def wf_table(tag, H, q, models_, R_):
        sub = R_[(R_.tag == tag) & (R_.H == H) & (R_.q == q)]
        years = [y for y in TEST_YEARS if (sub.year == y).any()] + ["all"]
        rows = []
        for Y in years:
            r = [Y, int(sub[(sub.year == Y) & (sub.model == "B0")].n_test.iloc[0])]
            pb0 = sub[(sub.year == Y) & (sub.model == "B0")].pinball.iloc[0]
            for m in models_:
                x = sub[(sub.year == Y) & (sub.model == m)]
                if len(x) == 0:
                    r += ["—", "—"]
                    continue
                r += [pc(x.coverage.iloc[0]), f2(x.pinball.iloc[0] / pb0, 3) if m != "B0" else f2(x.pinball.iloc[0], 3)]
            rows.append(r)
        hdr = ["Год", "n test"]
        for m in models_:
            hdr += [f"{m} покр.", f"{m} pinball" + ("" if m == "B0" else "/B0")]
        return md_table(hdr, rows)

    L.append("## Т2. Главный тест (S0, все бары, оба направления): покрытие и pinball относительно B0\n")
    L.append("B0 — константа «ATR × k» (квантиль обучения); B1 — HAR-RV; M1 — линейная квантильная регрессия; "
             "M2 — градиентный бустинг квантилей, как заявлено (400 итераций, с log_age); "
             "**M2es — пост-хок вариант** (без log_age, число итераций по временной валидации внутри обучения). "
             "Покрытие — доля MAE ≤ q̂ (цель = q). pinball/B0 < 1 — лучше базы.\n")
    for H in HS:
        for q in QS:
            L.append(f"### H = {H} баров (≈ {H * 4 / 24:.0f} дн.), q = {q}\n")
            L.append(wf_table("S0", H, q, models, R))
            L.append("")
    # критерии
    L.append("### Проверка критериев плана (q = 0.95)\n")
    rows = []
    for H in HS:
        for m in ("B1", "M1", "M2", "M2es"):
            sub = R[(R.tag == "S0") & (R.H == H) & (R.q == .95) & (R.year != "all")]
            yrs = sorted(set(sub.year))
            pb = {Y: sub[(sub.year == Y) & (sub.model == "B0")].pinball.iloc[0] for Y in yrs}
            ratio = [sub[(sub.year == Y) & (sub.model == m)].pinball.iloc[0] / pb[Y] for Y in yrs]
            cov = [sub[(sub.year == Y) & (sub.model == m)].coverage.iloc[0] for Y in yrs]
            n_better = sum(r_ <= .95 for r_ in ratio)
            n_cal = sum(.93 <= c_ <= .97 for c_ in cov)
            pooled = R[(R.tag == "S0") & (R.H == H) & (R.q == .95) & (R.year == "all")]
            rp = pooled[pooled.model == m].pinball.iloc[0] / pooled[pooled.model == "B0"].pinball.iloc[0]
            rows.append([H, m, f"{n_better}/{len(yrs)}", f2(rp, 3), f"{n_cal}/{len(yrs)}",
                         f2(min(ratio), 3) + "…" + f2(max(ratio), 3)])
    L.append(md_table(["H", "Модель", "лет с pinball/B0 ≤ 0.95", "pinball/B0 сводно", "лет с покрытием в [93; 97]%",
                       "pinball/B0 min…max по годам"], rows))
    L.append("")
    cov_b0 = []
    for H in HS:
        sub = R[(R.tag == "S0") & (R.H == H) & (R.q == .95) & (R.year != "all") & (R.model == "B0")]
        cov_b0.append(f"H={H}: " + ", ".join(f"{int(y)} {pc(c)}" for y, c in zip(sub.year, sub.coverage)))
    L.append("Покрытие базы B0 по годам (насколько «k прошлых лет» годится для нового года): " + "; ".join(cov_b0) + "\n")

    # --- 2. перенос на сигналы S2
    L.append("## Т3. Сигналы сетапа (4h, CHoCH (b) и доджи+CHoCH (d), гэп-цель 1d/1w): модель, обученная на S0\n")
    rows = []
    for H in HS:
        PH = P_main[P_main.H == H]
        for sname, col in (("(b) CHoCH", "sig_b"), ("(d) доджи+CHoCH", "sig_d"), ("S1: любой бар с гэп-целью", "gap_corr")):
            S = PH[PH[col].astype(bool)]
            if len(S) == 0:
                continue
            for q in QS:
                r = [H, sname, len(S), q]
                for m in models:
                    r += [pc(np.mean(S.y <= S[f"{m}_{q}"]))]
                r += [f2(S.y.quantile(.5)), f2(S.y.quantile(q))]
                rows.append(r)
    L.append(md_table(["H", "Выборка", "n", "q"] + [f"{m} покр." for m in models] + ["факт P50 MAE", "факт Pq MAE"], rows))
    L.append("")

    # --- 3. вторичный таргет: MAE до касания цели на S1
    L.append("## Т4. Вторичный таргет на S1: MAE до первого касания ближней границы гэпа (или до H)\n")
    s1 = D.gap_corr.to_numpy() > 0
    rows = []
    for H in HS:
        v = D[s1 & D[f"valid{H}"]]
        y = v[f"maetp{H}"]
        rows.append([H, len(v), pc(v[f"reached{H}"].mean()), f2(y.quantile(.5)), f2(y.quantile(.8)), f2(y.quantile(.95)),
                     f2(v[f"mae{H}"].quantile(.95)),
                     f2((v[f"maetp{H}"] / v.gap_dist_atr).quantile(.5)), pc((v[f"maetp{H}"] > v.gap_dist_atr).mean())])
    L.append(md_table(["H", "n", "цель достигнута до H", "P50", "P80", "P95", "P95 MAE_H (для сравн.)",
                       "P50 MAE_до_цели / расст. до гэпа", "доля: MAE_до_цели > расст. до гэпа"], rows))
    L.append("")
    sig_any = (D.sig_b | D.sig_d).to_numpy()
    rows = []
    for H in HS:
        v = D[sig_any & D[f"valid{H}"]]
        y = v[f"maetp{H}"]
        rows.append([H, len(v), pc(v[f"reached{H}"].mean()), f2(y.quantile(.5)), f2(y.quantile(.8)), f2(y.quantile(.95)),
                     f2((v[f"maetp{H}"] / v.gap_dist_atr).quantile(.5)), pc((v[f"maetp{H}"] > v.gap_dist_atr).mean())])
    L.append("То же только на сигналах (b)+(d):\n")
    L.append(md_table(["H", "n", "цель достигнута до H", "P50", "P80", "P95", "P50 MAE_до_цели / расст. до гэпа",
                       "доля: MAE_до_цели > расст. до гэпа"], rows))
    L.append("")
    res2: list = []
    P_tp = []
    for H in HS:
        P_tp.append(walk_forward(D, H, f"maetp{H}", s1, ("B0", "M2", "M2es"), "S1tp", rng, res2, keep))
    P_tp = pd.concat(P_tp, ignore_index=True)
    R2 = add_pooled(pd.DataFrame(res2), P_tp, "S1tp", ("B0", "M2", "M2es"))
    for H in HS:
        L.append(f"### MAE_до_цели, H = {H}, q = 0.95 (B0, M2, M2es)\n")
        L.append(wf_table("S1tp", H, .95, ("B0", "M2", "M2es"), R2))
        L.append("")
    R = pd.concat([R, R2], ignore_index=True)

    # --- 4. плечо и выживаемость
    L.append("## Т5. Плечо: «додержать в 95 % случаев» (изолированная маржа, MMR 0.5 %, две taker-ноги 0.1 %, фандинг)\n")
    rows = []
    for H in HS:
        PH = P_main[P_main.H == H].copy()
        PH = PH.merge(D[["sym", "ot", "d", f"cumf{H}", f"mae{H}"]], on=["sym", "ot", "d"], how="left")
        ot = D.ot.to_numpy()
        for Y in sorted(PH.year.unique()):
            a, _ = year_bounds(int(Y))
            tr = D[D[f"valid{H}"].to_numpy() & (ot + (H + 1) * BAR <= a)]
            budget = tr.groupby("d")[f"cumf{H}"].quantile(.95).to_dict()
            S = PH[PH.year == Y].copy()
            fb = S.d.map(budget).to_numpy()
            actual = S.y.to_numpy() * S.atr_pct.to_numpy() / 100 + FEES + S[f"cumf{H}"].to_numpy()
            r = [H, int(Y), len(S), f"{budget.get(1, np.nan) * 100:+.2f}% / {budget.get(-1, np.nan) * 100:+.2f}%"]
            for m in LEV_MODELS:
                need = S[f"{m}_0.95"].to_numpy() * S.atr_pct.to_numpy() / 100 + FEES + fb
                Lm = np.clip(np.floor(1 / (need + MMR)), 1, LMAX)
                surv = actual < 1 / Lm - MMR
                r += [f"{np.median(Lm):.0f}× / {np.quantile(Lm, .1):.0f}×", pc(surv.mean())]
            for Lf in FIXED_L:
                r += [pc((actual < 1 / Lf - MMR).mean())]
            rows.append(r)
    hdr = ["H", "Год", "n", "бюджет фандинга P95 (лонг/шорт)"]
    for m in LEV_MODELS:
        hdr += [f"{m}: плечо P50/P10", f"{m} выжив."]
    hdr += [f"выжив. {Lf}×" for Lf in FIXED_L]
    L.append(md_table(hdr, rows))
    L.append("\nВыживаемость при фиксированном плече — доля баров, где реальный MAE + комиссии + реальный фандинг "
             "не дошли до ликвидации (без стопа, без дозаливки). Плечо модели = floor(1/(q̂95·ATR% + 0.1 % + бюджет фандинга + MMR)), ≤ 20×.\n")
    rows = []
    for H in HS:
        v = D[D[f"valid{H}"]]
        actual = v[f"mae{H}"].to_numpy() * v.atr_pct.to_numpy() / 100 + FEES + v[f"cumf{H}"].to_numpy()
        for d, name in ((1, "лонг"), (-1, "шорт")):
            m = v.d.to_numpy() == d
            r = [H, name, int(m.sum())]
            for Lf in FIXED_L:
                r += [pc((actual[m] < 1 / Lf - MMR).mean())]
            cf = v[f"cumf{H}"].to_numpy()[m] * 100
            r += [f"{np.median(cf):+.2f}% / {np.quantile(cf, .95):+.2f}%"]
            rows.append(r)
    L.append("Выживаемость при фиксированном плече по всей истории, отдельно лонг/шорт; фандинг, уплаченный за H "
             "(медиана / P95, % notional; минус = получен):\n")
    L.append(md_table(["H", "d", "n"] + [f"{Lf}×" for Lf in FIXED_L] + ["фандинг за H: P50 / P95"], rows))
    L.append("")

    # --- 5. направление
    L.append("## Т6. Побочный тест: предсказуемо ли направление (sign(close_{t+H} − close_t)), те же признаки\n")
    rows = []
    feats_clf = [f for f in BASE_FEATS if f != "d"]
    for H in HS:
        base = (D.d.to_numpy() == 1) & D[f"valid{H}"].to_numpy() & np.isfinite(D[f"up{H}"].to_numpy())
        ot = D.ot.to_numpy()
        fe = feats_clf + [f"range{H}_atr"]
        for Y in TEST_YEARS:
            a, b = year_bounds(Y)
            tr_m = base & (ot + (H + 1) * BAR <= a)
            te_m = base & (ot >= a) & (ot < b)
            if tr_m.sum() < 1000 or te_m.sum() < 50:
                continue
            clf = HistGradientBoostingClassifier(max_iter=ITERS_CLF, learning_rate=0.05, max_leaf_nodes=15,
                                                 min_samples_leaf=300, l2_regularization=1.0, early_stopping=False,
                                                 random_state=SEED).fit(D.loc[tr_m, fe], D.loc[tr_m, f"up{H}"])
            p = clf.predict_proba(D.loc[te_m, fe])[:, 1]
            yv = D.loc[te_m, f"up{H}"].to_numpy()
            acc = float(np.mean((p > .5) == (yv > .5)))
            auc = float(roc_auc_score(yv, p)) if 0 < yv.mean() < 1 else np.nan
            rows.append([H, Y, int(te_m.sum()), pc(yv.mean()), pc(acc), f2(auc, 3)])
            res.append(dict(tag="dir", H=H, target=f"up{H}", year=Y, model="M2clf", q=np.nan, n_train=int(tr_m.sum()),
                            n_test=int(te_m.sum()), coverage=acc, pinball=auc, mean_qhat=float(yv.mean())))
        log(f"[dir H={H}] done")
    L.append(md_table(["H", "Год", "n test", "доля «вверх» (база)", "accuracy", "AUC"], rows))
    L.append("")

    R = pd.concat([R, pd.DataFrame([r for r in res if r["tag"] == "dir"])], ignore_index=True)
    R.to_csv(HERE / "results.csv", index=False, float_format="%.5g")
    P_main.drop(columns=["ot"]).assign(ot=P_main.ot).to_csv(HERE / "preds_s0.csv.gz", index=False, compression="gzip",
                                                           float_format="%.4g")
    meta = dict(symbols=SYMBOLS, rows=len(D), bars=int(len(D) // 2), first=str(pd.Timestamp(D.ot.min(), unit="ms").date()),
                last=str(pd.Timestamp(D.ot.max(), unit="ms").date()), quick=QUICK, seconds=round(time.time() - t_all),
                n_sig_b={H: int(((D.sig_b) & D[f"valid{H}"]).sum()) for H in HS},
                n_sig_d={H: int(((D.sig_d) & D[f"valid{H}"]).sum()) for H in HS},
                n_s1={H: int(((D.gap_corr > 0) & D[f"valid{H}"]).sum()) for H in HS})
    (HERE / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    (HERE / ("tables_quick.md" if QUICK else "tables.md")).write_text("\n".join(L), encoding="utf-8")
    log(f"done {time.time() - t_all:.0f}s; meta={meta}")


if __name__ == "__main__":
    main()
