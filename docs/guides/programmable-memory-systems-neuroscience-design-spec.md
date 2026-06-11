# Programmable Memory Systems for AI Agents: A Neuroscience-Grounded Design Specification

**The central thesis of this design is that LLMs are cortex — and cortex alone is not enough.** Large language models excel at language, reasoning, and pattern matching over presented context, mirroring the neocortex's statistical learning engine. What they fundamentally lack are the subcortical systems that make human cognition adaptive: the hippocampus's one-shot episodic binding, the basal ganglia's procedural crystallization, the amygdala's salience gating, the cerebellum's forward models, and the consolidation machinery that transforms fleeting experiences into durable knowledge. This specification designs those complementary systems as programmable infrastructure built on Redis/Valkey with Popoto as the ORM abstraction layer, deriving every computational primitive from established neuroscience and cognitive science models.

The architecture follows a single organizing principle from Complementary Learning Systems theory: **fast writing and slow consolidation must be structurally separated** to prevent catastrophic interference. Episodes land instantly in a sparse, high-dimensional store (the hippocampal tier), while a background consolidation pipeline gradually extracts statistical regularities into a durable, generalized knowledge tier (the neocortical tier). Salience gates what enters, prediction error gates what consolidates, and power-law decay ensures graceful forgetting. Every component maps to Redis-native data structures — no external brokers, no Celery, no dependencies beyond what Redis already provides.

---

## PART 1: The brain's memory systems — what each actually computes

### 1.1 Episodic memory: fast writing into sparse, non-interfering traces

The hippocampus solves a problem that no single-learning-rate system can: **learning new information without overwriting old information**. McClelland, McNaughton, and O'Reilly (1995) formalized this as Complementary Learning Systems (CLS) theory. The core computational argument is precise — when a neural network with distributed, overlapping representations updates weights to encode a new pattern (A→B), those same weight changes disrupt previously stored patterns (X→Y). This is catastrophic interference. If the learning rate η is high enough for one-shot learning, the weight perturbation magnitude is sufficient to corrupt prior associations. If η is kept low to prevent interference, the system cannot learn specific episodes.

The brain's solution is architectural: **two systems with radically different representations and learning rates**. The hippocampus uses sparse coding (~2% of neurons active per memory) via the dentate gyrus's ~10 million granule cells (human), which expand entorhinal cortex input into a much higher-dimensional space. This sparse, pattern-separated representation means each new memory has minimal overlap with existing memories — interference is structurally prevented. The hippocampal learning rate is effectively **η ≈ 1** (one-shot Hebbian learning via strong "detonator" mossy fiber synapses). The neocortex, by contrast, uses dense, distributed, overlapping representations with a learning rate of approximately **η ≈ 0.001–0.01**, requiring many interleaved exposures to extract statistical structure without interference.

**Pattern separation** (dentate gyrus) orthogonalizes similar inputs so that overlapping experiences map to dissimilar neural codes. The computational primitive is dimensionality expansion followed by sparsification — essentially a random projection into a high-dimensional space with competitive inhibition enforcing ~2% sparsity. **Pattern completion** (CA3 recurrent collaterals) reconstructs full memory traces from partial cues. CA3's ~2.3 million pyramidal cells (human) each connect to ~12,000 other CA3 cells, forming an autoassociative (Hopfield-like) attractor network with an estimated capacity of **~36,000 distinct memories**. Pattern completion activates when cue overlap exceeds **~20–25%** of the original pattern.

**Consolidation** bridges the two systems. During NREM slow-wave sleep, hippocampal sharp-wave ripples (150–250 Hz) replay episode sequences at **5–20× temporal compression**, coordinated with cortical slow oscillations and thalamocortical sleep spindles. This triple coupling — slow oscillation up-state → spindle → sharp-wave ripple — drives hippocampal-to-neocortical information transfer. Recent 2025 research from *Trends in Neurosciences* (van der Meer and Bendor) reframes awake replay: rather than supporting online decision-making, **awake replay primarily performs offline fictive learning for future goal-oriented behavior** and tags memories for subsequent sleep consolidation. Post-learning replay is biased by reward prediction error magnitude (Roscow et al., *Nature Communications* 2025), not by reward per se — behavior was best predicted by a Q-learning model with RPE-biased replay, not random or reward-biased alternatives.

**The spacing effect** — why distributed practice creates stronger memories than massed repetition — is captured by Pavlik and Anderson's (2005) extension of the ACT-R base-level activation equation. The activation of memory chunk *i* at time *T* is:

```
B_i(T) = ln( Σ_{j=1}^{n} t_j^{-d_j} )
```

Where *t_j* is time since the *j*th access and *d_j* is the decay rate for that access. The critical innovation: **decay rate depends on activation at the time of re-access**:

```
d_j = c · e^{m_j} + a     (c ≈ 0.217, a ≈ 0.177)
```

When re-studying at high activation (massed practice), *m_j* is high → *d_j* is high → the new trace decays rapidly. When re-studying at low activation (spaced practice), *m_j* is low → *d_j* is low → the trace persists. This is not "access count" — it is the *distribution* of accesses over time, and it must be modeled per-access, not as a simple aggregate.

### 1.2 Procedural memory: the brain's learned policy engine

The basal ganglia implement **reinforcement-based action selection** through the cortico-BG-thalamo-cortical loop. Multiple candidate actions compete via two pathways: the direct pathway (D1 "Go" neurons disinhibit thalamus, facilitating the selected action) and the indirect pathway (D2 "NoGo" neurons maintain thalamic inhibition, suppressing competitors). Dopamine reward prediction errors (Schultz, Dayan, and Montague 1997) provide the teaching signal:

```
δ = r + γV(s') − V(s)     (temporal difference error)
```

**Positive RPE** (δ > 0, unexpected reward): dopamine burst strengthens Go pathway synapses via LTP, weakens NoGo via LTD → the rewarded action becomes more likely. **Negative RPE** (δ < 0, reward omission): dopamine pause weakens Go, strengthens NoGo → the action is suppressed. A 2025 *Nature* study revealed that dopamine in the tail of striatum encodes an **action prediction error** distinct from classic RPE, serving as a value-free teaching signal for habit formation.

The cerebellum implements a **supervised forward model**: given a motor command copy and current state, it predicts sensory consequences before feedback arrives. The sensory prediction error (SPE = actual − predicted) serves as the learning signal. With ~50 billion granule cells performing massive dimensionality expansion and ~15 million Purkinje cells as the sole cortical output, this is a supervised regression engine. The computational primitive is: Input(state, action) → predicted_next_state, trained by SPE via climbing fiber signals.

