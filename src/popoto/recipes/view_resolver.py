"""Belief-sheet view resolver — read-path claim resolution over the journal (#565).

The assistant's read path can rank and truncate memory candidates but cannot
remove one because another record retracts it, has no reader-scoped
visibility, and emits no claim-level output: ``ContextAssembler.assemble()``
is additive-then-truncate, so a retracted claim keeps its rank and is
injected into the prompt as if it were live.

This module is the view layer that fixes that. :class:`BeliefSheetResolver`
wraps an ``inner: ContextAssembler`` (the ``AdaptiveAssembler`` composition
precedent — wrap, never extend ``assemble()``) and computes a
:class:`BeliefSheet`: surviving claims (retracted dropped, superseded
collapsed to winners, disjunctions shown as explicit uncertainty), each with
a provenance handle, a per-entry staleness annotation, and deterministic,
replayable resolution — a pure function over the journal parameterized by a
plain policy dict.

Per-``resolve()`` I/O budget (enforced by the call-count spy test)::

    1 assemble + <=1 gate batch + <=K chain reads

The gate batch is zero when visibility resolves from already-fetched record
fields (no reader tags); otherwise it is the single batched tag-membership
read ``_resolve_tag_keys`` already performs. Chains are fetched only for the
already-truncated top-K, and membership stays on the V0 exclusion path — no
chain walk decides membership. Winner confirmations resolve from the
winner's own chain when the winner was also selected, and read 0 otherwise.

Example:
    from popoto.recipes.context_assembler import ContextAssembler
    from popoto.recipes.view_resolver import BeliefSheetResolver

    resolver = BeliefSheetResolver(
        ContextAssembler(model_class=JournalEntry, score_weights={...})
    )
    sheet = resolver.resolve(
        {"subject": "launch"},
        reader={"agent_id": "agent-1", "purpose": "answer"},
    )
    for claim in sheet.claims:
        print(claim.key, claim.provenance)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..fields.constants import Defaults
from .context_assembler import (
    ContextAssembler,
    _get_key,
    _staleness_details,
)
from .provenance_journal import ProvenanceJournal

logger = logging.getLogger("POPOTO.ViewResolver")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

POLICY_KEYS = (
    "prefer",
    "staleness_threshold",
    "gate_overfetch_multiplier",
    "max_backfill_pulls",
)
"""Recognized ``policy`` dict keys. Unknown keys are ignored with a warning."""

PREFER_OPTIONS = ("recent", "self-stated", "confirmed")
"""Winner precedence when competing supersessions disagree.

