"""`oskar cohorts` — сводка counterfactual-когорт по журналу. ТОЛЬКО чтение (mode=ro).

Блок R3 (2026-10-16). По когортам и версиям резолвера: n, висящие, доли исходов
(tp/sl/unfilled/no_data/...), неоднозначные, средний realized_r с 95% CI.

Уроки ночных проверок скептиков 2026-10-16:
- гипотетические сделки идут сериями (кулдаун 30 мин при горизонте 12 ч — до 10 почти
  одинаковых строк): статистика и по сделкам, и по эпизодам (первая строка эпизода —
  по времени, без оглядки на исход, иначе отбор по результату);
- бутстреп — блоками по UTC-суткам начала эпизода (сутки >= горизонта 12 ч, серия не
  рвется между блоками). Простой перцентильный, без scipy; seed фиксирован — отчет
  воспроизводим. Меньше 2 суток — CI не считается.

R — после комиссий в риск-единицах сайзинга (стоп = -1R), unfilled = 0R (не вошли — нет
P&L). У резолвера v1 R нет: старые исходы не переписываются.
Цифры отсюда в промпты агентов не идут (ст. 3 п. 4: статистика — только для анализа).
"""

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from oskar.journal.episodes import episode_start_ms, iso_to_ms

DEFAULT_BOOT = 2000
DEFAULT_SEED = 20261016
CI_LEVEL = 0.95
# обязательные колонки долей; прочие исходы (duration_stop, horizon_close, expired_flat) — если есть
MAIN_OUTCOMES = ("tp_hit", "sl_hit", "unfilled", "no_data")
OUTCOME_ORDER = MAIN_OUTCOMES + ("duration_stop", "horizon_close", "expired_flat")


@dataclass(frozen=True)
class CfRow:
    id: int
    cohort: str
    version: int
    outcome: str | None
    r: float | None
    episode: str  # episode_id или 'row:<id>' (строка без эпизода — сама себе эпизод)
    ts_ms: int  # время кандидата (порядок внутри эпизода)
    block: str  # UTC-сутки начала эпизода — блок бутстрепа
    ambiguous: bool


@dataclass
class RStats:
    n: int = 0
    mean: float | None = None
    ci: tuple[float, float] | None = None
    blocks: int = 0