**Automaticity** follows Fitts and Posner's three stages: cognitive (slow, hippocampal/PFC-dependent), associative (errors decrease, BG increasingly involved), and autonomous (fast, automatic, BG/cerebellum-dominant). The transfer trigger is **repetition with consistent stimulus-response-outcome mappings**. Graybiel (1998) showed the neural signature: task-bracketing, where striatal neurons fire at sequence start and end but go silent during the middle, treating an entire multi-step sequence as a single chunk. Performance improves as a power law: *T = a · N^(-b)*, reflecting progressive chunking.

### 1.3 Semantic memory: where LLMs already excel, with one gap

LLMs *are* the neocortex's distributed semantic memory — they encode population-level statistical structure in connection weights built from gradual exposure to many examples. The fundamental gap is **personalized semantic memory**: each person's neocortex is shaped by their unique lifetime of consolidated episodes. An agent needs a personal semantic store gradually built from episodic experiences — extracting regularities like "this user prefers concise answers" or "deploy on Fridays causes problems" — via a consolidation pipeline that mirrors hippocampal-replay-driven neocortical learning.

The Thousand Brains Theory (Hawkins; Clay, Leadholm, Hawkins, arXiv 2412.18354, December 2024) proposes that every cortical column learns a complete object model using **reference frames** — explicit coordinate systems derived from grid cell mechanisms. The Monty framework implements this: objects are stored as graphs where nodes hold (location_in_reference_frame, features) pairs, and recognition proceeds by evidence accumulation across sensorimotor observations. Multiple learning modules reach consensus through lateral voting (intersecting hypothesis sets). For a database, this suggests **reference-frame-indexed memory** — not flat key-value pairs but spatially/structurally organized feature-at-location records enabling pose-invariant matching.

### 1.4 Salience and emotional tagging: what gets remembered

The basolateral amygdala (BLA) gates encoding strength via neuromodulatory amplification. BLA activation triggers locus coeruleus release of **norepinephrine** into the hippocampus, enhancing encoding and consolidation. This follows an inverted-U dose-response: moderate levels enhance memory; extreme levels impair it. The computational primitive is **multiplicative gain modulation**:

```
effective_learning_rate = η_base × (1 + α · S(x))
```

Where S(x) combines emotional arousal, novelty, reward relevance, and prediction error magnitude. Damasio's somatic marker hypothesis adds a second mechanism: the vmPFC stores learned associations between situations and past emotional outcomes as cached value lookups, bypassing deliberative reasoning. The Iowa Gambling Task demonstrates this — healthy participants generate anticipatory skin conductance responses before choosing "bad" options, even before conscious awareness.

The Miniature Brain Transformer (Jeong, 2025) implements salience as the **normalized Frobenius norm of retrieved context**: `s_t = ||r_t||_F / μ_s` where μ_s is a running mean. High-magnitude retrievals trigger preferential consolidation. This scalar gate computed from the L2 norm of retrieved context provides a fast, learnable salience signal.

### 1.5 Working memory: the context window that must be actively managed

The O'Reilly and Frank PBWM model describes working memory as ~3–5 independently maintained PFC "stripes," gated by the basal ganglia. The critical insight for implementation: **the brain's working memory is actively managed** — the BG learns WHEN to update each slot, WHAT to maintain, and WHEN to read out. This is fundamentally unlike LLM context windows, which passively accumulate all tokens equally. The memory system's job is to implement this gating policy — deciding what information to inject into the LLM's context, when to update it, and what to withhold.

---

## PART 2: Computational primitives — the operations a memory system needs

### 2.1 Encoding: binding what/where/when/who/how-it-felt into sparse traces

Encoding transforms a raw experience into a stored memory trace. The neuroscience demands three properties: **sparsity** (minimal overlap between memories to prevent interference), **binding** (linking disparate features — content, temporal context, emotional valence, agent state — into a coherent trace), and **salience-gated write strength** (important events get stronger traces).

The computational primitive for pattern separation in a key-value store is **high-dimensional sparse hashing**. Each episode gets a unique identifier (AutoKeyField UUID), but its retrievability is managed through multiple sorted set indexes rather than through the key itself. Binding is achieved by co-storing all contextual features in a single hash and maintaining sorted set cross-references for each dimension. Compression occurs via the LLM at encoding time — the raw experience is distilled to structured fields, not stored as full conversation transcript.

### 2.2 Consolidation: replay that extracts patterns and prevents interference

Consolidation is the **periodic batch process** that transforms episodic traces into durable knowledge. The neuroscience prescribes: (1) priority sampling biased by prediction error, salience, and reward relevance, (2) interleaved replay of old and new memories to prevent catastrophic interference (CLS dual learning rates), and (3) progressive abstraction — stripping context-specific details to extract generalizable patterns.

The optimal consolidation schedule for an AI agent need not mirror literal sleep cycles. The functional requirement is: **between active inference periods, process the episodic stream in batches, extract patterns, update procedural and semantic stores, and prune low-value episodes.** Redis Streams with consumer groups provide the native mechanism: episodes XADD to a consolidation stream on save, a background consumer group processes batches, and writes back extracted patterns as new Popoto model instances.

Consolidation triggers (the programmatic equivalent of neuromodulatory signals):
- **High prediction error events** — outcomes that differed significantly from expectations
- **Repeated pattern detection** — the same action sequence succeeding in similar contexts 3+ times
- **Temporal thresholds** — minimum idle time between consolidation runs (prevent over-consolidation)
- **Buffer fullness** — when the episodic replay buffer approaches capacity

### 2.3 Retrieval: cue-dependent recall with spreading activation

Retrieval in human memory is never "exact match lookup" — it is cue-dependent, context-sensitive, and competitive. The ACT-R framework provides the best implementable model:

```
A_i = B_i + Σ_j W_j · S_ji + P_i + ε_i
```

Where B_i is base-level activation (recency × frequency via power law), S_ji is spreading activation from contextual cues (diluted by fan: S_ji = S − ln(fan_j)), P_i is partial matching penalty, and ε_i is noise. Retrieval probability is sigmoidal: P(recall) = 1/(1 + e^((τ − A_i)/s)).

**Spreading activation** in a graph-like associative network can be implemented as BFS with exponential decay or, more robustly, via PersonalizedPageRank (PPR): `PPR(v) = α · e_s + (1−α) · Σ_{u→v} PPR(u)/outdegree(u)` with restart probability α ≈ 0.15. Redis sorted sets serve as adjacency lists with weights, and ZUNIONSTORE with WEIGHTS performs one-hop spreading.

**Retrieval-induced forgetting** (Anderson, Bjork, and Bjork 1994) means that retrieving Memory A actively suppresses competing memories B, C, D that share retrieval cues. This must be implemented: on retrieval, reduce the activation scores of competing items proportionally to their strength.

### 2.4 Forgetting: an active, essential process

Forgetting is not failure — it is adaptive compression. The system needs three mechanisms:

- **Decay**: Power-law reduction of activation scores over time. Score decay in sorted sets (not TTL deletion) preserves the episode hash while reducing retrievability. Decay function: score × (t/t_0)^(-d) with d ≈ 0.5.
- **Interference management**: When new similar memories are encoded, older similar memories receive activation penalties (proactive interference). Implemented via retrieval-induced forgetting on write.
- **Directed forgetting**: Explicit removal of memories marked for deletion (user request, policy compliance, error correction). Two-phase: mark for forgetting (reduce score below retrieval threshold), then garbage-collect in consolidation.

### 2.5 Metacognition: knowing what you know

The feeling-of-knowing (FOK) requires three computational mechanisms (Koriat and Levy-Sadot 2001):

- **Cue familiarity** (fast, pre-retrieval): "Have I encountered information related to this cue?" → Bloom filter or Count-Min Sketch. Bloom filters map directly to FOK: false positives possible (FOK can be wrong), but false negatives very rare ("I have no idea" is usually right).
- **Accessibility** (retrieval attempt): How much partial information surfaces during search? Count of subthreshold activations.
- **Trace access**: Is activation between the retrieval threshold and a lower awareness threshold?

Combined: `FOK_score = 0.4 · cue_familiarity + 0.4 · partial_retrieval_count + 0.2 · subthreshold_activation`

---

## PART 3: Design principles for complementing LLMs

### The division of labor

| Brain Region | Function | LLM Equivalent | Memory System Role |
|---|---|---|---|
| **Neocortex (associative)** | Semantic understanding, language, reasoning | **Core LLM strength** — don't compete | Provide the *right* context to reason over |
| **Hippocampus** | Episodic encoding, one-shot learning, temporal binding | **Fundamental gap** — no episodic memory | **Build**: Fast episodic store with temporal/contextual binding |
| **Basal Ganglia** | Procedural learning, habit formation, action selection | **Gap** — no runtime procedural learning | **Build**: Pattern → action crystallization with RL |
| **Cerebellum** | Forward models, prediction error correction | **Gap** — no outcome prediction from experience | **Build**: Prediction → outcome tracking, forward model |
| **Amygdala** | Salience gating, emotional tagging | **Gap** — no salience discrimination | **Build**: Multi-factor salience scoring, outcome valence tags |
| **vmPFC** | Somatic markers, cached decision heuristics | **Gap** — no emotion-tagged fast decisions | **Build**: Situation → valence lookup for decision biasing |
| **Thalamus** | Sensory gating, attention routing | **Partial** — attention exists but is passive | **Build**: Active retrieval gating policy |
| **Default Mode Network** | Consolidation, prospection, mental simulation | **Gap** — no offline processing | **Build**: Background consolidation pipeline |
| **Anterior Cingulate** | Conflict/error monitoring, prediction error | **Gap** — no cross-episode error tracking | **Build**: Prediction error computation and storage |
| **dlPFC** | Metacognition, confidence monitoring | **Weak** — poor self-calibration | **Build**: Per-memory confidence with Bayesian updates |

### What NOT to build

Do not build semantic understanding, language generation, text classification, entity extraction, summarization, or similarity computation. The LLM already handles these. Do not build a separate reasoning engine. The memory system is a **librarian, not a reader** — it organizes, stores, tracks, retrieves, gates, and forgets. The LLM does the understanding.

### What TO build

The seven capabilities that no existing system adequately provides:

1. **Temporal intelligence** — precise timestamping, temporal pattern detection, duration estimation, sequence ordering. LLMs show a 10–20% performance gap on temporal reasoning benchmarks.
2. **Outcome tracking** — prediction → outcome pairs with delta computation. The single biggest gap between current AI agents and adaptive human cognition.
3. **Salience-gated encoding** — fast, non-LLM-based multi-factor scoring at write time. Most systems store everything or use expensive LLM calls.
4. **Procedural crystallization** — detecting repeated successful patterns and converting them to cached routines. No production system implements this.
5. **Episodic → semantic consolidation** — gradual transformation of specific episodes into generalized knowledge via background processing.
6. **Confidence tracking** — per-memory Bayesian confidence with corroboration/contradiction updates.
7. **Forward models** — memory-guided outcome prediction before acting ("I tried something like this before and it failed because...").

---

## PART 4: Research directions and their computational implications

### 4.1 Numenta reference frames: structured knowledge beyond flat vectors

The Thousand Brains Project's Monty framework stores object knowledge as explicit graphs in 3D Cartesian space. Each node holds (location_in_reference_frame, feature_vector), and recognition proceeds by evidence accumulation over sensorimotor observations. Multiple learning modules vote by intersecting hypothesis sets.

**Computational implication for AI agent memory**: Knowledge should not be stored as flat feature vectors but as **features-at-locations within reference frames**. For an AI agent, "reference frames" are conceptual structures — a project has a timeline frame, a codebase has a dependency frame, a user has a preference frame. Memory nodes should store (position_in_frame, feature) pairs, enabling structured retrieval: "What do I know about this project's deployment phase?" retrieves nodes at the deployment position in the project timeline frame.

**Redis implementation sketch**: Each reference frame is a sorted set where scores encode position. Nodes are hashes with features. Cross-frame links are association sorted sets. Evidence accumulation uses atomic ZINCRBY across candidate object sorted sets.

### 4.2 CLS dual learning rates: the fast/slow architecture

The fast tier (Redis episodic store) and slow tier (consolidated patterns) must use different representations:

- **Fast tier**: Sparse, highly specific, context-rich episode hashes. Each episode is a complete trace with full binding. Stored immediately on experience. Decays following power law. Capacity-bounded (replay buffer with LTRIM or sorted set ZREMRANGEBYRANK).
- **Slow tier**: Dense, generalized, context-free pattern records. Extracted from episodic clusters during consolidation. Stable, low-decay. Grows slowly. Each pattern includes a confidence score, evidence count, and last-updated timestamp.

The 2016 CLS update (Kumaran, Hassabis, and McClelland) showed that information **consistent** with existing schemas can be rapidly integrated without the slow pathway. Implementation: if a new episode matches an existing consolidated pattern with high confidence, update the pattern directly (schema-consistent fast learning). If it conflicts, route through the full consolidation pipeline (schema-inconsistent slow learning).

### 4.3 Basal ganglia: reinforcement-based action selection over problem fingerprints

The procedural memory system implements tabular Q-learning over discretized problem states:

```python
# State representation: "problem fingerprint" = hash of (task_type, context_features, constraints)
# Action space: available strategies, tool sequences, approach choices
# Reward signal: outcome quality (0-1 scale from success/failure/partial)
# Update: Q(s,a) ← Q(s,a) + α · (r + γ · max_a' Q(s',a') - Q(s,a))
```

The state space must be discretized into meaningful "problem fingerprints" — not raw context strings but structured feature vectors capturing task type, complexity, constraints, and domain. This maps to Redis sorted sets where the key encodes the state and members are actions scored by Q-values.