``self-stated`` prefers the correction stated outright (``stated=True``),
``confirmed`` prefers the most corroborated, ``recent`` prefers the latest.
A complete tie is flagged as an unresolved contradiction for downstream
(escalation-only) LLM handling — the deterministic fold never guesses.
"""


def resolve_policy(policy: dict | None) -> tuple[dict, list[str]]:
    """Merge a caller policy dict over library defaults.

    ``None`` selects every default (read from :class:`Defaults` at call
    time, so deploy-level overrides apply). Unknown keys are ignored with
    a warning per key, never a crash. An invalid ``prefer`` falls back to
    the default with a warning.
    """
    warnings: list[str] = []
    merged = {
        "prefer": "self-stated",
        "staleness_threshold": Defaults.VIEW_RESOLVER_STALENESS_THRESHOLD,
        "gate_overfetch_multiplier": (Defaults.VIEW_RESOLVER_GATE_OVERFETCH_MULTIPLIER),
        "max_backfill_pulls": Defaults.VIEW_RESOLVER_MAX_BACKFILL_PULLS,
    }
    if not policy:
        return merged, warnings
    for key, value in policy.items():
        if key not in POLICY_KEYS:
            warnings.append(f"unknown policy key ignored: {key!r}")
            continue
        merged[key] = value
    if merged["prefer"] not in PREFER_OPTIONS:
        warnings.append(f"unknown prefer {merged['prefer']!r}; using 'self-stated'")
        merged["prefer"] = "self-stated"
    return merged, warnings


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    """One surviving claim on the belief sheet.

    Attributes:
        key: Stable provenance handle — the record's Redis key, matching
            the ``emit_trace`` ``{key, rank, score, source}`` handles.
        rank: Position in the final sheet order (0-based).
        score: Injection-time score proxy from the trace, or None when the
            record was not trace-attached.
        source: ``"pull"`` / ``"push"`` from the trace, else ``"unknown"``.
        content: The claim text (``statement``, else ``content`` / ``text`` /
            ``verbatim``, else ``""``).
        staleness: Decayed relevance score, or None when the model has no
            DecayingSortedField in ``score_weights``.
        stale: True when ``staleness`` is missing or below the policy
            threshold; None when staleness is unavailable.
        provenance: ``{"confirmations": int, "supersedes": [keys],
            "superseded_by": key | None, "disjunct_with": [keys]}``.
    """

    key: str
    rank: int = 0
    score: float | None = None
    source: str = "unknown"
    content: str = ""
    staleness: float | None = None
    stale: bool | None = None
    provenance: dict = field(default_factory=dict)


@dataclass
class BeliefSheet:
    """Ordered surviving claims plus operational warnings.

    Deterministic in (journal, policy): :meth:`serialize` renders a
    canonical byte string, so the same journal snapshot + policy dict
    replays byte-identical. Wall-clock-derived fields (retrieval timing)
    are deliberately excluded from the serialization.
    """

    claims: list[Claim] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def serialize(self) -> str:
        """Canonical JSON rendering for replay comparison."""
        payload = {
            "claims": [
                {
                    "content": c.content,
                    "key": c.key,
                    "provenance": {
                        "confirmations": c.provenance.get("confirmations", 0),
                        "disjunct_with": sorted(c.provenance.get("disjunct_with", [])),
                        "superseded_by": c.provenance.get("superseded_by"),
                        "supersedes": sorted(c.provenance.get("supersedes", [])),
                    },
                    "rank": c.rank,
                    "score": c.score,
                    "source": c.source,
                    "stale": c.stale,
                    "staleness": c.staleness,
                }
                for c in self.claims
            ],
            "metadata": {
                k: v
                for k, v in self.metadata.items()
                if k in ("counts", "policy", "reader")
            },
            "warnings": sorted(self.warnings),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Pure resolution
# ---------------------------------------------------------------------------

_CONTENT_ATTRS = ("statement", "content", "text", "verbatim")


def _record_text(record: Any) -> str:
    for attr in _CONTENT_ATTRS:
        try:
            value = getattr(record, attr, None)
        except Exception:
            continue
        if value:
            return str(value)
    return ""


def _annotation_sort_key(
    annotation: Any, index: int
) -> tuple[str, str, float, str, int]:
    """Deterministic fold order: ``(target, kind, ts, key, position)``.

    No wall-clock reads, no dict-iteration-order dependence, no RNG. The
    trailing position breaks full ties so the fold is total even over
    adversarially identical annotations.
    """
    try:
        target = str(getattr(annotation, "target", None) or "")
    except Exception:
        target = ""
    try:
        kind = str(getattr(annotation, "kind", None) or "")
    except Exception:
        kind = ""
    ts = 0.0
    for attr in ("valid_from", "captured_at"):
        try:
            candidate = getattr(annotation, attr, None)
        except Exception:
            continue
        if candidate is not None:
            try:
                ts = float(candidate)
            except (TypeError, ValueError):
                continue
            break
    try:
        key = _get_key(annotation)
    except Exception:
        key = ""
    return (target, kind, ts, key, index)


def _is_closing_kind(kind: Any) -> bool:
    """True for annotation kinds that end a target's live membership."""
    if kind in ("supersede", "retract"):
        return True
    try:
        return bool(ProvenanceJournal.entry_model.kind_is_closing(kind))
    except Exception:
        return False


def _is_targetless_kind(kind: Any) -> bool:
    if kind in ("assert", None, ""):
        return True
    try:
        return bool(ProvenanceJournal.entry_model.kind_is_targetless(kind))
    except Exception:
        return False


