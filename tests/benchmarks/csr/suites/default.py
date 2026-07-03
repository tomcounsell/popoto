# ---------------------------------------------------------------------------
# #408-#416 coverage enumeration (the explicit scope cut — issue #418 asks the
# seed suite to cover "the primitives implicated in #408-#416", but a
# ranked-list assertion DSL can only express retrieval-ranking properties):
#
# - #408 (token-budget counter honesty) — not-applicable: token accounting
#   inside assemble(), not a retrieval-ranking property; already fixed/closed
#   via PR #419.
# - #409 (query-blind default retrieval) — covered (case_id=query_blind_409,
#   case_id=composite_control_409): the harness's central target.
# - #410 (PolicyCache Q-values alias the DecayingSortedField score slot) —
#   not-applicable: Q-value storage semantics, not retrieval ranking.
# - #411 (StreamConsumer PEL never redelivers) — not-applicable: stream
#   delivery semantics; no ranked list involved.
# - #412 (concurrent-save index corruption) — not-applicable: multi-process
#   write race; CSR is single-threaded by design.
# - #413 (lifecycle tick / AccessTracker pollution) — not-applicable:
#   lifecycle side effects, not pull-path ranking; CSR models declare no
#   lifecycle machinery.
# - #414 (CMS depth-7 hash rows affine-correlated) — not-applicable:
#   frequency-sketch accuracy, not a ranking property; CSR models declare no
#   FrequencySketch.
# - #415 (temporal phase + chi-squared) — not-applicable: temporal
#   statistics, not a ranked-retrieval property.
# - #416 (co-occurrence clamp) — not-applicable in the MVP: CSR models
#   declare no CoOccurrenceField, so the graph-propagation branch is inert; a
#   graph-propagation CSR case is named follow-up breadth once #416 is fixed.
#
# The #409-catching case pair: `query_blind_409` (lexical — healthy path:
# DIFFERENT rankings for the standard vs adversarial query, high standard
# CSR) and `composite_control_409` (query-blind by construction: IDENTICAL
# rankings for both queries, Gap = 0 exactly, low standard CSR — the #409
# signature the detector fires on).
#
# Authoring rules for every case (enforced by the load-time lint below, using
# the REAL BM25 tokenizer):
#   1. The adversarial query shares NO indexed token with any relevant
#      memory's content (a weak paraphrase behaves like the standard query).
#   2. The adversarial query shares >= 1 indexed token with a distractor
#      memory, so BM25 still returns hits and the lexical path actually
#      executes (zero corpus-wide hits silently fall back to the query-blind
#      composite path).
#   3. No BM25 score ties between assertion-referenced ids for the given
#      query (Lua table.sort is unstable); give them clearly distinct term
#      overlap. The double-run determinism test in test_csr.py is the
#      tripwire.
# ---------------------------------------------------------------------------
"""Seed suite for the deterministic CSR harness (issue #418) — ~8 cases.

Every query and corpus below is authored fixture data (no runtime rewrite
step, no synonym RNG, no LLM) so the adversarial input is byte-identical
every run. ``lint_suite(SUITE)`` runs at import time.
"""

from ..corpus import CsrTestCase, PlantedMemory, lint_suite
from ..satisfaction import (
    CoversTopic,
    Excludes,
    InTopK,
    NoneOlderThan,
    RanksAbove,
)


def _m(mid, content, topic="misc", timestamp=1000.0, importance=0.5):
    return PlantedMemory(
        id=mid,
        content=content,
        timestamp=timestamp,
        topic=topic,
        importance=importance,
    )


# --- (a) basic lexical recall -------------------------------------------------