### 4.4 Sleep research: prioritized replay via Redis Streams

The consolidation pipeline processes episodes in priority order, not FIFO. Priority ranking from neuroscience:

1. **High prediction error** — outcomes that most surprised the agent (|δ| > θ_surprise)
2. **Reward-relevant** — episodes tagged with strong positive or negative outcomes
3. **Novel** — episodes with features not matching existing consolidated patterns
4. **Gap-filling** — episodes that bridge gaps between existing knowledge clusters

Implementation: the consolidation stream uses a separate priority sorted set (`consolidation:priority:{agent_id}`) where episode IDs are scored by `priority = w_1 · |prediction_error| + w_2 · |reward_signal| + w_3 · novelty_score + w_4 · gap_score`. The consumer reads from this sorted set, not directly from the stream, ensuring priority ordering.

### 4.5 Amygdala research: the salience function

```python
def compute_salience(surprise: float, consequence: float, 
                     novelty: float, emotional_valence: float,
                     goal_relevance: float) -> float:
    """
    Compute salience score for gating memory encoding.
    All inputs normalized to [0, 1]. Output in [0, 1].
    
    Based on: amygdala responds to arousal (surprise + consequence),
    modulated by novelty and goal relevance.
    Emotional valence affects magnitude but not direction (both 
    positive and negative emotions enhance encoding).
    """
    arousal = 0.4 * surprise + 0.3 * abs(consequence)
    relevance = 0.2 * goal_relevance + 0.1 * novelty
    emotional_boost = 1.0 + 0.5 * abs(emotional_valence)  # inverted U simplified
    
    raw_salience = (arousal + relevance) * emotional_boost
    return min(1.0, raw_salience)  # Clamp to [0, 1]
```

The write-gate threshold is tunable: episodes with salience < θ_min (default 0.2) are not stored. Episodes with salience > θ_high (default 0.7) receive enhanced encoding (stored with richer context, higher initial activation score, and automatic consolidation tagging).

### 4.6 Cerebellum research: forward models for outcome prediction

Before acting, the agent queries its forward model: "Given state S and proposed action A, what outcome do I predict?" After acting, it observes actual outcome O and computes SPE = O − Ô. This prediction-error pair is stored and used to update the model.

```python
# Forward model as Redis sorted set of (state_hash, action) → predicted_outcome
# Key: fwd_model:{agent_id}:{state_fingerprint}
# Members: action IDs, scored by predicted outcome quality
# On observation: compute |actual - predicted|, store as learning signal
# Update: ZINCRBY to adjust prediction score toward actual outcome
```

---

## PART 5: Implementation architecture — Redis structures, Popoto models, and the ORM boundary

### 5.1 Memory type taxonomy and data models

#### Episodic Memory

```python
# APPLICATION LAYER model (built on Popoto primitives)
class Episode(popoto.Model):
    """A single experience trace — the hippocampal fast store."""
    
    # Identity
    episode_id = popoto.AutoKeyField()           # UUID, unique
    agent_id = popoto.KeyField()                  # Index: all episodes for agent
    
    # Content (the "what")
    content_summary = popoto.Field()              # LLM-compressed summary
    content_hash = popoto.Field()                 # For deduplication
    
    # Temporal binding (the "when")
    created_at = popoto.SortedField(type=float)   # ZADD to temporal index
    duration_ms = popoto.Field(type=int)          # How long the interaction took
    
    # Context binding (the "where/who/how")
    context_type = popoto.KeyField()              # conversation, tool_use, observation
    context_tags = popoto.Field()                 # JSON list of context tags
    
    # Emotional/salience binding (the "how-it-felt")
    salience = popoto.SortedField(type=float)     # ZADD to salience index
    emotional_valence = popoto.Field(type=float)  # [-1, 1]
    prediction_error = popoto.Field(type=float)   # |actual - expected|
    outcome_quality = popoto.Field(type=float)    # [0, 1]
    
    # Memory dynamics
    access_count = popoto.Field(type=int, default=0)
    last_accessed = popoto.SortedField(type=float)
    activation_score = popoto.SortedField(type=float)  # Composite ACT-R-style score
    
    # Consolidation state
    consolidation_status = popoto.Field(default="raw")  # raw|tagged|consolidated|archived
    consolidated_to = popoto.Field(null=True)     # FK to Pattern if consolidated
    
    class Meta:
        order_by = "-created_at"
```

**Storage strategy**: Each episode is a Redis Hash (`Episode:{episode_id}`). Five sorted set indexes maintained atomically via pipeline in on_save: temporal (`$SoF:Episode:created_at`), salience (`$SoF:Episode:salience`), activation (`$SoF:Episode:activation_score`), last_accessed (`$SoF:Episode:last_accessed`), and a per-agent temporal index (`episode:temporal:{agent_id}`).

**Write path**: (1) LLM compresses raw experience into structured fields. (2) Salience function computes score — if below θ_min, discard. (3) Activation score initialized as `ln(1)` = 0 (single access at t=0). (4) Atomic pipeline: HSET episode hash + ZADD to all indexes + XADD to consolidation stream + publish notification. (5) If salience > θ_high, also ZADD to priority consolidation set.

**Read/retrieval path**: (1) Compute query activation for each candidate via ACT-R equation. (2) ZRANGEBYSCORE on activation index for candidates above threshold τ. (3) Apply spreading activation from current context cues. (4) Apply partial matching penalties for non-exact cue matches. (5) Return top-K by final activation. (6) On retrieval: ZINCRBY access_count, update last_accessed, recompute activation_score, apply retrieval-induced forgetting to competitors.

**Consolidation**: Consumer group on consolidation stream reads episodes in batches. Clusters similar episodes (by content_hash similarity and context overlap). Extracts generalizable patterns → writes Pattern model. Updates consolidation_status. Archives fully consolidated low-salience episodes (reduce to skeleton: keep metadata, discard full content).

**Decay**: Periodic job (every N minutes) applies power-law decay to activation scores via Lua script:

```lua
-- Decay activation scores for all episodes of an agent
-- KEYS[1] = activation sorted set key
-- ARGV[1] = current_time, ARGV[2] = decay_exponent (0.5)
local members = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local now = tonumber(ARGV[1])
local d = tonumber(ARGV[2])
for i = 1, #members, 2 do
    local member = members[i]
    local created = tonumber(redis.call('HGET', member, 'created_at') or now)
    local accesses = tonumber(redis.call('HGET', member, 'access_count') or 1)
    local last = tonumber(redis.call('HGET', member, 'last_accessed') or created)
    -- Simplified ACT-R: B = ln(n * (now - created)^(-d))
    local age = math.max(1, now - created)
    local activation = math.log(accesses * math.pow(age, -d))
    redis.call('ZADD', KEYS[1], activation, member)
end
return #members / 2
```

#### Procedural Memory

