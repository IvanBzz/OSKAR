#!/usr/bin/env python
"""Сборка расширенного датасета Binance USDⓈ-M USDT-перпов (см. plan.md рядом).

Запуск интерпретатором hold/.venv (копия окружения OSKAR; OSKAR только читается):
  python -I -B build_dataset.py klines [--with-1h]  # фаза A (1d кандидаты) -> B (4h универсум) [-> C (1h топ-50)]
  python -I -B build_dataset.py funding             # /fapi/v1/fundingRate для универсума, параллельный процесс
  python -I -B build_dataset.py index               # 1w из 1d, universe.csv, candidates_all.csv, сверки, summary.json
Докачка идемпотентна: готовые файлы пропускаются. Прогресс: build_dataset.log (klines/index),
build_dataset_funding.log (funding — отдельный файл, чтобы два процесса не писали в один).
Любой сетевой вызов — внутри try/except с повтором; 418 -> файл BAN.flag и остановка всех процессов.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
DRAFTS = HERE.parent
BAN_FLAG = HERE / "BAN.flag"
FAPI = "https://fapi.binance.com"
KL_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
           "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume"]
NUM = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"]
TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DAY = 86_400_000
WEEK = 7 * DAY
MON = 4 * DAY  # 1970-01-01 четверг -> первый понедельник эпохи = +4 дня
MIN_HISTORY_MS = 365 * DAY
TOP_N = 50
STALE_DAYS = 7       # делистнут, если последний закрытый 1d-бар старше 7 дней от снимка now_ms

KL_PAUSE = 0.34      # ~3 запр/с, вес 10 -> ~1800/мин из 2400
KL_WEIGHT_SOFT = 2000
FR_PAUSE = 0.72      # ~1.4 запр/с из лимита 500/5мин (отдельный лимит fundingRate)

LOG_MAIN = HERE / "build_dataset.log"
LOG_FUND = HERE / "build_dataset_funding.log"


class Banned(Exception):
    pass


try:  # stdout под Windows может быть cp1251 — имена символов с не-ASCII буквами валили print()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(fp, msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    with open(fp, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        print(line.encode("ascii", "replace").decode(), flush=True)


def iso(ms) -> str:
    if ms is None or ms == "" or (isinstance(ms, float) and np.isnan(ms)):
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def now_ms_snapshot() -> int:
    CACHE.mkdir(exist_ok=True)
    p = CACHE / "meta.json"
    if p.exists():
        return json.loads(p.read_text())["now_ms"]
    ms = int(time.time() * 1000)
    p.write_text(json.dumps({"now_ms": ms, "utc": pd.Timestamp(ms, unit="ms").isoformat()}))
    return ms


# ----------------------------------------------------------------------------- кандидаты
def candidates() -> dict[str, dict]:
    """USDT-перпы exchangeInfo (все статусы) + имена из публичного листинга data.binance.vision,
    которых exchangeInfo уже не отдаёт (source=vision_listing, status=REMOVED)."""
    ei = json.loads((HERE / "exchangeInfo.json").read_text())
    rows: dict[str, dict] = {}
    tradfi = set()
    for s in ei["symbols"]:
        if s["quoteAsset"] != "USDT":
            continue
        if s["contractType"] == "TRADIFI_PERPETUAL":
            tradfi.add(s["symbol"])
            continue
        if s["contractType"] != "PERPETUAL":
            continue
        rows[s["symbol"]] = dict(symbol=s["symbol"], status=s["status"], onboard_ms=s.get("onboardDate"),
                                 underlying=s.get("underlyingType", ""), source="exchangeInfo")
    vp = HERE / "vision_names.json"
    if vp.exists():
        for n in sorted(json.loads(vp.read_text())):
            if n.endswith("USDT") and "_" not in n and n not in rows and n not in tradfi:
                rows[n] = dict(symbol=n, status="REMOVED", onboard_ms=None, underlying="", source="vision_listing")
    return rows


# ----------------------------------------------------------------------------- HTTP
class Pacer:
    def __init__(self, pause: float):
        self.pause = pause
        self.t = 0.0

    def wait(self) -> None:
        d = self.t + self.pause - time.monotonic()
        if d > 0:
            time.sleep(d)
        self.t = time.monotonic()


class Stats:
    def __init__(self):
        self.nreq = 0
        self.n429 = 0
        self.max_weight = 0
        self.n_transport = 0


def get(cl: httpx.Client, path: str, params: dict, pacer: Pacer, st: Stats, logf, weight_guard: bool):
    """GET с паузой, повторами и контролем лимитов. Возвращает (json, http_status).
    Любая сетевая/разборная ошибка -> повтор; после 10 неудач -> ({'code': 'giveup', ...}, 0), без исключения."""
    last_err = "?"
    for attempt in range(10):
        if BAN_FLAG.exists():
            raise Banned("BAN.flag present")
        pacer.wait()
        try:
            r = cl.get(FAPI + path, params=params)
            st.nreq += 1
            w = r.headers.get("x-mbx-used-weight-1m")
            if w is not None and w.isdigit():
                st.max_weight = max(st.max_weight, int(w))
                if weight_guard and int(w) > KL_WEIGHT_SOFT:
                    log(logf, f"weight1m={w} > {KL_WEIGHT_SOFT}, pause 25s")
                    time.sleep(25)
            if r.status_code == 418:
                BAN_FLAG.write_text(f"{datetime.now(timezone.utc).isoformat()} 418 Retry-After={r.headers.get('Retry-After')}")
                log(logf, f"BANNED 418 Retry-After={r.headers.get('Retry-After')} {r.text[:200]}")
                raise Banned("418")
            if r.status_code == 429:
                st.n429 += 1
                ra = r.headers.get("Retry-After")
                sl = max(60, int(ra) if ra and ra.isdigit() else 0) + 5
                log(logf, f"429 (#{st.n429}) Retry-After={ra}; sleep {sl}s")
                time.sleep(sl)
                continue
            if r.status_code >= 500:
                last_err = f"http {r.status_code}"
                log(logf, f"{r.status_code} server error; retry in {5 * (attempt + 1)}s")
                time.sleep(5 * (attempt + 1))
                continue
            data = r.json()
        except Banned:
            raise
        except Exception as e:  # обрыв соединения, таймаут, битый json, что угодно
            st.n_transport += 1
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            sl = min(60, 5 * (attempt + 1))
            log(logf, f"net/parse error ({last_err}); retry in {sl}s")
            time.sleep(sl)
            continue
        if isinstance(data, dict) and "code" in data:
            if data["code"] == -1003:  # too many requests
                st.n429 += 1
                log(logf, f"-1003 {data.get('msg')}; sleep 65s")
                time.sleep(65)
                continue
            return data, r.status_code
        return data, r.status_code
    log(logf, f"giving up {path} {params}: {last_err}")
    return {"code": "giveup", "msg": last_err}, 0


def fetch_klines(cl, sym: str, tf: str, now_ms: int, pacer, st, logf) -> tuple[pd.DataFrame | None, dict]:
    bar = TF_MS[tf]
    start = 1
    rows: list = []
    nreq0 = st.nreq
    t0 = time.monotonic()
    err = None
    while True:
        data, code = get(cl, "/fapi/v1/klines",
                         {"symbol": sym, "interval": tf, "startTime": start, "endTime": now_ms - 1, "limit": 1500},
                         pacer, st, logf, weight_guard=True)
        if isinstance(data, dict):
            err = f"code={data.get('code')} msg={data.get('msg')}"
            break
        if not data:
            break
        rows += data
        nxt = data[-1][0] + bar
        if len(data) < 1500 or nxt >= now_ms:
            break
        start = nxt
    info = dict(symbol=sym, tf=tf, nreq=st.nreq - nreq0, sec=round(time.monotonic() - t0, 1), n_raw=len(rows))
    if err:
        info["error"] = err
        return None, info
    if not rows:
        info.update(n_saved=0, ghost_trimmed=0)
        return pd.DataFrame(columns=KL_COLS), info
    df = pd.DataFrame([x[:11] for x in rows], columns=KL_COLS)
    df[NUM] = df[NUM].astype(float)
    df["trades"] = df["trades"].astype(int)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    df = df[df.close_time < now_ms]
    nz = np.flatnonzero(df.volume.values > 0)
    n_before = len(df)
    df = df.iloc[: nz[-1] + 1] if len(nz) else df.iloc[0:0]
    info.update(n_saved=len(df), ghost_trimmed=n_before - len(df))
    return df.reset_index(drop=True), info


def save_df(df: pd.DataFrame, p: Path) -> None:
    tmp = p.with_suffix(".tmp")
    df.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(p)


def load_df(p: Path) -> pd.DataFrame:
    return pd.read_csv(p)


def append_jsonl(p: Path, rec: dict) -> None:
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def real_span(df: pd.DataFrame) -> tuple[int | None, int | None]:
    if df is None or len(df) == 0:
        return None, None
    return int(df.open_time.iloc[0]), int(df.open_time.iloc[-1])


def save_timing(timing_p: Path, timing: dict) -> None:
    timing_p.write_text(json.dumps(timing, indent=1))


# ----------------------------------------------------------------------------- режимы
def run_klines(with_1h: bool) -> None:
    logf = LOG_MAIN
    jl = HERE / "fetch_log_klines.jsonl"
    timing_p = HERE / "timing_klines.json"
    timing = json.loads(timing_p.read_text()) if timing_p.exists() else {}
    now_ms = now_ms_snapshot()
    cand = candidates()
    st = Stats()
    pacer = Pacer(KL_PAUSE)
    t_run = time.monotonic()
    log(logf, f"start klines: candidates={len(cand)} now_ms={now_ms} ({pd.Timestamp(now_ms, unit='ms')} UTC) "
              f"existing 1d={len(list(CACHE.glob('*_1d.csv.gz')))} 4h={len(list(CACHE.glob('*_4h.csv.gz')))}")
    try:
        with httpx.Client(timeout=30) as cl:
            # ---- фаза A: 1d для всех кандидатов
            timing.setdefault("A_1d", {}).setdefault("start", datetime.now(timezone.utc).isoformat())
            timing["A_1d"]["resumed_at"] = datetime.now(timezone.utc).isoformat()
            spans: dict[str, tuple] = {}
            skipped_p = HERE / "skipped_1d.json"
            skipped: dict[str, str] = json.loads(skipped_p.read_text()) if skipped_p.exists() else {}
            n_new = 0
            for i, sym in enumerate(sorted(cand)):
                p = CACHE / f"{sym}_1d.csv.gz"
                if p.exists():
                    df = load_df(p)
                    skipped.pop(sym, None)
                else:
                    df, info = fetch_klines(cl, sym, "1d", now_ms, pacer, st, logf)
                    append_jsonl(jl, info)
                    if df is None:
                        skipped[sym] = info["error"]
                        log(logf, f"[A {i+1}/{len(cand)}] {sym} 1d SKIP {info['error']}")
                        skipped_p.write_text(json.dumps(skipped, indent=1))
                        continue
                    save_df(df, p)
                    n_new += 1
                    log(logf, f"[A {i+1}/{len(cand)}] {sym} 1d n={len(df)} trimmed={info['ghost_trimmed']} "
                              f"req={info['nreq']} w={st.max_weight}")
                spans[sym] = real_span(df)
            skipped_p.write_text(json.dumps(skipped, indent=1))
            timing["A_1d"].update(end=datetime.now(timezone.utc).isoformat(), nreq_this_run=st.nreq, n429=st.n429,
                                  max_weight=st.max_weight, n_new_files=n_new)
            save_timing(timing_p, timing)
            # ---- фильтр универсума
            universe = sorted(s for s, (a, b) in spans.items() if a is not None and b - a >= MIN_HISTORY_MS)
            (HERE / "universe_symbols.json").write_text(json.dumps(universe))
            est_req = sum(int(np.ceil((spans[s][1] - spans[s][0]) / TF_MS["4h"] / 1500)) + 1 for s in universe
                          if not (CACHE / f"{s}_4h.csv.gz").exists())
            log(logf, f"phase A done: 1d files={len(spans)} skipped={len(skipped)} new={n_new}; "
                      f"universe (>=365d)={len(universe)} of {len(cand)}; 4h to fetch: ~{est_req} req "
                      f"(~{est_req * KL_PAUSE / 60:.0f} min)")
            # ---- фаза B: 4h для универсума
            timing.setdefault("B_4h", {}).setdefault("start", datetime.now(timezone.utc).isoformat())
            timing["B_4h"]["resumed_at"] = datetime.now(timezone.utc).isoformat()
            r0 = st.nreq
            tB = time.monotonic()
            n_new = 0
            todo = [s for s in universe if not (CACHE / f"{s}_4h.csv.gz").exists()]
            for i, sym in enumerate(todo):
                p = CACHE / f"{sym}_4h.csv.gz"
                df, info = fetch_klines(cl, sym, "4h", now_ms, pacer, st, logf)
                append_jsonl(jl, info)
                if df is None:
                    log(logf, f"[B {i+1}/{len(todo)}] {sym} 4h SKIP {info['error']}")
                    continue
                save_df(df, p)
                n_new += 1
                log(logf, f"[B {i+1}/{len(todo)}] {sym} 4h n={len(df)} trimmed={info['ghost_trimmed']} "
                          f"req={info['nreq']} w={st.max_weight}")
                if (i + 1) % 25 == 0:
                    el = time.monotonic() - tB
                    log(logf, f"  B progress {i+1}/{len(todo)} elapsed={el/60:.1f}min req={st.nreq - r0} "
                              f"eta={(el / (i+1)) * (len(todo) - i - 1) / 60:.1f}min n429={st.n429}")
            timing["B_4h"].update(end=datetime.now(timezone.utc).isoformat(), nreq_this_run=st.nreq - r0,
                                  n429=st.n429, max_weight=st.max_weight, n_new_files=n_new,
                                  n_4h_files=len(list(CACHE.glob("*_4h.csv.gz"))))
            save_timing(timing_p, timing)
            log(logf, f"phase B done: 4h files={timing['B_4h']['n_4h_files']} of universe {len(universe)}; "
                      f"new={n_new} req={st.nreq - r0} {(time.monotonic() - tB)/60:.1f}min")
            # ---- топ-50 по quote_volume за 365д среди живых символов универсума
            qv = {}
            for sym in universe:
                df = load_df(CACHE / f"{sym}_1d.csv.gz")
                a, b = real_span(df)
                live = (now_ms - (b + DAY)) <= STALE_DAYS * DAY and cand[sym]["status"] == "TRADING"
                if not live:
                    continue
                qv[sym] = float(df.loc[df.open_time >= now_ms - MIN_HISTORY_MS, "quote_volume"].sum())
            top = sorted(qv, key=qv.get, reverse=True)[:TOP_N]
            (HERE / "top50.json").write_text(json.dumps({s: qv[s] for s in top}, indent=1))
            log(logf, f"top{TOP_N} by 365d quote volume: {top[:10]} ...")
            # ---- фаза C (опционально): 1h для топ-50
            if with_1h:
                timing.setdefault("C_1h", {}).setdefault("start", datetime.now(timezone.utc).isoformat())
                r0 = st.nreq
                for i, sym in enumerate(top):
                    p = CACHE / f"{sym}_1h.csv.gz"
                    if p.exists():
                        continue
                    df, info = fetch_klines(cl, sym, "1h", now_ms, pacer, st, logf)
                    append_jsonl(jl, info)
                    if df is None:
                        log(logf, f"[C {i+1}/{len(top)}] {sym} 1h SKIP {info['error']}")
                        continue
                    save_df(df, p)
                    log(logf, f"[C {i+1}/{len(top)}] {sym} 1h n={len(df)} trimmed={info['ghost_trimmed']} "
                              f"req={info['nreq']} w={st.max_weight}")
                timing["C_1h"].update(end=datetime.now(timezone.utc).isoformat(), nreq_this_run=st.nreq - r0,
                                      n429=st.n429, max_weight=st.max_weight)
            else:
                log(logf, "phase C (1h top-50) skipped: run with --with-1h to fetch")
    except Banned as e:
        log(logf, f"STOP: banned ({e}); nreq={st.nreq}")
        save_timing(timing_p, timing)
        sys.exit(3)
    runs = timing.setdefault("runs", [])
    runs.append(dict(mode="klines", start=datetime.fromtimestamp(time.time() - (time.monotonic() - t_run), tz=timezone.utc).isoformat(),
                     end=datetime.now(timezone.utc).isoformat(), minutes=round((time.monotonic() - t_run) / 60, 1),
                     nreq=st.nreq, n429=st.n429, max_weight=st.max_weight, n_transport_err=st.n_transport))
    save_timing(timing_p, timing)
    log(logf, f"done klines: nreq={st.nreq} n429={st.n429} max_weight1m={st.max_weight} "
              f"net_errors={st.n_transport} {(time.monotonic() - t_run)/60:.1f}min")


def run_funding() -> None:
    logf = LOG_FUND
    jl = HERE / "fetch_log_funding.jsonl"
    timing_p = HERE / "timing_funding.json"
    now_ms = now_ms_snapshot()
    cand = candidates()
    up = HERE / "universe_symbols.json"
    syms = json.loads(up.read_text()) if up.exists() else sorted(cand)
    st = Stats()
    pacer = Pacer(FR_PAUSE)
    t_run = time.monotonic()
    timing = json.loads(timing_p.read_text()) if timing_p.exists() else {}
    timing.setdefault("start", datetime.now(timezone.utc).isoformat())
    todo = [s for s in syms if not (CACHE / f"{s}_funding.csv.gz").exists()]
    log(logf, f"start funding: symbols={len(syms)} ({'universe' if up.exists() else 'all candidates'}) "
              f"existing={len(syms) - len(todo)} todo={len(todo)}")
    try:
        with httpx.Client(timeout=30) as cl:
            for i, sym in enumerate(todo):
                p = CACHE / f"{sym}_funding.csv.gz"
                start = 1
                rows: list = []
                nreq0 = st.nreq
                t0 = time.monotonic()
                err = None
                while True:
                    data, code = get(cl, "/fapi/v1/fundingRate", {"symbol": sym, "startTime": start, "limit": 1000},
                                     pacer, st, logf, weight_guard=False)
                    if isinstance(data, dict):
                        err = f"code={data.get('code')} msg={data.get('msg')}"
                        break
                    if not data:
                        break
                    rows += data
                    if len(data) < 1000:
                        break
                    start = int(data[-1]["fundingTime"]) + 1
                info = dict(symbol=sym, tf="funding", nreq=st.nreq - nreq0, sec=round(time.monotonic() - t0, 1),
                            n_raw=len(rows))
                if err:
                    info["error"] = err
                    append_jsonl(jl, info)
                    log(logf, f"[{i+1}/{len(todo)}] {sym} funding SKIP {err}")
                    continue
                try:
                    df = pd.DataFrame([(int(x["fundingTime"]), float(x["fundingRate"]),
                                        float(x["markPrice"]) if x.get("markPrice") not in (None, "") else np.nan)
                                       for x in rows], columns=["funding_time", "funding_rate", "mark_price"])
                except (KeyError, TypeError, ValueError) as e:
                    info["error"] = f"parse: {type(e).__name__}: {e}"
                    append_jsonl(jl, info)
                    log(logf, f"[{i+1}/{len(todo)}] {sym} funding SKIP {info['error']}")
                    continue
                df = df.drop_duplicates("funding_time").sort_values("funding_time")
                df = df[df.funding_time < now_ms].reset_index(drop=True)
                info["n_saved"] = len(df)
                append_jsonl(jl, info)
                save_df(df, p)
                log(logf, f"[{i+1}/{len(todo)}] {sym} funding n={len(df)} req={info['nreq']}")
    except Banned as e:
        log(logf, f"STOP: banned ({e}); nreq={st.nreq}")
        sys.exit(3)
    timing.setdefault("runs", []).append(dict(end=datetime.now(timezone.utc).isoformat(),
                                              minutes=round((time.monotonic() - t_run) / 60, 1),
                                              nreq=st.nreq, n429=st.n429, n_transport_err=st.n_transport))
    timing.update(end=datetime.now(timezone.utc).isoformat(), n_files=len(list(CACHE.glob("*_funding.csv.gz"))))
    save_timing(timing_p, timing)
    log(logf, f"done funding: nreq={st.nreq} n429={st.n429} net_errors={st.n_transport} "
              f"{(time.monotonic() - t_run)/60:.1f}min files={timing['n_files']}")


# ----------------------------------------------------------------------------- индекс
def weekly_from_daily(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) == 0:
        return pd.DataFrame(columns=KL_COLS + ["n_days"])
    d = df.sort_values("open_time").copy()
    d["week"] = ((d.open_time - MON) // WEEK) * WEEK + MON
    g = d.groupby("week", sort=True)
    w = pd.DataFrame({
        "open_time": g["week"].first(),
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "close_time": g["week"].first() + WEEK - 1,
        "quote_volume": g["quote_volume"].sum(), "trades": g["trades"].sum(),
        "taker_buy_volume": g["taker_buy_volume"].sum(), "taker_buy_quote_volume": g["taker_buy_quote_volume"].sum(),
        "n_days": g.size(),
    }).reset_index(drop=True)
    return w


def compare_close(a: pd.DataFrame, b: pd.DataFrame, cols=("open", "high", "low", "close")) -> dict:
    m = a.merge(b, on="open_time", suffixes=("_a", "_b"))
    if len(m) == 0:
        return dict(n_common=0)
    out = dict(n_common=len(m))
    bad = np.zeros(len(m), dtype=bool)
    for c in cols:
        rel = np.abs(m[f"{c}_a"] - m[f"{c}_b"]) / np.maximum(np.abs(m[f"{c}_b"]), 1e-12)
        bad |= rel.values > 1e-9
        out[f"maxrel_{c}"] = float(rel.max())
    out["n_mismatch"] = int(bad.sum())
    return out


def run_index() -> None:
    logf = LOG_MAIN
    t_run = time.monotonic()
    now_ms = now_ms_snapshot()
    cand = candidates()
    log(logf, f"start index: candidates={len(cand)}")
    fi = {x["symbol"]: x for x in json.loads((HERE / "fundingInfo.json").read_text())} \
        if (HERE / "fundingInfo.json").exists() else {}
    fl = {}
    for p in (HERE / "fetch_log_klines.jsonl", HERE / "fetch_log_funding.jsonl"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                fl[(r["symbol"], r["tf"])] = r  # последняя запись по (symbol, tf) побеждает
    top = json.loads((HERE / "top50.json").read_text()) if (HERE / "top50.json").exists() else {}
    rank = {s: i + 1 for i, s in enumerate(top)}
    recs = []
    n_bars = dict(zip(["1h", "4h", "1d", "1w", "1w_all", "funding"], [0] * 6))
    for sym, c in sorted(cand.items()):
        p1d = CACHE / f"{sym}_1d.csv.gz"
        rec = dict(symbol=sym, source=c["source"], status=c["status"], underlying=c["underlying"],
                   onboard_date=iso(c["onboard_ms"]), in_universe=0, data_status="", exclude_reason="",
                   first_bar="", last_bar="", history_days="", bars_1d=0, bars_4h=0, bars_1w=0, bars_1w_all=0,
                   bars_1h=0, bars_funding=0, delisted="", delisted_no_data=0, delist_date="", stale_days="")
        if not p1d.exists():
            err = fl.get((sym, "1d"), {}).get("error", "not_fetched")
            if c["status"] == "PENDING_TRADING":
                rec.update(data_status="not_listed_yet", exclude_reason="no_1d_data:" + err)
            elif c["status"] in ("SETTLING", "REMOVED"):
                # делистнутый символ, по которому API не отдаёт свечей
                rec.update(data_status="delisted_no_data", delisted=1, delisted_no_data=1,
                           exclude_reason="no_1d_data:" + err)
            else:
                rec.update(data_status="no_data", exclude_reason="no_1d_data:" + err)
            recs.append(rec)
            continue
        d1 = load_df(p1d)
        a, b = real_span(d1)
        if a is None:
            if c["status"] in ("SETTLING", "REMOVED"):
                rec.update(data_status="delisted_no_data", delisted=1, delisted_no_data=1, exclude_reason="empty_1d")
            else:
                rec.update(data_status="empty_1d", exclude_reason="empty_1d")
            recs.append(rec)
            continue
        hist_days = (b - a) // DAY + 1
        stale_days = (now_ms - (b + DAY)) / DAY          # дней с закрытия последнего реального 1d-бара
        stale = stale_days > STALE_DAYS
        delisted = int(c["status"] in ("SETTLING", "REMOVED") or stale)
        zero_inner = int((d1.volume.values == 0).sum())
        rec.update(first_bar=iso(a), last_bar=iso(b), history_days=int(hist_days), delisted=delisted,
                   delist_date=iso(b + DAY) if delisted else "", stale_days=round(stale_days, 1),
                   stale_but_trading=int(stale and c["status"] == "TRADING"),
                   settling_but_fresh=int((not stale) and c["status"] == "SETTLING"),
                   bars_1d=len(d1), ghost_inner_1d=zero_inner,
                   ghost_trimmed_1d=fl.get((sym, "1d"), {}).get("ghost_trimmed", ""),
                   missing_1d=int((b - a) // DAY + 1 - len(d1)))
        in_u = int(b - a >= MIN_HISTORY_MS)
        rec["in_universe"] = in_u
        rec["data_status"] = "ok" if in_u else "history_lt_365d"
        if not in_u:
            rec["exclude_reason"] = f"history_{hist_days}d<365"
        # 1w (агрегат из 1d) — только для универсума
        if in_u:
            w = weekly_from_daily(d1)
            save_df(w, CACHE / f"{sym}_1w.csv.gz")
            rec["bars_1w"] = int((w.n_days == 7).sum())
            rec["bars_1w_all"] = len(w)
            n_bars["1w"] += rec["bars_1w"]
            n_bars["1w_all"] += len(w)
            n_bars["1d"] += len(d1)
        # 4h
        p4 = CACHE / f"{sym}_4h.csv.gz"
        if p4.exists():
            d4 = load_df(p4)
            a4, b4 = real_span(d4)
            rec["bars_4h"] = len(d4)
            rec["first_bar_4h"] = iso(a4)
            rec["last_bar_4h"] = iso(b4)
            rec["missing_4h"] = int((b4 - a4) // TF_MS["4h"] + 1 - len(d4)) if a4 is not None else 0
            rec["ghost_trimmed_4h"] = fl.get((sym, "4h"), {}).get("ghost_trimmed", "")
            if in_u:
                n_bars["4h"] += len(d4)
        else:
            rec["missing_4h"] = ""
        # 1h
        p1h = CACHE / f"{sym}_1h.csv.gz"
        if p1h.exists():
            dh = load_df(p1h)
            ah, bh = real_span(dh)
            rec["bars_1h"] = len(dh)
            rec["missing_1h"] = int((bh - ah) // TF_MS["1h"] + 1 - len(dh)) if ah is not None else 0
            if in_u:
                n_bars["1h"] += len(dh)
        else:
            rec["missing_1h"] = ""
        # funding
        pf = CACHE / f"{sym}_funding.csv.gz"
        if pf.exists():
            f = load_df(pf)
            rec["bars_funding"] = len(f)
            rec["funding_first"] = iso(f.funding_time.iloc[0]) if len(f) else ""
            rec["funding_last"] = iso(f.funding_time.iloc[-1]) if len(f) else ""
            if in_u:
                n_bars["funding"] += len(f)
        rec["funding_interval_h"] = fi.get(sym, {}).get("fundingIntervalHours", 8)
        qv = float(d1.loc[d1.open_time >= now_ms - MIN_HISTORY_MS, "quote_volume"].sum())
        rec["qv_365d_usdt"] = round(qv)
        rec["rank_365d"] = rank.get(sym, "")
        rec["top50"] = int(sym in rank)
        recs.append(rec)
    allc = pd.DataFrame(recs)
    for c in allc.columns:  # целочисленные колонки без хвоста ".0" и с пустыми ячейками вместо NaN
        if c.startswith(("bars_", "missing_", "ghost_")) or c in ("delisted", "delisted_no_data", "top50", "rank_365d",
                                                                   "qv_365d_usdt", "history_days", "funding_interval_h",
                                                                   "stale_but_trading", "settling_but_fresh", "in_universe"):
            allc[c] = pd.to_numeric(allc[c], errors="coerce").round().astype("Int64")
    allc.to_csv(HERE / "candidates_all.csv", index=False)
    # universe.csv = универсум (>=365д истории) + делистнутые без данных (помечены delisted_no_data)
    uni = allc[(allc.in_universe == 1) | (allc.data_status == "delisted_no_data")].copy()
    cols = ["symbol", "status", "onboard_date", "first_bar", "last_bar", "bars_1d", "bars_4h", "bars_1w",
            "bars_funding", "delisted", "delisted_no_data", "data_status", "delist_date", "stale_days",
            "qv_365d_usdt", "rank_365d", "top50", "source", "underlying", "history_days", "bars_1w_all",
            "bars_1h", "first_bar_4h", "last_bar_4h", "funding_first", "funding_last", "funding_interval_h",
            "stale_but_trading", "settling_but_fresh", "missing_1d", "missing_4h", "missing_1h",
            "ghost_inner_1d", "ghost_trimmed_1d", "ghost_trimmed_4h", "exclude_reason"]
    uni = uni[[c for c in cols if c in uni.columns]]
    uni.to_csv(HERE / "universe.csv", index=False)
    u = allc[allc.in_universe == 1]

    # ---- сверки с прежними кешами (10 монет, пересечение по open_time)
    checks = {}
    g1w = DRAFTS / "gaps" / "cache"
    c4h = DRAFTS / "candles" / "cache"
    ten = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "SUI"]
    try:
        for sym in ["BTCUSDT", "ETHUSDT"]:
            if (g1w / f"{sym}_1w.csv").exists() and (CACHE / f"{sym}_1w.csv.gz").exists():
                mine = load_df(CACHE / f"{sym}_1w.csv.gz")
                mine = mine[mine.n_days == 7]
                checks[f"1w_agg_vs_binance_native_{sym}"] = compare_close(mine, pd.read_csv(g1w / f"{sym}_1w.csv"))
        for tf, src in (("1d", g1w), ("4h", c4h)):
            agg = dict(n_common=0, n_mismatch=0, symbols=0)
            for s in ten:
                s += "USDT"
                if (src / f"{s}_{tf}.csv").exists() and (CACHE / f"{s}_{tf}.csv.gz").exists():
                    r = compare_close(load_df(CACHE / f"{s}_{tf}.csv.gz"), pd.read_csv(src / f"{s}_{tf}.csv"))
                    agg["n_common"] += r["n_common"]; agg["n_mismatch"] += r.get("n_mismatch", 0); agg["symbols"] += 1
            checks[f"{tf}_vs_old_cache_10coins"] = agg
    except Exception as e:  # сверка не должна валить индекс
        checks["error"] = f"{type(e).__name__}: {e}"

    # ---- сводка
    tk = json.loads((HERE / "timing_klines.json").read_text()) if (HERE / "timing_klines.json").exists() else {}
    tf_ = json.loads((HERE / "timing_funding.json").read_text()) if (HERE / "timing_funding.json").exists() else {}
    skipped = json.loads((HERE / "skipped_1d.json").read_text()) if (HERE / "skipped_1d.json").exists() else {}
    size_mb = sum(p.stat().st_size for p in CACHE.glob("*.csv.gz")) / 1e6
    m4 = pd.to_numeric(u.missing_4h, errors="coerce").astype(float).fillna(0)
    share4 = (m4 / u.bars_4h.astype(float).replace(0, np.nan)).fillna(0)
    dl = u[u.delisted.fillna(0) == 1]
    summary = dict(
        now_utc=pd.Timestamp(now_ms, unit="ms").isoformat(),
        candidates=len(allc), candidates_by_source=allc.source.value_counts().to_dict(),
        universe=len(u), universe_by_source=u.source.value_counts().to_dict(),
        universe_by_status=u.status.value_counts().to_dict(),
        delisted_with_data=int(len(dl)), delisted_share=round(float(u.delisted.mean()), 4),
        delisted_by_status=dl.status.value_counts().to_dict(),
        delisted_no_data=int((allc.data_status == "delisted_no_data").sum()),
        delisted_no_data_symbols=allc.loc[allc.data_status == "delisted_no_data", "symbol"].tolist(),
        stale_but_trading=int(u.stale_but_trading.sum()),
        stale_but_trading_symbols=u.loc[u.stale_but_trading == 1, "symbol"].tolist(),
        settling_but_fresh=int(u.settling_but_fresh.sum()),
        first_bar_min=u.first_bar.min(), last_bar_max=u.last_bar.max(),
        listing_years=u.first_bar.str[:4].value_counts().sort_index().to_dict(),
        delist_years=dl.delist_date.str[:4].value_counts().sort_index().to_dict(),
        bars_universe=n_bars, bars_1d_all_candidates=int(allc.bars_1d.sum()),
        files=len(list(CACHE.glob("*.csv.gz"))), size_mb=round(size_mb, 1),
        universe_missing_4h_file=u.loc[u.bars_4h == 0, "symbol"].tolist(),
        n_4h_missing_gt_0_5pct=int((share4 > 0.005).sum()),
        symbols_4h_missing_gt_0_5pct=u.loc[share4 > 0.005, "symbol"].tolist(),
        universe_no_funding=u.loc[u.bars_funding == 0, "symbol"].tolist(),
        ghost_inner_1d_symbols=int((u.ghost_inner_1d.fillna(0) > 0).sum()),
        skipped_1d=skipped, checks=checks, timing_klines=tk, timing_funding=tf_,
        excluded_reasons=allc[allc.in_universe == 0].data_status.value_counts().to_dict(),
        top50=list(rank), index_minutes=round((time.monotonic() - t_run) / 60, 1),
    )
    (HERE / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    log(logf, f"done index: universe={len(u)} delisted={len(dl)} no_data={summary['delisted_no_data']} "
              f"bars={n_bars} files={summary['files']} {summary['index_minutes']}min")
    print(json.dumps(summary, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else ""
    if mode == "klines":
        run_klines(with_1h="--with-1h" in args)
    elif mode == "funding":
        run_funding()
    elif mode == "index":
        run_index()
    else:
        print(__doc__)
        sys.exit(2)