_LEXICAL_RECALL = CsrTestCase(
    case_id="lexical_recall_basic",
    primitive="lexical_recall",
    planted_corpus=(
        _m(
            "dep-1",
            "The deployment pipeline pushes container images to the "
            "registry every release cycle.",
            topic="deploy",
        ),
        _m(
            "dep-2",
            "Deployment requires manual approval before the container "
            "reaches production servers.",
            topic="deploy",
        ),
        _m("dis-1", "The office kitchen restocks coffee beans and oat milk."),
        _m("dis-2", "Quarterly budget review covers travel spending and hardware."),
        # Distractor sharing the adversarial token "rollout":
        _m("dis-3", "The rollout timetable for staff onboarding lives in a wiki."),
        _m("dis-4", "Annual security training modules finish before the audit."),
        _m("dis-5", "Marketing drafts newsletter copy for the conference booth."),
        _m("dis-6", "The parking garage assigns numbered spots by seniority."),
    ),
    standard_query="deployment container production release registry",
    adversarial_query="rollout of freshly built software onto the live fleet",
    assertions=(
        InTopK("dep-1", k=5),
        InTopK("dep-2", k=5),
        CoversTopic("deploy", k=10),
        Excludes("dis-6", k=10),
    ),
    relevant_ids=frozenset({"dep-1", "dep-2"}),
)


# --- (b) ordering: RanksAbove on clearly score-separated ids ------------------

_ORDERING = CsrTestCase(
    case_id="ordering_score_separated",
    primitive="ordering",
    planted_corpus=(
        # 5 of 5 query tokens vs 1 of 5 — clearly distinct BM25 scores.
        _m(
            "inc-1",
            "The incident postmortem blames the cache stampede for the "
            "outage cascade.",
            topic="incident",
        ),
        _m(
            "inc-2",
            "The outage on Tuesday started in the payments cluster.",
            topic="incident",
        ),
        _m("inc-d1", "Lunch orders arrive at noon on Fridays from the taco truck."),
        _m("inc-d2", "The design team reviews mockups during the weekly critique."),
        # Distractor sharing the adversarial token "service":
        _m("inc-d3", "Customer service agents rotate weekend shifts each month."),
        _m("inc-d4", "The rooftop garden needs volunteers to water tomatoes."),
        _m("inc-d5", "Expense reports require receipts scanned as documents."),
        _m("inc-d6", "The annual picnic moves to the lakeside pavilion."),
    ),
    standard_query="incident postmortem cache stampede outage",
    adversarial_query="retrospective writeup covering why the service went dark",
    assertions=(
        InTopK("inc-1", k=1),
        RanksAbove("inc-1", "inc-2"),
        Excludes("inc-d1", k=10),
    ),
    relevant_ids=frozenset({"inc-1", "inc-2"}),
)


# --- (c) recency: NoneOlderThan over planted fixture timestamps ---------------

_RECENCY = CsrTestCase(
    case_id="recency_none_older",
    primitive="recency",
    planted_corpus=(
        _m(
            "rm-1",
            "The roadmap milestone adds vector search to the memory layer.",
            topic="roadmap",
            timestamp=2000.0,
        ),
        _m(
            "rm-2",
            "Vector search ships behind a feature flag next milestone.",
            topic="roadmap",
            timestamp=2100.0,
        ),
        # Old memories share no token with either query, so they are never
        # retrieved and NoneOlderThan(1000.0) holds on the standard run.
        _m(
            "old-1",
            "Archived meeting notes mention a chatbot pilot from years back.",
            topic="archive",
            timestamp=100.0,
        ),
        _m(
            "old-2",
            "The deprecated billing system exported invoices as spreadsheets.",
            topic="archive",
            timestamp=150.0,
        ),
        # Recent distractor sharing the adversarial token "delivery":
        _m(
            "rc-d1",
            "Package delivery for the office arrives through the loading dock.",
            timestamp=1900.0,
        ),
        _m(
            "rc-d2",
            "Employee badges unlock the bicycle storage cage downstairs.",
            timestamp=1950.0,
        ),
        _m(
            "rc-d3",
            "The espresso machine descaling happens on alternate Mondays.",
            timestamp=1800.0,
        ),
        _m(
            "rc-d4",
            "Fire drill procedures post beside every stairwell exit.",
            timestamp=1850.0,
        ),
    ),
    standard_query="vector search milestone roadmap",
    adversarial_query="embedding lookup capability planned delivery timeline",
    assertions=(
        NoneOlderThan(1000.0, k=10),
        InTopK("rm-1", k=5),
        InTopK("rm-2", k=5),
        CoversTopic("roadmap", k=10),
    ),
    relevant_ids=frozenset({"rm-1", "rm-2"}),
)


