"""Регрессии на находки ревью Codex (gpt-6-astra), раунд 2 — 2026-10-15.

Каждый тест воспроизводит сценарий, который Codex собрал на влитом коде.
"""

from datetime import timedelta

import ccxt

from oskar.core import executor as executor_mod
from oskar.data.client import classify_error
from oskar.execution.base import (
    OpenOrderRef,
    OrderState,
    PlacedOrder,
    PositionInfo,
    ProtectiveOrder,
    cid_base,
    owns_protection,
)
from tests.unit.test_position_monitor import SYM, T0, add_trade, make_monitor, trade
from tests.unit.test_protection_lifecycle import ScriptedExchange, make_service, row

# ------------------------------------------------- high #1: таймаут с «429» в URL


def test_timeout_with_429_in_url_is_network_not_rate_limit():
    """В тексте ccxt — URL запроса: quantity=429 делал таймаут «rate limit»."""
    e = ccxt.RequestTimeout(
        "bingx POST https://open-api-vst.bingx.com/openApi/swap/v2/trade/order"
        "?quantity=429&symbol=X-USDT&timestamp=1 timed out"
    )
    assert classify_error(e) == "network"
    assert classify_error(ccxt.RateLimitExceeded("bingx 429 Too Many Requests")) == "cooldown"


def test_entry_timeout_with_429_in_url_never_retried_blindly(tmp_path):
    """Таймаут входа (исход неизвестен) -> поиск по clientOrderId, а не голый повтор.
    Раньше: повтор отклонялся как дубль — позиция есть, записи нет, entry_sent=False."""
    ex = ScriptedExchange()
    ex.entry_error = ccxt.RequestTimeout("bingx POST .../trade/order?quantity=429 timed out")
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "market")
    assert [c for c in ex.calls if c[0] == "entry"] == [("entry", "market", p.sl, p.tp)]  # один POST
    assert out.entry_sent and not out.ok
    assert row(j, p)["status"] == "pending"  # намерение записано — монитор разрешит


def test_rate_limited_entry_is_not_retried_bare(tmp_path):
    ex = ScriptedExchange()
    ex.reject_attached = ccxt.ExchangeError('bingx HTTP 429 {"code":429,"msg":"too many"}')
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "limit")
    assert not out.ok and len([c for c in ex.calls if c[0] == "entry"]) == 1


# ------------------------------------- high #2: сироты vs исполнитель «в процессе»


def test_orphan_scan_spares_inflight_execution(tmp_path):
    """Исполнитель уже выставил SL/TP, записи в журнале еще нет — монитор не снимает их."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid = executor_mod.make_client_order_id("r9", "SOLUSDTUSDT-1-long")
    ex.open_list = [OpenOrderRef(SYM, "s1", cid + "-sl", "sl"), OpenOrderRef(SYM, "t1", cid + "-tp", "tp")]
    with executor_mod._INFLIGHT_LOCK:
        executor_mod._INFLIGHT.add(cid_base(cid))
    try:
        rep = mon.tick()
    finally:
        with executor_mod._INFLIGHT_LOCK:
            executor_mod._INFLIGHT.discard(cid_base(cid))
    assert ex.cancelled == [] and rep.orphans == []


def test_executor_registers_inflight_only_during_execution(tmp_path):
    seen = []
    ex = ScriptedExchange()
    orig = ex.place_entry

    def spy(*a, **k):
        seen.append(set(executor_mod.inflight_client_ids()))
        return orig(*a, **k)

    ex.place_entry = spy
    svc, j, p, _ = make_service(tmp_path, ex)
    svc.execute(p.candidate_id, "limit")
    key = cid_base(executor_mod.make_client_order_id("r1", p.candidate_id))
    assert key in seen[0] and key not in executor_mod.inflight_client_ids()


# ------------------------------------------ high #3: ручной стоп — не наша защита


def test_partial_manual_stop_is_not_adopted(tmp_path):
    """Ручной SL на 0.1 для позиции 2 раньше «усыновлялся»; теперь монитор ставит свой."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", qty=2.0)
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.5)]
    ex.protection = [ProtectiveOrder("manual", "sl", 98.0, "open", qty=0.1)]
    placed = []
    ex.place_sl = lambda s, side, px, cid: (
        placed.append(cid) or PlacedOrder("own-sl", cid, "STOP_MARKET", "open")
    )
    mon.tick()
    assert placed and trade(j, "d1")["sl_order_id"] == "own-sl"


