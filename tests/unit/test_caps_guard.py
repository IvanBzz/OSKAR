"""Страж капсов: пин в конституции соответствует файлу; нарушение ловится."""

from pathlib import Path

from oskar.ops.caps_guard import caps_hash, pinned_hash, verify_caps


def test_caps_pin_is_valid():
    """Главный тест ст. 4: caps.yaml на диске = пин в CONSTITUTION.md.
    Падение = кто-то тронул капсы в обход процедуры поправки."""
    assert verify_caps(), (
        f"caps.yaml hash {caps_hash()} != constitution pin {pinned_hash()}. "
        "Amendment procedure (CONSTITUTION.md Art. 4) required."
    )


def test_tamper_detected(tmp_path: Path):
    tampered = tmp_path / "caps.yaml"
    tampered.write_text("max_leverage: 100\n", encoding="utf-8")
    assert caps_hash(tampered) != pinned_hash()


def test_crlf_normalization(tmp_path: Path):
    """git autocrlf не должен ломать пин: CRLF и LF версии дают один хеш."""
    lf = tmp_path / "lf.yaml"
    crlf = tmp_path / "crlf.yaml"
    lf.write_bytes(b"a: 1\nb: 2\n")
    crlf.write_bytes(b"a: 1\r\nb: 2\r\n")
    assert caps_hash(lf) == caps_hash(crlf)