# --- (coverage) CoversTopic across three planted facts ------------------------

_COVERAGE = CsrTestCase(
    case_id="coverage_all_topic_facts",
    primitive="coverage",
    planted_corpus=(
        _m(
            "pr-1",
            "Enterprise pricing tier includes dedicated support and custom "
            "contracts.",
            topic="pricing",
        ),
        _m(
            "pr-2",
            "The starter pricing tier caps usage at ten thousand requests monthly.",
            topic="pricing",
        ),
        _m(
            "pr-3",
            "Pricing discounts apply when customers commit to annual billing.",
            topic="pricing",
        ),
        # Distractor sharing the adversarial token "subscription":
        _m("pd-1", "The team shares a magazine subscription for the lobby."),
        _m("pd-2", "Server racks in the basement need cable management labels."),
        _m("pd-3", "The wellness program reimburses gym memberships quarterly."),
        _m("pd-4", "Recycling bins sit beside the loading dock entrance."),
        _m("pd-5", "Interview panels rotate among senior engineers monthly."),
    ),
    standard_query="pricing tier discounts enterprise starter",
    adversarial_query="cost structure across subscription plans varies per seat",
    assertions=(
        CoversTopic("pricing", k=10),
        InTopK("pr-3", k=10),
        Excludes("pd-2", k=10),
    ),
    relevant_ids=frozenset({"pr-1", "pr-2", "pr-3"}),
)


# --- (exclusion) distractors stay out of the top-k -----------------------------

_EXCLUSION = CsrTestCase(
    case_id="exclusion_unrelated",
    primitive="exclusion",
    planted_corpus=(
        _m(
            "au-1",
            "Session tokens expire after thirty minutes of idle authentication.",
            topic="auth",
        ),
        _m(
            "au-2",
            "Password reset emails route through the authentication service queue.",
            topic="auth",
        ),
        _m("au-d1", "The gardening club plants tomato seedlings behind the shed."),
        # Distractor sharing the adversarial token "window":
        _m("au-d2", "Office window blinds close automatically at sunset."),
        _m("au-d3", "The mailroom forwards packages every Thursday afternoon."),
        _m("au-d4", "Cafeteria trays return to the conveyor after meals."),
        _m("au-d5", "The lobby aquarium gets cleaned by an outside vendor."),
        _m("au-d6", "Standing desks require an ergonomic assessment request."),
    ),
    standard_query="authentication session tokens expire",
    adversarial_query="login credential validity window before forced signout",
    assertions=(
        InTopK("au-1", k=1),
        RanksAbove("au-1", "au-2"),
        Excludes("au-d1", k=10),
    ),
    relevant_ids=frozenset({"au-1", "au-2"}),
)


# --- (precision) multi-topic corpus, off-topic memories excluded ---------------

_PRECISION = CsrTestCase(
    case_id="precision_multi_topic",
    primitive="precision",
    planted_corpus=(
        _m(
            "se-1",
            "Search indexing rebuilds nightly and refreshes stale postings.",
            topic="search",
        ),
        _m(
            "se-2",
            "Search latency dropped after sharding the index by tenant.",
            topic="search",
        ),
        _m(
            "bi-1",
            "Billing invoices generate on the first weekday every month.",
            topic="billing",
        ),
        _m(
            "bi-2",
            "Refund requests need finance approval within seven days.",
            topic="billing",
        ),
        # Distractor sharing the adversarial token "catalog":
        _m("mi-1", "The support team publishes a catalog of troubleshooting guides."),
        _m("mi-2", "Conference room bookings clear automatically at midnight."),
        _m("mi-3", "The vending machine accepts contactless payments only."),
        _m("mi-4", "Quarterly all-hands meetings stream to remote offices."),
    ),
    standard_query="search indexing latency sharding",
    adversarial_query="query engine speed improvements and catalog refresh cadence",
    assertions=(
        CoversTopic("search", k=10),
        RanksAbove("se-2", "se-1"),
        Excludes("bi-1", k=10),
        Excludes("bi-2", k=10),
    ),
    relevant_ids=frozenset({"se-1", "se-2"}),
)


