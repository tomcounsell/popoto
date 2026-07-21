"""SIQ — Subconscious Injection Quality benchmark (issue #459).

The flagship *native* Popoto benchmark. It measures the thing composite
(query-blind) retrieval actually does and no public benchmark measures:
*without an explicit query cueing it,* did the right memory get injected into
context at the right turn?

Every other benchmark in this repo (Tier 1-4 scenarios, the LongMemEval-S /
LoCoMo external harness, the Tier-5 judged-answer harness) is *query-driven*:
an explicit question or ``query_cues`` dict is supplied and the harness scores
whether the right evidence came back. SIQ is the complement — it replays
multi-turn agent traces where a later turn needs a memory established earlier
but the turn's own message never lexically cues it (coreference, implication,
or need-to-know that's simply never restated). A ``lint_trace`` authoring guard
proves the cue is *mechanically* absent (BM25-tokenizer-disjoint), so the
benchmark can't be gamed by picking easy targets.

Design invariants (issue #459 + ``docs/plans/siq_benchmark.md``):

- **Deterministic, committed fixtures.** Traces live as JSON under
  ``fixtures/`` — no runtime generation, no RNG in scoring, no wall-clock in
  ranking (planted ``importance`` drives the query-blind composite order).
- **pytest DB-15 native.** The CI-facing suite is ``tests/benchmarks/test_siq.py``
  and runs under the standard db15 isolation plugin. The optional CLI
  (``run_siq.py``) reuses ``external_base._bench_db`` (default 14, rejects 0)
  for the same isolation reason — SIQ never touches db0.
- **Competitor-fair.** ``SiqAdapter`` (``adapters.py``) is the extension point
  mirroring the Tier-5 ``JudgeProtocol`` pattern. This module ships the native
  Popoto adapter plus a dependency-free query-driven baseline
  (``QueryOnlyStubAdapter``) that scores ~0 on these traces *by construction* —
  the harness's own validity proof. Real Mem0/Zep/Hindsight adapters are a
  tracked follow-up (not fabricated here).
"""