```python
# APPLICATION LAYER model
class ProceduralRule(popoto.Model):
    """A crystallized context → action → outcome pattern — the basal ganglia."""
    
    rule_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    
    # State representation (the "when to fire")
    state_fingerprint = popoto.KeyField()     # Hash of discretized state features
    state_description = popoto.Field()        # Human-readable state description
    state_features = popoto.Field()           # JSON of state feature vector
    
    # Action (the "what to do")
    action_type = popoto.KeyField()           # tool_call, strategy, response_template
    action_specification = popoto.Field()     # JSON action details
    
    # Reinforcement (the "how well it works")
    q_value = popoto.SortedField(type=float)  # Q(s,a) — expected utility
    success_count = popoto.Field(type=int, default=0)
    failure_count = popoto.Field(type=int, default=0)
    total_trials = popoto.Field(type=int, default=0)
    
    # Automaticity tracking
    consistency_score = popoto.Field(type=float, default=0.0)  # [0,1]
    automaticity_level = popoto.Field(default="deliberate")     # deliberate|associative|automatic
    
    # Provenance
    source_episodes = popoto.Field()          # JSON list of episode IDs that generated this rule
    created_at = popoto.SortedField(type=float)
    last_fired = popoto.SortedField(type=float)
```

**Storage strategy**: Hash per rule. Sorted sets index by q_value (for action selection in state), by state_fingerprint (for lookup), and by automaticity level. The key data structure for action selection is a per-state sorted set: `procedural:actions:{agent_id}:{state_fingerprint}` where members are rule_ids scored by q_value.

**Write path (crystallization)**: Triggered when the consolidation pipeline detects an episodic pattern repeated ≥ **3 times** with consistent outcomes (consistency threshold). The statistical test: compute the success rate's 95% Wilson confidence interval — if the lower bound exceeds 0.6, crystallize. The new rule's initial Q-value is the empirical success rate from source episodes.

**Read path (action selection)**: Given current state fingerprint, ZREVRANGE the action sorted set to get top-K candidate actions by Q-value. Apply softmax selection with temperature: `P(select_i) = e^(Q_i/τ) / Σ_j e^(Q_j/τ)`. Return the selected action with its Q-value as confidence.

**Reinforcement update** (on outcome observation):

```lua
-- Q-value update via TD learning
-- KEYS[1] = rule hash, KEYS[2] = action sorted set
-- ARGV[1] = rule_id, ARGV[2] = reward, ARGV[3] = alpha, ARGV[4] = gamma, ARGV[5] = max_future_q
local reward = tonumber(ARGV[2])
local alpha = tonumber(ARGV[3])  -- 0.1 default
local gamma = tonumber(ARGV[4])  -- 0.95 default
local max_future_q = tonumber(ARGV[5])
local current_q = tonumber(redis.call('HGET', KEYS[1], 'q_value') or '0')

local td_error = reward + gamma * max_future_q - current_q
local new_q = current_q + alpha * td_error

redis.call('HSET', KEYS[1], 'q_value', tostring(new_q))
redis.call('HINCRBY', KEYS[1], 'total_trials', 1)
if reward > 0.5 then
    redis.call('HINCRBY', KEYS[1], 'success_count', 1)
else
    redis.call('HINCRBY', KEYS[1], 'failure_count', 1)
end
redis.call('ZADD', KEYS[2], new_q, ARGV[1])
return tostring(td_error)
```

#### Forward Model (Cerebellar)

```python
# APPLICATION LAYER model
class ForwardModel(popoto.Model):
    """Prediction → outcome pairs — the cerebellum's error correction."""
    
    model_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    
    state_fingerprint = popoto.KeyField()
    action_taken = popoto.Field()
    
    predicted_outcome = popoto.Field()        # What the agent expected
    actual_outcome = popoto.Field(null=True)  # What actually happened (filled post-action)
    prediction_error = popoto.SortedField(type=float, null=True)  # |actual - predicted|
    
    created_at = popoto.SortedField(type=float)
    resolved = popoto.Field(type=bool, default=False)
```

**Storage strategy**: Hash per prediction. Sorted set indexes by prediction_error (for prioritized learning) and by state_fingerprint (for lookup). The write path has two phases: (1) pre-action HSET with predicted_outcome, (2) post-action HSET actual_outcome + compute prediction_error + ZADD to error-sorted index.

#### Salience Tag

```python
# APPLICATION LAYER model (lightweight — often just sorted set entries)
class SalienceTag(popoto.Model):
    """Emotional/importance tag on a memory — the amygdala's output."""
    
    tag_id = popoto.AutoKeyField()
    target_id = popoto.KeyField()         # Episode, Pattern, or Rule ID
    target_type = popoto.KeyField()       # episode, pattern, rule
    agent_id = popoto.KeyField()
    
    salience_score = popoto.SortedField(type=float)  # [0, 1]
    emotional_valence = popoto.Field(type=float)      # [-1, 1]
    surprise_level = popoto.Field(type=float)         # [0, 1]
    goal_relevance = popoto.Field(type=float)         # [0, 1]
    consequence_magnitude = popoto.Field(type=float)  # [0, 1]
```

#### Consolidated Pattern (Semantic/Extracted Knowledge)

```python
# APPLICATION LAYER model
class ConsolidatedPattern(popoto.Model):
    """Extracted generalization from episodic clusters — neocortical knowledge."""
    
    pattern_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    
    pattern_description = popoto.Field()   # LLM-generated generalization
    pattern_type = popoto.KeyField()       # preference, fact, tendency, rule
    
    confidence = popoto.SortedField(type=float)     # Bayesian posterior [0, 1]
    evidence_count = popoto.Field(type=int, default=1)
    
    source_episode_count = popoto.Field(type=int)
    first_observed = popoto.SortedField(type=float)
    last_updated = popoto.SortedField(type=float)
    
    # Stability (CLS slow-learning tier)
    contradictions = popoto.Field(type=int, default=0)
    corroborations = popoto.Field(type=int, default=0)
```

**Confidence update** (conceptual design sketch — see note):

> **Note:** The shipped Popoto `ConfidenceField` uses a **capped-evidence Bayesian** rule, not the precision-weighted sqrt sketch below. The sketch is retained as a design exploration of the sqrt-precision approach; the actual implementation uses `n_eff = min(evidence_count + 1, cap)` with a fixed cap (default 20), giving an exact running mean within the window and bounded exponential forgetting beyond it. See [ConfidenceField docs](../features/confidence-field.md).