def resolve_entries(
    records: list[Any],
    chains_by_key: dict[str, list[Any]],
    policy: dict | None,
    *,
    handles_by_key: dict[str, dict] | None = None,
    staleness_by_key: dict[str, tuple[Any, bool]] | None = None,
) -> BeliefSheet:
    """Fold selected records + annotation chains into a belief sheet.

    Pure function: no Redis, no clock, no RNG. Drop retracted stragglers,
    collapse superseded losers to their winners (loser→winner handle links
    on the winner's ``provenance["supersedes"]``), count confirmations,
    pair structural disjuncts as explicit uncertainty, and flag unresolved
    contradictions for downstream LLM escalation only.

    Args:
        records: Selected record objects in rank order.
        chains_by_key: ``{record_key: [annotation objects]}`` — every entry
            annotating the record, from ``annotations_for`` or a test stub.
        policy: Plain dict (see :func:`resolve_policy`); None selects
            library defaults.
        handles_by_key: ``{key: {"score":.., "source":..}}`` trace handles.
        staleness_by_key: ``{key: (decayed_score_or_None, stale_bool)}``.

    Membership notes: a record targeted by ANY closing annotation
    (supersede/retract, including one that landed after the V0 snapshot —
    the Race 1 window) is dropped as a loser, never kept. That re-check
    costs no extra read because the chain postdates the snapshot.
    """
    merged_policy, warnings = resolve_policy(policy)
    prefer = merged_policy["prefer"]
    handles_by_key = handles_by_key or {}
    staleness_by_key = staleness_by_key or {}

    key_of: dict[int, str] = {}
    for record in records:
        try:
            key_of[id(record)] = _get_key(record)
        except Exception:
            continue

    sheet_claims: list[Claim] = []
    stats = {
        "admitted": len(records),
        "claims": 0,
        "disjunct_groups": 0,
        "retracted_dropped": 0,
        "superseded_collapsed": 0,
        "unresolved": 0,
    }

    # Partition selected records into claims vs annotations. Annotation-kind
    # records are evidence, never standalone claims: confirms/retracts are
    # absorbed into their target's fold, supersedes surface as winners.
    claims_in: list[tuple[Any, str]] = []
    annotations_in: dict[str, list[tuple[Any, str]]] = {}
    for record in records:
        key = key_of.get(id(record))
        if key is None:
            continue
        try:
            kind = getattr(record, "kind", None)
        except Exception:
            kind = None
        has_target = False
        try:
            has_target = bool(getattr(record, "target", None))
        except Exception:
            has_target = False
        if kind is None or _is_targetless_kind(kind):
            claims_in.append((record, key))
        elif has_target:
            try:
                target_key = str(getattr(record, "target"))
            except Exception:
                target_key = ""
            annotations_in.setdefault(target_key, []).append((record, key))
        else:
            # Targeted kind with no target address: corrupt evidence
            # pointing nowhere. Flag it for escalation and drop it —
            # surfacing it as a claim would invent provenance.
            stats["unresolved"] += 1
            warnings.append(
                f"unresolved contradiction: {key} has kind " f"{kind!r} with no target"
            )

    # Union the journal chains with the selected-annotation index so a
    # correction that ranked into the candidate set is visible even when
    # the chain read predates it (same deterministic sort either way).
    chains: dict[str, list[Any]] = {}
    for key, prior in chains_by_key.items():
        chains[key] = list(prior or [])
    for target_key, pairs in annotations_in.items():
        chains.setdefault(target_key, []).extend(a for a, _ in pairs)

    for key in chains:
        indexed = list(enumerate(chains[key]))
        indexed.sort(key=lambda pair: _annotation_sort_key(pair[1], pair[0]))
        chains[key] = [annotation for _, annotation in indexed]

    for record, key in claims_in:
        annotations = chains.get(key, [])
        confirms = [a for a in annotations if _safe_kind(a) == "confirm"]
        closing = [a for a in annotations if _is_closing_kind(_safe_kind(a))]

        # A retract anywhere in the chain drops the claim outright — a
        # retracted entry never appears in the belief sheet, even as a
        # V0 straggler (coupling off) or a Race 1 arrival.
        if any(_safe_kind(a) == "retract" for a in closing):
            stats["retracted_dropped"] += 1
            continue

        supersedes = [a for a in closing if _safe_kind(a) == "supersede"]
        if supersedes:
            winner = _pick_winner(supersedes, chains, prefer)
            if winner is None:
                stats["unresolved"] += 1
                winner_keys = sorted(_safe_key(a) for a in supersedes)
                warnings.append(
                    "unresolved contradiction: competing supersessions "
                    f"of {key}: {', '.join(winner_keys)}"
                )
                continue
            winner_key = _safe_key(winner)
            winner_chain = chains.get(winner_key, [])
            winner_confirms = sum(1 for a in winner_chain if _safe_kind(a) == "confirm")
            loser_keys = sorted(
                {_safe_target(a) for a in supersedes if _safe_target(a)} or {key}
            )
            sheet_claims.append(
                _make_claim(
                    winner,
                    winner_key,
                    handles_by_key,
                    staleness_by_key,
                    confirmations=winner_confirms,
                    supersedes=loser_keys,
                    superseded_by=None,
                )
            )
            stats["superseded_collapsed"] += 1
            continue

        sheet_claims.append(
            _make_claim(
                record,
                key,
                handles_by_key,
                staleness_by_key,
                confirmations=len(confirms),
                supersedes=[],
                superseded_by=None,
            )
        )

    # A selected supersede whose target never ranked is itself a winner
    # claim: its statement IS the correction. (Selected confirms/retracts
    # whose target is absent corroborate nothing visible and are dropped.)
    claimed_keys = {c.key for c in sheet_claims}
    claimed_keys.update(key for _, key in claims_in)
    for target_key, pairs in sorted(annotations_in.items()):
        for annotation, annotation_key in pairs:
            if annotation_key in claimed_keys:
                continue
            if _safe_kind(annotation) != "supersede":
                continue
            try:
                target_present = any(k == target_key for _, k in claims_in)
            except Exception:
                target_present = False
            if target_present:
                continue
            winner_chain = chains.get(annotation_key, [])
            winner_confirms = sum(1 for a in winner_chain if _safe_kind(a) == "confirm")
            sheet_claims.append(
                _make_claim(
                    annotation,
                    annotation_key,
                    handles_by_key,
                    staleness_by_key,
                    confirmations=winner_confirms,
                    supersedes=[target_key] if target_key else [],
                    superseded_by=None,
                )
            )
            claimed_keys.add(annotation_key)

    # M5-optional disjunct pairing: records carrying the same structural
    # class_id surface TOGETHER as explicit uncertainty — never collapsed.
    # Consumed structurally (never imported) so the parallel M5 lane cannot
    # break this build; without M5 ids this degrades to per-record output.
    objects_by_key: dict[str, Any] = {}
    for record, record_key in claims_in:
        objects_by_key.setdefault(record_key, record)
    for pairs in annotations_in.values():
        for record, record_key in pairs:
            objects_by_key.setdefault(record_key, record)
    for chain in chains.values():
        for annotation in chain:
            annotation_key = _safe_key(annotation)
            if annotation_key:
                objects_by_key.setdefault(annotation_key, annotation)
    by_class: dict[str, list[Claim]] = {}
    for claim in sheet_claims:
        class_id = _structural_class_id(objects_by_key.get(claim.key))
        if class_id is not None:
            by_class.setdefault(class_id, []).append(claim)
    for members in by_class.values():
        if len(members) < 2:
            continue
        stats["disjunct_groups"] += 1
        member_keys = sorted(m.key for m in members)
        for claim in members:
            claim.provenance["disjunct_with"] = [
                k for k in member_keys if k != claim.key
            ]

    for rank, claim in enumerate(sheet_claims):
        claim.rank = rank
    stats["claims"] = len(sheet_claims)
    return BeliefSheet(
        claims=sheet_claims,
        warnings=warnings,
        metadata={"counts": stats, "policy": merged_policy},
    )


