"""Deterministic CSR (Constraint Satisfaction Rate) eval harness (issue #418).

Each test case is ``(planted corpus, standard query, adversarial query,
assertions)``. The corpus is planted into the isolated test DB, both queries
run through ``ContextAssembler.assemble()`` (the primary retrieval path), and
deterministic assertions are scored with no LLM judge, no embedding model, and
no Redis-module command — identical on Redis and Valkey.

Metrics:

- **CSR** (per case): passed assertions / total assertions, fractional.
- **RSR (standard / adversarial)**: mean case CSR across the suite.
- **Adversarial Gap**: ``RSR(standard) - RSR(adversarial)``. A LARGE gap on
  the lexical path is the healthy keyword-dependence signature (pure BM25
  only matches surface tokens). It is NOT the #409 signature.
- **Query-blindness flag** (the #409 signature): a query-blind path produces
  the IDENTICAL ranked list for the standard and adversarial query
  (Gap = 0 exactly) while the standard CSR is low (the ranking ignores the
  query, so it cannot satisfy query-relevance assertions).

Determinism guarantee: no unseeded randomness, no LLM calls, no wall-clock
dependence in scoring — the same suite produces bit-identical RSR/Gap on
every run.
"""

# ---------------------------------------------------------------------------
# Named constants. These are magic numbers for experimental tuning of the
# harness itself — they live in code, never as user config
# (per feedback_magic_numbers).
# ---------------------------------------------------------------------------

# Default top-k window for assertions (InTopK/NoneOlderThan/CoversTopic/
# Excludes) when a case does not override k. Matches the #409 audit's P@10
# framing and the recipe's max_items defaults.
DEFAULT_TOP_K = 10

# max_items handed to ContextAssembler — a comfortable superset of
# DEFAULT_TOP_K so top-k assertions are never truncated by the assembler.
ASSEMBLER_MAX_ITEMS = 20

# Reporting threshold: a suite Adversarial Gap at or above this flags "likely
# keyword-dependent retrieval" in the markdown report. A LARGE gap means
# keyword dependence (expected of pure BM25), NOT query-blindness. Reported,
# never enforced as a CI gate in the MVP.
ADVERSARIAL_GAP_ALERT = 0.30

# Reporting threshold: a case with identical standard/adversarial rankings
# AND a standard CSR below this is flagged with the #409 query-blind
# signature. Reported, never enforced as a CI gate in the MVP.
QUERY_BLIND_CSR_ALERT = 0.5