```lua
-- Conceptual sketch: precision-weighted Bayesian update
-- NOTE: not the actual Popoto implementation (see ConfidenceField docs)
-- KEYS[1] = pattern hash key
-- ARGV[1] = new_evidence (0 or 1 for contradiction/corroboration), ARGV[2] = evidence_weight
local key = KEYS[1]
local evidence = tonumber(ARGV[1])  -- 1 = corroboration, 0 = contradiction
local weight = tonumber(ARGV[2])    -- evidence strength [0, 1]

local prior = tonumber(redis.call('HGET', key, 'confidence') or '0.5')
local n = tonumber(redis.call('HGET', key, 'evidence_count') or '1')

-- Precision-weighted update (design exploration)
local prior_precision = math.sqrt(n)
local likelihood_precision = weight

local posterior = (prior_precision * prior + likelihood_precision * evidence) 
                  / (prior_precision + likelihood_precision)
posterior = math.max(0.01, math.min(0.99, posterior))

redis.call('HSET', key, 'confidence', tostring(posterior))
redis.call('HINCRBY', key, 'evidence_count', 1)
if evidence > 0.5 then
    redis.call('HINCRBY', key, 'corroborations', 1)
else
    redis.call('HINCRBY', key, 'contradictions', 1)
end
redis.call('HSET', key, 'last_updated', tostring(ARGV[3] or os.time()))
return tostring(posterior)
```

### 5.2 The interaction model: how memory types feed each other

The memory types form a directed graph of information flow:

```
[Experience] → SALIENCE GATE → [Episodic Store]
                                    |
                         ┌──────────┼──────────┐
                         ↓          ↓          ↓
                   [Consolidation Stream]      [Forward Model]
                         |                         |
                    ┌────┴────┐              [Prediction Error]
                    ↓         ↓                    |
           [Consolidated   [Procedural         [Salience
            Patterns]       Rules]              Signal]
                    ↓         ↓                    |
              [Semantic    [Action              [Priority
               Index]       Selection]           Replay Queue]
                    ↓         ↓                    |
              ┌─────┴─────────┴────────────────────┘
              ↓
        [RETRIEVAL ENGINE]  ←  [Current Context/Cues]
              ↓
        [WORKING MEMORY ASSEMBLY]  →  [LLM Context Window]
```

**Episodic → Procedural**: When consolidation detects ≥3 similar episodes with consistent outcomes (same state fingerprint, same action type, success rate lower CI bound > 0.6), it crystallizes a ProceduralRule. The source episodes are linked. The rule starts at "deliberate" automaticity and promotes to "automatic" after 10+ consistent firings.

**Salience → Consolidation**: The salience score computed at encoding time directly sets the episode's priority in the consolidation queue. High-salience episodes are processed first. Episodes below θ_min are never consolidated — they decay naturally.

**Forward Model → Salience**: Large prediction errors (|actual − predicted| > θ_surprise) generate high salience signals, tagging the episode for priority consolidation. This closes the loop: surprising outcomes drive learning.

**Confidence → Retrieval**: Retrieval scores are modulated by confidence: `retrieval_weight = activation × confidence`. Low-confidence memories are retrievable but contribute less to the assembled context. The LLM receives confidence annotations so it can reason about uncertainty.

**Consolidation → Forgetting**: Fully consolidated episodes with low salience and no unique contextual information are candidates for archival (reduce to skeleton metadata) or deletion. The consolidation process maintains a "representativeness" score — if an episode is fully represented by its parent consolidated pattern, it can be archived.

### 5.3 ORM abstractions: what Popoto should provide

#### New field mixins

**ActivationFieldMixin** — ORM LAYER

Maintains ACT-R-style base-level activation automatically.

```python
class ActivationFieldMixin:
    """
    Mixin for SortedField that maintains ACT-R base-level activation.
    
    On each access (get/retrieve), records timestamp and recomputes activation.
    Key pattern: $ActF:{ClassName}:{field_name} → sorted set (member=pk, score=activation)
    Secondary: $ActF:{ClassName}:{field_name}:access_log:{pk} → list of access timestamps
    
    Hooks:
      on_access(instance, pipeline=None): Appends timestamp, recomputes B_i
      on_save(instance, pipeline=None): Initial activation = 0 (ln(1))
    """
    decay_exponent: float = 0.5  # ACT-R default d parameter
    max_access_log: int = 100    # Cap access history (LTRIM)
    
    def compute_activation(self, access_timestamps: list[float], now: float) -> float:
        """B_i = ln(Σ (now - t_j)^(-d))"""
        total = sum(max(1.0, now - t) ** (-self.decay_exponent) for t in access_timestamps)
        return math.log(max(1e-10, total))
    
    def on_access(self, instance, pipeline=None):
        p = pipeline or instance._redis.pipeline()
        now = time.time()
        log_key = f"$ActF:{instance.__class__.__name__}:{self.field_name}:access_log:{instance.pk}"
        p.rpush(log_key, str(now))
        p.ltrim(log_key, -self.max_access_log, -1)  # Cap at max_access_log
        # Recompute activation (simplified: use count and age)
        p.hincrbyfloat(instance._key, 'access_count', 1)
        p.hset(instance._key, 'last_accessed', str(now))
        if not pipeline:
            p.execute()
```

**SalienceFieldMixin** — ORM LAYER

Gates writes via on_save hook and maintains a salience-sorted index.

```python
class SalienceFieldMixin:
    """
    Mixin that gates episode storage based on salience threshold.
    
    Key pattern: $SalF:{ClassName}:{field_name} → sorted set (member=pk, score=salience)
    
    Hooks:
      on_save(instance, pipeline=None): 
        - Computes salience from instance fields (surprise, consequence, novelty, valence)
        - If salience < threshold, raises SkipSaveException
        - Otherwise, ZADD to salience index
    
    Configuration:
      salience_threshold: float = 0.2
      salience_fields: dict mapping component names to instance field names
    """
    salience_threshold: float = 0.2
    high_salience_threshold: float = 0.7
    
    def on_save(self, instance, pipeline=None):
        salience = self._compute_salience(instance)
        if salience < self.salience_threshold:
            raise SkipSaveException("Below salience threshold")
        
        p = pipeline or instance._redis.pipeline()
        zset_key = f"$SalF:{instance.__class__.__name__}:{self.field_name}"
        p.zadd(zset_key, {instance.pk: salience})
        
        # Tag for priority consolidation if high salience
        if salience > self.high_salience_threshold:
            priority_key = f"consolidation:priority:{instance.agent_id}"
            p.zadd(priority_key, {instance.pk: salience})
        
        if not pipeline:
            p.execute()
```

**AssociationFieldMixin** — ORM LAYER

Manages weighted edges between model instances as sorted sets (the "association cortex").

