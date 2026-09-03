# Changelog

All notable changes to Popoto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The pytest plugin is opt-in** — it still ships as a `pytest11` entry point, but it now does nothing until `popoto_test_db` is set in the pytest ini options or `POPOTO_TEST_DB` is exported. Previously any project that merely depended on popoto had DB 15 of its `REDIS_URL` flushed before every one of its own tests. This repository opts in through `pyproject.toml`; add the same one line to yours.
- **`popoto.integrations` refuses to write agent memory to Redis database 0** ([#584](https://github.com/tomcounsell/popoto/issues/584)) — `DEFAULT_URL` is unchanged; constructing a `MemoryService` whose effective database is 0 raises `Db0RefusedError` (a `ValueError`, so the hook, MCP dispatcher and `doctor` all surface it as a message rather than a traceback). The error names the variable to change and suggests an empty database. `POPOTO_MEMORY_ALLOW_DB0=1` is the deploy-level opt-in.
- **The harness hook stops after the first connection failure** — one Redis outage no longer costs the user's prompt five sequential 5-second timeouts. When the integration binds its own connection (`POPOTO_MEMORY_URL` or `REDIS_URL` set) it uses a 1-second socket and connect timeout and no redis-py retries; an in-process host that configured its own connection keeps its settings. `MemoryService` short-circuits every later operation in that process once a `ConnectionError`/`TimeoutError` has been recorded, and the shipped Claude Code and Codex hook configs carry an explicit 10-second `timeout`.
- **Recipes let outages propagate** — `SubconsciousMemory.inject_context`, `extract_memories`, `report_outcomes` and every `ContextAssembler` retrieval path re-raise `redis.exceptions.ConnectionError`/`TimeoutError` instead of logging them and returning an empty result. A dead server was previously indistinguishable from "no relevant memories". Retrieval-quality failures still degrade as before; the harness boundary (`hooks.run`, the MCP dispatcher) is where outages are caught.
- **`DefaultMemory` caps records per agent** — `Defaults.DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT` (1000). Past the cap, each save evicts the stalest records by decay timestamp with a full `delete()`. Nothing on the default path evicted before, so a long-lived install grew one record per turn forever. Override `_max_records_per_agent` on a subclass to change it.
- **Lua scripts are registered once and invoked with `EVALSHA`** — every call site that sent script source with `EVAL` on each invocation (38 of them, including the 7 KB decay scorer on every `top_by_decay`) now goes through `popoto.redis_db.run_lua`, which caches a redis-py `Script` per script text and falls back to loading on `NOSCRIPT`. Inside a pipeline the SHA is loaded once per process and `EVALSHA` is queued directly, so pipelines do not pay redis-py's per-execute `SCRIPT EXISTS`; a `SCRIPT FLUSH` between that load and a pipeline's `execute()` surfaces as `NoScriptError` from that execute.
- **Competitive suppression is one pipeline** — `ContextAssembler` used to issue `EXISTS` + `EVAL` sequentially for every fetched-but-not-selected candidate on every turn. `ConfidenceField.update_confidence` now honors its `pipeline` argument (the existence check moved into the Lua script), so a turn's suppressions ride the post-effects pipeline. Measured with 60 seeded memories and 10 injected, counting each command and each pipeline `execute()` as one round trip: 25 per `inject_context` down to 8.
- **Field index namespace collisions are refused at class definition** — `FieldBase` derives `field_class_key` with `str.strip("Field")`, which strips a character set rather than a suffix (`FloatField` indexes under `$oatF`), so two unrelated class names could fold onto one on-disk namespace silently. The spelling is frozen, since it is on disk for every deployment including downstream subclasses; defining a second field class that folds onto an existing namespace now raises `TypeError` naming the owner, and a class can set `field_class_key` explicitly. No stored key changes.
- **`MemoryService.status()` redacts the connection URL's password** — it feeds the MCP `memory_status` tool and `doctor`, both of which land in transcripts. `url_source` (which variable produced the URL) is now reported alongside.
- **`enable_error_reporting()` sets `send_default_pii=False`.**

### Fixed

- **`Meta.ttl` no longer leaves permanent index ghosts** — `EXPIRE` applies to the hash only, so after expiry the class set, key-field pointer sets and sorted-set indexes still held the member and every query logged an error pointing at a `repair_indexes()` that did not exist. A read that finds a member without a hash now purges it from every index derivable from the key alone (class set, non-auto `KeyField` sets, sorted sets partitioned by key fields) in one pipeline of per-key Lua calls that re-check `EXISTS` first, so a record re-created under the same key mid-purge keeps its fresh entries. `IndexedField`/`UniqueField`/`TagField` sets, `GeoField`, `Relationship` reverse indexes and sorted sets partitioned on non-key fields still need `clean_indexes()`, which the message now names (`repair_indexes()` never existed).
- **`query.get(field=...)` executed the same query three times** — the `QueryBuilder` it built re-ran on `len()`, `len()` and `[0]`. It now materializes once with `limit=2`.

- **`fuse()` now raises `QueryException` on Q-object filters** ([#576](https://github.com/tomcounsell/popoto/issues/576)) — **breaking for direct callers.** `fuse()` applies its builder's filters to the fused set (see Fixed, below), but a Q object cannot be resolved to a key set there. Fusing an unscoped set under a query that *reads* as filtered is exactly the cross-agent leak that scoping exists to prevent, so `fuse()` refuses rather than silently widening. `Model.query.filter(Q(...)).fuse(...)` now raises where it previously returned unscoped results. Use keyword filters, or a `post_filter` callback. `fuse()` with no filters on the builder is byte-for-byte unchanged.

- **A `KeyField(type=datetime)` row's identity is now the instant, not the representation** ([#537](https://github.com/tomcounsell/popoto/issues/537), [#538](https://github.com/tomcounsell/popoto/issues/538)) — `DB_key` built both the primary hash key and the `$KeyF:` index key from `str(value)`. A `datetime`'s `str()` carries the UTC offset when the value is aware and omits it when it is naive, so a row's Redis identity depended on how its value happened to decode. Keys are now derived through `canonical_key_str`, which normalizes to UTC and renders a fixed-width `2026-08-07T05:00:00.123456Z`. Every non-`datetime` key value is byte-identical to 1.8.2; `date` and `time` are untouched. The **stored value** still preserves the caller's exact offset (#521) — key and value are deliberately different projections.
  - **Version boundary — read this before a mixed-version rollout:** keys written by Popoto **≥ 1.9.0** for `KeyField(type=datetime)` values are **not** found by Popoto **< 1.9.0**, and the symptom is an empty query result rather than an error. Roll readers forward before writers. `POPOTO_DATETIME_KEY_LEGACY=1` (or `Defaults.DATETIME_KEY_LEGACY = True`) reproduces 1.8.2 key bytes exactly, so "roll readers forward" can be done independently of "move key bytes".
  - **Read before upgrading — this merges identities.** An aware `12:00+07:00`, its UTC equivalent `05:00Z`, and the equivalent naive `05:00` now address **one** row. A deployment that treated a naive value and an aware UTC value as two distinct rows will find them collapsed. `Model.audit_datetime_keys()` reports every such pair before anything moves, and `Model.migrate_datetime_keys()` refuses to run while one exists. (`SortedField` has scored those three identically since #519, so the system has treated them as one instant since 1.7.x.)
  - **Legacy offset-free rows are now decoded as UTC** ([#537](https://github.com/tomcounsell/popoto/issues/537)) — proposed in #521, implemented, reverted, and deferred, because stamping UTC shifted `str()` and therefore duplicated every legacy row on its next save. Canonical keys remove that coupling. The legacy branch is selected by an anchored shape test rather than by `fromisoformat` raising, because `fromisoformat` parses the legacy shape on 3.11+ but not on the 3.10 floor. Code comparing a reloaded legacy value against a naive `datetime.now()` will now raise `TypeError`.
  - **1.8.2's CHANGELOG said "no offline migration is required (and none is possible)"** — that was true only because the assumption was dropped. A migration is possible and is now provided.

### Added

- **Auditable extraction: opt-in candidate generator, enum-verdict LLM stage, and per-candidate decision log** ([#562](https://github.com/tomcounsell/popoto/issues/562)) — `SubconsciousMemory(auditable_extraction=AuditableExtractionConfig(...))` replaces the "extract and maybe drop" step of the existing extraction providers with three deterministic stages: `popoto.extraction.candidates.generate_candidates()` (pure, LLM-free sentence/entity enumeration), the M2 firewall (`scan_never_record()`) run per candidate, and `popoto.extraction.verdict` (an LLM call that may reply only `{candidate_id, verdict, reason_code}` — enum fields, never free text). `popoto.extraction.decision_log.DecisionLog` gives every candidate exactly one terminal state (`firewall_drop | accept | reject | withhold`), so a dropped candidate is a queryable row instead of a `logger.warning`. The default extraction path (and `extract_memories()` without the flag) is byte-for-byte unchanged.
  - **`accept` is a two-phase write** — a `pending` decision row is committed before `ProvenanceJournal.append()`, and the same row is transitioned to a terminal state after, so no candidate ever reaches the journal with zero decision-log rows and no crash between the two writes loses the candidate's identity. Every terminal write, including the pre-LLM `firewall_drop`, goes through one conditional Lua `EVAL` that refuses to overwrite a row already terminal `accept` with a non-empty `entry_id` — the guard a non-deterministic LLM verdict retry needs so it can never split the decision log from the journal.
  - **`DecisionRecord`** is composite-keyed (`agent_id` + `turn_id` + `candidate_id`, all `KeyField`s — deliberately no `AutoKeyField`) so re-saving transitions the row in place. `DecisionLog.compute_metrics()` computes precision/recall/F1 from the decision log alone, no journal read and no separate eval harness required.
  - **Decision-log detail rows are unbounded in v1** — no `LTRIM`, no cap, and no TTL, since a TTL would delete the audit evidence this module exists to keep. A `pending` row that outlives its writing process is recoverable manually via `DecisionLog.list_pending(agent_id, older_than=...)`; a periodic sweep/alert is deferred to M9 ([#568](https://github.com/tomcounsell/popoto/issues/568)).
  - New docs: [Auditable Extraction](https://popoto.io/features/auditable-extraction/).
- **Provenance journal: append-only memory entries with confirm/supersede/retract annotations** ([#560](https://github.com/tomcounsell/popoto/issues/560)) — `AppendOnlyMixin` (exported as `popoto.AppendOnlyMixin`) plus `JournalEntry` and the `ProvenanceJournal` façade (both in `popoto.recipes`) record *what an agent was told and by whom*, as a log that only ever grows. A correction is a new record naming the old one, never an edit: `save()` raises `AppendOnlyViolation` on an existing key, on `migrate_key=True`, and on a set `obsolete_redis_key`; `delete()` always raises, which also covers `delete_all()`/`bulk_delete()` since both route through the instance method. Annotations are reified — `confirm`/`supersede`/`retract` each append a real entry with a `target` pointer — so "who disputed this, and when" survives, where a boolean flag would not. `ProvenanceJournal.append/confirm/supersede/retract` return an `AnnotationResult`, and `annotations_for()`/`chain()` read the provenance back.
  - **Supersession is coupled to `ValidityField` in one transaction.** A `supersede()` or `retract()` queues the annotation append and the target's interval close into a single `MULTI`/`EXEC`, so the claim stops entering default context assembly the moment the correction lands. The atomicity claim is deliberately narrow: **no interleaving reader observes the annotation without the close.** It is *not* rollback-safe — Redis does not roll back a partially applied `EXEC`, and this does not pretend otherwise.
  - **`POPOTO_JOURNAL_COUPLING_DISABLE=1`** (or `Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False`) is a deploy-level kill switch, read at import, that keeps the annotation append but stops the interval close — the journal still records what was said while membership stops changing. Degraded mode is readable off `AnnotationResult.coupling_enabled`/`target_closed` without a Redis round trip.
  - **Do not subclass `JournalEntry`** — Popoto's `ModelBase` metaclass does not inherit `Field` attributes, so a subclass has an *empty* field set and would persist nothing. This is a pre-existing ORM limitation that the journal is simply the first module to hit; `ProvenanceJournal` refuses such a model (`TypeError`) rather than filling the keyspace with empty records. Extend the vocabulary with `JournalEntry.register_kind(name, targetless=, closing=)` instead, or hand-declare a sibling `Model` with the identical field set. **Registered kinds live in process memory and are not persisted** — a process importing entries that use a registered kind must re-run the same `register_kind()` calls first, or those records are classified `ERRORED`.
  - **Unknown kinds are inert, by rule.** A reader that does not recognize an entry's `kind` must leave the target's membership alone and never promote it to `supersede`/`retract` behavior — that is what lets the vocabulary grow across modules without a simultaneous upgrade everywhere.
  - **`hard_delete()` is the one sanctioned hole in the contract**, kept greppable for audit. It exists for erasure, not convenience: with the never-record firewall disabled, a keyspace that can only grow has no other way to remove a secret that landed in it. Its scope is exact and does **not** mean "every trace anywhere" — an index key whose *name* embeds the erased record's key (a referencing annotation's `target` index) and any `EventStreamMixin` entries both survive, and the docstring names both rather than implying otherwise.
  - **Export/import**: append-only models do not support `on_conflict="overwrite"` — every collision raises `AppendOnlyViolation` and is classified `ERRORED`. Use `on_conflict="skip"`.
  - **Firewall composition**: `JournalEntry` narrows the never-record scan surface by exactly one machine-generated field (`target`, a Popoto-generated Redis key). Scanning it for payment cards was a category error with a measured cost — a uuid4 hex sometimes contains a Luhn-passing digit run, which silently dropped ~0.25–0.4% of annotations. Every field a human or model wrote is still scanned, and `agent_id` and each `subjects` tag are scanned separately by the façade's pre-flight because the mixin's scan cannot see a `KeyField` or a list.
  - Appending through the façade costs ~3.5x a same-field-set control model at p50 (measured 3.56–3.71x over 20k real appends). That is the full `append()` path — pre-flight validation, two firewall scans, and five eager index/tag EVAL sites — not an isolated round trip.
- **Never-record firewall: a deterministic pre-storage privacy gate** ([#561](https://github.com/tomcounsell/popoto/issues/561)) — `NeverRecordMixin` plus the pure function `scan_never_record()` (new `popoto.privacy` package) drop credential-shaped strings, off-the-record-marked turns, and an enumerated set of sensitive categories before anything is written. The gate runs inside `Model.save()` **before** the `WriteFilterMixin` check and before `pre_save()`, so blocked content is never serialized and never reaches a secondary index, BM25 posting, embedding provider, or co-occurrence edge. It is regex and entropy only — no model, no network — because a prompt instructing an LLM to skip secrets is an instruction, not a guarantee. Nine reason codes: `off_the_record`, `private_key_block`, `credential_prefix`, `jwt`, `credential_assignment`, `url_userinfo`, `payment_card` (Luhn-validated), `government_id`, `high_entropy`. Every pattern detector also runs against a de-whitespaced rendering, so a key split across a line break is still caught. New docs: [Never-Record Firewall](https://popoto.io/features/never-record-firewall/).
  - **Read this before upgrading — `DefaultMemory` now carries the mixin, which changes its behavior.** A `DefaultMemory` save whose content matches a detector now returns `False` and writes nothing, where it previously persisted the row. This is deliberate: the harness shipped in #515 fires on every turn and stores raw turn text, so a pasted API key was being persisted verbatim. `POPOTO_NEVER_RECORD_DISABLE=1` (or `Defaults.NEVER_RECORD_ENABLED = False`) restores the previous behavior exactly. Models that do not inherit the mixin are entirely unaffected — this is not a global change to every `Model`.
  - **Over-blocking is accepted by design and the guarantee has enumerated holes.** Long random-looking tokens in ordinary prose are dropped. Canonical git SHAs and UUIDs are excluded from entropy scoring (otherwise every commit SHA a developer mentions would be dropped), which means a bare 40-hex-character secret with no vendor prefix escapes the entropy backstop. An unknown-prefix secret split across whitespace also escapes it. The [feature page](https://popoto.io/features/never-record-firewall/) documents each hole rather than burying it; none of the numeric constants are sweep-backed.
  - **Drops leave a content-free tombstone** — `$NR:{ClassName}:counts` (HASH) and `$NR:{ClassName}:drops` (capped LIST), read via `never_record_counts()` / `never_record_log()`. The entry id is a `uuid4` and deliberately **not** a hash of the content: a content-derived id would be a confirmation oracle for anyone holding a candidate secret. Both are plain Redis types, so this works identically on Valkey. `NeverRecordException` subclasses `SkipSaveException`, and its message carries only a reason code — never the matched text, since exception messages reach plaintext log files.
  - **`SubconsciousMemory.extract_memories()` gained a turn-level gate** running before the extraction provider, so an off-the-record marker voids the whole turn rather than a guessed span, and blocked text is never sent to the Claude extraction API. It applies regardless of `model_class`. New property `last_extraction_privacy_dropped`; `MemoryService.capture()` reads it so a deliberate drop is no longer written to the harness failure log or counted as an outage.
- **`Model.audit_datetime_keys()` and `Model.migrate_datetime_keys()`** ([#538](https://github.com/tomcounsell/popoto/issues/538)) — detection and repair for `KeyField(type=datetime)` rows on non-canonical keys, including rows **duplicated before 1.8.2**, when an aware value reloaded naive and any `save()` wrote a second hash and orphaned the first. The audit is strictly read-only and classifies rows by key shape without loading a hash. The migration is dry-run by default, idempotent and resumable, uses `RENAMENX` so an unexpected collision fails loudly, and fixes `$Class:` membership and side keys.
  - **Duplicate pairs are reported, never resolved automatically.** The two hashes are both real records and may have diverged, so the audit prints a per-field diff naming both and marking the fields that differ, and the migration refuses the whole run until the operator resolves it. There is deliberately no `strategy=` argument: last-write-wins, newest-by-timestamp and field-wise merge are each occasionally destructive and none is right often enough to be a default. See migration cookbook recipes 19 and 20.
  - Inbound `Relationship` references are detected and reported the same way — renaming a hash breaks them, and they are not rewritten automatically because a Relationship value is indistinguishable from an ordinary string field's content.
  - **A `BM25Field`, `EmbeddingField`, `ConfidenceField`, `CoOccurrenceField`, or `ContentField` on the model being migrated now also refuses the run by default.** Each keys its own per-instance Redis structures (or, for `EmbeddingField`, filesystem paths) off the instance's `redis_key` through a path this migration cannot rename, so any model shaped like `DefaultMemory` — declaring `ConfidenceField`/`BM25Field` — hits this refusal immediately. Pass `allow_orphaned_per_instance_fields=True` to acknowledge and proceed; that field's data is not moved and is left keyed by the old `redis_key`. Reported on the audit as `per_instance_field_risks`.
  - **The audit and migration are truthful regardless of `POPOTO_DATETIME_KEY_LEGACY`.** A row's canonical target is computed independently of the kill switch, so `audit_datetime_keys()` cannot report a false `is_clean` (or a false-negative collision count) just because the switch happens to be set — which matters because recipe 19 has the operator run the audit with the switch set. `migrate_datetime_keys(dry_run=False)` additionally refuses outright while the switch is set *in the calling process*, since applying the move there would immediately re-diverge the row on its next save; a `dry_run=True` preview is unaffected by the switch either way.
- **Generic export/import with per-field round-trip fidelity** ([#554](https://github.com/tomcounsell/popoto/issues/554)) — `Model.export_records()`/`Model.import_records()` (delegating to the new `popoto.transfer` module) move records between Redis instances as a JSONL stream: one manifest record carrying per-field/per-mixin round-trip policies, then one record per instance. `Field` gains `export_state`/`import_state` classmethods plus `roundtrip_policy` (`"rebuild"` default, `"carry"`, or `"partial"`) and `roundtrip_note` class attributes, all with working no-op defaults, so existing and third-party `Field` subclasses round-trip unchanged without implementing anything. `AccessTrackerMixin`, `ConfidenceField`, `CyclicDecayField`, and `EmbeddingField` implement the protocol so their auxiliary Redis structures (access counts, evidence, decay state, vectors, and embedding provenance) survive a round trip.
  - `import_records(stream, on_conflict="error", on_write_gate="reject", on_embedding_mismatch="error")` — `on_conflict` controls existing-key handling (`error`/`skip`/`overwrite`), `on_write_gate` controls whether a `WriteFilterMixin` rejection is honored (`reject`) or bypassed (`bypass`), and `on_embedding_mismatch` controls provider/model/dimension drift on `EmbeddingField` (`error`/`carry`/`regenerate`).
  - **Write-gate precedence**: `save()`'s return value is authoritative for classifying a record as landed vs. rejected — never a post-write `EXISTS` check, and never truthiness (`HSET` returns `0` on a legitimate pure-overwrite success, which would otherwise look like a rejection). A write-gate rejection under `on_conflict="overwrite"` is reported as rejected with the destination's old values left intact, even though `EXISTS` returns 1 afterwards.
  - `Model.save(skip_write_filter=False, ...)` — new kwarg lets import bypass a `WriteFilterMixin` gate per-record; default preserves existing save behavior.
  - New docs: [Writing Custom Fields](https://popoto.io/field-authoring/) documents the round-trip protocol contract for field authors, and [Export & Import](https://popoto.io/guides/export-import/) is the user-facing guide.
- **`ValidityField` and `SupersessionProtocol`** — bitemporal validity intervals and supersession chains, so a memory closed by a newer contradicting claim stops entering default context assembly immediately, instead of only losing ground gradually through decay ([#580](https://github.com/tomcounsell/popoto/issues/580)). Validity decides *membership* (is this record still true), decay decides *ordering among the valid* — the two axes compose without either knowing the other's constants.
  - **`ValidityField`** is a plain `Field` (deliberately not a `SortedFieldMixin`, so it can never win a query's ordering) that maintains six derived Redis keys per model/field: `valid_from`/`invalid_at`/`ingested_at` ZSETs, `chain:fwd`/`chain:rev` HASHes, and a per-identity open-claim pointer STRING. No bytes are written into the model's own hash, so an append-only journal can adopt it unchanged. All six keys survive a `popoto.transfer` export/import: the field declares `roundtrip_policy = "carry"` and restores interval scores, chain links, and open-claim pointers explicitly — without which an import would give every superseded record a fresh *open* interval and silently resurrect it (gating is subtractive), and a dropped open pointer would leave the identity's next supersession closing nothing. Export pays one `SCAN` of `{prefix}:open:*` per record for the pointer lookup, an accepted admin-path cost. `filter(validity__current=True|False)` and `filter(validity__as_of=t)` are deliberate, filter-dict queries — `current=False` is the literal complement of `current=True` (closed AND not-yet-started), and a record with no interval entry satisfies neither, since it makes no claim about its own validity.
  - **`SupersessionProtocol`** — a stateless coordinator mirroring `ObservationProtocol`'s shape: `identity_key(subject, predicate)` normalizes and `blake2b`-hashes a claim identity; `supersede()`/`invalidate()` close an interval, write both chain links, and repoint the open pointer in one atomic `EVAL` (`SUPERSEDE_LUA`); `superseded_by()`/`supersedes()`/`chain()` traverse provenance in either direction from any anchor. Records are closed, never deleted. Wired into `_apply_contradicted` so an existing "this memory is wrong" signal now writes provenance, not just a scalar nudge.
  - **Three independent gating layers**, because `ContextAssembler` never calls `top_by_decay` and its composite/`fuse` paths merge indexes with `ZUNIONSTORE ... AGGREGATE SUM`, under which a member merely absent from the decay arm still surfaces via any other weighted arm: (1) `DECAY_SCORE_LUA` gains `KEYS[3]`/`KEYS[4]`/`ARGV[7]`, authoritative for `top_by_decay` on a plain `DecayingSortedField` (but not on a `CyclicDecayField` — see Known limitations); (2) `QueryBuilder._apply_validity_mask` subtracts a `ZRANGESTORE`/`ZDIFFSTORE` exclusion set from the post-union composite key, the only layer that enforces membership on `composite_score`; (3) `ContextAssembler._resolve_excluded_keys`/`_scope_by_validity`, mirroring the #492 tag-scoping pattern, covers the `fuse`/BM25/graph arms that bypass the `filters` dict entirely. All three gates are subtractive: a record with no interval entry is unmanaged and stays fully retrievable, which is what makes adding `ValidityField` to an existing model safe.
  - **`assemble(as_of=t)`** (and the keyword-only `as_of` on `composite_score`/`top_by_decay`) reconstructs what an agent believed at a past instant, superseded records included; the default `None` means "now."
  - **`Defaults.VALIDITY_GATING_ENABLED = True`** is a deploy-level kill switch, read at call time, restoring byte-identical pre-#580 retrieval when set `False`. Zero blast radius at merge — no shipped model, including `DefaultMemory`, declares a `ValidityField`.
  - **Known limitations**: a direct `Model.query.top_by_decay()` on a `CyclicDecayField` is ungated — `CYCLIC_DECAY_LUA` was deliberately left unmodified, so it is reached by none of the three layers and will return a superseded record. This is a direct-caller gap, not a live retrieval bug: `ContextAssembler` never calls `top_by_decay`, its push path uses `composite_score` (layer 2), and every candidate it assembles — cyclic proxy results included — is post-filtered by `_scope_by_validity` (layer 3). Pinned by `TestCyclicDecayGatingGap`. Also: gating costs up to two `ZSCORE`s per member inside the decay Lua, which already full-scans its partition (~1.4x measured on `top_by_decay` at 20k records); the TTL-interaction warning (`Meta.ttl` + `ValidityField` on the same model) fires on first save, not at model-definition time; a TTL on a `ValidityField`-bearing model truncates supersession chains and silently breaks `as_of` correctness once a record's hash expires ahead of its index entries.
  - See [ValidityField and SupersessionProtocol](https://popoto.io/features/validity-and-supersession/) for the full keyspace, query, and gating reference.

### Fixed

- **Retrieval no longer leaks across agents through `fuse()`** ([#576](https://github.com/tomcounsell/popoto/issues/576)) — `QueryBuilder.fuse()` never consulted its own queryset's filters. It fused the ranked lists it was handed and hydrated by `redis_key`, so `Memory.query.filter(agent_id=...).fuse(...)` filtered **nothing** — the `.filter()` call served only to carry `model_class`. Since `BM25Field.search()` and graph propagation both return every matching key in the database, a query that read as scoped fused across every agent's records. Measured with 60 competing records, `agent-beta` received 10 of alpha's records and none of its own. `fuse()` now applies its builder's filters before the top-K slice, so `limit` backfills with the next-best in-scope candidates rather than returning short. **This affects published 1.8.2** for anyone using hybrid or lexical retrieval through the recipes API; it was not yet reachable through the integration hooks, since `popoto.integrations` first ships in 1.9.0.
  - **Fixing `fuse()` alone would have shipped a silently broken fix.** BM25's `limit` is applied *inside* the Lua script, so other agents' records fill the candidate window before any scope filter runs — closing the leak without touching the fetch turned "leaks 10 wrong records" into "returns 0 records", just as silently. `BM25Field.search()` therefore gained **`allowed_keys`**, making `limit` count *in-scope* hits via a widening fetch loop capped so a nearly-empty scope cannot walk an unbounded corpus per query. At the cap the result is honestly short rather than silently empty.
  - **`allowed_keys=None` means "no scoping"; `allowed_keys=set()` means "nothing is in scope"** and returns `[]`. An empty set is deliberately *not* read as "no filter" — matching the `exclude_keys` convention from [#592](https://github.com/tomcounsell/popoto/issues/592) — so a caller that computes a scope and gets nothing back fails closed.
  - **Filters on unindexed plain `Field`s are honored too**, at the cost of hydrating the surviving candidates. `filter_for_keys_set()` has no index to resolve for a plain `Field`, so it returns the whole keyspace; intersecting against that was a no-op and reopened the identical leak for an entire filter class. Prefer a `KeyField`/`SortedField` for anything you routinely scope by: in the recipes' hybrid path, an **all-unindexed** scope leaves the BM25 candidate window unnarrowed (correct, but subject to crowding), while a scope containing at least one indexed field keeps that narrowing.
  - Scope is not deletion: out-of-scope records stay in the store and the BM25 index and remain retrievable by their own owner.
- **`rebuild_indexes()` no longer indexes rows to a key that does not exist** ([#537](https://github.com/tomcounsell/popoto/issues/537)) — it reconstructs indexes from a row's *decoded* values, so for a row whose stored key disagrees with its derived key it wrote a `$KeyF:` member naming a nonexistent hash, leaving `query.all()` returning the row while `query.filter()` returned a dangling reference. Such rows are now skipped, counted, and named in a `WARNING` pointing at `audit_datetime_keys()`. The return value is a `RebuildIndexesResult`, an `int` subclass equal to the number of rows indexed (so `count = Model.rebuild_indexes()` is unchanged) carrying `.diverged_keys` and `.diverged_count`.
- **A loaded instance now remembers the key it was read from** ([#537](https://github.com/tomcounsell/popoto/issues/537), [#538](https://github.com/tomcounsell/popoto/issues/538)) — `_redis_key` was recomputed from the decoded KeyField values, so an instance's idea of where it came from followed the decode. That is what made row duplication *silent*: it blinds `save()`'s `KeyMutationError` guard and its obsolete-key branch simultaneously, so a second hash was written with no exception and no log line. Loaders now pass the source key, which turns a decode/key divergence into `save()`'s existing rename and makes the datetime-key migration self-healing for rows re-saved before it runs.
- **`IndexedField`/`UniqueField` of an encoder-registry type no longer raises `TypeError` on save** ([#534](https://github.com/tomcounsell/popoto/issues/534)) — the atomic index-maintenance path in `IndexedFieldMixin.on_save()` packed the raw field value with a bare `msgpack.packb()` instead of routing it through `TYPE_ENCODER_DECODERS` first, so `datetime`, `date`, `time`, and `Decimal` fields raised `TypeError: can not serialize ...` before the row was written. The value now goes through its registered encoder exactly as `encode_popoto_model_obj()` does, which also restores the byte-for-byte parity between the index-path hash write and the canonical hash encoding that the surrounding comment already claimed. Introduced in 1.8.0 by the Lua-backed index swap ([#412](https://github.com/tomcounsell/popoto/issues/412)).

- **`DB_key`'s colon escape is now self-escaping** ([#525](https://github.com/tomcounsell/popoto/issues/525)) — `DB_key.clean()` escapes a literal `:` to the seven-character sequence `{&#58;}`, but did not escape that sequence when it already appeared in the input, so `unclean()` could not tell an escape it produced from data the caller supplied: `unclean(clean('{&#58;}'))` round-tripped to `:` instead of `{&#58;}`. `clean()` now pre-escapes a literal `{&#58;}` in the input as `/{&#58;}` before encoding real colons, and `unclean()` is rewritten as a single-pass left-to-right scanner (replacing the old sequential-replace chain, which could not distinguish the two cases) so that `DB_key.unclean(DB_key.clean(v)) == v` holds for every string, including ones containing the literal escape sequence.
  - **Encoding changes only for values containing the literal `{&#58;}` sequence** — every other value's stored bytes are unchanged (verified: 0 encoding diffs over a 200k+ sample sweep). Those values were already lossy at write time under the old implementation, so no correct behavior regresses.
  - **Version boundary — read this before a mixed-version rollout:** keys written by Popoto **≥ 1.8.3** for values containing the literal `{&#58;}` sequence are **not** decoded correctly by Popoto **< 1.8.3**. Roll readers forward before writers. No migration is provided or needed for existing data — the affected value set was already unrecoverable before this fix (the information needed to disambiguate was never stored), so there is nothing to migrate.

- **`get_by_id()` / `AutoKeyField` lookups could silently return `None` for rows that exist** ([#540](https://github.com/tomcounsell/popoto/issues/540)) — the 1.8.1/1.8.2 fix for #476 moved the `IndexedField`/`UniqueField` pointer to a side key derived by suffixing the model hash key (`<Model:key>\x00idxptr\x00<field_name>`, and `\x00tagptr\x00` for `TagField`). A NUL byte does not keep a key out of a glob — Redis glob `*` matches any byte, including NUL — so that side key was matched by the same `scan_keys()` glob `AutoKeyField` lookups (and pattern `KeyField` lookups: `__startswith`, `__endswith`, `__isnull`) use, and the batched `HGETALL` that followed failed with `WRONGTYPE`.
  - Both pointer side keys now live under Popoto's internal key namespace — `$IdxPtr:<Model:key>:<field_name>` and `$TagPtr:<Model:key>:<field_name>`, alongside `$Class:`, `$KeyF:`, `$SortedF:`. A model name can never begin with `$`, so neither key can ever collide with a model key glob again. See [Indexed Fields → Operator Note](https://popoto.io/indexed_fields/#operator-note).
  - Every `scan_keys()`-backed lookup in `KeyFieldMixin.filter_query` now filters results by actual Redis `TYPE` before `HGETALL` (`_scan_hash_keys`), dropping any non-hash key that matches the glob. This also fixes a same-version crash from `ListField`'s capped-list companion key (`<Model:key>::<field_name>`, a Redis LIST) reaching the same scan path.
  - Records written by an affected 1.8.0-1.8.2 release are read via a migration fallback and self-heal (the legacy pointer is scrubbed) the next time they're saved — no offline migration required, though `Model.rebuild_indexes()` remains available for a clean cut-over.
  - **Rolling-upgrade note**: while any node in a fleet is still on 1.8.1/1.8.2, it keeps writing the legacy NUL-suffixed key; upgraded nodes tolerate this via the `TYPE` guard above. A not-yet-upgraded node reading a record already migrated to `$IdxPtr:`/`$TagPtr:` by an upgraded node falls back to its own stale in-memory snapshot on delete, which can leave an orphaned index-Set member until the record is next saved by an upgraded node — self-healing, bounded, no worse than pre-#476 behavior.

## [1.8.2] - 2026-08-07

### Added

- **`popoto.recipes.DefaultMemory`** — a shipped, batteries-included agent-memory model ([#513](https://github.com/tomcounsell/popoto/issues/513)). It declares `AutoKeyField`, `KeyField`, `StringField`, `FloatField`, `DecayingSortedField`, `ConfidenceField`, `BM25Field`, and `CoOccurrenceField` plus `AccessTrackerMixin`, so `ContextAssembler(retrieval_mode="auto")` resolves to the query-sensitive `"lexical"` mode over it. Import it instead of authoring a schema:

  ```python
  from popoto.recipes import SubconsciousMemory

  sm = SubconsciousMemory(agent_id="agent-1")
  ```

  `WriteFilterMixin` is deliberately excluded (it discards records silently) and `EmbeddingField` too (it needs a provider). Subclass `DefaultMemory` for your own keyspace.
- **Query-blind resolution now warns** ([#513](https://github.com/tomcounsell/popoto/issues/513)) — `ContextAssembler` logs a `WARNING` on the `POPOTO.ContextAssembler` logger when `retrieval_mode="auto"` falls through to `"composite"`, naming the model and the missing `BM25Field`. That resolution silently ignores query cues, and previously emitted nothing at any log level. An explicit `retrieval_mode="composite"` is treated as an informed choice and stays quiet.
- **`output_format="content"`** ([#513](https://github.com/tomcounsell/popoto/issues/513)) — a content-first `ContextAssembler` format emitting memory text as a `- ` bullet list, with no field names, key values, or scores. New `content_field` ctor kwarg names the text field (auto-detected from the model's `BM25Field` source, else a field named `content`). Measured over `DefaultMemory` with a 71-character memory: `"structured"` emitted 262 characters (3.69x, ~104 estimated tokens), `"content"` emitted 73 (1.03x, ~16 tokens).

- **Confidence-modulated decay** — per-record outcome evidence now changes how fast a memory is forgotten ([#491](https://github.com/tomcounsell/popoto/issues/491)). `DecayingSortedField` and `CyclicDecayField` read each record's `ConfidenceField` value inside their ranking Lua and derive a per-record effective decay *rate*, rather than only scaling the curve's magnitude as `base_score_field` does. Corroborated memories persist longer; repeatedly dismissed or contradicted ones fade faster. This closes the learn→forget loop: `ObservationProtocol` already wrote outcome evidence, but nothing consumed it to change forgetting.
  - **Formula**: `eff = decay_rate * 2^(s * 2 * (c0 - c))`, then `score = base_score * t^(-decay_rate) * max(t, 1.0)^(-(eff - decay_rate))`, where `c` is the record's confidence (clamped to `[0,1]`), `c0` is the confidence field's own `initial_confidence`, and `s` is `Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH` (0.5). Centering on `c0` rather than a literal `0.5` keeps neutrality exact for any configured `initial_confidence`. The `max(t, 1.0)` clamp is load-bearing: for `t < 1` day, `t^(-rate)` is a multiplier greater than 1 that a larger rate amplifies more, so without it modulation would run backwards for the first 24 hours.
  - **Default-ON at upgrade time — read this before upgrading.** A model with exactly one `ConfidenceField` gets modulation with **no configuration change**, so ranking can shift after `pip install -U` without any code edit on your side. Zero `ConfidenceField`s means off; two or more with no explicit kwarg means off plus a `logger.warning` naming the candidates. Escape hatches: `DecayingSortedField(confidence_modulation_field="<name>")` selects explicitly, `confidence_modulation_field=False` disables per field, and **`Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False` is a deploy-level kill switch that needs no model-code edit** — set it at process start to restore pre-#491 ranking byte-for-byte.
  - **Bit-exact neutrality**: records with no confidence evidence, models with no `ConfidenceField`, `s = 0`, `confidence_modulation_field=False`, and the kill switch all produce byte-identical scores to v1.8.0. Disabled paths skip the per-member `HGET` entirely, so unmodulated deployments pay no latency cost.
  - **Rank-inversion caveat**: rate modulation makes two records' log-log score lines cross exactly once, so a record's *rank* can improve over time even as its score falls. Cached top-N snapshots drift in a way magnitude weighting never produces — re-run `top_by_decay()` rather than caching its output if ordering stability matters.
  - **Partitioned confidence**: if the `ConfidenceField` declares `partition_by` and the query does not filter on those fields, `top_by_decay()` raises `QueryException` naming the missing filters. Modulation never silently degrades to partial coverage.
  - Nothing is persisted for modulation — `ConfidenceField` remains the single source of truth and the decay path is a pure reader. No Redis modules; identical on Redis and Valkey.
- **`MemoryLifecycle` tombstones** ([#491](https://github.com/tomcounsell/popoto/issues/491)) — new public surface: `tombstone()`, `restore()`, `list_tombstones()`, `get_tombstone()`, `tombstone_count()`, `purge_tombstone()`, `purge_all_tombstones()`, `forget_hard()`, `confidence_forget_eligible()`, and the `Tombstone` dataclass (`redis_key`, `fingerprint`, `tier`, `importance_at_death`, `confidence_at_death`, `evidence_count`, `dismissal_count`, `tombstoned_at`, `reason`). Tombstones live under `$TOMB:{Model}:*`, outside the model keyspace, and retention is bounded by `LIFECYCLE_TOMBSTONE_RETENTION_LIMIT` (1000) with the oldest aging out.
- **Five tuning constants** registered in `Defaults` and [Tuning Magic Numbers](https://popoto.io/guides/tuning-magic-numbers/): `DECAY_CONFIDENCE_MODULATION_STRENGTH` (0.5), `DECAY_CONFIDENCE_MODULATION_ENABLED` (`True`), `LIFECYCLE_FORGET_CONFIDENCE_CEILING` (0.3), `LIFECYCLE_FORGET_MIN_EVIDENCE` (5), `LIFECYCLE_TOMBSTONE_RETENTION_LIMIT` (1000).
- **`ContextAssembler` confidence gate** — opt-in confidence-gated retrieval ([#463](https://github.com/tomcounsell/popoto/issues/463)): two new keyword-only ctor kwargs, `confidence_gate_threshold: float | None = None` and `confidence_gate_mode: str = "refuse"`. When a threshold is set, `assemble()` reads the rank-0 pull-path candidate's `ConfidenceField` value and, if below threshold, either drops all pull-path records (`"refuse"`) or retains them and only flags the decision (`"flag"`); either way it reports the decision in `AssemblyResult.metadata["gate"]`. Mode-agnostic by construction — works under composite, lexical, and hybrid retrieval — because `ConfidenceField` values are always in `[0, 1]` regardless of the ranking algorithm's score scale. Inert and bit-for-bit backward compatible unless a threshold is explicitly configured; enabling it requires a `ConfidenceField` on the model (raises `QueryException` otherwise).
  - **Ships with no default.** `confidence_gate_threshold` defaults to `None` and `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD` (0.5) is a benchmark-only in-code constant, deliberately **not** promoted to `Defaults.CONFIDENCE_GATE_THRESHOLD` or a ctor default. Per the 2026-06-11 maintainer-decisions memo, promoting any policy-level default threshold requires explicit maintainer sign-off and remains a maintainer decision.
- **`ContextAssembler(retrieval_mode=...)`** — new `retrieval_mode` parameter controls pull-path strategy ([#395](https://github.com/tomcounsell/popoto/issues/395)):
  - `"auto"` *(default)* — detects `BM25Field` + `EmbeddingField` on the model; uses hybrid RRF path if both present, composite otherwise
  - `"hybrid"` — BM25 lexical + vector semantic signals fused via Reciprocal Rank Fusion (k=60), optional CoOccurrence graph expansion; raises `QueryException` at init if required fields are absent
  - `"composite"` — original `CompositeScoreQuery` weighted-sum path (pre-v1.7 behaviour)
  - Existing callers without `retrieval_mode` keep working unchanged: auto-mode falls back to composite on models without `BM25Field`/`EmbeddingField`
- **`QueryBuilder._get_vector_scores(query_text, limit)`** — private helper that returns `[(redis_key, cosine_similarity)]` tuples for RRF fusion input; mirrors `semantic_search()` internals without hydration
- **Benchmark R@K improvement** — external harness ([#394](https://github.com/tomcounsell/popoto/issues/394)) now shows measurable signal with BM25 retrieval:
  - LongMemEval-S (fixture): R@5 0.0 → 1.0, MRR 0.0 → 0.667
  - LoCoMo (fixture): R@5 0.0 → 0.667, MRR 0.0 → 0.375
- **Full-dataset LoCoMo baseline** ([#447](https://github.com/tomcounsell/popoto/issues/447)) — the 6-question fixture baseline is replaced by complete 1986-question runs (10 dialogues) in both retrieval modes:
  - Lexical (BM25): R@1 0.2986, R@5 0.5534, R@10 0.6400, MRR 0.4124 — **superseded by the #514 scoring correction below** (corrected: 0.2981 / 0.5302 / 0.6017 / 0.4005)
  - Hybrid (BM25 + vector RRF): R@1 0.1667, R@5 0.4235, R@10 0.5403, MRR 0.2835 — hybrid underperforms lexical on LoCoMo; both rows use pre-#514 scoring
  - Category-5 (adversarial) questions score normally in this dataset snapshot (`evidence` is populated), correcting the earlier zero-by-construction assumption
  - Artifacts: `tests/benchmarks/results/external/locomo_latest{,_hybrid}.{json,md}`; analysis in [docs/benchmarks.md](https://popoto.io/benchmarks/)
- **`MemoryLifecycle`** recipe (`src/popoto/recipes/memory_lifecycle.py`) — policy layer orchestrating memory tier transitions and auto-forget. Composes `DecayingSortedField`, `ConfidenceField`, and `AccessTrackerMixin` into a two-tier episodic → semantic lifecycle without replacing any existing primitive. ([#396](https://github.com/tomcounsell/popoto/issues/396))
  - `MemoryLifecycle(model_class, importance_field)` — init with capability detection and `ModelException` guards
  - `tag_new(record, tier="episodic")` — assign starting tier; handles `KeyField` migration automatically
  - `tick()` → `{"promoted": N, "forgotten": N, "duration_ms": F}` — idempotent periodic lifecycle pass with paginated batch scanning
  - `assess(record)` → `LifecycleState` — snapshot of tier, access count, importance score, and promotion/forget eligibility
  - `LifecycleState` dataclass — return type for `assess()`
  - Custom `should_promote` and `should_forget` callables — injectable for application-specific policies
  - `partition_filters` — scope each lifecycle instance to a sub-partition (e.g. per-agent)
  - Five tuning constants (`PROMOTION_ACCESS_COUNT`, `PROMOTION_CONFIDENCE_THRESHOLD`, `PROMOTION_MIN_AGE_SECONDS`, `FORGET_IMPORTANCE_FLOOR`, `FORGET_IDLE_SECONDS`) registered in `Defaults` and the Tier 5 benchmark sweep grid
- **`LifecycleState`** exported from `popoto.recipes`
- **Tier 5 benchmark sweep grid** (`TIER5_SWEEPS` in `tests/benchmarks/run_sweeps.py`) — five lifecycle constants with sweep ranges for tuning against LoCoMo + LongMemEval-S
- **`docs/benchmarks/memory_lifecycle_baseline.md`** — pre-lifecycle retrieval baseline and sweep grid documentation

### Changed

- **`SubconsciousMemory` argument defaults** ([#513](https://github.com/tomcounsell/popoto/issues/513)). `model_class` and `score_weights` become optional, and the injected payload changes shape. Existing callers are unaffected: passing `model_class` keeps `confidence_field`/`co_occurrence_field` at `None` as before, passing `score_weights` overrides the new default, and the positional order `(model_class, agent_id, score_weights)` is unchanged.
  - `model_class` default `None` → `DefaultMemory`, which also wires that model's `confidence` and `associations` fields.
  - `score_weights` default `None` → `{"relevance": 1.0}`, the benchmarked vector (`tests/benchmarks/results/sweep_20260326_125145.json` → `constants.score_weights.best_value`). Full transparency: all six swept vectors tied at nDCG@5 = 1.0 on the coding_assistant / research_agent / support_agent scenarios, so this is the selected `best_value` and simplest single-index vector, not one measured to beat the alternatives. The guides previously showed `{"relevance": 0.6, "confidence": 0.3}`, which was never the benchmarked configuration.
  - **`agent_id` is now required** and raises `ValueError` when omitted. It was already effectively required (it is the partition key); this makes the failure immediate instead of producing a shared memory pool.
  - **New `output_format` kwarg defaults to `"content"`**, so injected context carries memory text only. The previous JSON payload spent ~2.8x the content's characters on `memory_id` UUIDs, the caller's own `agent_id`, and `relevance` as a raw epoch float. Pass `output_format="structured"` to restore it verbatim.
- **`limit` now bounds a SortedField query before hydration instead of after.** Previously the sorted-set read returned the whole range and `limit` was applied once every matching record had been loaded, so `Model.query.filter(room="r", ts__gte=0, order_by="-ts", limit=5)` over a 2,000-record partition issued 2,000 `HGETALL`s to return 5 rows. Measured on that partition:

    | | HGETALL | wall clock |
    |---|---|---|
    | before | 4,000 | 58.5ms |
    | after | 26 | 0.8ms |

  - **Two bounds, and the widely-applicable one matters most.** Slicing the ordered key list before `get_many_objects` is what removes the hydration blowup, and it applies even when another index participates in the query, because `filter_for_keys_set` has already intersected the key list without loading anything. Pushing `offset`/`num` into the `ZRANGEBYSCORE`/`ZREVRANGEBYSCORE` call additionally avoids transferring the key list, but it only applies when the sorted field is the sole filter dimension. Both cut hydration to the same 26 `HGETALL`s; the range-read bound accounts for the remaining 5ms (0.8ms versus 5.8ms with an `IndexedField` predicate in play).
  - **Only qualifying queries change.** Both bounds require a positive int `limit`, ordering by the sorted field itself, no Q objects, and no predicate that could eliminate a row after hydration. Anything else reads the full range exactly as before. The guard is the point of the change: a bound spent before a later predicate runs would silently return short results rather than raising. `bool` is rejected explicitly, since it subclasses `int` and would otherwise reach Redis as `num=1`.
  - **Stale index members cannot silently shorten a result.** Members whose backing hash is gone hydrate to nothing and would come straight off the result count. Queries request `Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN` (8) extra members so ordinary orphan density costs no extra round trip; past that, one unbounded re-read restores a correct answer. Either way a short read logs at WARNING with the model, sorted field, partition, requested-versus-returned counts, and orphan count, and points at `repair_indexes()` — the re-read tolerates orphans, it does not fix them.
  - Results are unchanged for every query shape, qualifying or not. `SortedFieldMixin.filter_query` gains keyword-only `_limit` and `_desc`; the ordinary filter surface is untouched.
- **Three behavior changes ship default-on with [#491](https://github.com/tomcounsell/popoto/issues/491).** All three are beta-substrate changes to the agent-memory layer; core ORM behavior is untouched.
  1. **Decay ranking may shift after upgrade.** Confidence modulation is enabled by auto-detection — any model with exactly one `ConfidenceField` and a `DecayingSortedField`/`CyclicDecayField` gets per-record rate modulation with no configuration change. Records with no accumulated evidence are byte-identical; records that have been reported on via `ObservationProtocol` will rank differently. Restore the previous behavior with `Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False` (deploy-level, no model-code edit), `confidence_modulation_field=False` on the field, or `DECAY_CONFIDENCE_MODULATION_STRENGTH = 0`.
  2. **`MemoryLifecycle.tick()` tombstones instead of hard-deleting.** Forgotten records are archived under `$TOMB:{Model}:*` and removed from the live corpus — so exclusion from every retrieval mode is structural rather than a filter each read path must apply — and are recoverable via `restore()`. `tick()`'s summary dict gains a `tombstoned` key alongside `promoted`/`forgotten`. Code asserting that a forgotten record is gone from Redis entirely should either use `forget_hard()` or account for the tombstone. `forget_hard(record)` preserves the old irreversible-delete semantics.
  3. **`top_by_decay()` now raises on a partitioned `ConfidenceField` queried without its partition filters.** If the auto-detected `ConfidenceField` declares `partition_by` and the query omits filters for those fields, `top_by_decay()` raises `QueryException` naming the missing filters — a call that succeeded before this release, on a path nobody opted into. It fails loudly rather than silently modulating against partial confidence coverage. This is the most upgrade-hostile of the three: it raises rather than shifting scores. Restore the previous behavior by adding the partition filter to the query (`Model.query.top_by_decay(..., agent_id="x")`), setting `confidence_modulation_field=False` on the decay field, or flipping the deploy-level kill switch `Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False` (no model-code edit).
- **`MemoryLifecycle` forget criteria now read confidence** ([#491](https://github.com/tomcounsell/popoto/issues/491)). The rule becomes `(importance < FORGET_IMPORTANCE_FLOOR OR (confidence < FORGET_CONFIDENCE_CEILING AND evidence_count >= FORGET_MIN_EVIDENCE)) AND idle > FORGET_IDLE_SECONDS`, closing the promote/forget asymmetry — confidence already gated promotion but was blind to forgetting. The `evidence_count` conjunct is a safety floor, not a tuning knob: confidence moves on every reported outcome, so without a minimum track record one unlucky dismissal could bury a memory. Semantic-tier protection is unchanged, and models with no `ConfidenceField` take the importance-only path, identical to previous behavior.

### Fixed

- **Benchmark integrity: LoCoMo retrieval scoring is now gold-blind** ([#514](https://github.com/tomcounsell/popoto/issues/514)) — the external harness consulted the answer key when collapsing retrieved turns to result IDs (`chosen_ids = matching if matching else candidate_ids[:1]` over a merged `[session_id, turn_id]` candidate list). For LoCoMo, whose ground truth is turn IDs, a gold turn emitted its unique `turn_id` and kept its own rank slot while every non-gold turn collapsed into one shared `session_id` slot — 20 retrieved turns became **13.2 rank slots on average** across the full 1986-question corpus — so gold was systematically lifted. The harness now ranks one unit per dataset (turn IDs for LoCoMo, session IDs for LongMemEval-S), resolved from dataset metadata before retrieval runs, with gold-blind first-occurrence dedup; the unit is recorded in every report's `ranking_unit` block and its Markdown header.
  - **Corrected LoCoMo lexical, full 1986 questions:** R@1 0.2981, R@5 0.5302, R@10 0.6017, MRR 0.4005 (was 0.2986 / 0.5534 / 0.6400 / 0.4124). Artifact `tests/benchmarks/results/external/locomo_20260807.json`; the superseded run stays committed as `locomo_20260708.json`.
  - **LongMemEval-S is unaffected** — its ground truth is session IDs, which the old rule emitted on both branches. Re-verified by a full 500-question lexical re-run (`longmemeval_s_20260807.json`): identical Recall@1/5/10 and MRR on all 500 questions individually and in aggregate (0.8560 / 0.9520 / 0.9780 / 0.8987), with only the latency fields differing. `longmemeval_s_latest` still points at the published `longmemeval_s_20260630` run.
  - LoCoMo **hybrid**, **graph**, and **judged** artifacts still carry pre-correction scoring and are labelled as such on the docs site; corrected re-runs are pending.
  - Also removed two dangling `*_latest*` symlinks (`locomo_latest_graph`, `locomo_latest_ext-heuristic`) whose targets were never committed, and added a test that fails on any broken symlink under `tests/benchmarks/results/`.
- **A `SortedField(type=datetime)` score is now a pure function of the stored value** ([#519](https://github.com/tomcounsell/popoto/issues/519)). The hash encoding carries no UTC offset, so a datetime arrives aware and comes back from a reload naive, and `.timestamp()` reads a naive datetime as *local* time. Saving a reloaded row therefore re-scored it by the machine's UTC offset: measured at -7.00 h on a +07:00 machine, so a row re-saved by any lifecycle method sorted seven hours earlier than it happened. `convert_to_numeric` now treats a naive datetime as UTC when deriving the score, which agrees with the v1.7.1 `auto_now`/`auto_now_add` UTC change ([#421](https://github.com/tomcounsell/popoto/issues/421)).
  - **Mostly invisible before now, and about to stop being so.** A caller that reads a range and re-sorts in Python compares uniformly-naive decoded values and gets the right answer, so only callers that trust score ordering saw it: `top_by_decay`, any `ZRANGEBYSCORE` bound, and the bounded reads added in this release.
  - **Existing scores stay skewed until reindexed.** Run `Model.rebuild_indexes()`, which re-derives every score from the stored hashes without touching the values. **A partially-reindexed keyspace orders incorrectly**, since a pre-fix row sorts by the local offset earlier than a post-fix one, so treat the reindex as a coordinated step rather than assuming the upgrade is transparent. `rebuild_indexes()` drops the index keys before reconstructing them, so range queries are incomplete for its duration and it is not safe to run against live read traffic that depends on them.
  - Only the score changes. Stored values are untouched, and the underlying encoding data loss is fixed separately below ([#521](https://github.com/tomcounsell/popoto/issues/521)).

- **`datetime` and `time` values no longer lose their UTC offset in storage** ([#521](https://github.com/tomcounsell/popoto/issues/521)). The encoder used `%Y%m%dT%H:%M:%S.%f`, which has no offset directive: a timezone-aware value went in and a naive one came out, and `12:00+07:00` and `12:00+00:00` produced byte-identical storage. This was the root cause underneath #519 — that fix made the *score* consistent, this one makes the *value* correct. Both types now encode with `isoformat()`, which carries the offset when the value is aware and omits it when the value is naive, so awareness round-trips in both directions.
  - **Naive values are unaffected.** Naive in, naive out; the encoder does not assume a zone at write time. `datetime.date` is unchanged — it has no offset to lose.
  - **Rows written before this release still read, and still read naive.** The decoder accepts both shapes. A legacy string has no offset to recover, so none is invented: stamping UTC onto it would move every value ever written by a non-UTC process and would break callers comparing it against `datetime.now()`. `convert_to_numeric` treats a naive value as UTC when scoring (#519), so legacy rows still score consistently. **No offline migration is required**, and none is possible — a value originally saved with a non-UTC offset is already unrecoverable.
  - **Behavior change for `auto_now`/`auto_now_add` consumers.** These fields have stamped aware UTC since v1.7.1 ([#421](https://github.com/tomcounsell/popoto/issues/421)) but read back naive, and the documented workaround was for consumers to re-attach UTC on read. They now read back aware UTC, so that workaround is unnecessary — and code that compares such a field against a naive `datetime.now()` will raise `TypeError` where it previously compared (against a value that was silently wrong by the host offset). Compare against `datetime.now(timezone.utc)` instead. Values written before this release still read naive, so a corpus spanning the upgrade contains both shapes.
  - **Fixes `KeyField(type=datetime)` duplication for aware values.** `DB_key` builds both the hash key and the `$KeyF:` index key from `str(value)`, so a value that reloaded naive derived a different key than it was stored under: loading an aware-keyed row and saving it wrote a *second* hash and orphaned the original. Awareness now survives the reload, so the key is stable. Rows already duplicated by this on an earlier version are not repaired automatically. This is also why legacy rows are left naive — stamping an offset on read would move their derived keys and reintroduce the same split for every pre-upgrade row.
  - **`ZoneInfo` degrades to a fixed offset.** A `ZoneInfo`-aware value round-trips as the UTC offset in effect at that instant, not as the zone. The instant is preserved and `fold` is resolved at encode time, but later arithmetic across a DST boundary on a reloaded value differs from the same arithmetic on the original.
  - **Sorted-set scores change for aware values, so reindex alongside #519.** An aware value previously decoded to its own naive wall clock and scored as though that clock were UTC; it now decodes aware and scores the true instant. `Model.rebuild_indexes()` re-derives scores from the stored hashes and covers both fixes in one pass — the same coordinated-step and live-read caveats above apply.

- **`SortedField(type=datetime.time)` now works at all.** `datetime.time` is on `SortedFieldMixin`'s allowed-type list, but `convert_to_numeric` scored it with `field_value.timestamp()` — a method `datetime.time` does not have — so **every save of such a field raised `AttributeError`**. The advertised type had never worked and had no test coverage. The score is now seconds since midnight, which preserves time-of-day ordering. `tzinfo` deliberately does not participate: a bare time has no date, so normalizing by the offset would wrap past midnight and sort `01:00+07:00` before `23:00+00:00` on the same clock face. Use `datetime.datetime` when the offset must order. Found while fixing #521; no upgrade impact, since no existing deployment can hold data in a field that could never be saved.

- **`RetrievalQuality` score proxy is now partition- and decay-aware** ([#474](https://github.com/tomcounsell/popoto/issues/474)): the shared metacognitive score proxy (`_score_proxy_for_records` / `_staleness_ratio`) read each record's composite score from the **non-partitioned** sorted-set index key. Agent-memory models almost always declare `partition_by` on their sorted fields, so the real scores live in a partition-specific ZSET; the base key had zero members, every `ZSCORE` returned `None`, and the proxy reported `0.0` for every record — silently collapsing `RetrievalQuality.score_spread` (to `0.0`), `staleness_ratio` (to `1.0`), and the FOK `subthreshold_activation` component for partitioned models, and feeding `AdaptiveAssembler` a flatlined quality signal. The proxy now reads the partition-specific index, and for `DecayingSortedField` / `CyclicDecayField` (whose index stores a last-updated timestamp, not relevance) decays that timestamp into relevance via the same Lua scripts `top_by_decay()` uses. `ContextAssembler._injection_scores` (the telemetry trace) is reconciled onto the shared helper. Non-partitioned models are unaffected. Read-only and Valkey-safe (pipelined `ZSCORE` / read-only `EVAL`; no `ZUNIONSTORE`, temp keys, or Redis modules). Beta-substrate behavioural change: `score_spread` / `staleness_ratio` become non-degenerate for partitioned models.
- **`BM25Field.search()` now returns deterministic ordering for equal scores** ([#446](https://github.com/tomcounsell/popoto/issues/446)): Lua 5.1 `table.sort` is unstable and candidates were collected in hash order, so equal-scored documents came back in undefined order — across runs and across the `limit` truncation boundary. Ties are now broken inside the scoring Lua script by member `redis_key` ascending (byte-wise), so identical searches return identical orderings on both Redis and Valkey. `keyword_search()`, RRF `fuse()`, and hybrid retrieval inherit the determinism.
- **`DecayingSortedField` / `CyclicDecayField` `top_by_decay()` now returns deterministic ordering for equal scores** ([#448](https://github.com/tomcounsell/popoto/issues/448)): the same unstable-`table.sort` defect as #446 in both decay-field Lua scripts. When members share a base score and timestamp (e.g. batch-inserted memories) their equal decayed/effective scores left the tied run — and which members survived the `n` truncation — in undefined order. Both comparators now apply a two-level total order (score descending, then member `redis_key` ascending byte-wise) before truncation, entirely inside the Lua script, so identical queries return identical orderings on both Redis and Valkey.
- **`ContextAssembler` token budget now enforced for real** ([#408](https://github.com/tomcounsell/popoto/issues/408)):
  - The default `token_counter` previously counted characters in the Redis key (`str(record)`, typically 12–14 "tokens" per record regardless of content size), so `max_tokens` never engaged for realistic budgets. The counter now receives the serialized per-record string the formatter emits, and `metadata["token_count"]` reflects actual serialized content.
  - **Breaking change for users who set `max_tokens`**: assemblies will now admit fewer records. Audit and raise your `max_tokens` values if needed. See [ContextAssembler — Token Budget Semantics](features/context-assembler.md#token-budget-semantics).
  - New `token_counter` contract: `callable(serialized_text: str) -> int`. Old-contract `callable(record)` counters trigger `DeprecationWarning` at construction and fall back to the stdlib heuristic per call.
  - New default heuristic (`_estimate_tokens`): escape-aware character-class estimator accurate to within ±25% vs tiktoken cl100k_base across English, code, CJK, and emoji content (worst case −15% underestimate on URL/hash-heavy records; all other content types err in the safe overestimate direction).
  - Packing now uses skip-not-break semantics: a record that does not fit is skipped; later smaller records may still be admitted. The first record is always admitted (never-zero-records guarantee).
  - Wrapper framing (JSON array brackets, `<records>` envelope) is excluded from counting — fixed residual under 20 tokens per assembly.
- **`EmbeddingField` cross-process cache invalidation** — in multi-worker deployments (gunicorn, multiple containers/pods) a write on one worker no longer leaves peers serving a stale embedding matrix ([#403](https://github.com/tomcounsell/popoto/issues/403)):
  - New `POPOTO_EMBEDDING_INVALIDATION` environment variable selects the strategy: `pubsub` *(default)* uses a Valkey pub/sub bus to notify peers within ~100 ms; `mtime` uses an on-disk `_version` counter checked on the next `semantic_search()`; `none` restores the original zero-overhead single-process behavior.
  - The default `pubsub` mode degrades to the on-disk `_version` check (never back to the stale-cache bug) if the subscriber thread cannot start, and the listener self-heals via lazy respawn after a connection drop.
  - Single-process **results** are unchanged in all modes; the default adds one daemon listener thread, one Valkey connection, and one loopback `PUBLISH` per write per model class. Set `POPOTO_EMBEDDING_INVALIDATION=none` for zero overhead. See [EmbeddingField → Multi-Worker Deployments](https://popoto.io/fields/#embeddingfield).
- **`IndexedField`/`UniqueField` secondary-index pointer moved out of the model hash** ([#476](https://github.com/tomcounsell/popoto/issues/476)) — a 1.8.0 revision of the atomic-index feature (#412/#424) wrote its internal `{field}\x00idxset` pointer as a **raw, non-msgpack field inside the model hash**. Any decoder that unconditionally `msgpack.unpackb()`s every hash field — every pre-1.8.0 release — crashed with `msgpack.exceptions.ExtraData` reading such records (mixed-deploy / rollback hazard). Two further version-independent bugs were found in the same code path:
  - `delete()` read the index pointer via `HGET` **after** the model hash had already been `DELETE`d, so the read always returned nil and silently fell back to a possibly-stale in-memory snapshot — risking an orphaned index Set member pointing at a deleted hash.
  - The internal (no-external-pipeline) save path queued the uniqueness-check EVAL into the *same* Redis MULTI/EXEC as the base `HSET` and other bookkeeping. Redis does not roll back other queued commands when one command in a transaction errors, so a genuine uniqueness conflict could leave the base `HSET` committed while the index write failed — an orphaned, index-less "ghost" hash.

  Fix: the pointer now lives in a standalone side key (never a model-hash field, so the forward-incompat break cannot recur); `delete()` runs field cleanup hooks before removing the hash; and indexed/unique field writes on the internal save path execute as their own atomic step before any other write for that record is queued, so a uniqueness conflict raises before anything else commits. Records written by the affected 1.8.0 revision are read via a migration fallback and self-heal (the legacy in-hash field is scrubbed) the next time they're saved — no offline migration required, though `Model.rebuild_indexes()` remains available for a clean cut-over. See [Indexed Fields → Operator Note](https://popoto.io/indexed_fields/#operator-note).

  **Open question for a future release:** this closes the *specific* orphan-hash bug (indexed field write vs. rest-of-record write), but a record's non-indexed fields and its indexed fields still commit in two separate steps on the internal save path — full single-transaction atomicity across all fields would need a larger redesign (folding the entire hash write into one Lua script) and is out of scope here.

### Removed

- **`MemoryLifecycle.TICK_BATCH_SIZE`** — removed in #413 (single-pass refactor eliminated batch scanning; the constant was never registered in `Defaults` and was always an internal implementation detail). Public-API break acceptable under beta.

## [1.7.1] - 2026-06-15

### Fixed

- **`DatetimeField` `auto_now` / `auto_now_add` now stamp UTC** ([tomcounsell/ai#1653](https://github.com/tomcounsell/ai/issues/1653)) — `format_value_pre_save` previously returned naive `datetime.now()` (host **local** wall-clock). Since the encoder serializes wall-clock without tzinfo, every `auto_now`/`auto_now_add` timestamp on a non-UTC host was skewed by the host's UTC offset, breaking downstream "age since update" math. It now returns `datetime.now(timezone.utc)`, so timestamps are correct regardless of host timezone. Uses `timezone.utc` (valid on the `requires-python = ">=3.10"` floor), not the 3.11+ `datetime.UTC`. Write-only and non-migrating: existing rows are unchanged; UTC hosts already stamped UTC.

## [1.5.0](https://github.com/tomcounsell/popoto/compare/v1.0.3...v1.5.0) (2026-04-21)

### Popoto Agent Memory — Now in Beta

After shipping 14 independent primitives across the 1.1–1.4 series, the Popoto Agent Memory system exits alpha with this release. The system gives AI agents non-cortical memory capabilities — episodic recall, salience gating, temporal decay, confidence tracking, prediction-error learning, and now retrieval self-assessment — all built on plain Redis/Valkey with no module dependencies.

**Metacognitive layer** (the final piece — [#352](https://github.com/tomcounsell/popoto/issues/352)):

Agents using `ContextAssembler` can now ask "how much should I trust this context?" before reasoning over it, and automatically tune their retrieval strategy over time without any ML training pipeline.

#### Added

- **`RetrievalQuality`** dataclass — four-signal retrieval self-assessment: `fok_score` (feeling-of-knowing via `ExistenceFilter`), `avg_confidence` (mean Bayesian certainty of returned memories), `score_spread` (retrieval confidence interval), `staleness_ratio` (fraction of memories past their decay threshold)
- **`ContextAssembler.assess(query_cues)`** — standalone quality probe; returns `RetrievalQuality` without executing a full retrieval
- **`ContextAssembler.assemble(query_cues, assess_quality=True)`** — attaches `RetrievalQuality` to `AssemblyResult.metadata["quality"]`; off by default, zero overhead when not used
- **`PredictionLedgerMixin.error_summary(group_by=...)`** — aggregate prediction errors across instances, grouped by hour of day, day of week, error band, or any callable bucketer; uses pipelined Redis reads, Valkey-compatible
- **`ObservationProtocol` `"used"` outcome** — agent consumed the memory but hasn't acted yet; confirms the staged `AccessTracker` read and auto-resolves the pending prediction with a configurable error signal (default 0.3); distinct from `"deferred"` which discards the staged read
- **`AdaptiveAssembler`** recipe (`src/popoto/recipes/adaptive_assembler.py`) — wraps any `ContextAssembler` with an autoresearch-style keep/revert loop: proposes symmetric `score_weights` perturbations, measures quality over a rolling window, keeps improvements, reverts regressions; single-threaded by design, purely opt-in, no Redis writes for the adaptation state

#### Also in this cycle (1.1–1.4 series highlights)

- `OllamaProvider` — local embedding generation via Ollama, no API key required
- `ExistenceFilter.batch_might_exist()` + `BM25Field.get_idf()` IDF selectivity signal
- `Model.check_indexes()` — read-only index health check for production
- `Model.clean_indexes()` — production-safe orphan index removal
- `Query.get_many()` — bulk key hydration in a single pipeline
- Companion hash key public API
- `ConfidenceField` partition support
- Adaptive constant optimizer sweep (closes constant-sensitivity variance gap from benchmarks)

#### Fixed

- `PredictionLedgerMixin.error_summary(group_by=...)` returned `{"__all__": ...}` instead of `{}` when called on a model with zero recorded predictions and a non-`None` `group_by`

#### Migration

If you were using a custom `"echoed"` outcome (or any application-specific
label semantically between `"used"` and `"dismissed"`):

- Map it to `"used"` if the agent reasoned over the memory (staged read
  should be confirmed; prediction auto-resolves with moderate error).
- Map it to `"dismissed"` if the overlap was purely coincidental keyword
  match (staged read discarded; confidence/cycle weakened).

`on_context_used()` raises `ValueError` on unknown outcome labels — coerce
to a valid value before calling.

#### Notes

- All metacognitive features are **opt-in** and additive — existing `ContextAssembler` API is unchanged
- No Redis module commands anywhere in the stack — works on Redis ≥ 6 and Valkey ≥ 7
- Cross-restart persistence for `AdaptiveAssembler` is deferred to v1.6; adaptation state is per-process

---

## [1.0.3](https://github.com/tomcounsell/popoto/compare/v1.0.2...v1.0.3) (2026-03-22)


### Documentation

* add temperature parameter to composite_score references ([3600fbd](https://github.com/tomcounsell/popoto/commit/3600fbd856d2e4229661efc4106beebbb72d67e9))

## [1.0.2](https://github.com/tomcounsell/popoto/compare/v1.0.1...v1.0.2) (2026-03-20)


### Documentation

* AccessTrackerMixin — update agent-memory, api-reference, query docs ([5bc29e5](https://github.com/tomcounsell/popoto/commit/5bc29e5d27b1b6f9af7152791422ff6a6215f13b))
* add composite_score() to query and API reference ([#223](https://github.com/tomcounsell/popoto/issues/223)) ([53b6318](https://github.com/tomcounsell/popoto/commit/53b63185456b4084a8b9e649eb9c3ed2fbd57974))
* audit cleanup + allow docs pushes to main ([#227](https://github.com/tomcounsell/popoto/issues/227)) ([0ef0baf](https://github.com/tomcounsell/popoto/commit/0ef0baf1fa8289c7c82399c189c267093d8137c1))
* cascade updates for PredictionLedgerMixin (PR [#231](https://github.com/tomcounsell/popoto/issues/231)) ([#237](https://github.com/tomcounsell/popoto/issues/237)) ([a65255a](https://github.com/tomcounsell/popoto/commit/a65255a79ee4387e9af1831b0e8385033d57155a))

## [1.0.1](https://github.com/tomcounsell/popoto/compare/popoto-v1.0.0...popoto-v1.0.1) (2026-03-12)


### Bug Fixes

* update README community section ([8627346](https://github.com/tomcounsell/popoto/commit/8627346dbcf93b952cb33778405678fb773172c3))

## [1.0.0] - 2026-03-11

Popoto 1.0.0 is the first General Availability release. It marks the project's graduation from beta to a stable, production-ready Redis/Valkey ORM with Django-like model syntax. This release consolidates all features and fixes from the beta series (1.0.0b1, 1.0.0b2) plus additional hardening work.

### Highlights

- **Full async/await support** with native `redis.asyncio` — no more `asyncio.to_thread()` wrappers
- **Chainable query builder** with Q objects and expression-based filtering
- **Bulk operations** (create, update, delete) via Redis pipelines
- **Atomic saves** via internal pipeline for data integrity
- **Migration utilities** for production schema changes
- **Comprehensive index integrity** — all known ghost-entry and corruption bugs resolved
- **Valkey compatibility** — works identically with Redis and Valkey

### Added

#### Query System
- **Chainable Query Builder** (#91): Fluent interface for building queries incrementally
  ```python
  results = Model.query.filter(status="active").order_by("name").limit(10).all()
  ```
  `QueryBuilder` supports `filter()`, `limit()`, `order_by()`, `values()`, `all()`, `first()`, `last()`, `count()`

- **Q Objects for OR Queries** (#92): Django-style Q objects for complex query logic
  ```python
  from popoto import Q
  Model.query.filter(Q(status="active") | Q(type="premium"))
  Model.query.filter(~Q(status="inactive"))
  ```

- **Expression-Based Queries** (#96): Python comparison operators on Field attributes
  ```python
  Model.query.filter(Model.rating > 4.0)
  Model.query.filter((Model.rating > 4.0) & (Model.status == "active"))
  ```

- **`__between` range query operator** (#131): Filter SortedField by range
  ```python
  Model.query.filter(score__between=(50, 100))
  ```

- **Plain Field filtering** (#122): Filter on non-indexed fields with client-side fallback

- **`last()` query method** (#137): Retrieve the last result from a query

- **Sorted field ordering preservation** (#139): Queries filtering on SortedField return results in sorted order by default

#### Model Methods
- **`get_or_create()` and `update_or_create()`** (#132): Django-style convenience methods
  ```python
  obj, created = Model.query.get_or_create(name="test", defaults={"score": 100})
  obj, created = Model.query.update_or_create(name="test", defaults={"score": 200})
  ```
  Async variants: `async_get_or_create()`, `async_update_or_create()`

- **`to_dict()` method** (#129): Dictionary serialization with relationship expansion
  ```python
  obj.to_dict()                          # All fields
  obj.to_dict(include=["name", "score"]) # Specific fields
  obj.to_dict(expand=True, max_depth=2)  # Expand relationships
  ```

- **`delete_all()` classmethod** (#115): Delete all instances of a model with index cleanup

- **`Model.pk` property** (#121): Clean primary key access

- **`Model.objects` alias** (#94): Django-style query manager — `Model.objects.filter()` works identically to `Model.query.filter()`

#### Bulk Operations
- **Bulk Create/Update/Delete** (#93): Efficient batch operations using Redis pipelines
  ```python
  Model.bulk_create([obj1, obj2, obj3])
  Model.bulk_update(Model.query.filter(status="pending"), status="active")
  Model.bulk_delete(Model.query.filter(status="inactive"))
  ```
  All support `batch_size` parameter and async variants

#### Migration Utilities
- **`save(skip_auto_now, update_fields)`** (#144): Fine-grained save control for migrations
- **`rebuild_indexes()`** (#146): Rebuild all secondary indexes from stored data
- **`raw_update()`** (#146): Low-level field updates bypassing hooks
- **Comprehensive migration cookbook** (#143): Step-by-step guide for common migration scenarios

#### Field Enhancements
- **Sortable ID Strategies** (#95): ULID and KSUID support for `AutoKeyField`
  ```python
  id = AutoKeyField(strategy="ulid")   # Time-sortable (requires ulid-py)
  id = AutoKeyField(strategy="ksuid")  # Time-sortable (requires cyksuid)
  ```

- **`auto_now_add` and `auto_now` on SortedField** (#133): Automatic timestamps
  ```python
  created_at = SortedField(type=float, auto_now_add=True)
  updated_at = SortedField(type=float, auto_now=True)
  ```

- **Renamed `sort_by` to `partition_by`** (#138): Better reflects the parameter's purpose. Deprecation shim maintains backward compatibility.

#### Async Support
- **Native `redis.asyncio` support** (#130): True async Redis operations — significant performance improvement over the `asyncio.to_thread()` wrapper used in beta 1
- **Full async API**: `async_save()`, `async_delete()`, `async_create()`, `async_load()`, `async_get()`, `async_filter()`, `async_all()`, `async_count()`, `async_get_or_create()`, `async_update_or_create()`, `async_delete_all()`, `async_bulk_create()`, `async_bulk_update()`, `async_bulk_delete()`

#### Developer Experience
- **`get_redis()` helper** (#137): Direct access to the Redis connection
- **`popoto.testing` module** (#137): `use_test_db()` and `flush_test_db()` helpers
- **Popoto Kitchen TUI** (#112): Interactive terminal example app for exploring features

#### Infrastructure
- **Valkey Support** (#55): Full compatibility with Valkey (Redis fork) via `REDIS_URL` or `VALKEY_URL`
- **Optional Pandas** (#63): Core library no longer requires pandas — install with `pip install popoto[dataframe]`
- **Comprehensive stress tests** (#65): Bulk ops, concurrent access, memory efficiency, geo queries, TTL

### Changed

- **Atomic saves** (#148): `save()` now executes via internal Redis pipeline for atomicity
- **SCAN vs KEYS** (#77): Pattern queries use SCAN instead of KEYS to prevent blocking
- **Msgpack deserialization** (#78): 60% faster query times via lazy field deserialization
- **Validation logic** (#72, #73): Optimized field validation with merged iteration loops
- **`filter()` with no arguments** (#81): Now correctly returns all objects
- **Pre-release polish** (#171): Exception hierarchy cleanup, connection hardening, logging improvements, type hints on core CRUD methods

### Fixed

- **KeyField index corruption on value mutation** (#150): `on_save()` now removes instance from old index Set when field value changes
- **SortedField ghost entries on partition key change** (#159): `on_save()` and `on_delete()` clean up old partition's sorted set
- **Obsolete key in class set after key change** (#161): `save()` removes old redis_key from class tracking set
- **Partial save obsolete key cleanup** (#156): `update_fields` saves properly clean up obsolete redis keys
- **Relationship index cleanup on value change** (#155): `Relationship.on_save()` removes old relationship indexes
- **Relationship validation on re-save** (#113): Lazy-loaded `redis_key` strings accepted during validation
- **Exact match queries on SortedField**: Now work correctly
- **Field.name attribute**: Properly set for expression-based queries
- **Relationship on_delete edge cases**: Lazy-loaded relationships handled correctly during deletion

### Known Considerations

- **`~Q` Negation Performance**: Negating Q objects requires scanning all keys — use with caution on large datasets
- **Bulk Operations Memory**: `bulk_update` and `bulk_delete` materialize the full queryset before processing. For 100K+ items, consider batching.

### Migration Guide from 0.x

No breaking changes. All new features are additive with full backward compatibility.

**Recommended upgrades:**

1. Switch to chainable queries: `Model.query.filter(status="active").order_by("-created").limit(10).all()`
2. Use Q objects for OR logic: `Model.query.filter(Q(status="active") | Q(type="premium"))`
3. Use bulk operations for batch processing: `Model.bulk_create(items)`
4. Rename `sort_by` to `partition_by` on SortedField (old name still works via deprecation shim)

---

## [1.0.0b2] - 2026-02-12

Beta 2 release. See [1.0.0] above for consolidated changelog.

## [1.0.0b1] - 2026-02-03

Beta 1 release. See [1.0.0] above for consolidated changelog.

## [0.9.0] - 2025-12-15

See [commit history](https://github.com/tomcounsell/popoto/compare/v0.8.3...v0.9.0) for changes.

## [0.8.3] and earlier

See [commit history](https://github.com/tomcounsell/popoto/commits/v0.8.3) for changes.
