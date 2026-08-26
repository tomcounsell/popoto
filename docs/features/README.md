# Features Index

Agent-memory primitives and composed layers shipped in Popoto. Start at
[Agent Memory](agent-memory.md) for the map of how they fit together, or at the
[Quickstart](../guides/agent-memory-quickstart.md) to build one.

| Feature | Description | Status |
|---------|-------------|--------|
| [Agent Memory](agent-memory.md) | The map: 17 primitives, the layers composed on them, and where each reference lives | Stable |
| [Auditable Extraction](auditable-extraction.md) | Opt-in candidate generator + enum-verdict LLM stage + per-candidate decision log — extraction precision/recall computable offline | Stable |
| [CoOccurrenceField](co-occurrence-field.md) | Associative co-occurrence graph for candidate expansion | Stable |
| [CompositeScoreQuery](composite-score-query.md) | Multi-factor ranked retrieval across sorted indexes | Stable |
| [ConfidenceField](confidence-field.md) | Capped-evidence certainty tracking with corroborate/contradict updates | Stable |
| [ContentField + EmbeddingField](content-and-embedding-fields.md) | Large content routing and vector embedding storage | Stable |
| [ContextAssembler](context-assembler.md) | Retrieval-to-injection bridge orchestrating pull and push paths | Stable |
| [CyclicDecayField](cyclic-decay-field.md) | Cyclical resonance, pressure, proactive surfacing, and confidence-modulated decay | Stable |
| [DecayingSortedField](decaying-sorted-field.md) | Time-decayed sorted index for relevance ranking, with confidence-modulated per-record decay rates | Stable |
| [ExistenceFilter](existence-filter.md) | Probabilistic membership pre-check (Bloom-style) | Stable |
| [Harness Integration](harness-integration.md) | Subconscious memory for Claude Code, Codex, Hermes, and OpenClaw via hooks and MCP | Stable |
| [Hybrid Retrieval (BM25 + RRF)](hybrid-retrieval.md) | BM25 keyword search fused with vector scores via RRF | Stable |
| [LLM Memory Extraction](llm-memory-extraction.md) | Pluggable extraction providers (heuristic default, opt-in Claude) for `SubconsciousMemory` | Stable |
| [Metacognitive Layer](metacognitive-layer.md) | Retrieval quality scoring, FOK, `"used"` outcome, AdaptiveAssembler | Stable |
| [NeverRecordFirewall](never-record-firewall.md) | Deterministic pre-storage privacy gate that blocks credentials and secrets from ever reaching Redis | Stable |
| [ObservationProtocol](observation-protocol.md) | Outcome-driven memory effects: acted, dismissed, deferred, contradicted, used | Stable |
| [ParametricSweep](parametric-sweep.md) | Automated benchmark sweeps for tuning numeric constants | Stable |
| [PolicyCache](policy-cache.md) | Learned action selection with crystallization and TD updates | Stable |
| [PredictionLedger](prediction-ledger.md) | Prediction recording, resolution, and `error_summary` aggregation | Stable |
| [Provenance Journal](provenance-journal.md) | Append-only attributed entries with confirm/supersede/retract annotations | Stable |
| [ValidityField and SupersessionProtocol](validity-and-supersession.md) | Bitemporal validity intervals and supersession chains — validity decides membership, decay decides ordering | Stable |