def test_ownership_rules():
    owner = "OSKAR-r1-abc"
    assert owns_protection(
        ProtectiveOrder("a", "sl", 1.0, "open", qty=2.0), owner_cid=owner, position_qty=2.0
    )
    assert owns_protection(
        ProtectiveOrder("b", "sl", 1.0, "open", close_position=True), owner_cid=owner, position_qty=2.0
    )
    assert not owns_protection(
        ProtectiveOrder("c", "sl", 1.0, "open", qty=0.1), owner_cid=owner, position_qty=2.0
    )
    assert not owns_protection(
        ProtectiveOrder("d", "sl", 1.0, "open", qty=2.0, client_order_id="OSKAR-r2-zzz"),
        owner_cid=owner,
        position_qty=2.0,
    )  # другая сделка OSKAR
    assert owns_protection(
        ProtectiveOrder("e", "sl", 1.0, "open", qty=2.0, client_order_id="oskar-r1-abc-sl-r2"),
        owner_cid=owner,
        position_qty=2.0,
    )


def test_manual_stop_fill_in_history_not_attributed(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", qty=2.0, opened_at=T0 - timedelta(minutes=30))
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ms = int((T0 - timedelta(minutes=10)).timestamp() * 1000)
    ex.protection = [
        ProtectiveOrder("manual", "sl", 98.0, "filled", avg_price=97.0, timestamp_ms=ms, qty=0.1),
        ProtectiveOrder("manual-tp", "tp", 104.0, "open", qty=0.1),
    ]
    ex.pos = []
    mon.tick()
    mon.tick()
    t = trade(j, "d1")
    assert t["close_reason"] == "manual" and "manual-tp" not in ex.cancelled  # чужое не трогаем


# ---------------------------------- high #4: частичный филл голой лимитки — сразу


def test_partial_fill_protected_immediately_not_at_ttl(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, limit_ttl=3600)
    add_trade(j, "p1", status="pending", qty=2.0, opened_at=T0 - timedelta(minutes=2))
    j.execute(
        "UPDATE trades SET entry_order_id='e1', sl_order_id=NULL, tp_order_id=NULL WHERE decision_id='p1'"
    )
    j.commit()
    ex.orders["e1"] = OrderState("e1", "open", filled_qty=0.5, avg_price=99.8)
    placed = []

    def cancel(symbol, oid):
        ex.cancelled.append(oid)
        ex.orders["e1"] = OrderState("e1", "canceled", filled_qty=0.5, avg_price=99.8)
        return True

    ex.cancel_order = cancel
    ex.place_sl = lambda s, side, px, cid: (
        placed.append("sl") or PlacedOrder("s1", cid, "STOP_MARKET", "open")
    )
    ex.place_tp = lambda s, side, px, cid: (
        placed.append("tp") or PlacedOrder("t1", cid, "TAKE_PROFIT_MARKET", "open")
    )
    mon.tick_pending()  # задолго до TTL
    t = trade(j, "p1")
    assert ex.cancelled == ["e1"] and t["status"] == "open" and t["qty"] == 0.5
    assert placed == ["sl", "tp"]


# ------------------------------- high #5: список защиты недоступен — не молчим


def test_protection_list_failure_still_places_own_stop(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.5)]

    def boom(*a, **k):
        raise ccxt.NetworkError("open orders unavailable")

    ex.protection_orders = boom
    placed = []
    ex.place_sl = lambda s, side, px, cid: placed.append(cid) or PlacedOrder("s9", cid, "STOP_MARKET", "open")
    mon.tick()
    assert placed and trade(j, "d1")["sl_order_id"] == "s9"


def test_protection_list_failure_and_placement_failure_flip_safe_mode(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.5)]
    ex.protection_orders = lambda *a, **k: (_ for _ in ()).throw(ccxt.NetworkError("down"))
    ex.place_sl = lambda *a, **k: (_ for _ in ()).throw(ccxt.NetworkError("down"))
    mon.tick()
    assert state.safe_mode and any("БЕЗ SL" in n for n in notes)