@dataclass
class GroupStats:
    cohort: str
    version: int
    n: int = 0
    pending: int = 0
    ambiguous: int = 0
    n_episodes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    trades: RStats = field(default_factory=RStats)
    episodes: RStats = field(default_factory=RStats)

    @property
    def resolved(self) -> int:
        return self.n - self.pending


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Журнал только на чтение: без DDL и миграций connect() (журнал v4 читается как есть)."""
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"journal not found: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _ts_ms(candidate_json: str, created_at: str | None) -> int:
    try:
        return iso_to_ms(json.loads(candidate_json)["ts"])
    except (TypeError, ValueError, KeyError):
        pass
    try:
        return iso_to_ms(str(created_at))
    except (TypeError, ValueError):
        return 0


def _is_ambiguous(detail: str | None) -> bool:
    try:
        return bool(json.loads(detail or "").get("ambiguous"))
    except (TypeError, ValueError, AttributeError):
        return False


def load_rows(conn: sqlite3.Connection) -> list[CfRow]:
    """Строки counterfactuals; журнал до v5 — версия 1, без R, деталей и эпизодов."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(counterfactuals)")}

    def col(name: str) -> str:
        return name if name in cols else f"NULL AS {name}"

    rows = conn.execute(
        "SELECT id, cohort, candidate_json, hypothetical_outcome, created_at, "
        f"{col('resolver_version')}, {col('realized_r')}, {col('outcome_detail')}, "
        f"{col('episode_id')} FROM counterfactuals ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        ts_ms = _ts_ms(r["candidate_json"], r["created_at"])
        episode = r["episode_id"]
        try:
            start = episode_start_ms(episode) if episode else ts_ms
        except (ValueError, IndexError):
            start = ts_ms
        out.append(CfRow(
            id=int(r["id"]), cohort=str(r["cohort"]), version=int(r["resolver_version"] or 1),
            outcome=r["hypothetical_outcome"],
            r=None if r["realized_r"] is None else float(r["realized_r"]),
            episode=episode or f"row:{r['id']}", ts_ms=ts_ms, block=_day(start),
            ambiguous=_is_ambiguous(r["outcome_detail"]),
        ))
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def bootstrap_ci(
    blocks: dict[str, list[float]], n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED,
    level: float = CI_LEVEL,
) -> tuple[float, float] | None:
    """Перцентильный CI среднего: ресэмплинг БЛОКОВ (суток) с возвращением, среднее по
    объединению строк выбранных блоков. None — меньше 2 непустых блоков."""
    sums = [(sum(v), len(v)) for v in blocks.values() if v]
    if len(sums) < 2 or n_boot < 1:
        return None
    rng = random.Random(seed)
    k = len(sums)
    means = []
    for _ in range(n_boot):
        s = n = 0.0
        for _ in range(k):
            bs, bn = sums[rng.randrange(k)]
            s += bs
            n += bn
        means.append(s / n)
    means.sort()
    tail = (1.0 - level) / 2.0
    return _percentile(means, tail), _percentile(means, 1.0 - tail)


def r_stats(rows: list[CfRow], n_boot: int, seed: int) -> RStats:
    with_r = [x for x in rows if x.r is not None]
    if not with_r:
        return RStats()
    blocks: dict[str, list[float]] = {}
    for x in with_r:
        blocks.setdefault(x.block, []).append(float(x.r))
    mean = sum(x.r for x in with_r) / len(with_r)
    return RStats(n=len(with_r), mean=mean, ci=bootstrap_ci(blocks, n_boot, seed), blocks=len(blocks))


def first_of_episodes(rows: list[CfRow]) -> list[CfRow]:
    """Первая по времени строка каждого эпизода (исход не смотрится — иначе отбор по результату)."""
    first: dict[str, CfRow] = {}
    for x in rows:
        cur = first.get(x.episode)
        if cur is None or (x.ts_ms, x.id) < (cur.ts_ms, cur.id):
            first[x.episode] = x
    return list(first.values())


def summarize(rows: list[CfRow], n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED) -> list[GroupStats]:
    """Группы (когорта, версия резолвера), по версии — затем когорте."""
    groups: dict[tuple[str, int], list[CfRow]] = {}
    for x in rows:
        groups.setdefault((x.cohort, x.version), []).append(x)
    out = []
    for (cohort, version), xs in sorted(groups.items(), key=lambda kv: (-kv[0][1], kv[0][0])):
        g = GroupStats(cohort=cohort, version=version, n=len(xs))
        for x in xs:
            if x.outcome is None:
                g.pending += 1
            else:
                g.counts[x.outcome] = g.counts.get(x.outcome, 0) + 1
            g.ambiguous += int(x.ambiguous)
        g.trades = r_stats(xs, n_boot, seed)
        firsts = first_of_episodes(xs)
        g.n_episodes = len(firsts)
        g.episodes = r_stats(firsts, n_boot, seed)
        out.append(g)
    return out


def _signed(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.3f}"


def _r_line(label: str, s: RStats) -> str:
    if s.n == 0:
        return f"    {label}: no R yet"
    ci = f"[{_signed(s.ci[0])}; {_signed(s.ci[1])}]" if s.ci else "n/a (<2 days)"
    return f"    {label}: n={s.n} mean R {_signed(s.mean)} CI95 {ci} days={s.blocks}"


def render(groups: list[GroupStats], *, source: str, n_boot: int, seed: int) -> str:
    lines = [
        f"OSKAR counterfactual cohorts: {source} (read-only)",
        "R after fees in sizing risk units (stop = -1R), unfilled = 0R; "
        f"CI95: bootstrap over UTC days of episode start ({n_boot} resamples, seed {seed})",
        "outcome shares: of resolved rows; per-episode R: first row of each episode",
    ]
    if not groups:
        lines.append("no counterfactuals in the journal")
        return "\n".join(lines)
    for g in groups:
        lines.append("")
        lines.append(f"{g.cohort} v{g.version}: n={g.n} resolved={g.resolved} pending={g.pending} "
                     f"episodes={g.n_episodes} ambiguous={g.ambiguous}")
        shown = [o for o in OUTCOME_ORDER if o in MAIN_OUTCOMES or g.counts.get(o)]
        shown += sorted(o for o in g.counts if o not in OUTCOME_ORDER)
        denom = g.resolved
        parts = [f"{o} {g.counts.get(o, 0) / denom * 100:.1f}%" if denom else f"{o} -" for o in shown]
        lines.append("    outcomes: " + " | ".join(parts))
        if g.trades.n == 0 and g.version < 2:
            lines.append("    R: n/a (resolver v1 has no R)")
            continue
        lines.append(_r_line("R per trade  ", g.trades))
        lines.append(_r_line("R per episode", g.episodes))
    return "\n".join(lines)


def cli_main(db_path: Path, *, n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED) -> int:
    """`oskar cohorts`: сводка в stdout; журнал не меняется (ни миграций, ни записи)."""
    try:
        conn = open_readonly(db_path)
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"ERROR: {e}")
        return 1
    try:
        rows = load_rows(conn)
    finally:
        conn.close()
    print(render(summarize(rows, n_boot, seed), source=str(db_path), n_boot=n_boot, seed=seed))
    return 0
