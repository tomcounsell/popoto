"""Never-record firewall — deterministic pre-storage privacy gate (#561).

Runs on the pytest plugin's isolated database (DB 15 by default). The
strongest assertions here sweep the whole keyspace for the secret text rather
than checking the model key alone, because the acceptance criterion is
"never persists in ANY Redis key" and a proxy check would pass while an index
or a BM25 posting still held the string.
"""

import json
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto import Model, NeverRecordMixin, scan_never_record  # noqa: E402
from popoto.exceptions import NeverRecordException  # noqa: E402
from popoto.extraction import ExtractedFact  # noqa: E402
from popoto.fields.constants import Defaults  # noqa: E402
from popoto.fields.shortcuts import (  # noqa: E402
    AutoKeyField,
    FloatField,
    KeyField,
    StringField,
)
from popoto.recipes import DefaultMemory, SubconsciousMemory  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

AGENT = "test-never-record"

# A representative credential for the "nothing persists" sweeps. Distinctive
# enough that a substring sweep cannot collide with unrelated fixture data.
SECRET = "sk-ant-api03-ZZQQWWEERRTTYYUUIIOOPPAASSDDFFGG"


class GatedMemory(NeverRecordMixin, Model):
    """Minimal model carrying the firewall, independent of DefaultMemory."""

    record_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    note = StringField(default="")
    importance = FloatField(default=1.0)


class UngatedMemory(Model):
    """Control model without the mixin — proves the gate is the mixin's doing."""

    record_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")


class RecordingProvider:
    """Extraction provider that records whether it was ever invoked."""

    def __init__(self):
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return [ExtractedFact(text=text)]


def keyspace_contains(needle: str) -> bool:
    """True if ``needle`` appears in any key name or any value in the DB.

    Walks every key and decodes by type, so a match inside a hash field, a
    set member, a sorted-set member, a list element, or a plain string is all
    caught. This is the literal form of "never persists in any Redis key".
    """
    raw = needle.encode("utf-8")
    for key in POPOTO_REDIS_DB.scan_iter(match="*", count=500):
        key_bytes = key if isinstance(key, bytes) else str(key).encode("utf-8")
        if raw in key_bytes:
            return True
        try:
            key_type = POPOTO_REDIS_DB.type(key)
        except Exception:  # pragma: no cover - key vanished mid-scan
            continue
        if isinstance(key_type, bytes):
            key_type = key_type.decode("utf-8")

        if key_type == "string":
            blobs = [POPOTO_REDIS_DB.get(key)]
        elif key_type == "hash":
            mapping = POPOTO_REDIS_DB.hgetall(key) or {}
            blobs = list(mapping.keys()) + list(mapping.values())
        elif key_type == "list":
            blobs = POPOTO_REDIS_DB.lrange(key, 0, -1)
        elif key_type == "set":
            blobs = list(POPOTO_REDIS_DB.smembers(key))
        elif key_type == "zset":
            blobs = list(POPOTO_REDIS_DB.zrange(key, 0, -1))
        else:  # pragma: no cover - stream/other types unused by popoto models
            continue

        for blob in blobs:
            if blob is None:
                continue
            if not isinstance(blob, bytes):
                blob = str(blob).encode("utf-8")
            if raw in blob:
                return True
    return False


# ---------------------------------------------------------------------------
# 1. Nothing persists
# ---------------------------------------------------------------------------


def test_credential_save_leaves_no_trace_anywhere_in_redis():
    assert not keyspace_contains(SECRET), "fixture leak: secret present before test"

    instance = GatedMemory(agent_id=AGENT, content=f"my key is {SECRET}")
    assert instance.save() is False

    assert not keyspace_contains(SECRET)
    assert GatedMemory.query.filter(agent_id=AGENT).count() == 0


def test_ungated_model_is_unaffected():
    """The gate is the mixin's doing, not a global change to every model."""
    assert UngatedMemory(agent_id=AGENT, content=f"key {SECRET}").save() is not False
    assert keyspace_contains(SECRET)


