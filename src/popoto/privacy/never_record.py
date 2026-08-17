"""Never-record firewall — deterministic pre-storage privacy gate (#561).

What this guarantees
--------------------

Content matching the enumerated *guaranteed class* below never reaches Redis:
it is dropped in :meth:`popoto.models.base.Model.save` before the write
filter, before ``pre_save()``, and therefore before serialization, HSET,
index writes, BM25 tokenization, embedding calls, and co-occurrence edges.
Nothing about the dropped text is persisted — only a content-free tombstone
carrying a random id and a reason code.

The guaranteed class:

``off_the_record``
    An explicit human marker. Voids the **entire turn**, not a guessed span.
``private_key_block``
    PEM / OpenSSH / PuTTY private key headers.
``credential_prefix``
    Vendor-prefixed API tokens (Anthropic, OpenAI, GitHub, AWS, Google,
    Slack, GitLab, npm, HuggingFace, DigitalOcean, SendGrid, Stripe).
``jwt``
    Three-segment base64url JSON Web Tokens.
``credential_assignment``
    ``password=``/``api_key:``/``token=`` and friends followed by a value.
``url_userinfo``
    ``scheme://user:password@host``.
``payment_card``
    13-19 digit runs that pass the Luhn checksum.
``government_id``
    US SSN-shaped strings with a valid-area guard.
``high_entropy``
    The backstop for unknown-prefix tokens: long, credential-charset,
    high-Shannon-entropy tokens.

What this does NOT guarantee
----------------------------

This is a deterministic pattern gate, not an oracle. Read the holes:

- **Over-blocking is accepted and expected.** Base64 blobs and long random
  identifiers in ordinary prose will be dropped. That is the price of the
  zero-false-negative goal on the class above.
- **Canonical git SHAs and UUIDs are excluded from entropy scoring**
  (:data:`_STRUCTURAL_EXCLUSIONS`). Developer memory content mentions commit
  SHAs constantly, and dropping every one of them would make the firewall
  worse than useless. The cost is a real, enumerated hole: a bare 40-hex-char
  secret with no vendor prefix is not caught by the entropy backstop. It is
  still caught if it appears as ``password=<sha>`` or similar.
- **A novel credential format with no known prefix, low entropy, and no
  assignment context can pass.** The detector corpus is explicit and lives in
  one module so it can be extended; issue #561's module M9 (seeded audit) is
  the ongoing measurement of false negatives.
- **Semantically sensitive content is out of scope.** The gate matches shape,
  not meaning. An LLM voter may be added later and may only ADD drops — the
  guarantee above rests solely on this deterministic core.

Why patterns are module constants, not ``Defaults`` entries
-----------------------------------------------------------

The numeric thresholds are tuning dials and live in
:class:`popoto.fields.constants.Defaults`. The pattern corpus does not: it is
a security-relevant list, and exposing it as a mutable registry would let a
caller silently weaken the guarantee.

No Redis modules are used — tombstones are a plain HASH and a capped LIST, so
the firewall works identically on Redis and Valkey.
"""

import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from ..exceptions import NeverRecordException
from ..fields.constants import Defaults

# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

#: Stable reason codes. These are written into tombstones and surfaced by
#: ``never_record_counts()``, so they are part of the public contract --
#: renaming one breaks an adopter's dashboards. Add, don't rename.
NEVER_RECORD_REASONS = (
    "off_the_record",
    "private_key_block",
    "credential_prefix",
    "jwt",
    "credential_assignment",
    "url_userinfo",
    "payment_card",
    "government_id",
    "high_entropy",
)


@dataclass(frozen=True)
class NeverRecordVerdict:
    """Result of a never-record scan.

    Attributes:
        blocked: True if the content must never be stored.
        reason: Stable reason code from :data:`NEVER_RECORD_REASONS`, or None.
        detector: Name of the specific detector that fired, or None.

    Deliberately carries no matched text, no offset, and no length. This
    object's repr reaches log files, and any of those three would be a
    content side channel that defeats the whole module.
    """

    blocked: bool
    reason: Optional[str] = None
    detector: Optional[str] = None

    def __bool__(self) -> bool:
        return self.blocked


#: Singleton for the overwhelmingly common "nothing found" case.
_CLEAN = NeverRecordVerdict(blocked=False)


# ---------------------------------------------------------------------------
# Detector corpus
# ---------------------------------------------------------------------------