# --- (d)+(e) the #409 case pair ------------------------------------------------
#
# Shared corpus. Distractor importance dominates relevant importance, so the
# composite control (which ranks ONLY by importance — _pull_path_composite
# never reads query text) buries all three relevant memories below rank 10 of
# 11: every assertion fails, standard CSR = 0, and the ranked list is
# IDENTICAL for both queries (Gap = 0 exactly) — the #409 signature. The
# lexical case on the same corpus retrieves exactly the relevant memories for
# the standard query (CSR 1.0) and different results for the adversarial one.

_CORPUS_409 = (
    _m(
        "qb-1",
        "Agent memory retrieval ranks stored facts by keyword overlap " "strength.",
        topic="memory",
        importance=0.30,
    ),
    _m(
        "qb-2",
        "Retrieval quality degrades when memory queries share no keywords.",
        topic="memory",
        importance=0.25,
    ),
    _m(
        "qb-3",
        "The ranking index stores facts about agent memory retrieval.",
        topic="memory",
        importance=0.20,
    ),
    _m(
        "qb-d1",
        "The cafeteria menu rotates soups and salads by season.",
        topic="noise",
        importance=0.90,
    ),
    # Distractor sharing the adversarial token "recall":
    _m(
        "qb-d2",
        "The safety recall notice for desk chairs arrived last week.",
        topic="noise",
        importance=0.85,
    ),
    _m(
        "qb-d3",
        "Parking validation stickers expire at the end of the quarter.",
        topic="noise",
        importance=0.80,
    ),
    _m(
        "qb-d4",
        "The book club discusses a mystery novel this cycle.",
        topic="noise",
        importance=0.75,
    ),
    _m(
        "qb-d5",
        "Window washers visit the tower during the second week.",
        topic="noise",
        importance=0.70,
    ),
    _m(
        "qb-d6",
        "Holiday schedules post to the bulletin board in December.",
        topic="noise",
        importance=0.65,
    ),
    _m(
        "qb-d7",
        "The fitness room replaces treadmill belts twice yearly.",
        topic="noise",
        importance=0.60,
    ),
    _m(
        "qb-d8",
        "Visitor badges must come back to the front lobby by evening.",
        topic="noise",
        importance=0.55,
    ),
)

_QUERY_409_STANDARD = "memory retrieval keyword ranking"
_QUERY_409_ADVERSARIAL = "recall ordering favors surface term matches"
_ASSERTIONS_409 = (
    InTopK("qb-1", k=5),
    InTopK("qb-2", k=5),
    InTopK("qb-3", k=5),
    CoversTopic("memory", k=10),
    Excludes("qb-d1", k=10),
)
_RELEVANT_409 = frozenset({"qb-1", "qb-2", "qb-3"})

_QUERY_BLIND_409 = CsrTestCase(
    case_id="query_blind_409",
    primitive="query_independence",
    planted_corpus=_CORPUS_409,
    standard_query=_QUERY_409_STANDARD,
    adversarial_query=_QUERY_409_ADVERSARIAL,
    assertions=_ASSERTIONS_409,
    relevant_ids=_RELEVANT_409,
    # retrieval_mode="auto" resolves to lexical (BM25Field, no vector field)
)

_COMPOSITE_CONTROL_409 = CsrTestCase(
    case_id="composite_control_409",
    primitive="query_independence",
    planted_corpus=_CORPUS_409,
    standard_query=_QUERY_409_STANDARD,
    adversarial_query=_QUERY_409_ADVERSARIAL,
    assertions=_ASSERTIONS_409,
    relevant_ids=_RELEVANT_409,
    retrieval_mode="composite",
    # Non-empty weights over the model's SortedField — with score_weights={},
    # composite_score() raises QueryException (swallowed) and ranks nothing.
    score_weights={"importance": 1.0},
)


SUITE = [
    _LEXICAL_RECALL,
    _ORDERING,
    _RECENCY,
    _COVERAGE,
    _EXCLUSION,
    _PRECISION,
    _QUERY_BLIND_409,
    _COMPOSITE_CONTROL_409,
]

# Load-time lint: token rules via the real BM25 tokenizer, non-empty
# assertions, unique case ids. Raises CsrAuthoringError on any violation.
lint_suite(SUITE)
