"""
Round-trip and regression tests for DB_key's escaping scheme (issue #525).

DB_key.clean() escapes ":" as the literal seven-character sequence
"{&#58;}". Before this fix, that sequence was not self-escaping: if it
already appeared in the input, clean() passed it through unchanged and
unclean() could not tell the encoder's own escape from data the caller
supplied, silently corrupting the round trip.

This file is pure Python (no Redis calls) so it runs fast, and pins the
round-trip property, backward-compatible decoding of legacy-produced keys,
byte-identical encoding for values without the escape sequence, and the
non-widening behavior of the new single-pass unclean() scanner.

`hypothesis` is not a dependency of this repo (see pyproject.toml `[dev]`),
so property coverage here is exhaustive-over-short-strings plus a seeded
randomized sweep over longer ones, rather than a hypothesis strategy.
"""

import itertools
import random

from src.popoto.models.db_key import DB_key, COLON_ESCAPE, GLOB_CHARS

# ---------------------------------------------------------------------------
# Frozen reference implementations: the pre-fix clean()/unclean(), inlined
# so the legacy-decode-compatibility test has a fixed oracle that cannot
# drift if DB_key.clean()/unclean() change again later.
# ---------------------------------------------------------------------------


def _legacy_clean(value: str) -> str:
    value = value.replace("/", "//")
    for char in "'?*^[]-":
        value = value.replace(char, f"/{char}")
    value = value.replace(":", "{&#58;}")
    return value


def _legacy_unclean(value: str) -> str:
    value = value.replace("{&#58;}", ":")
    for char in "'?*^[]-":
        value = value.replace(f"/{char}", char)
    value = value.replace("//", "/")
    return value


# ---------------------------------------------------------------------------
# Shared alphabet / sample generation, matching spike-2's hostile alphabet.
# `5` and `8` are included deliberately: without them the exhaustive arm
# cannot construct near-miss sequences like "{&#5" adjacent to "8;}".
# ---------------------------------------------------------------------------

ALPHABET = [
    "a",
    "/",
    ":",
    "-",
    "*",
    "[",
    "]",
    "^",
    "?",
    "'",
    "{",
    "}",
    "&",
    "#",
    "5",
    "8",
    ";",
]
TOKENS = ALPHABET + [COLON_ESCAPE]


def _exhaustive_strings(max_length=3):
    for length in range(max_length + 1):
        for combo in itertools.product(TOKENS, repeat=length):
            yield "".join(combo)


def _random_strings(count=50000, max_length=12, seed=0):
    rng = random.Random(seed)
    for _ in range(count):
        length = rng.randint(0, max_length)
        yield "".join(rng.choice(TOKENS) for _ in range(length))


def _sweep():
    """Exhaustive length-0..3 strings plus a seeded randomized sweep."""
    yield from _exhaustive_strings()
    yield from _random_strings()


# ---------------------------------------------------------------------------
# Round-trip property test
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_holds_for_sweep(self):
        failures = [v for v in _sweep() if DB_key.unclean(DB_key.clean(v)) != v]
        assert (
            failures == []
        ), f"{len(failures)} round-trip failures, e.g. {failures[:5]!r}"

    def test_round_trip_empty_string(self):
        assert DB_key.clean("") == ""
        assert DB_key.unclean("") == ""

    def test_issue_525_reproductions(self):
        # Exact reproductions from issue #525 -- these were lossy before the fix.
        for v in ["{&#58;}", "a{&#58;}b", "{&#58;}-/:"]:
            assert DB_key.unclean(DB_key.clean(v)) == v


# ---------------------------------------------------------------------------
# Legacy decode-compatibility: the new unclean() must decode every
# legacy-produced encoding identically to the old unclean().
# ---------------------------------------------------------------------------


class TestLegacyDecodeCompatibility:
    def test_new_unclean_matches_legacy_unclean_on_legacy_encodings(self):
        mismatches = []
        for v in _sweep():
            legacy_encoded = _legacy_clean(v)
            new_decoded = DB_key.unclean(legacy_encoded)
            legacy_decoded = _legacy_unclean(legacy_encoded)
            if new_decoded != legacy_decoded:
                mismatches.append((v, legacy_encoded, new_decoded, legacy_decoded))
        assert (
            mismatches == []
        ), f"{len(mismatches)} legacy-decode divergences, e.g. {mismatches[:5]!r}"


# ---------------------------------------------------------------------------
# Encoding stability: clean() must be byte-identical to the legacy
# implementation for every value that does not contain COLON_ESCAPE, and
# must differ for every value that does (proving the fix actually engages).
# ---------------------------------------------------------------------------