# Explicit human markers. Word-boundary anchored so "off the recording studio"
# does not fire, but tolerant of hyphenation.
_OFF_THE_RECORD = re.compile(
    r"\b(?:"
    r"off[\s\-]the[\s\-]record"
    r"|do[\s\-]?n[o']?t\s+record"
    r"|do\s+not\s+record"
    r"|do[\s\-]?n[o']?t\s+remember"
    r"|do\s+not\s+remember"
    r"|don't\s+(?:record|remember|save)\s+this"
    r"|no[\s\-]memory"
    r"|no[\s\-]record"
    r"|forget\s+(?:this|that)\s+immediately"
    r")\b",
    re.IGNORECASE,
)

# Markup-style markers an integration can emit programmatically.
_OFF_THE_RECORD_MARKUP = re.compile(
    r"(?:<!--\s*no-?memory\s*-->|\[\[\s*private\s*\]\]|\[\[\s*no-?memory\s*\]\])",
    re.IGNORECASE,
)

_PRIVATE_KEY_BLOCK = re.compile(
    r"(?:-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----"
    r"|-----BEGIN OPENSSH PRIVATE KEY-----"
    r"|PuTTY-User-Key-File)",
    re.IGNORECASE,
)

# Vendor-prefixed tokens. Each alternative pins a prefix AND a minimum body
# length, so the literal word "sk-" in prose does not fire.
_CREDENTIAL_PREFIX = re.compile(
    r"(?:"
    r"sk-ant-[A-Za-z0-9_\-]{8,}"  # Anthropic
    r"|sk-proj-[A-Za-z0-9_\-]{8,}"  # OpenAI project keys
    r"|sk-[A-Za-z0-9]{20,}"  # OpenAI classic
    r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"  # Stripe
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub PAT / OAuth / server
    r"|github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained PAT
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key id
    r"|AIza[A-Za-z0-9_\-]{35}"  # Google API key
    r"|ya29\.[A-Za-z0-9_\-]{20,}"  # Google OAuth token
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"  # Slack
    r"|glpat-[A-Za-z0-9_\-]{15,}"  # GitLab PAT
    r"|npm_[A-Za-z0-9]{30,}"  # npm
    r"|hf_[A-Za-z0-9]{30,}"  # HuggingFace
    r"|dop_v1_[a-f0-9]{50,}"  # DigitalOcean
    r"|SG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"  # SendGrid
    r")"
)

_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

# key=value / key: value credential assignments. The value must be long
# enough to be a real secret and must not be an obvious placeholder.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:api[_\-]?key|secret[_\-]?key|secret|password|passwd|pwd"
    r"|auth[_\-]?token|access[_\-]?token|refresh[_\-]?token|token"
    r"|access[_\-]?key|private[_\-]?key|client[_\-]?secret|credential)"
    r"\s*[:=]\s*"
    r"[\"']?(?P<value>[^\s\"'<>,;]{%d,})"
    % Defaults.NR_ASSIGNMENT_MIN_VALUE_LEN,
    re.IGNORECASE,
)

# Obvious documentation placeholders. Matching these keeps the quickstart's
# own examples storable -- an over-eager gate that eats "password=<your
# password here>" trains adopters to disable it.
_PLACEHOLDER_VALUE = re.compile(
    r"^(?:[\*x\.\-_]+|<.*>|\{\{?.*\}?\}|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|your[_\-].*|my[_\-].*|example.*|changeme|redacted|none|null|nil"
    r"|true|false|password|secret)$",
    re.IGNORECASE,
)

_URL_USERINFO = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:[^\s/@]{%d,}@[^\s/]+"
    % Defaults.NR_ASSIGNMENT_MIN_VALUE_LEN
)

# 13-19 digits, optionally separated by single spaces or hyphens. Verified by
# Luhn before it counts, so a 16-digit order number does not fire.
_CARD_CANDIDATE = re.compile(r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])")

# US SSN. Area/group/serial guards drop 000/666/9xx areas and 00 groups,
# which are never issued -- that is what keeps a date-like 123-45-6789 from
# being the only thing this matches.
_SSN = re.compile(r"(?<!\d)(?!000|666|9\d\d)(\d{3})-(?!00)(\d{2})-(?!0000)(\d{4})(?!\d)")