@pytest.mark.parametrize(
    "content",
    [
        f"my key is {SECRET}",
        "off the record: the prod database password rotates on Fridays",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA",
        "AKIAIOSFODNN7EXAMPLE is the access key id",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dBjftJeZ4CVPmB92K27u",
        "export DATABASE_PASSWORD=Tr0ub4dorXKcd9zQ",
        "postgres://admin:s3cretpw@db.internal:5432/app",
        "card on file 4242 4242 4242 4242",
        "his ssn is 123-45-6789",
    ],
)
def test_guaranteed_class_never_saves(content):
    assert GatedMemory(agent_id=AGENT, content=content).save() is False
    assert GatedMemory.query.filter(agent_id=AGENT).count() == 0


def test_secret_in_a_non_content_field_is_also_blocked():
    """The scan is field-agnostic: a secret in a metadata field still counts."""
    instance = GatedMemory(agent_id=AGENT, content="ordinary text", note=SECRET)
    assert instance.save() is False
    assert not keyspace_contains(SECRET)


# ---------------------------------------------------------------------------
# 2. Tombstones are content-free
# ---------------------------------------------------------------------------


def test_every_drop_leaves_a_counted_tombstone():
    GatedMemory(agent_id=AGENT, content=f"key {SECRET}").save()
    GatedMemory(agent_id=AGENT, content="off the record, forget it").save()

    counts = GatedMemory.never_record_counts()
    assert counts.get("credential_prefix") == 1
    assert counts.get("off_the_record") == 1

    log = GatedMemory.never_record_log()
    assert len(log) == 2
    for entry in log:
        assert set(entry) == {"id", "reason", "detector", "at"}


def test_tombstone_contains_no_fragment_of_the_dropped_text():
    secret = "sk-ant-api03-QQWWEERRTTYYUUIIOOPPAASSDD"
    GatedMemory(agent_id=AGENT, content=f"my key is {secret}").save()

    blob = json.dumps(GatedMemory.never_record_log())
    for start in range(len(secret) - 4):
        assert secret[start : start + 5] not in blob


def test_tombstone_id_is_not_derived_from_content():
    """Two drops of identical text must not produce the same id.

    A content-derived id would be a confirmation oracle: anyone holding a
    candidate secret could reproduce the id and check the log for a match.
    """
    for _ in range(2):
        GatedMemory(agent_id=AGENT, content=f"my key is {SECRET}").save()

    log = GatedMemory.never_record_log()
    assert len({entry["id"] for entry in log}) == 2


def test_exception_message_carries_no_content():
    """The message reaches plaintext log files — it must not quote the match."""
    instance = GatedMemory(agent_id=AGENT, content=f"my key is {SECRET}")
    with pytest.raises(NeverRecordException) as excinfo:
        instance._check_never_record()

    message = str(excinfo.value)
    assert SECRET not in message
    for start in range(len(SECRET) - 4):
        assert SECRET[start : start + 5] not in message
    assert "credential_prefix" in message


# ---------------------------------------------------------------------------
# 3. Ordering — the gate runs before the write filter and before pre_save
# ---------------------------------------------------------------------------


def test_gate_runs_before_write_filter_and_before_pre_save():
    """Ordering is the guarantee: neither hook may observe blocked content."""
    from popoto import WriteFilterMixin

    seen = []

    class OrderProbe(NeverRecordMixin, WriteFilterMixin, Model):
        record_id = AutoKeyField()
        agent_id = KeyField()
        content = StringField(default="")

        def compute_filter_score(self):
            seen.append("write_filter")
            return 1.0

        def pre_save(self, **kwargs):
            seen.append("pre_save")
            return super().pre_save(**kwargs)

    assert OrderProbe(agent_id=AGENT, content=f"key {SECRET}").save() is False
    assert seen == []

    assert OrderProbe(agent_id=AGENT, content="ordinary memory").save() is not False
    assert seen == ["write_filter", "pre_save"]