```python
class AssociationFieldMixin:
    """
    Maintains bidirectional weighted associations between instances.
    
    Key pattern: $AssocF:{ClassName}:{field_name}:{pk} → sorted set of associated pks with weights
    
    Methods:
      associate(source_pk, target_pk, weight, pipeline=None): ZADD to both directions
      strengthen(source_pk, target_pk, delta, pipeline=None): ZINCRBY both directions
      get_associated(pk, min_weight=0.0, limit=10): ZREVRANGEBYSCORE
      spread_activation(pk, depth=2, decay=0.7, threshold=0.1): BFS with decay
    """
    symmetric: bool = True  # Bidirectional by default
    max_associations: int = 1000  # Cap per node
    
    def associate(self, source_pk, target_pk, weight, pipeline=None):
        p = pipeline or self._redis.pipeline()
        fwd_key = f"$AssocF:{self.class_name}:{self.field_name}:{source_pk}"
        p.zadd(fwd_key, {target_pk: weight})
        if self.symmetric:
            rev_key = f"$AssocF:{self.class_name}:{self.field_name}:{target_pk}"
            p.zadd(rev_key, {source_pk: weight})
        if not pipeline:
            p.execute()
    
    def strengthen(self, source_pk, target_pk, delta, pipeline=None):
        """Hebbian: co-accessed items strengthen their association."""
        p = pipeline or self._redis.pipeline()
        fwd_key = f"$AssocF:{self.class_name}:{self.field_name}:{source_pk}"
        p.zincrby(fwd_key, delta, target_pk)
        if self.symmetric:
            rev_key = f"$AssocF:{self.class_name}:{self.field_name}:{target_pk}"
            p.zincrby(rev_key, delta, source_pk)
        if not pipeline:
            p.execute()
```

**ConsolidationStreamMixin** — ORM LAYER

Automatically writes to a Redis Stream on save for the consolidation pipeline.

```python
class ConsolidationStreamMixin:
    """
    Mixin for Model that XADDs to consolidation stream on every save.
    
    Key pattern: stream:consolidation:{agent_id} → Redis Stream
    
    on_save hook signature:
      on_save(self, instance, pipeline=None):
        pipeline.xadd(stream_key, {
            "model": ClassName,
            "pk": instance.pk,
            "salience": instance.salience,
            "prediction_error": instance.prediction_error,
            "timestamp": now
        }, maxlen=10000, approximate=True)
    """
    stream_maxlen: int = 10000
    
    def on_save(self, instance, pipeline=None):
        p = pipeline or instance._redis.pipeline()
        stream_key = f"stream:consolidation:{instance.agent_id}"
        entry = {
            "model": instance.__class__.__name__,
            "pk": str(instance.pk),
            "salience": str(getattr(instance, 'salience', 0.5)),
            "prediction_error": str(getattr(instance, 'prediction_error', 0.0)),
            "timestamp": str(time.time())
        }
        p.xadd(stream_key, entry, maxlen=self.stream_maxlen, approximate=True)
        if not pipeline:
            p.execute()
```

#### New query methods

**activation_query()** — ORM LAYER

Retrieves memories by composite activation score (base-level + spreading + partial match).

```python
# Proposed Popoto query method
results = Episode.query.activation_query(
    agent_id="agent_1",
    cues={"context_type": "code_review", "tags": ["python", "testing"]},
    spreading_sources=["concept:python", "concept:testing"],
    threshold=-2.0,       # ACT-R retrieval threshold
    noise=0.4,            # ACT-R noise parameter
    limit=5
)
# Returns: List[Episode] ranked by A_i = B_i + S_i + P_i + ε_i
```

**computed_sort()** — ORM LAYER

Enables composite scoring at query time by combining multiple sorted field indexes.

```python
# Composite retrieval score from multiple dimensions
results = Episode.query.computed_sort(
    agent_id="agent_1",
    weights={
        "activation_score": 0.4,   # Base-level activation
        "salience": 0.3,           # Importance
        "last_accessed": 0.2,      # Recency
        "confidence": 0.1          # How sure we are
    },
    aggregate="SUM",
    limit=10
)
# Implementation: ZUNIONSTORE with weights, then ZREVRANGE
```

#### New hook signatures

```python
class Model:
    def on_save(self, pipeline: redis.client.Pipeline = None) -> None:
        """Called after HSET, before pipeline.execute(). 
        Add secondary index updates, stream events, notifications."""
        pass
    
    def on_delete(self, pipeline: redis.client.Pipeline = None) -> None:
        """Called before entity deletion. Clean up indexes, associations, stream entries."""
        pass
    
    def on_access(self, pipeline: redis.client.Pipeline = None) -> None:
        """Called on read/retrieval. Update activation, access count, last_accessed.
        Implement retrieval-induced forgetting for competitors."""
        pass
    
    def on_consolidate(self, pipeline: redis.client.Pipeline = None) -> None:
        """Called when episode is processed by consolidation pipeline.
        Update consolidation_status, link to patterns."""
        pass
```

### 5.4 The ORM vs. application layer boundary

| Feature | Layer | Rationale |
|---|---|---|
| **SortedField, KeyField, AutoKeyField** | ORM | Core data access primitives |
| **ActivationFieldMixin** | ORM | Generic decay-scored sorted set — reusable across any model |
| **SalienceFieldMixin** | ORM | Generic gated write with threshold — reusable pattern |
| **AssociationFieldMixin** | ORM | Generic weighted edge management — fundamental graph primitive |
| **ConsolidationStreamMixin** | ORM | Generic stream-on-save — reusable event sourcing pattern |
| **on_save/on_delete/on_access hooks** | ORM | Framework-level extension points |
| **computed_sort()** | ORM | ZUNIONSTORE with weights — generic query capability |
| **Pipeline parameter threading** | ORM | Transactional consistency — must be in ORM |
| **Lua script execution helper** | ORM | `Model.execute_lua(script, keys, args)` — generic capability |
| **Bloom filter field** | ORM | `ExistenceFilter` — Lua-based Bloom filter via SETBIT/GETBIT (no Redis modules) |
| **Episode model** | Application | Domain-specific schema; too specific for ORM |
| **ProceduralRule model** | Application | Domain-specific; combines multiple ORM primitives |
| **ForwardModel model** | Application | Domain-specific prediction tracking |
| **ConsolidatedPattern model** | Application | Domain-specific knowledge extraction |
| **SalienceTag model** | Application | Domain-specific valence tagging |
| **Consolidation pipeline/consumer** | Application | Business logic for pattern extraction, clustering, crystallization |
| **Salience computation function** | Application | Domain-specific weighting of surprise, consequence, novelty |
| **Q-value update logic** | Application | Domain-specific RL parameters and reward signals |
| **Bayesian confidence update** | Application (uses ORM Lua helper) | Domain-specific prior/likelihood definitions |
| **Activation decay batch job** | Application | Scheduling, agent-specific parameters |
| **Retrieval assembly for LLM context** | Application | Domain-specific context budget and ranking |
| **Forward model training** | Application | Domain-specific state/action/outcome definitions |
| **Chunking/pattern detection** | Application | Statistical analysis over episode clusters |