def _safe_kind(annotation: Any) -> Any:
    try:
        return getattr(annotation, "kind", None)
    except Exception:
        return None


def _safe_key(annotation: Any) -> str:
    try:
        return _get_key(annotation)
    except Exception:
        return ""


def _safe_target(annotation: Any) -> str:
    try:
        return str(getattr(annotation, "target", None) or "")
    except Exception:
        return ""


def _annotation_ts(annotation: Any) -> float:
    for attr in ("valid_from", "captured_at"):
        try:
            candidate = getattr(annotation, attr, None)
        except Exception:
            continue
        if candidate is not None:
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
    return 0.0


def _pick_winner(
    supersedes: list[Any], chains: dict[str, list[Any]], prefer: str
) -> Any | None:
    """Choose one winner among competing supersessions, or None on a tie."""
    if len(supersedes) == 1:
        return supersedes[0]

    def criterion(annotation: Any) -> tuple:
        if prefer == "confirmed":
            chain = chains.get(_safe_key(annotation), [])
            confirms = sum(1 for a in chain if _safe_kind(a) == "confirm")
            return (confirms,)
        if prefer == "recent":
            return (_annotation_ts(annotation),)
        # self-stated: stated outright outranks inferred.
        try:
            stated = bool(getattr(annotation, "stated", False))
        except Exception:
            stated = False
        return (1 if stated else 0, _annotation_ts(annotation))

    ranked = sorted(
        supersedes,
        key=lambda a: (criterion(a), _safe_key(a)),
        reverse=True,
    )
    if criterion(ranked[0]) == criterion(ranked[1]):
        return None
    return ranked[0]