# ---------------------------------------------------------------------------
# 4-5. Off-the-record voids the whole turn, and the provider never sees it
# ---------------------------------------------------------------------------


def test_off_the_record_voids_the_entire_turn():
    provider = RecordingProvider()
    memory = SubconsciousMemory(
        model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
    )

    turn = (
        "Off the record. The deploy runs blue-green. Rollback is automatic. "
        "The team standup is at 10am."
    )
    assert memory.extract_memories(turn) == []
    assert GatedMemory.query.filter(agent_id=AGENT).count() == 0
    assert memory.last_extraction_privacy_dropped is True


def test_extraction_provider_never_sees_off_the_record_content():
    """On the Claude provider path this means the text never leaves the box."""
    provider = RecordingProvider()
    memory = SubconsciousMemory(
        model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
    )

    memory.extract_memories("Off the record: the root password is Xk9mQ2wL7p")
    assert provider.calls == []

    memory.extract_memories("The user prefers dark mode.")
    assert len(provider.calls) == 1


def test_clean_turn_still_saves_and_clears_the_flag():
    provider = RecordingProvider()
    memory = SubconsciousMemory(
        model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
    )

    assert memory.extract_memories("Off the record, ignore this") == []
    assert memory.last_extraction_privacy_dropped is True

    assert len(memory.extract_memories("The user prefers dark mode.")) == 1
    assert memory.last_extraction_privacy_dropped is False


def test_credential_in_a_turn_is_caught_before_the_provider_runs():
    """The turn-level gate covers the whole guaranteed class, not just markers.

    So a pasted credential is dropped before the extractor sees it — on the
    ClaudeExtractionProvider path, before it would be sent to the API.
    """
    provider = RecordingProvider()
    memory = SubconsciousMemory(
        model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
    )

    assert memory.extract_memories(f"the api key is {SECRET}") == []
    assert provider.calls == []
    assert memory.last_extraction_privacy_dropped is True


def test_all_facts_dropped_at_save_level_sets_the_flag():
    """The save-level path also marks the turn as a privacy drop.

    Reached when the extractor *produces* blocked content from an innocuous
    turn — the realistic case being an LLM provider that synthesizes a fact
    quoting a credential the raw turn did not contain in scannable form.
    """

    class SynthesizingProvider:
        def __init__(self):
            self.calls = []

        def extract(self, text):
            self.calls.append(text)
            return [ExtractedFact(text=f"the key is {SECRET}")]

    provider = SynthesizingProvider()
    memory = SubconsciousMemory(
        model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
    )

    assert memory.extract_memories("The user set up their credentials today.") == []
    assert provider.calls, "provider should run: the raw turn is clean"
    assert memory.last_extraction_privacy_dropped is True
    assert not keyspace_contains(SECRET)


def test_drop_does_not_reach_the_broad_exception_handler(caplog):
    """The gate signals via SkipSaveException, so nothing is logged as failure.

    ``SubconsciousMemory.extract_memories`` wraps ``save()`` in a broad
    ``except Exception`` that downgrades everything to a log warning. A drop
    must not land there — issue #561 names this as a pre-requisite hazard.
    """
    provider = RecordingProvider()
    memory = SubconsciousMemory(
        model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
    )

    with caplog.at_level("WARNING"):
        assert memory.extract_memories(f"the api key is {SECRET}") == []

    assert "Failed to save extracted memory" not in caplog.text


# ---------------------------------------------------------------------------
# 6. Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_restores_previous_behavior():
    original = Defaults.NEVER_RECORD_ENABLED
    Defaults.NEVER_RECORD_ENABLED = False
    try:
        instance = GatedMemory(agent_id=AGENT, content=f"key {SECRET}")
        assert instance.save() is not False
        assert keyspace_contains(SECRET)
    finally:
        Defaults.NEVER_RECORD_ENABLED = original


