"""자동 영점의 shared 계약 검사: `ProximityResult.unzeroed`, `AutoZero`, `AutoZeroState`, `AutoZeroStore`
(specs/core-proximity-common-model/spec.md, FR-16, FR-22, #252)."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.interfaces import AutoZeroStore
from nearpilot.shared.models import AutoZero, AutoZeroState, ProximityResult

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _result(posteriors, **kwargs) -> ProximityResult:
    return ProximityResult(posteriors, NOW, NOW, "m1", "7", **kwargs)


def _zero(node_id="a", zero_db=1.5, revision=1) -> AutoZero:
    return AutoZero(node_id, zero_db, "model-a", "1.0.0", revision, NOW)


# ── ProximityResult.unzeroed ────────────────────────────────────────


def test_FR16_result_unzeroed_subset():
    assert _result({"a": 0.5}).unzeroed == frozenset()  # 없으면 모두 자동 영점이 있다
    assert isinstance(_result({"a": 0.5}).unzeroed, frozenset)

    from_list = _result({"a": 0.5, "b": 0.3}, unzeroed=["b", "b"])
    assert from_list.unzeroed == frozenset({"b"}) and isinstance(from_list.unzeroed, frozenset)

    source = {"a"}
    from_set = _result({"a": 0.5, "b": 0.3}, unzeroed=source)
    source.add("b")  # 입력을 나중에 바꿔도 결과는 그대로다
    assert from_set.unzeroed == frozenset({"a"})

    every = _result({"a": 0.5, "b": 0.3}, unzeroed=("a", "b"))  # 전부여도 부분집합이다
    assert every.unzeroed == frozenset({"a", "b"})

    with pytest.raises(ValueError):
        _result({"a": 0.5}, unzeroed=["b"])  # posteriors 에 없는 기기
    with pytest.raises(ValueError):
        _result({"a": 0.5}, unzeroed=["a", "b"])
    with pytest.raises(ValueError):
        _result({}, unzeroed=["a"])


def test_FR16_result_keeps_existing_positional_construction():
    """`unzeroed`는 뒤에 더한 기본값 필드다. 다섯 인자로 만드는 기존 호출을 깨지 않는다."""
    result = ProximityResult({"a": 0.2, "b": 0.1}, NOW, NOW, "m1", "7")
    assert result.best == ("a", 0.2) and result.unzeroed == frozenset()


# ── AutoZero, AutoZeroState ─────────────────────────────────────────


def test_FR22_auto_zero_is_immutable_and_marks_discard():
    kept, discarded = _zero(zero_db=-2.0), _zero(zero_db=None, revision=2)
    assert (kept.zero_db, discarded.zero_db) == (-2.0, None)  # None 이 버림이다
    with pytest.raises(FrozenInstanceError):
        kept.zero_db = 0.0


def test_FR22_auto_zero_state_zeros_are_read_only_copy():
    source = {"a": _zero("a")}
    state = AutoZeroState(revision=1, zeros=source)
    source["b"] = _zero("b", revision=2)  # 입력을 나중에 바꿔도 상태는 그대로다
    assert set(state.zeros) == {"a"}
    with pytest.raises(TypeError):
        state.zeros["b"] = _zero("b")
    with pytest.raises(TypeError):
        state.zeros.clear()
    with pytest.raises(FrozenInstanceError):
        state.revision = 2


def test_FR22_auto_zero_state_rejects_negative_revision():
    assert AutoZeroState(revision=0, zeros={}).revision == 0  # 행이 없는 상태는 개정 0이다
    with pytest.raises(ValueError):
        AutoZeroState(revision=-1, zeros={})


# ── AutoZeroStore ───────────────────────────────────────────────────


def _shape(function) -> list[tuple]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def test_FR22_sqlite_repository_implements_auto_zero_store():
    conn = connect(":memory:")
    try:
        assert isinstance(SqliteRepository(conn), AutoZeroStore)
    finally:
        conn.close()
    # runtime_checkable Protocol 은 메서드 이름만 본다. 시그니처는 따로 맞춘다
    for name in ("auto_zeros", "save_auto_zero"):
        assert _shape(getattr(SqliteRepository, name)) == _shape(getattr(AutoZeroStore, name))
    assert [p for p, *_ in _shape(AutoZeroStore.auto_zeros)] == ["self", "revision"]
    assert [p for p, *_ in _shape(AutoZeroStore.save_auto_zero)] == [
        "self", "node_id", "zero_db", "vendor_model", "vendor_firmware", "recorded_at"]


def test_FR22_auto_zero_store_rejects_object_without_methods():
    class Missing:
        def auto_zeros(self, revision=None):
            return AutoZeroState(revision=0, zeros={})

    assert not isinstance(Missing(), AutoZeroStore)  # save_auto_zero 가 없다
