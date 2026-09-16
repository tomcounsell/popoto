"""Key-regenerating import (``preserve_keys=False``) -- issue #557.

Every test runs against live Redis (DB 15 by default, isolated and flushed by
the popoto pytest plugin). No mocks.

What is covered here:

- Keys are minted, the originals are left untouched, and ``report.key_map``
  reports old -> new for every record.
- ``Relationship`` values are rewritten through the declared
  ``remap_references`` hook: self-reference, a circular A<->B pair, ``None``,
  and a target the map does not cover (which dangles and is counted).
- The remap is *partial by design*: a key-shaped string stored in a plain
  ``Field`` is never rewritten.
- The eligibility predicate is two conditions. A model keyed by a plain
  ``KeyField`` is refused, and so is one that mixes a ``KeyField`` with an
  ``AutoKeyField`` -- ``_get_auto_key_field_name()`` returns a name for the
  second, which a single-condition check would accept.
- All three input shapes, including a **real opened file** and a
  non-seekable stream. ``io.StringIO`` alone cannot cover this: CPython
  disables ``tell()`` on a ``TextIOWrapper`` once ``__next__`` has driven it,
  so an implementation that seeks the caller's stream passes every
  ``StringIO`` test and fails on the first real file.
- ``preserve_keys=True`` is unchanged, and ``key_map`` without it is refused.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

import popoto  # noqa: E402
from popoto.exceptions import ModelException  # noqa: E402
from popoto.fields.supersession import SupersessionProtocol  # noqa: E402
from popoto.fields.validity_field import ValidityField  # noqa: E402
from popoto.transfer import export_records, import_records  # noqa: E402

# --- Test models -------------------------------------------------------


class RegenNode(popoto.Model):
    """Implicit ``_auto_key``, a self-relationship, and a decoy pointer.

    ``pointer`` holds a key-shaped string in a plain ``Field``. It is the
    control for the partial-remap guarantee: it must come back byte-identical.
    """

    label = popoto.Field(type=str)
    pointer = popoto.Field(type=str, null=True)


# Registered after the class body so the relationship can name its own model.
RegenNode.peer = popoto.Relationship(model=RegenNode, null=True)
RegenNode._meta.add_field("peer", RegenNode.peer)


class RegenAuthor(popoto.Model):
    name = popoto.Field(type=str)


class RegenBook(popoto.Model):
    """Second model in a two-model migration, for ``key_map`` chaining."""

    title = popoto.Field(type=str)
    author = popoto.Relationship(model=RegenAuthor, null=True)


class RegenSlug(popoto.Model):
    """Keyed by a plain ``KeyField`` -- nothing to mint, so it is refused."""

    slug = popoto.KeyField()
    body = popoto.Field(type=str, null=True)


class RegenMixed(popoto.Model):
    """``KeyField`` + ``AutoKeyField``.

    ``_get_auto_key_field_name()`` returns ``"ident"`` here while
    ``key_field_names`` is ``{"slug", "ident"}``: minting ``ident`` alone
    would not produce the record's key. The second condition is what catches
    it.
    """

    slug = popoto.KeyField()
    ident = popoto.AutoKeyField()


class NonSeekableStream:
    """Iterable text stream with no ``seek``/``tell`` at all.

    A pipe read through ``sys.stdin`` behaves this way. The import must never
    reach for either method on the caller's stream.
    """

    def __init__(self, text: str) -> None:
        self._lines = iter(text.splitlines(keepends=True))

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)


# --- Helpers -----------------------------------------------------------


def _export_text(model_class) -> str:
    buffer = io.StringIO()
    export_records(model_class, stream=buffer)
    return buffer.getvalue()


def _reference(instance, field_name: str):
    """Read a relationship as a redis_key string, however it resolved."""
    value = getattr(instance, field_name)
    if value is None or isinstance(value, str):
        return value
    return value.db_key.redis_key


def _by_label(model_class, label: str):
    return [o for o in model_class.query.all() if o.label == label]


# --- Minting and the key map -------------------------------------------


def test_keys_are_minted_and_originals_untouched():
    a = RegenNode.create(label="a", pointer="RegenNode:not-a-real-key")
    original_key = a.db_key.redis_key
    text = _export_text(RegenNode)

    report = import_records(RegenNode, io.StringIO(text), preserve_keys=False)

    assert report.count("landed") == 1
    assert list(report.key_map) == [original_key]
    new_key = report.key_map[original_key]
    assert new_key != original_key
    # Both copies exist: the mode duplicates, it does not move.
    assert len(list(RegenNode.query.all())) == 2
    assert RegenNode.query.get(redis_key=original_key) is not None
    assert RegenNode.query.get(redis_key=new_key) is not None


def test_regeneration_is_not_idempotent():
    RegenNode.create(label="a")
    text = _export_text(RegenNode)

    import_records(RegenNode, io.StringIO(text), preserve_keys=False)
    import_records(RegenNode, io.StringIO(text), preserve_keys=False)

    # One original plus one copy per run. Convergence is the *preserving*
    # mode's promise, not this one's.
    assert len(list(RegenNode.query.all())) == 3


def test_plain_field_pointer_is_never_rewritten():
    a = RegenNode.create(label="a")
    decoy = a.db_key.redis_key
    RegenNode.create(label="b", pointer=decoy)
    text = _export_text(RegenNode)

    report = import_records(RegenNode, io.StringIO(text), preserve_keys=False)

    copies = [
        o
        for o in RegenNode.query.all()
        if o.db_key.redis_key in report.key_map.values()
    ]
    imported_b = [o for o in copies if o.label == "b"]
    assert len(imported_b) == 1
    # Key-shaped, remapped for *declared* references, and still the old value
    # here -- a plain Field is indistinguishable from ordinary text.
    assert imported_b[0].pointer == decoy


# --- Reference remapping -----------------------------------------------


def test_self_reference_is_remapped():
    a = RegenNode.create(label="a")
    a.peer = a
    a.save()
    text = _export_text(RegenNode)

    report = import_records(RegenNode, io.StringIO(text), preserve_keys=False)

    new_key = report.key_map[a.db_key.redis_key]
    imported = RegenNode.query.get(redis_key=new_key)
    assert _reference(imported, "peer") == new_key
    # The original still points at itself.
    assert _reference(RegenNode.query.get(redis_key=a.db_key.redis_key), "peer") == (
        a.db_key.redis_key
    )


def test_circular_pair_is_remapped_in_both_directions():
    a = RegenNode.create(label="a")
    b = RegenNode.create(label="b")
    a.peer = b
    a.save()
    b.peer = a
    b.save()
    text = _export_text(RegenNode)

    report = import_records(RegenNode, io.StringIO(text), preserve_keys=False)

    new_a = report.key_map[a.db_key.redis_key]
    new_b = report.key_map[b.db_key.redis_key]
    # The first record written points at a key that does not exist yet.
    # Model.__init__ resolves relationship strings eagerly and substitutes
    # None on a miss, so this assertion is the one that catches a regression
    # in the post-construction re-assert.
    assert _reference(RegenNode.query.get(redis_key=new_a), "peer") == new_b
    assert _reference(RegenNode.query.get(redis_key=new_b), "peer") == new_a


def test_none_relationship_passes_through():
    a = RegenNode.create(label="a")
    assert a.peer is None
    text = _export_text(RegenNode)

    report = import_records(RegenNode, io.StringIO(text), preserve_keys=False)

    new_key = report.key_map[a.db_key.redis_key]
    assert _reference(RegenNode.query.get(redis_key=new_key), "peer") is None


def test_unmapped_target_dangles_and_is_counted():
    author = RegenAuthor.create(name="ann")
    book = RegenBook.create(title="t", author=author)
    text = _export_text(RegenBook)

    # The author is not part of this run and no key_map was seeded, so the
    # reference cannot be remapped.
    report = import_records(RegenBook, io.StringIO(text), preserve_keys=False)

    new_key = report.key_map[book.db_key.redis_key]
    imported = RegenBook.query.get(redis_key=new_key)
    assert _reference(imported, "author") == author.db_key.redis_key
    warning = "\n".join(report.warnings)
    assert "1 key(s) minted" in warning
    assert "0 reference value(s) remapped" in warning
    assert "1 reference value(s) left pointing at keys absent" in warning


def test_key_map_chains_across_two_models():
    author = RegenAuthor.create(name="ann")
    book = RegenBook.create(title="t", author=author)
    author_text = _export_text(RegenAuthor)
    book_text = _export_text(RegenBook)

    author_report = import_records(
        RegenAuthor, io.StringIO(author_text), preserve_keys=False
    )
    book_report = import_records(
        RegenBook,
        io.StringIO(book_text),
        preserve_keys=False,
        key_map=author_report.key_map,
    )

    new_author = author_report.key_map[author.db_key.redis_key]
    new_book = book_report.key_map[book.db_key.redis_key]
    assert _reference(RegenBook.query.get(redis_key=new_book), "author") == new_author
    # The seeded entries are carried into the second report, so a third model
    # can be chained off it.
    assert book_report.key_map[author.db_key.redis_key] == new_author


# --- Eligibility predicate ---------------------------------------------


def test_plain_key_field_model_is_refused():
    RegenSlug.create(slug="s1", body="x")
    text = _export_text(RegenSlug)

    with pytest.raises(ModelException) as excinfo:
        import_records(RegenSlug, io.StringIO(text), preserve_keys=False)

    message = str(excinfo.value)
    assert "RegenSlug" in message
    assert "slug" in message
    # Refused before any write.
    assert len(list(RegenSlug.query.all())) == 1


def test_mixed_key_and_auto_key_model_is_refused():
    # A single-condition check on _get_auto_key_field_name() accepts this
    # model and mints a key that is only half of the record's real key.
    assert RegenMixed._get_auto_key_field_name() == "ident"
    assert set(RegenMixed._meta.key_field_names) == {"slug", "ident"}

    RegenMixed.create(slug="s1")
    text = _export_text(RegenMixed)

    with pytest.raises(ModelException) as excinfo:
        import_records(RegenMixed, io.StringIO(text), preserve_keys=False)

    assert "RegenMixed" in str(excinfo.value)
    assert len(list(RegenMixed.query.all())) == 1


# --- Input shapes ------------------------------------------------------


def test_regeneration_from_a_real_opened_file(tmp_path):
    a = RegenNode.create(label="a")
    a.peer = a
    a.save()
    path = tmp_path / "nodes.jsonl"
    path.write_text(_export_text(RegenNode), encoding="utf-8")

    with open(path, "r", encoding="utf-8") as handle:
        report = import_records(RegenNode, handle, preserve_keys=False)

    new_key = report.key_map[a.db_key.redis_key]
    assert _reference(RegenNode.query.get(redis_key=new_key), "peer") == new_key


def test_regeneration_from_a_non_seekable_stream():
    a = RegenNode.create(label="a")
    a.peer = a
    a.save()
    text = _export_text(RegenNode)

    report = import_records(RegenNode, NonSeekableStream(text), preserve_keys=False)

    new_key = report.key_map[a.db_key.redis_key]
    assert _reference(RegenNode.query.get(redis_key=new_key), "peer") == new_key


def test_real_file_tell_is_disabled_after_iteration():
    """Why the non-seekable arm above is not redundant with StringIO.

    This is a property of CPython's io layer, asserted here so a future
    implementation that reaches for ``seek()``/``tell()`` on the caller's
    stream fails with an explanation rather than a bare OSError in an
    unrelated test.
    """
    with tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", suffix=".jsonl", delete=False
    ) as handle:
        handle.write('{"a": 1}\n{"a": 2}\n')
        name = handle.name
    try:
        with open(name, "r", encoding="utf-8") as stream:
            assert stream.seekable()
            next(iter(stream))
            with pytest.raises(OSError):
                stream.tell()
    finally:
        os.unlink(name)

    # io.StringIO is immune, which is why it cannot stand in for the arm above.
    buffer = io.StringIO('{"a": 1}\n{"a": 2}\n')
    next(iter(buffer))
    assert buffer.tell() > 0


# --- The preserving path is unchanged ----------------------------------


def test_preserve_keys_default_is_unchanged():
    a = RegenNode.create(label="a")
    a.peer = a
    a.save()
    original_key = a.db_key.redis_key
    text = _export_text(RegenNode)

    report = import_records(RegenNode, io.StringIO(text), on_conflict="overwrite")

    assert report.count("landed") == 1
    assert report.key_map == {}
    assert not any("key regeneration" in w for w in report.warnings)
    assert len(list(RegenNode.query.all())) == 1
    assert _reference(RegenNode.query.get(redis_key=original_key), "peer") == (
        original_key
    )


def test_key_map_without_regeneration_is_refused():
    RegenNode.create(label="a")
    text = _export_text(RegenNode)

    with pytest.raises(ValueError) as excinfo:
        import_records(RegenNode, io.StringIO(text), key_map={"a": "b"})

    assert "preserve_keys" in str(excinfo.value)


def test_file_supplied_remap_slot_is_ignored():
    """The in-memory stash name carries no authority when read off a file."""
    a = RegenNode.create(label="a")
    text = _export_text(RegenNode)
    lines = text.splitlines()
    record = json.loads(lines[1])
    record["__remapped_references__"] = {"peer": "RegenNode:injected"}
    tampered = "\n".join([lines[0], json.dumps(record)]) + "\n"

    report = import_records(RegenNode, io.StringIO(tampered), on_conflict="overwrite")

    assert report.count("landed") == 1
    assert _reference(RegenNode.query.get(redis_key=a.db_key.redis_key), "peer") is (
        None
    )


# --- Model method and CLI surfaces -------------------------------------


def test_model_method_forwards_both_arguments():
    a = RegenNode.create(label="a")
    a.peer = a
    a.save()
    text = _export_text(RegenNode)

    report = RegenNode.import_records(io.StringIO(text), preserve_keys=False)

    new_key = report.key_map[a.db_key.redis_key]
    assert _reference(RegenNode.query.get(redis_key=new_key), "peer") == new_key


def test_cli_exposes_regenerate_keys_flag():
    from popoto.transfer.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "import",
            "--model",
            "tests.test_transfer_key_regeneration:RegenNode",
            "--in",
            "x.jsonl",
            "--regenerate-keys",
        ]
    )
    assert args.regenerate_keys is True

    default_args = parser.parse_args(
        [
            "import",
            "--model",
            "tests.test_transfer_key_regeneration:RegenNode",
            "--in",
            "x.jsonl",
        ]
    )
    assert default_args.regenerate_keys is False


# --- Carried state is NOT remapped (pinned limitation) ------------------


class RegenFact(popoto.Model):
    """Implicit ``_auto_key`` plus a ``ValidityField``.

    ``ValidityField`` is ``roundtrip_policy = "carry"``: its companion hashes
    travel in the record's ``state``, and two of those values -- ``chain_fwd``
    and ``chain_rev`` -- are **full redis_keys of other records**.
    """

    name = popoto.Field(type=str)
    validity = ValidityField()


def test_carried_state_keys_are_not_remapped():
    """Pins a documented limitation, deliberately, rather than hiding it.

    ``remap_references`` is applied to ``record["values"]`` only. Carried
    state travels through ``export_state``/``import_state``, which the remap
    never sees, so a ``ValidityField`` supersession chain imported under
    ``preserve_keys=False`` still names the *source* records. Closing this
    needs a per-field state-remap protocol, and it cannot reuse one map for
    every field: ``chain_fwd``/``chain_rev`` hold full redis_keys while
    ``CoOccurrenceField``'s ``target_pk`` holds a bare pk. Out of scope for
    #557; see the plan's No-Gos.
    """
    identity = SupersessionProtocol.identity_key("subject", "predicate")
    old = RegenFact.create(name="old")
    SupersessionProtocol.supersede(old, identity_key=identity)
    new = RegenFact.create(name="new")
    SupersessionProtocol.supersede(new, identity_key=identity)

    text = _export_text(RegenFact)
    old_key = old.db_key.redis_key
    exported = [json.loads(line) for line in text.splitlines()[1:]]
    chained = [
        record
        for record in exported
        if (record.get("state") or {}).get("validity", {}).get("chain_fwd")
    ]
    assert chained, "expected the superseded record to carry a forward chain link"

    report = import_records(RegenFact, io.StringIO(text), preserve_keys=False)

    assert report.count("landed") == len(exported)
    # The record's own key was regenerated...
    assert old_key in report.key_map
    # ...but the chain link it carries still points into the source database.
    chain_fwd = ValidityField.get_chain_fwd_key(RegenFact, "validity")
    source_new_key = new.db_key.redis_key
    imported_old_key = report.key_map[old_key]
    link = popoto.get_redis().hget(chain_fwd, imported_old_key)
    assert link is not None
    if isinstance(link, bytes):
        link = link.decode()
    assert link == source_new_key