def test_kill_switch_also_disables_the_turn_level_gate():
    original = Defaults.NEVER_RECORD_ENABLED
    Defaults.NEVER_RECORD_ENABLED = False
    try:
        provider = RecordingProvider()
        memory = SubconsciousMemory(
            model_class=GatedMemory, agent_id=AGENT, extraction_provider=provider
        )
        assert len(memory.extract_memories("Off the record: keep this anyway")) == 1
    finally:
        Defaults.NEVER_RECORD_ENABLED = original


# ---------------------------------------------------------------------------
# 7. Adversarial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "sk-ant-api03-ZZQQWWEERRTTYYUUIIOOPP AASSDDFFGG",
        "sk-ant-\napi03-ZZQQWWEERRTTYYUUIIOOPPAASSDDFFGG",
        "sk-ant-api03-ZZQQWWEERR\n  TTYYUUIIOOPPAASSDDFFGG",
        "AKIA\nIOSFODNN7EXAMPLE",
    ],
)
def test_credential_split_across_whitespace_is_still_caught(content):
    """The de-whitespaced rendering defeats split-token evasion."""
    assert scan_never_record(content).blocked
    assert GatedMemory(agent_id=AGENT, content=content).save() is False


def test_high_entropy_token_without_a_known_prefix_is_caught():
    verdict = scan_never_record("the value is Zq7Z1kXpLm4TvB8NwR2yHc6JdFgA9sQe")
    assert verdict.blocked
    assert verdict.reason == "high_entropy"


def test_luhn_invalid_digit_run_is_not_a_payment_card():
    """The card detector is Luhn-validated, not "any 16 digits"."""
    assert not scan_never_record("order number 4242424242424241 shipped").blocked
    assert scan_never_record("card 4242424242424242").blocked


def test_card_split_across_newlines_is_caught():
    assert scan_never_record("card\n4242\n4242\n4242\n4242").blocked


def test_documentation_placeholders_are_not_treated_as_secrets():
    """An over-eager gate that eats the quickstart trains adopters to kill it."""
    for text in (
        "set password=<your password here>",
        "api_key = YOUR_API_KEY",
        "export TOKEN=${GITHUB_TOKEN}",
        "password: ********",
    ):
        assert not scan_never_record(text).blocked, text


# ---------------------------------------------------------------------------
# 8. No false positives on ordinary content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "The user prefers dark mode and lives in Berlin.",
        "Deploy uses blue-green with automatic rollback.",
        "The fix landed in 3b21c7cbd9a1f4e2a7c8d5b6e9f0a1b2c3d4e5f6.",
        "Short sha 3b21c7c is on main.",
        "See 550e8400-e29b-41d4-a716-446655440000 for the trace.",
        "Set POPOTO_TEST_DB=15 before importing popoto in a repro script.",
        "Migration 19 in the cookbook covers the datetime KeyField change.",
        "Standup moved to 10am on Tuesdays and Thursdays.",
        "Redis DB 0 is the live agent store; never flush it.",
    ],
)
def test_ordinary_content_is_not_blocked(content):
    assert not scan_never_record(content).blocked
    assert GatedMemory(agent_id=AGENT, content=content).save() is not False


def test_generated_key_never_trips_a_detector():
    """AutoKeyField values are UUID-shaped — the entropy detector's target.

    Key fields are excluded from the scan so a model cannot block on its own
    identity. Saving many records exercises many generated keys.
    """
    for index in range(25):
        instance = GatedMemory(agent_id=AGENT, content=f"memory number {index}")
        assert instance.save() is not False
    assert GatedMemory.query.filter(agent_id=AGENT).count() == 25


def test_default_memory_carries_the_firewall():
    """The batteries-included model and the harness path are gated by default."""
    assert issubclass(DefaultMemory, NeverRecordMixin)
    assert DefaultMemory(agent_id=AGENT, content=f"key {SECRET}").save() is False
    assert DefaultMemory.never_record_counts().get("credential_prefix") == 1


def test_scan_tolerates_non_string_and_empty_input():
    for value in (None, 42, b"bytes", "", "   "):
        assert not scan_never_record(value).blocked