**The principle**: Popoto provides **generic, reusable data primitives** (activation-decayed sorted sets, gated writes, weighted associations, stream-on-save, composite scoring, Bloom filters). The application layer composes these primitives into **domain-specific memory systems** (episodes, procedures, forward models, consolidation logic). If a feature could be useful in any Redis-backed model (not just AI memory), it belongs in Popoto. If it encodes domain-specific semantics about memory, it belongs in the application.

### 5.5 Key Redis patterns summary

| Memory Operation | Redis Implementation | Key Pattern |
|---|---|---|
| Episode storage | Hash | `Episode:{episode_id}` |
| Temporal index | Sorted Set (score=timestamp) | `$SoF:Episode:created_at:{agent_id}` |
| Activation index | Sorted Set (score=B_i) | `$ActF:Episode:activation:{agent_id}` |
| Salience index | Sorted Set (score=salience) | `$SalF:Episode:salience:{agent_id}` |
| Access log per episode | List (capped) | `$ActF:Episode:access_log:{episode_id}` |
| Association weights | Sorted Set (score=weight) | `$AssocF:Episode:associations:{episode_id}` |
| Category membership | Set | `category:{category}:{agent_id}` |
| Consolidation stream | Stream | `stream:consolidation:{agent_id}` |
| Priority replay queue | Sorted Set (score=priority) | `consolidation:priority:{agent_id}` |
| Procedural actions per state | Sorted Set (score=Q-value) | `procedural:actions:{agent_id}:{state_fp}` |
| Forward model predictions | Sorted Set (score=pred_error) | `fwd_model:errors:{agent_id}` |
| Concept familiarity | Bloom Filter (Lua + SETBIT/GETBIT) | `$EF:{ClassName}:{field_name}` |
| Concept frequency | Count-Min Sketch (Lua + HINCRBY/HGET) | `$FS:{ClassName}:{field_name}` |
| Most accessed memories | Top-K | `topk:accessed:{agent_id}` |
| Notifications | Pub/Sub | `memory:events:{agent_id}` |
| Atomic updates | Lua scripts | Named scripts registered at startup |

---

## What's novel in this design

### 1. Activation-dependent decay rates — not just recency × frequency

No existing AI memory system implements the Pavlik and Anderson (2005) spacing effect model where the **decay rate of each access trace depends on the activation at the time of that access**. Current systems use simple exponential decay or recency weighting. This design tracks per-access timestamps and computes decay rates as `d_j = 0.217 · e^{m_j} + 0.177`, meaning memories accessed when already strong decay faster than memories accessed when weak. This single mechanism naturally produces the spacing effect: reviewing a memory just before forgetting (low activation → low decay) creates more durable traces than reviewing it immediately (high activation → high decay).

### 2. Salience-gated encoding that operates below the LLM

Every existing system either stores everything (LangChain buffers) or uses expensive LLM calls to decide importance (MemGPT). This design computes salience as a fast numerical function of surprise, consequence magnitude, novelty, emotional valence, and goal relevance — **no LLM call required**. The gate operates at the ORM level via the SalienceFieldMixin's on_save hook, before the data even reaches the consolidation stream. This is three orders of magnitude cheaper than LLM-based importance scoring.

### 3. Retrieval-induced forgetting as a memory sharpening mechanism

No AI agent memory system implements competitive memory dynamics. In this design, retrieving Memory A actively suppresses competing memories (those sharing retrieval cues) by reducing their activation scores proportionally to their strength. Implemented as a ZINCRBY with negative delta on competitors during the on_access hook. Over time, this naturally sharpens the memory store, preventing confusion between similar episodes and implementing attention focusing at the memory level.

### 4. True episodic → semantic consolidation via Redis Streams

While BMAM (Jan 2026) proposes consolidation theoretically, no production system implements it. This design uses Redis Streams with consumer groups as the consolidation pipeline — episodes XADD on save, a background consumer processes batches, clusters similar episodes, extracts generalizable patterns via LLM call, writes ConsolidatedPattern instances, and updates episode consolidation status. The architecture is fully Redis-native: no Celery, no external message broker, no additional infrastructure.

### 5. Forward models for outcome prediction before acting

No existing agent memory system supports prospection. This design's ForwardModel tracks prediction → outcome pairs with explicit prediction error computation. Before acting, the agent queries: "Given state S and proposed action A, what outcomes have I seen before?" After acting, it computes |actual − predicted| and stores the delta as a learning signal. High prediction errors automatically elevate salience, closing the loop between surprise and learning.

### 6. Metacognition via probabilistic data structures

The use of Bloom filters for "feeling of knowing" and Count-Min Sketches for concept frequency approximation provides O(1) metacognitive queries: "Have I encountered this concept before?" (Bloom filter — false positives possible, false negatives very rare, precisely matching the FOK phenomenon) and "How often have I dealt with this topic?" (CMS — approximate frequency with bounded error). This enables the agent to signal confidence at the memory-system level before any retrieval attempt.

### 7. Prediction error as the universal consolidation currency

Following the 2025 *Nature Communications* finding that replay is biased by RPE rather than reward, this design uses |prediction_error| as the primary consolidation priority signal. This is a departure from systems that prioritize by recency, reward, or access frequency. The implication: **the most informative memories — those that surprised the agent — are what drive learning, not the most recent or most rewarding ones.**

---

## Conclusion: from neuroscience primitives to implementable infrastructure

This design specification derives seven computational systems from established neuroscience — episodic encoding with pattern separation, CLS-based consolidation, RL-based procedural crystallization, cerebellar forward models, amygdaloid salience gating, somatic marker caching, and metacognitive monitoring — and maps each to Redis-native data structures through Popoto ORM abstractions. The key architectural decisions are: (1) fast episodic writes to Hash + sorted set indexes with salience gating at the ORM layer, (2) background consolidation via Redis Streams consumer groups extracting patterns into a durable semantic tier, (3) activation-dependent power-law decay computed via Lua scripts for the spacing effect, (4) Q-learning over discretized state fingerprints for procedural crystallization, (5) prediction-error-prioritized replay for consolidation scheduling, and (6) Lua-based Bloom filter/CMS metacognition for fast "feeling of knowing" queries (implemented via core Redis commands for Valkey compatibility).

The ORM boundary is drawn clearly: Popoto provides five new mixins (ActivationFieldMixin, SalienceFieldMixin, AssociationFieldMixin, ConsolidationStreamMixin, ExistenceFilter), hook extensions (on_access, on_consolidate), composite query methods (computed_sort, activation_query), and Lua script execution helpers. The application layer builds domain-specific models (Episode, ProceduralRule, ForwardModel, ConsolidatedPattern, SalienceTag) and implements the consolidation pipeline, salience computation, Q-value updates, and retrieval assembly logic. Every component is independently testable, every sorted set is inspectable, and every Lua script is atomic. The result is a programmable memory infrastructure that gives AI agents the subcortical capabilities LLMs lack — not by replicating the brain, but by implementing its computational principles in the data structures Redis already provides.