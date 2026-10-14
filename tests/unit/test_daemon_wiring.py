"""Демон: адрес API и путь журнала из settings.yaml, остановка потоков при выходе
(ревью 2026-10-13: дрейф конфига; stop() без join)."""

import threading

import pytest

from oskar.core.daemon import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    REPO_ROOT,
    api_endpoint,
    journal_path,
    stop_and_join,
)


def test_api_endpoint_from_settings_loopback_only():
    assert api_endpoint({}) == (DEFAULT_HOST, DEFAULT_PORT)
    assert api_endpoint({"api": {"host": "localhost", "port": 9123}}) == ("localhost", 9123)
    with pytest.raises(ValueError, match="loopback"):
        api_endpoint({"api": {"host": "0.0.0.0", "port": 8321}})  # panic/approve в сеть — никогда
    with pytest.raises(ValueError, match="range"):
        api_endpoint({"api": {"port": 70000}})


def test_journal_path_from_settings(tmp_path):
    assert journal_path({}) == REPO_ROOT / "data" / "oskar.db"
    assert journal_path({"journal": {"db_path": "data/alt.db"}}) == REPO_ROOT / "data" / "alt.db"
    absolute = tmp_path / "j.db"
    assert journal_path({"journal": {"db_path": str(absolute)}}) == absolute


class Worker:
    """Как runner/poller/monitor: stop() ставит событие, поток выходит на нем."""

    def __init__(self, name: str, ignores_stop: bool = False):
        self.stop_event = threading.Event()
        self.release = threading.Event()  # «зависший» (long-poll) отпускается в конце теста
        gate = self.release if ignores_stop else self.stop_event
        self.thread = threading.Thread(target=gate.wait, args=(10,), name=name, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()


class BrokenStop:
    def stop(self):
        raise RuntimeError("stop exploded")


def test_stop_and_join_stops_everyone_and_names_the_stuck():
    ok, no_thread, stuck = Worker("w-ok"), Worker("w-nothread"), Worker("w-stuck", ignores_stop=True)
    try:
        left = stop_and_join(
            [(BrokenStop(), None), (ok, ok.thread), (None, None), (no_thread, None), (stuck, stuck.thread)],
            timeout=0.2,
        )
        assert left == ["w-stuck"]
        assert not ok.thread.is_alive()  # join дождался
        assert no_thread.stop_event.is_set() and stuck.stop_event.is_set()  # stop() всем, даже после сбоя
    finally:
        stuck.release.set()
        no_thread.thread.join(1)
