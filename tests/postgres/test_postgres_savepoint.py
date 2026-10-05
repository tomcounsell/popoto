"""``[PG-only]`` a refused save inside a caller-owned ``transaction()`` rolls
back only its own writes (#759 M3).

A save with a declared ``valid_from`` that disagrees with the stored start is
refused *after* its search CTEs (postings, document length, vector, filters)
ran in the same statement. Outside a ``transaction()`` the save owns its
transaction and the refusal rolls everything back. Inside a caller's, the
refusal used to leave the CTE writes in the caller's transaction: a caller
that caught it and committed kept the postings while the record's validity
write was refused. The save now runs in a SAVEPOINT there.
"""

import numpy as np
import pytest

import popoto
from popoto import ValidityField, ValidityValidFromConflictError
from popoto.backends import get_backend
from popoto.embeddings import AbstractEmbeddingProvider
from popoto.fields.bm25_field import BM25Field
from popoto.fields.embedding_field import EmbeddingField

psycopg = pytest.importorskip("psycopg")


class _Hash(AbstractEmbeddingProvider):
    def embed(self, texts, input_type=None):
        out = []
        for text in texts:
            rng = np.random.RandomState(sum(map(ord, text)) % (2**31))
            out.append(rng.randn(8).tolist())
        return out

    @property
    def dimensions(self):
        return 8

    @property
    def max_batch_size(self):
        return 32


class SpDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    text = popoto.StringField(default="")
    content = BM25Field(source="text")
    embedding = EmbeddingField(source="text", provider=_Hash())
    validity = ValidityField()


T0 = 1_000_000.0


def _rows(admin, schema, pk):
    """Every side-table row of one record, as comparable values."""
    t = f'"{schema}".sp_doc'

    def q(sql):
        return admin.execute(sql, (pk,)).fetchall()

    return {
        "post": q(
            f"SELECT scope, term, tf FROM {t}__content__post WHERE _pk=%s ORDER BY 1,2"
        ),
        "dl": q(f"SELECT len FROM {t}__content__dl WHERE _pk=%s"),
        "vec": q(f"SELECT v::text FROM {t}__embedding__vec WHERE _pk=%s"),
        "main": q(f"SELECT text, validity__valid_from FROM {t} " "WHERE _pk=%s"),
    }


@pytest.mark.parametrize("caller_transaction", [True, False])
def test_a_refused_save_rolls_back_only_its_own_writes(
    pg, pg_schema, admin, caller_transaction, monkeypatch
):
    a = SpDoc(name="a", text="alpha quick brown fox", validity=T0)
    b = SpDoc(name="b", text="bravo slow green turtle", validity=T0)
    b.save()
    b_pk = b.db_key.redis_key
    b_before = _rows(admin, pg_schema.name, b_pk)
    assert b_before["post"] and b_before["dl"] and b_before["vec"]

    # The client-side pre-check refuses before any write; the race this guards
    # is a writer landing after it, so take the pre-check out of the way and
    # let the upsert's own guard refuse -- after the search CTEs ran.
    monkeypatch.setattr(ValidityField, "pre_save_validate", lambda *a, **k: None)
    b.text = "zulu entirely different words"
    b.validity = T0 - 500.0  # disagrees with the stored start

    if caller_transaction:
        with get_backend(SpDoc).transaction() as tx:
            a.save(pipeline=tx)
            with pytest.raises(ValidityValidFromConflictError):
                b.save(pipeline=tx)
            # the caller caught the refusal and commits anyway
    else:
        a.save()
        with pytest.raises(ValidityValidFromConflictError):
            b.save()

    a_rows = _rows(admin, pg_schema.name, a.db_key.redis_key)
    assert a_rows["post"] and a_rows["dl"] and a_rows["vec"]
    assert a_rows["main"][0][1] == T0
    # B is exactly what it was before the refused save: no zulu postings, the
    # old document length, the old vector, the old validity start and text.
    b_after = _rows(admin, pg_schema.name, b_pk)
    assert b_after == b_before
    assert "zulu" not in {term for _s, term, _tf in b_after["post"]}


def test_a_refused_save_and_invalidate_rolls_back_its_save_in_a_caller_transaction(
    pg, pg_schema, admin
):
    """The successor's save and the refused close are one unit: when the close
    is refused (the named incumbent is not a member) and the caller catches it
    and commits, the successor's row and postings are not left behind."""
    from popoto import SupersessionProtocol, ValidityMemberAbsentError

    ghost = SpDoc(name="ghost", text="never saved", validity=T0)
    keeper = SpDoc(name="keeper", text="kept record", validity=T0)
    new = SpDoc(name="new", text="successor text", validity=T0 + 10)
    with get_backend(SpDoc).transaction() as tx:
        keeper.save(pipeline=tx)
        with pytest.raises(ValidityMemberAbsentError):
            SupersessionProtocol.save_and_invalidate(new, closes=ghost, pipeline=tx)
    assert _rows(admin, pg_schema.name, keeper.db_key.redis_key)["post"]
    gone = _rows(admin, pg_schema.name, new.db_key.redis_key)
    assert gone == {"post": [], "dl": [], "vec": [], "main": []}