#: Token shapes excluded from *entropy* scoring only (critique C1). These are
#: structural shape rules, not corpus-tuned heuristics: they encode "this is
#: the canonical rendering of a git object id / a UUID", which cannot drift.
#: Every other detector still applies to these tokens.
_STRUCTURAL_EXCLUSIONS = (
    re.compile(r"^[0-9a-f]{7,40}$"),  # git SHA (abbrev through full)
    re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    ),  # canonical UUID
)

#: A token is entropy-scored only if it looks like an encoded blob: base64,
#: base64url, or hex, with no natural-language punctuation.
_ENTROPY_CHARSET = re.compile(r"^[A-Za-z0-9+/=_\-]+$")


def _shannon_entropy(token: str) -> float:
    """Shannon entropy of ``token`` in bits per character."""
    if not token:
        return 0.0
    counts: dict = {}
    for char in token:
        counts[char] = counts.get(char, 0) + 1
    length = len(token)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _luhn_valid(digits: str) -> bool:
    """True if ``digits`` (a bare digit string) passes the Luhn checksum."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

#: Detectors that are meaningful on the de-whitespaced rendering. Markers and
#: assignments are excluded there because removing whitespace mangles their
#: structure ("off the record" -> "offtherecord" would need its own pattern,
#: and an assignment's value boundary disappears).
_REGEX_DETECTORS = (
    ("off_the_record", "marker_prose", _OFF_THE_RECORD, False),
    ("off_the_record", "marker_markup", _OFF_THE_RECORD_MARKUP, True),
    ("private_key_block", "pem_header", _PRIVATE_KEY_BLOCK, True),
    ("credential_prefix", "vendor_token", _CREDENTIAL_PREFIX, True),
    ("jwt", "jwt_triplet", _JWT, True),
    ("url_userinfo", "url_userinfo", _URL_USERINFO, False),
    ("government_id", "us_ssn", _SSN, False),
)


def _strip_whitespace(text: str) -> str:
    """De-whitespaced rendering used to defeat split-across-lines evasion."""
    return re.sub(r"\s+", "", text)


def _scan_credential_assignment(text: str) -> Optional[NeverRecordVerdict]:
    """Assignment-form detector, skipping documentation placeholders."""
    for match in _CREDENTIAL_ASSIGNMENT.finditer(text):
        value = match.group("value")
        if _PLACEHOLDER_VALUE.match(value):
            continue
        return NeverRecordVerdict(
            blocked=True, reason="credential_assignment", detector="assignment"
        )
    return None


def _scan_payment_card(text: str) -> Optional[NeverRecordVerdict]:
    """Luhn-validated payment card detector."""
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[ \-]", "", match.group(0))
        if _luhn_valid(digits):
            return NeverRecordVerdict(
                blocked=True, reason="payment_card", detector="luhn"
            )
    return None


def _scan_high_entropy(text: str) -> Optional[NeverRecordVerdict]:
    """Backstop detector for unknown-prefix credential-shaped tokens."""
    min_len = Defaults.NR_ENTROPY_MIN_TOKEN_LEN
    min_bits = Defaults.NR_ENTROPY_MIN_BITS
    for raw_token in text.split():
        # Trim natural-language punctuation clinging to the token.
        token = raw_token.strip(".,;:!?()[]{}\"'<>")
        if len(token) < min_len:
            continue
        if any(pattern.match(token) for pattern in _STRUCTURAL_EXCLUSIONS):
            continue
        if not _ENTROPY_CHARSET.match(token):
            continue
        if _shannon_entropy(token) >= min_bits:
            return NeverRecordVerdict(
                blocked=True, reason="high_entropy", detector="shannon"
            )
    return None


def scan_never_record(text) -> NeverRecordVerdict:
    """Scan ``text`` for content that must never be recorded.

    Pure and deterministic: no Redis, no model, no network, no clock. Safe to
    call from an extraction pipeline before an LLM sees the candidate.

    Every regex detector marked whitespace-insensitive runs against two
    renderings — the raw text and a de-whitespaced rendering — so a
    credential split across whitespace or newlines is still caught. That is
    the adversarial case named in issue #561's acceptance criteria.

    Args:
        text: The candidate content. Non-str input returns a clean verdict;
            callers pass field values of arbitrary type.

    Returns:
        NeverRecordVerdict. Falsy when the content may be stored. Carries a
        reason code and detector name but never any fragment of the input.
    """
    if not isinstance(text, str) or not text.strip():
        return _CLEAN

    stripped = _strip_whitespace(text)

    for reason, detector, pattern, whitespace_insensitive in _REGEX_DETECTORS:
        if pattern.search(text):
            return NeverRecordVerdict(blocked=True, reason=reason, detector=detector)
        if whitespace_insensitive and pattern.search(stripped):
            return NeverRecordVerdict(
                blocked=True, reason=reason, detector=f"{detector}_dewhitespaced"
            )

    for scanner in (_scan_credential_assignment, _scan_payment_card):
        verdict = scanner(text)
        if verdict is not None:
            return verdict

    # Payment cards are the one non-regex detector worth re-running on the
    # de-whitespaced rendering: a card split across lines is a real shape.
    verdict = _scan_payment_card(stripped)
    if verdict is not None:
        return verdict

    # Entropy scoring runs on the RAW rendering only. Running it on the
    # de-whitespaced rendering was tried and is wrong: any prose paragraph
    # collapses into a single long token over the credential charset whose
    # entropy comfortably exceeds the threshold, so the firewall blocked
    # ordinary sentences like "Deploy uses blue-green with automatic
    # rollback". Whole-document entropy carries no signal about whether a
    # secret is present.
    #
    # The cost is a real, enumerated hole: an unknown-prefix secret split
    # across whitespace escapes the backstop. Split *known* formats are still
    # caught -- every vendor-prefix, JWT, and PEM pattern runs against the
    # de-whitespaced rendering above, which is the adversarial case #561's
    # acceptance criteria names.
    return _scan_high_entropy(text) or _CLEAN


# ---------------------------------------------------------------------------
# Tombstones -- content-free by construction
# ---------------------------------------------------------------------------


def never_record_key(class_name: str, kind: str) -> str:
    """Build a never-record Redis key.

    The ``$NR:`` namespace is disjoint from ``$WF:`` (write filter) and
    ``$TOMB:`` (lifecycle tombstones, which deliberately archive the payload).

    Args:
        class_name: Model class name.
        kind: ``"counts"`` or ``"drops"``.

    Returns:
        str: Redis key like ``$NR:DefaultMemory:counts``.
    """
    return f"$NR:{class_name}:{kind}"


def write_tombstone(class_name: str, verdict: NeverRecordVerdict, redis_client=None):
    """Record that a drop happened, without recording what was dropped.

    Writes two plain Redis/Valkey structures (no modules):

    - ``$NR:{class}:counts`` -- HASH, ``HINCRBY <reason> 1``
    - ``$NR:{class}:drops``  -- LIST, capped at
      :attr:`Defaults.NR_TOMBSTONE_LOG_MAX`

    The entry id is :func:`uuid.uuid4`, deliberately **not** a hash or
    fingerprint of the content. A content-derived id would be a confirmation
    oracle: anyone holding a candidate secret could hash it and check the log
    for a match. This is the precise point where the never-record tombstone
    diverges from :class:`popoto.recipes.memory_lifecycle.Tombstone`, which
    stores an ExistenceFilter fingerprint on purpose so writes can be matched
    against it.

    No field of the entry contains, derives from, or is length-correlated
    with the dropped text.

    Args:
        class_name: Model class name, for the key namespace.
        verdict: The blocking verdict. Only its reason/detector are stored.
        redis_client: Optional client override, for tests.

    Returns:
        str: The tombstone entry id.
    """
    entry_id = str(uuid.uuid4())
    entry = json.dumps(
        {
            "id": entry_id,
            "reason": verdict.reason,
            "detector": verdict.detector,
            "at": round(time.time(), 3),
        },
        sort_keys=True,
    )

    if redis_client is None:
        from ..redis_db import POPOTO_REDIS_DB

        redis_client = POPOTO_REDIS_DB

    counts_key = never_record_key(class_name, "counts")
    drops_key = never_record_key(class_name, "drops")

    # Best-effort: a firewall that raises when its audit log is unwritable
    # would fail *open* under Redis trouble, which is the one direction this
    # module must never fail in. The drop itself is enforced by the caller.
    try:
        pipe = redis_client.pipeline()
        pipe.hincrby(counts_key, verdict.reason or "unknown", 1)
        pipe.lpush(drops_key, entry)
        pipe.ltrim(drops_key, 0, Defaults.NR_TOMBSTONE_LOG_MAX - 1)
        pipe.execute()
    except Exception:  # pragma: no cover - exercised only on Redis failure
        pass

    return entry_id


# ---------------------------------------------------------------------------
# The mixin
# ---------------------------------------------------------------------------


class NeverRecordMixin:
    """Model mixin enforcing the never-record firewall on ``save()``.

    Deliberately **not** a :class:`~popoto.fields.write_filter.WriteFilterMixin`
    subclass. The write filter is a scalar salience score with one
    ``compute_filter_score()`` slot per class, already claimed by salience
    scoring in the documented pattern; a firewall is a boolean content
    predicate with a tombstone side effect. Different shape, different slot.

    Usage::

        class Memory(NeverRecordMixin, Model):
            content = StringField()

        Memory(content="my key is sk-ant-api03-AAAA...").save()  # -> False

        Memory.never_record_counts()   # {"credential_prefix": 1}

    :class:`~popoto.recipes.default_memory.DefaultMemory` carries this mixin
    already, so the batteries-included path and the Claude Code / Codex
    harness are gated with no adopter code change.

    Key fields are excluded from the scan. ``AutoKeyField``/``KeyField``
    values are generated UUID/hex shapes -- exactly what the entropy detector
    targets -- so scanning them would let a model block on its own identity.
    A key is never user content.
    """

    # Export/import: the $NR: keys are audit telemetry about writes that were
    # refused, not state belonging to any record. Nothing to carry across a
    # round trip. Matches WriteFilterMixin's precedent.
    roundtrip_policy: str = "rebuild"

    def _never_record_scan_values(self):
        """Yield the string field values this instance exposes to the scan.

        Excludes key fields (see the class docstring) and any non-string
        value. Mirrors the key-field iteration the ``KeyMutationError`` guard
        uses in ``Model.save()``.
        """
        meta = self._meta
        key_names = set(meta.key_field_names) | set(meta.auto_field_names)
        for field_name in meta.field_names:
            if field_name in key_names:
                continue
            value = getattr(self, field_name, None)
            if isinstance(value, str) and value.strip():
                yield value

    def _check_never_record(self):
        """Evaluate the firewall and gate the save.

        Each field is scanned independently under both renderings. There is
        deliberately no cross-field concatenated pass: with whitespace
        removed, any join separator either vanishes -- letting two innocuous
        adjacent values merge into a shape neither contains -- or has to be a
        normalization-surviving sentinel, which is per-field scanning with
        extra steps. Issue #561 names only intra-value splitting as the
        adversarial case.

        Returns:
            NeverRecordVerdict: A clean verdict when the save may proceed.

        Raises:
            NeverRecordException: If any scanned value is blocked. The
                message carries only the reason code and detector name --
                never the matched text, an offset, or a length. That is
                load-bearing: ``MemoryService._record_failure`` writes
                ``f"{type(exc).__name__}: {exc}"`` into a plaintext log file,
                so a message quoting the match would defeat this module
                through a side channel.
        """
        for value in self._never_record_scan_values():
            verdict = scan_never_record(value)
            if verdict.blocked:
                write_tombstone(type(self).__name__, verdict)
                raise NeverRecordException(
                    f"never-record: {verdict.reason} ({verdict.detector})"
                )
        return _CLEAN

    @classmethod
    def never_record_counts(cls, redis_client=None) -> dict:
        """Return ``{reason_code: count}`` of drops for this model.

        Content-free by construction -- this is the auditable half of the
        guarantee.

        Args:
            redis_client: Optional client override, for tests.

        Returns:
            dict: Reason code to integer count. Empty if nothing dropped.
        """
        if redis_client is None:
            from ..redis_db import POPOTO_REDIS_DB

            redis_client = POPOTO_REDIS_DB
        raw = redis_client.hgetall(never_record_key(cls.__name__, "counts")) or {}
        counts = {}
        for key, value in raw.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            counts[key] = int(value)
        return counts

    @classmethod
    def never_record_log(cls, limit: int = 100, redis_client=None) -> list:
        """Return the most recent drop tombstones, newest first.

        Args:
            limit: Maximum entries to return.
            redis_client: Optional client override, for tests.

        Returns:
            list: Dicts with ``id``, ``reason``, ``detector``, ``at``. No
            entry contains any fragment of the dropped content.
        """
        if redis_client is None:
            from ..redis_db import POPOTO_REDIS_DB

            redis_client = POPOTO_REDIS_DB
        raw = redis_client.lrange(never_record_key(cls.__name__, "drops"), 0, limit - 1)
        entries = []
        for item in raw or []:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            try:
                entries.append(json.loads(item))
            except (TypeError, ValueError):  # pragma: no cover - corrupt entry
                continue
        return entries