# ------------------------------- medium #6: время входа — биржевое, не опроса


def test_opened_at_from_exchange_fill_time_and_early_sl_found(tmp_path):
    """Лимитка исполнилась и SL сработал ДО первого опроса: opened_at — время филла
    по бирже, история находит стоп (раньше — manual без P&L)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending", qty=2.0, opened_at=T0 - timedelta(minutes=20))
    j.execute(
        "UPDATE trades SET entry_order_id='e1', sl_order_id=NULL, tp_order_id=NULL WHERE decision_id='p1'"
    )
    j.commit()
    fill_ms = int((T0 - timedelta(minutes=15)).timestamp() * 1000)
    ex.orders["e1"] = OrderState("e1", "filled", 2.0, 99.5, 0.05, timestamp_ms=fill_ms)
    ex.protection = [
        ProtectiveOrder("a-sl", "sl", 98.0, "filled", avg_price=97.95, timestamp_ms=fill_ms + 60_000, qty=2.0)
    ]
    ex.pos = []  # позиции уже нет: стоп сработал до опроса
    mon.tick_pending()  # promote: opened_at = время филла
    assert trade(j, "p1")["opened_at"].startswith((T0 - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M"))
    mon.tick()
    mon.tick()
    t = trade(j, "p1")
    assert t["close_reason"] == "sl_hit" and t["exit_price"] == 97.95


# ---------------------------------- идея: open без SL — в быстром проходе


def test_fast_lane_protects_open_trade_without_stop(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "o1")
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='o1'")
    j.commit()
    ex.orders["o1-tp"] = OrderState("o1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    placed = []
    ex.place_sl = lambda s, side, px, cid: placed.append(cid) or PlacedOrder("s2", cid, "STOP_MARKET", "open")
    mon.tick_pending()
    assert placed and trade(j, "o1")["sl_order_id"] == "s2"


# ------------------------------------------- перепроверка Codex: частичные пункты


def test_orphan_scan_reads_registry_after_order_list(tmp_path):
    """Исполнение стартовало ПОСЛЕ прежнего снимка реестра, но до ответа списка ордеров:
    его свежий SL уже в списке. Реестр теперь читается после списка — SL не снимается."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid = executor_mod.make_client_order_id("r7", "ETHUSDTUSDT-1-long")
    key = cid_base(cid)

    def listing():
        with executor_mod._INFLIGHT_LOCK:  # исполнитель зарегистрировался и поставил SL
            executor_mod._INFLIGHT.add(key)
        return [OpenOrderRef(SYM, "fresh-sl", cid + "-sl", "sl")]

    ex.open_orders_all = listing
    try:
        rep = mon.tick()
    finally:
        with executor_mod._INFLIGHT_LOCK:
            executor_mod._INFLIGHT.discard(key)
    assert ex.cancelled == [] and rep.orphans == []


def test_history_never_cancels_foreign_full_size_order(tmp_path):
    """Ручной TP Ивана объемом 3 (позиция 2) по другой цене — не наш: не снимается
    при разборе истории; наш прикрепленный SL (объем = позиции, триггер плана) — засчитан."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", qty=2.0, sl=98.0, tp=104.0, opened_at=T0 - timedelta(minutes=30))
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ms = int((T0 - timedelta(minutes=5)).timestamp() * 1000)
    ex.protection = [
        ProtectiveOrder("att-sl", "sl", 98.0, "filled", avg_price=97.9, timestamp_ms=ms, qty=2.0),
        ProtectiveOrder("ivan-tp", "tp", 110.0, "open", qty=3.0),
        ProtectiveOrder("att-tp", "tp", 104.0, "open", qty=2.0),
    ]
    ex.pos = []
    mon.tick()
    mon.tick()
    t = trade(j, "d1")
    assert t["close_reason"] == "sl_hit" and t["exit_price"] == 97.9
    assert "ivan-tp" not in ex.cancelled and "att-tp" in ex.cancelled


def test_fast_lane_runs_with_only_unprotected_open_trade(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "o1")
    assert not mon._has_pending()  # защищенная open — быстрому проходу делать нечего
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='o1'")
    j.commit()
    assert mon._has_pending()  # open без SL — повод для быстрого прохода