class TestEncodingStability:
    def test_clean_unchanged_when_no_literal_colon_escape(self):
        mismatches = []
        for v in _sweep():
            if COLON_ESCAPE in v:
                continue
            new_encoded = DB_key.clean(v)
            legacy_encoded = _legacy_clean(v)
            if new_encoded != legacy_encoded:
                mismatches.append((v, new_encoded, legacy_encoded))
        assert (
            mismatches == []
        ), f"{len(mismatches)} unexpected encoding changes, e.g. {mismatches[:5]!r}"

    def test_clean_differs_when_literal_colon_escape_present(self):
        # This is a sanity check that the fix actually does something --
        # not every v containing COLON_ESCAPE is guaranteed to differ in
        # the exhaustive+random sweep necessarily, but the explicit issue
        # #525 repro values must.
        for v in ["{&#58;}", "a{&#58;}b", "{&#58;}-/:"]:
            assert DB_key.clean(v) != _legacy_clean(v)

    def test_tag_field_style_value_is_unaffected(self):
        # Mirrors tests/test_tag_field.py's encoding-stability canary:
        # "agent:valor" contains no literal COLON_ESCAPE, so the new
        # pre-escape step never fires and the encoding is byte-identical.
        assert DB_key.clean("agent:valor") == "agent{&#58;}valor"
        assert DB_key.clean("agent:valor") == _legacy_clean("agent:valor")


# ---------------------------------------------------------------------------
# Delimiter safety: clean() must never emit a literal colon, or
# from_redis_key()'s split(":") becomes ambiguous.
# ---------------------------------------------------------------------------


class TestDelimiterSafety:
    def test_clean_never_emits_a_literal_colon(self):
        offenders = [v for v in _sweep() if ":" in DB_key.clean(v)]
        assert (
            offenders == []
        ), f"{len(offenders)} values produced a literal colon, e.g. {offenders[:5]!r}"


# ---------------------------------------------------------------------------
# Composite-key round trip through DB_key / from_redis_key.
# ---------------------------------------------------------------------------


class TestCompositeKeyRoundTrip:
    def test_from_redis_key_recovers_hostile_partials(self):
        hostile_pairs = [
            ("{&#58;}", "a{&#58;}b"),
            ("a:b", "c:d"),
            ("{&#58;}-/:", "/-/-/"),
            ("", "{&#58;}"),
            ("plain", "value"),
        ]
        for v1, v2 in hostile_pairs:
            key = DB_key("Model", v1, v2)
            recovered = DB_key.from_redis_key(str(key))
            assert list(recovered) == ["Model", v1, v2]


# ---------------------------------------------------------------------------
# Malformed input: unclean() must not raise on strings clean() never
# produced.
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_unclean_does_not_raise(self):
        for v in ["", "/", "{", "{&#58", "a/"]:
            DB_key.unclean(v)  # must not raise


# ---------------------------------------------------------------------------
# Non-widening: pins the ESCAPABLE guard on the "/" branch of the new
# scanner. "Does not raise" passes under both a greedy and a narrowed
# scanner, so this test asserts the actual decoded value against the
# frozen legacy decoder as the oracle.
# ---------------------------------------------------------------------------


class TestNonWideningScanner:
    def test_slash_followed_by_non_escapable_char_is_not_widened(self):
        # A greedy scanner would turn "/a" into "a" and "x/y" into "xy".
        # The narrowed (ESCAPABLE-guarded) scanner must not.
        assert DB_key.unclean("/a") == "/a"
        assert DB_key.unclean("x/y") == "x/y"

    def test_parity_with_legacy_decoder_on_never_produced_strings(self):
        # These strings are not producible by clean() (old or new), so the
        # new decoder must match the legacy decoder byte-for-byte.
        parity_cases = ["/a", "x/y", "/", "a/", "//", "/-"]
        for v in parity_cases:
            assert DB_key.unclean(v) == _legacy_unclean(v), v

    def test_expected_divergence_on_new_encoder_output(self):
        # "/{" and "/{&#58;}" ARE producible by the *new* clean() (the
        # self-escape step), so the new decoder is expected to diverge
        # from the legacy decoder on these -- pin the divergence
        # explicitly rather than asserting parity.
        assert DB_key.unclean("/{") == "{"
        assert _legacy_unclean("/{") == "/{"

        assert DB_key.unclean("/{&#58;}") == "{&#58;}"
        assert _legacy_unclean("/{&#58;}") == "/:"

        # And the plain (non-slash-prefixed) escape sequence still decodes
        # identically under both -- it's the pre-fix ambiguous case.
        assert DB_key.unclean("{&#58;}") == ":"
        assert _legacy_unclean("{&#58;}") == ":"
