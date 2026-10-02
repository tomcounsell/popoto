"""Postgres implementation of the storage backend seam (#631) -- WS0 stub.

Every method raises :class:`NotImplementedError` naming itself. WS3 fills them
in family by family (records/increment/purge, sorted/set/map, swaps, decay,
supersede), each behind the WS2 conformance harness.

``psycopg`` is deliberately **not** imported at module scope: selection in
:func:`popoto.backends.get_backend` must be able to import this module to test
the stub, and importing ``popoto`` must never dial Postgres. The driver import
belongs inside the connection-opening code WS3 adds.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterator, Literal, Mapping, Sequence

from . import UnitOfWork

__all__ = ["PostgresBackend"]


def _todo(name: str) -> NotImplementedError:
    return NotImplementedError(
        f"PostgresBackend.{name} is not implemented in the backend-seam POC (WS0 "
        "stub; see docs/plans/sdlc-631.md WS3)"
    )


class PostgresBackend:
    """WS0 stub: holds the URL it was selected with, implements nothing.

    May hold a pool later (there is no rebind protocol to honour for
    Postgres); ``set_backend(None)`` resets the cache and the WS2 fixture uses
    it per session.
    """

    def __init__(self, url: str) -> None:
        self.url = url

    # -- A. Unit of work ---------------------------------------------------

    def begin(self) -> UnitOfWork:
        raise _todo("begin")

    def native(self) -> Any:
        raise NotImplementedError(
            "native() is Redis-only in the backend-seam POC: the out-of-scope "
            "mixin code it serves has no Postgres path"
        )

    # -- B. Records ---------------------------------------------------------

    def save_record(
        self,
        key: str,
        fields: Mapping[Any, bytes],
        *,
        class_set: str | None = None,
        obsolete_key: str | None = None,
        ttl: int | None = None,
        expire_at: float | None = None,
        numeric: Mapping[str, float] | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("save_record")

    def set_expiry(
        self,
        key: str,
        *,
        ttl: int | None = None,
        expire_at: float | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("set_expiry")

    def load_record(self, key: str) -> dict[Any, bytes] | None:
        raise _todo("load_record")

    def load_records(self, keys: Sequence[str]) -> list[dict[Any, bytes] | None]:
        raise _todo("load_records")

    def load_fields(self, key: str, names: Sequence[str]) -> list[bytes | None]:
        raise _todo("load_fields")

    def record_exists(self, key: str) -> bool:
        raise _todo("record_exists")

    def records_exist(self, keys: Sequence[str]) -> list[bool]:
        raise _todo("records_exist")

    def delete_record(
        self, key: str, *, class_set: str, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("delete_record")

    def list_keys(self, class_set: str) -> set[str]:
        raise _todo("list_keys")

    def count_records(self, class_set: str) -> int:
        raise _todo("count_records")

    # -- C. Atomic increment -------------------------------------------------

    def increment_field(
        self,
        key: str,
        field: str,
        delta: int | float | Decimal,
        *,
        kind: Literal["int", "float", "decimal"],
        uow: UnitOfWork | None = None,
    ) -> int | float | Decimal | None:
        raise _todo("increment_field")

    # -- D. Side maps --------------------------------------------------------

    def map_get(self, idx: str, member: str) -> bytes | None:
        raise _todo("map_get")

    def map_set(
        self,
        idx: str,
        member: str,
        value: bytes,
        *,
        only_if_absent: bool = False,
        uow: UnitOfWork | None = None,
    ) -> bool | None:
        raise _todo("map_set")

    def map_delete(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> int | None:
        raise _todo("map_delete")

    def map_scan(
        self, idx: str, pattern: str = "*", count: int = 100
    ) -> dict[str, bytes]:
        raise _todo("map_scan")

    # -- E. Set indexes ------------------------------------------------------

    def index_add(self, idx: str, member: str, *, uow: UnitOfWork | None = None) -> Any:
        raise _todo("index_add")

    def index_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("index_remove")

    def index_members(self, idx: str) -> set[str]:
        raise _todo("index_members")

    def index_union(self, idxs: Sequence[str]) -> set[str]:
        raise _todo("index_union")

    def index_intersection(self, idxs: Sequence[str]) -> set[str]:
        raise _todo("index_intersection")

    def scan_index_names(self, pattern: str) -> list[str]:
        raise _todo("scan_index_names")

    def scan_record_keys(self, pattern: str) -> list[str]:
        raise _todo("scan_record_keys")

    # -- F. Sorted indexes ---------------------------------------------------

    def sorted_add(
        self, idx: str, member: str, score: float, *, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("sorted_add")

    def sorted_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        raise _todo("sorted_remove")

    def sorted_score(self, idx: str, member: str) -> float | None:
        raise _todo("sorted_score")

    def sorted_count(self, idx: str) -> int:
        raise _todo("sorted_count")

    def sorted_members(
        self, idx: str, start: int = 0, stop: int = -1, *, reverse: bool = False
    ) -> list[str]:
        raise _todo("sorted_members")

    def sorted_range(
        self,
        idx: str,
        lo: float,
        hi: float,
        *,
        lo_inclusive: bool = True,
        hi_inclusive: bool = True,
        reverse: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        raise _todo("sorted_range")

    def sorted_increment(
        self, idx: str, member: str, delta: float, *, uow: UnitOfWork | None = None
    ) -> float | None:
        raise _todo("sorted_increment")

    # -- G. Atomic swaps -----------------------------------------------------

    def swap_index(
        self,
        record_key: str,
        field: str,
        new_idx: str,
        value: bytes,
        *,
        unique: bool,
        legacy_old_idx: str = "",
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("swap_index")

    def drop_index_entry(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idx: str,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_index_entry")

    def swap_tags(
        self,
        record_key: str,
        field: str,
        new_idxs: Sequence[str],
        value: bytes,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("swap_tags")

    def drop_tag_entries(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idxs: Sequence[str],
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_tag_entries")

    # -- H. Decay and confidence ---------------------------------------------

    def decayed_rank(
        self,
        idx: str,
        *,
        now: float,
        decay_rate: float,
        limit: int | None,
        base_score_field: str = "",
        confidence: tuple[str, float, float] | None = None,
        validity: tuple[str, str, float] | None = None,
        pretrim_max_ratio: float,
    ) -> list[Any]:
        raise _todo("decayed_rank")

    def confidence_update(
        self,
        idx: str,
        member: str,
        signal: float,
        *,
        initial: float,
        cap: int,
        require_record: str | None = None,
        uow: UnitOfWork | None = None,
    ) -> tuple[float, int, int, int] | None:
        raise _todo("confidence_update")

    # -- I. Validity ---------------------------------------------------------

    def supersede(
        self,
        model_prefix: str,
        field: str,
        *,
        mode: str,
        new_member: str,
        old_member: str = "",
        now: float,
        valid_from: float | None,
        ingested_at: float | None,
        close_at: float | None,
        assert_valid_from: bool,
        pointer_digest: str | None,
        uow: UnitOfWork | None = None,
    ) -> str | None:
        raise _todo("supersede")

    def interval_of(
        self, valid_idx: str, invalid_idx: str, member: str
    ) -> tuple[float | None, float | None]:
        raise _todo("interval_of")

    def interval_members(
        self,
        valid_idx: str,
        invalid_idx: str,
        as_of: float,
        *,
        select: Literal["valid", "excluded"],
    ) -> set[str]:
        raise _todo("interval_members")

    def drop_validity(
        self,
        model_prefix: str,
        field: str,
        member: str,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_validity")

    def open_pointer(self, model_prefix: str, field: str, digest: str) -> str | None:
        raise _todo("open_pointer")

    # -- J. Orphan purge and maintenance ------------------------------------

    def purge_orphan(
        self,
        record_key: str,
        refs: Sequence[tuple[str, Literal["sorted", "set"]]],
        *,
        uow: UnitOfWork | None = None,
    ) -> int | None:
        raise _todo("purge_orphan")

    def scan_index_members(
        self, idx: str, kind: Literal["sorted", "set"]
    ) -> Iterator[str]:
        raise _todo("scan_index_members")

    def drop_index(
        self,
        idx: str,
        kind: Literal["sorted", "set", "map"],
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        raise _todo("drop_index")