def _make_claim(
    record: Any,
    key: str,
    handles_by_key: dict[str, dict],
    staleness_by_key: dict[str, tuple[Any, bool]],
    *,
    confirmations: int,
    supersedes: list[str],
    superseded_by: str | None,
) -> Claim:
    handle = handles_by_key.get(key, {}) if handles_by_key else {}
    staleness_entry = staleness_by_key.get(key) if staleness_by_key else None
    score = handle.get("score")
    source = handle.get("source", "unknown")
    if staleness_entry is None:
        staleness, stale = None, None
    else:
        staleness, stale = staleness_entry[0], bool(staleness_entry[1])
    return Claim(
        key=key,
        score=score,
        source=source,
        content=_record_text(record),
        staleness=staleness,
        stale=stale,
        provenance={
            "confirmations": int(confirmations),
            "disjunct_with": [],
            "superseded_by": superseded_by,
            "supersedes": list(supersedes),
        },
    )


def _structural_class_id(record: Any) -> str | None:
    """Structural M5 class id for a record object, without importing M5."""
    if record is None:
        return None
    try:
        class_id = getattr(record, "class_id", None)
    except Exception:
        return None
    return str(class_id) if class_id is not None else None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class BeliefSheetResolver:
    """Read-path view over a ``ContextAssembler`` plus the journal (#565).

    Wraps ``inner: ContextAssembler`` (never extends ``assemble()``) and
    owns gate → resolve → annotate → emit. The resolver performs no writes,
    so it cannot race itself; concurrent annotations landing between
    retrieval and chain resolution are dropped with a warning via the
    chain re-check in :func:`resolve_entries` (Race 1), and replay pins a
    journal snapshot so it stays deterministic.

    Per-``resolve()`` I/O budget: ``1 assemble + <=1 gate batch + <=K
    chain reads``. The gate batch is zero when the reader carries no tags
    (visibility resolves from already-fetched fields); otherwise it is one
    batched tag-membership read. Back-fill pulls (capped by policy) each
    cost one further assemble.
    """

    def __init__(
        self,
        inner: ContextAssembler,
        journal: Any | None = None,
    ) -> None:
        self.inner = inner
        self.journal = journal if journal is not None else ProvenanceJournal

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        query_cues: dict | None = None,
        *,
        reader: dict | None = None,
        policy: dict | None = None,
        as_of: float | None = None,
        exclude_keys: Any = None,
        now: float | None = None,
        **assemble_kwargs: Any,
    ) -> BeliefSheet:
        """Resolve the belief sheet for a concrete reader under a policy.

        Args:
            query_cues: Retrieval cues, passed through to ``assemble()``.
            reader: Concrete ``{"agent_id": str, "purpose": str (optional),
                "tags": [...] (optional), "tag_match": "any"|"all"}``.
                None raises ``ValueError`` immediately (a missing reader is
                a caller bug, fail fast — not a gate decision). ``purpose``
                is recorded in the sheet metadata; per-record purpose
                enforcement has no schema field to check against, so the
                gate enforces ``agent_id`` + ``tags``.
            policy: Plain dict (see :func:`resolve_policy`); None selects
                library defaults.
            as_of: Point-in-time membership, passed through to ``assemble()``.
            exclude_keys: Caller suppression keys, unioned per pull.
            now: Clock override for the staleness read (replay pins this).
            assemble_kwargs: Further ``assemble()`` kwargs (partition
                filters, output format, ...). An explicit ``tags`` here
                wins over ``reader["tags"]`` for gate enforcement; the
                inner ``assemble()`` itself always runs unscoped on this
                path so the gate is the single enforcement point.

        Returns:
            :class:`BeliefSheet`. Runtime gate failures (e.g. Redis errors
            mid-gating) fail CLOSED without raising: an empty sheet plus a
            ``warnings`` entry. Only a ``None`` / malformed reader raises.
        """
        if reader is None:
            raise ValueError("resolve() requires a reader triple")
        agent_id = reader.get("agent_id")
        if not agent_id:
            raise ValueError("reader must carry a non-empty 'agent_id'")
        purpose = reader.get("purpose")
        reader_tags = assemble_kwargs.pop("tags", reader.get("tags"))
        tag_match = reader.get("tag_match", "any")
        # The resolver owns the gate seam and the trace: a caller-supplied
        # record_gate would fight the reader gate, and handles always come
        # from the per-pull traces stitched below.
        assemble_kwargs.pop("record_gate", None)
        assemble_kwargs.pop("gate_overfetch", None)
        assemble_kwargs.pop("emit_trace", None)

        merged_policy, policy_warnings = resolve_policy(policy)
        warnings: list[str] = list(policy_warnings)

        model_class = self.inner.model_class
        try:
            model_fields = model_class._meta.fields
        except Exception:
            model_fields = {}
        agent_scoped = "agent_id" in model_fields

        base_filters = dict(assemble_kwargs.pop("partition_filters", None) or {})
        if agent_scoped:
            base_filters["agent_id"] = agent_id

        max_items = self.inner.max_items
        try:
            multiplier = max(1, int(merged_policy["gate_overfetch_multiplier"]))
        except (TypeError, ValueError):
            multiplier = 1
        try:
            max_pulls = max(0, int(merged_policy["max_backfill_pulls"]))
        except (TypeError, ValueError):
            max_pulls = 0
        overfetch = max_items * (multiplier - 1)

        # Resolve tag visibility ONCE per resolve() (the single gate batch),
        # from the reader tags. Any failure here fails the whole gate
        # CLOSED — today's cooperative degrade to unscoped retrieval is a
        # leak for privacy purposes and is inverted on this path only.
        try:
            allowed_keys = self.inner._resolve_tag_keys(reader_tags, tag_match)
        except Exception as e:
            logger.warning("reader gate failed closed: %s", e)
            return BeliefSheet(
                claims=[],
                warnings=warnings + [f"reader gate failed closed; empty sheet: {e}"],
                metadata={
                    "counts": {
                        "admitted": 0,
                        "assembles": 0,
                        "chain_reads": 0,
                        "claims": 0,
                        "disjunct_groups": 0,
                        "gate_rejected": 0,
                        "retracted_dropped": 0,
                        "superseded_collapsed": 0,
                        "unresolved": 0,
                        "validity_excluded": 0,
                    },
                    "policy": merged_policy,
                    "reader": {
                        "agent_id": agent_id,
                        "purpose": purpose,
                        "tag_match": tag_match,
                        "tags": list(reader_tags or []),
                    },
                },
            )

        def gate(record: Any) -> bool:
            if agent_scoped:
                try:
                    record_agent = getattr(record, "agent_id", None)
                except Exception as e:
                    logger.warning("reader gate denied a record on error: %s", e)
                    return False
                if record_agent != agent_id:
                    return False
            if allowed_keys is not None:
                try:
                    record_key = _get_key(record)
                except Exception as e:
                    logger.warning("reader gate denied a record on error: %s", e)
                    return False
                if record_key not in allowed_keys:
                    return False
            return True

        admitted: list[Any] = []
        seen_keys: set[str] = set()
        trace_by_key: dict[str, dict] = {}
        validity_excluded = 0
        gate_rejected = 0
        assembles = 0
        caller_excluded = {str(k) for k in exclude_keys} if exclude_keys else set()
        exclude: set[str] = set(caller_excluded)

        pulls = 0
        while True:
            # tags=None here is deliberate: the reader gate below is the
            # single tag-enforcement point on this path. Passing reader
            # tags into assemble as well would double-enforce (the
            # cooperative arm scoping would consume rejections the gate
            # must count) and would re-expose the cooperative degrade the
            # gate exists to invert. Fail-closed lives only here; bare
            # assemble() keeps its cooperative default untouched.
            result = self.inner.assemble(
                query_cues,
                partition_filters=base_filters,
                tags=None,
                tag_match=tag_match,
                as_of=as_of,
                exclude_keys=(exclude or None),
                record_gate=gate,
                gate_overfetch=overfetch,
                emit_trace=True,
                **assemble_kwargs,
            )
            assembles += 1
            gate_meta = result.metadata.get("reader_gate", {})
            validity_excluded += int(gate_meta.get("validity_excluded", 0))
            gate_rejected += int(gate_meta.get("gate_rejected", 0))
            # Stitch the per-pull trace handles (free: emit_trace already
            # computed them inside assemble). First pull wins on overlap.
            try:
                pull_trace = result.metadata.get("trace") or []
            except Exception:
                pull_trace = []
            for entry in pull_trace:
                try:
                    trace_key = entry.get("key")
                except Exception:
                    continue
                if trace_key and trace_key not in trace_by_key:
                    trace_by_key[trace_key] = entry
            new_records = []
            for record in result.records:
                try:
                    record_key = _get_key(record)
                except Exception:
                    continue
                if record_key in seen_keys:
                    continue
                seen_keys.add(record_key)
                new_records.append(record)
            admitted.extend(new_records)
            pulls += 1
            if len(admitted) >= max_items or pulls > max_pulls:
                break
            if not new_records:
                break
            exclude = set(caller_excluded) | set(seen_keys)

        admitted = admitted[:max_items]
        handles_by_key = {
            key: {
                "score": entry.get("score"),
                "source": entry.get("source", "unknown"),
            }
            for key, entry in trace_by_key.items()
        }

        # Chains for the truncated top-K only: membership already decided
        # on the V0 path, these reads serve display/replay handles.
        chains_by_key: dict[str, list[Any]] = {}
        chain_reads = 0
        for record in admitted:
            try:
                record_key = _get_key(record)
            except Exception:
                continue
            try:
                annotations = self.journal.annotations_for(record)
            except Exception as e:
                warnings.append(f"chain unavailable for {record_key}: {e}")
                chains_by_key[record_key] = []
                continue
            chain_reads += 1
            try:
                chains_by_key[record_key] = list(annotations or [])
            except Exception as e:
                warnings.append(f"chain unreadable for {record_key}: {e}")
                chains_by_key[record_key] = []

        staleness_by_key = _staleness_details(
            admitted,
            model_class=model_class,
            score_weights=self.inner.score_weights,
            surfacing_threshold=merged_policy["staleness_threshold"],
            decaying_sorted_field_name=self.inner._decaying_sorted_field_name,
            now=now,
        )
        if admitted and not staleness_by_key:
            warnings.append(
                "staleness unavailable: model has no DecayingSortedField "
                "in score_weights"
            )

        sheet = resolve_entries(
            admitted,
            chains_by_key,
            merged_policy,
            handles_by_key=handles_by_key,
            staleness_by_key=staleness_by_key,
        )
        sheet.warnings = warnings + sheet.warnings
        counts = sheet.metadata.get("counts", {})
        counts.update(
            {
                "assembles": assembles,
                "chain_reads": chain_reads,
                "gate_rejected": gate_rejected,
                "validity_excluded": validity_excluded,
            }
        )
        sheet.metadata["counts"] = counts
        sheet.metadata["reader"] = {
            "agent_id": agent_id,
            "purpose": purpose,
            "tag_match": tag_match,
            "tags": list(reader_tags or []),
        }
        if len(admitted) < max_items and (gate_rejected or validity_excluded):
            sheet.warnings.append(
                f"sheet short: admitted {len(admitted)}/{max_items} "
                f"(validity_excluded={validity_excluded}, "
                f"gate_rejected={gate_rejected})"
            )
        return sheet


__all__ = [
    "BeliefSheet",
    "BeliefSheetResolver",
    "Claim",
    "POLICY_KEYS",
    "PREFER_OPTIONS",
    "resolve_entries",
    "resolve_policy",
]
