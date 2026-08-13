"""Generate the Benchmarks results tree under /benchmarks/results/.

Reads the committed ``*_latest.{json,md}`` benchmark artifacts under
``tests/benchmarks/results/`` and emits one virtual results page per available
artifact, plus a section index (Overview) carrying the mandatory content
framing. This is the same ``mkdocs-gen-files`` build-time generator pattern
established by ``gen_api_pages.py`` — the pages exist only in the built site,
not as tracked ``.md`` files under ``docs/``.

The point of this generator is that a *future* benchmark run only needs to
commit its refreshed ``_latest`` artifact: the next docs deploy re-runs this
script and the published numbers update automatically, with zero hand-editing
of prose tables.

Page body vs. index headline:
    * The page **body** is spliced verbatim from the committed ``.md`` artifact
      (which the harness already renders as Summary + By-question-type tables).
      Embedding the rendered ``.md`` guarantees the published numbers are
      exactly the committed artifact's numbers — no re-derivation, no drift.
    * The index **headline row** is read from the ``.json`` ``summary`` block.

Skip rules (graceful degradation — a missing dataset must NEVER fail
``mkdocs build --strict``):
    * If the ``.md`` artifact (required for the page body) is missing or a
      dangling symlink, the page is skipped with a stderr note and ``continue``
      — never raised. The index then simply omits that dataset.
    * If the ``.json`` is missing/malformed, the page still renders from the
      ``.md``; only the index headline row for that dataset is dropped (narrow
      try/continue with a stderr note).

H1 handling:
    The committed ``.md`` artifacts open with their own ``# Popoto ...`` H1.
    This generator strips that leading H1 and splices the remaining body under
    its own spec-authored H1 (e.g. ``# LongMemEval-S — Hybrid Retrieval``) so
    each page has exactly one top-level heading.

Navigation:
    The Benchmarks nav section references ``benchmarks/results/`` as a bare
    directory. The ``literate-nav`` plugin resolves that entry against the
    ``benchmarks/results/SUMMARY.md`` this script emits, which lists only the
    pages that were actually generated. That is what makes graceful degradation
    work under ``--strict``: a skipped page is absent from SUMMARY, so there is
    no dangling nav reference to fail the build (a *static* nav entry pointing
    at a skipped page would fail). The hand-written how-to and baseline pages
    stay as static nav entries alongside the ``benchmarks/results/`` directory.

Output layout (virtual files under ``benchmarks/results/``):
    benchmarks/results/SUMMARY.md                  -- literate-nav index (generated pages only)
    benchmarks/results/index.md                    -- Overview + framing + headline table
    benchmarks/results/longmemeval_s_lexical.md
    benchmarks/results/longmemeval_s_hybrid.md
    benchmarks/results/longmemeval_s_vector.md
    benchmarks/results/locomo_lexical.md
    benchmarks/results/locomo_hybrid.md
    benchmarks/results/locomo_vector.md
    benchmarks/results/rlt_redis.md
    benchmarks/results/csr.md

A page is emitted only when its artifact is committed. The ``_vector`` pages
(dense/embedding retrieval, produced by the ``--retrieval-mode vector`` harness
in #455/PR #467) therefore appear on the docs site the moment a
``{dataset}_latest_vector.{json,md}`` artifact lands under
``tests/benchmarks/results/external/`` — no code edit required at that point.
Any ``*_latest*`` artifact not covered by a ``Spec`` is reported as a loud
build-time warning by ``_warn_orphan_artifacts`` (never raised), so a new
retrieval mode can never be silently dropped from the site.

Valkey-safety: this generator writes only framing prose plus the embedded
harness markdown. It introduces no Redis-module command strings (the search,
bloom, count-min, and top-k module command families) — the harnesses
themselves never use them, so both Redis and Valkey run these benchmarks
identically.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import mkdocs_gen_files

RESULTS_ROOT = Path("tests/benchmarks/results")
OUTPUT_DIR = Path("benchmarks/results")

# Repo-root-relative source paths for the "Edit this page" links.
REPO_RESULTS_PREFIX = RESULTS_ROOT


@dataclass(frozen=True)
class Spec:
    """One benchmark results page.

    slug:      output filename stem under ``benchmarks/results/``.
    title:     spec-authored H1 for the page (replaces the artifact's own H1).
    nav_title: short label used in the generated SUMMARY.md nav entry.
    stem:      artifact stem relative to ``RESULTS_ROOT`` (``.json``/``.md``
               appended). ``_latest`` symlinks resolve to the newest report.
    kind:      "external" (Recall@k/MRR summary) or "csr" (RSR summary).
    note:      per-page framing admonition inserted under the H1.
    """

    slug: str
    title: str
    nav_title: str
    stem: str
    kind: str
    note: str


# Explicit spec list (NOT a directory glob) so page order, titles, and
# per-dataset framing are deterministic and never coupled to filesystem
# accidents. LongMemEval-S is the headline; LoCoMo follows; CSR last.
SPECS: tuple[Spec, ...] = (
    Spec(
        slug="longmemeval_s_lexical",
        title="LongMemEval-S — Lexical (BM25) Retrieval",
        nav_title="LongMemEval-S (lexical)",
        stem="external/longmemeval_s_latest",
        kind="external",
        note=(
            '!!! note "Lexical (BM25-only) baseline"\n'
            "    Query-sensitive BM25 with no vector signal fused in. This is the\n"
            "    score-only baseline; the LongMemEval-S hybrid variant adds the\n"
            "    vector arm and is the headline result (see the\n"
            "    [Overview](index.md)).\n"
        ),
    ),
    Spec(
        slug="longmemeval_s_hybrid",
        title="LongMemEval-S — Hybrid Retrieval",
        nav_title="LongMemEval-S (hybrid)",
        stem="external/longmemeval_s_latest_hybrid",
        kind="external",
        note=(
            '!!! success "Headline result — like-for-like win over the reference"\n'
            "    Hybrid retrieval (BM25 + all-MiniLM-L6-v2 vector fused via Reciprocal\n"
            "    Rank Fusion, k=60) reaches **Recall@1 0.892 / Recall@5 0.986 /\n"
            "    Recall@10 0.992 / MRR 0.931**, beating the agentmemory BM25+Vector\n"
            "    reference (Recall@5 0.952 / Recall@10 0.986 / MRR 0.882). Both use the\n"
            "    same retrieval-recall metric family on the same dataset, so this\n"
            "    comparison is like-for-like and real.\n"
            "\n"
            '!!! info "Full 500 questions, re-confirmed 2026-08-07, not a sample"\n'
            "    This page is the complete LongMemEval-S question set, no sampling.\n"
            "    It replaces a run that predated the\n"
            "    [#457](https://github.com/tomcounsell/popoto/issues/457) weighted\n"
            "    fusion change and whose only post-#457 evidence was a 100-question\n"
            "    sample ([#530](https://github.com/tomcounsell/popoto/issues/530)).\n"
            "    Recall@1 moved 0.894 → 0.892 (one question of 500), Recall@5 and\n"
            "    Recall@10 are unchanged, MRR moved 0.9317 → 0.9307. LongMemEval-S was\n"
            "    never affected by the #514 scoring correction: its ground truth is\n"
            "    session IDs, which the old rule already emitted on both branches.\n"
        ),
    ),
    Spec(
        slug="longmemeval_s_vector",
        title="LongMemEval-S — Vector (Dense/Embedding) Retrieval",
        nav_title="LongMemEval-S (vector)",
        stem="external/longmemeval_s_latest_vector",
        kind="external",
        note=(
            '!!! note "Dense-arm diagnostic — not the headline"\n'
            "    Vector retrieval ranks by **pure cosine** over the\n"
            "    all-MiniLM-L6-v2 embedding (no BM25, no Reciprocal Rank Fusion).\n"
            "    It isolates the **dense arm only** — a diagnostic baseline for the\n"
            "    hybrid-vs-lexical investigation, not a headline result. The\n"
            "    LongMemEval-S headline remains the hybrid variant (see the\n"
            "    [Overview](index.md)). Same retrieval-recall metric family as the\n"
            "    lexical and hybrid pages, so the three are directly comparable.\n"
        ),
    ),
    Spec(
        slug="locomo_lexical",
        title="LoCoMo — Lexical (BM25) Retrieval",
        nav_title="LoCoMo (lexical)",
        stem="external/locomo_latest",
        kind="external",
        note=(
            '!!! note "Retrieval regime — read the [Overview](index.md) first"\n'
            "    Read this as *retrieval recall*, not answer accuracy. The nearest\n"
            "    published reference point measured the same way is MEMTIER\n"
            "    (arXiv:2605.03675), whose own LoCoMo retrieval baselines its authors\n"
            "    describe as uninformative, so treat it as evidence that LoCoMo\n"
            "    retrieval numbers run low across systems, not as a ranking to place\n"
            "    Popoto within. This variant is the 10-dialogue / 5-category\n"
            "    (adversarial included) / 1986-QA-pair LoCoMo, not the 1,540-QA /\n"
            "    4-category leaderboard variant.\n"
            "\n"
            '!!! info "Corrected 2026-08-07 — these numbers replace an inflated set"\n'
            "    The pre-correction run reported Recall@1 0.2986 / Recall@5 0.5534 /\n"
            "    Recall@10 0.6400 / MRR 0.4124. Its scoring consulted the answer key\n"
            "    when collapsing retrieved turns to result IDs, so gold turns kept\n"
            "    their own rank slot while non-gold turns shared one — 20 retrieved\n"
            "    turns became 13.2 rank slots on average, lifting gold. Scoring now\n"
            "    ranks turn IDs for every record alike\n"
            "    ([#514](https://github.com/tomcounsell/popoto/issues/514)); the\n"
            "    superseded artifact stays committed as `locomo_20260708.json`.\n"
        ),
    ),
    Spec(
        slug="locomo_hybrid",
        title="LoCoMo — Hybrid Retrieval",
        nav_title="LoCoMo (hybrid)",
        stem="external/locomo_latest_hybrid",
        kind="external",
        note=(
            '!!! warning "This page is a 250-question SAMPLE, not a full run"\n'
            "    Unlike every other LoCoMo page here, this artifact covers **250 of\n"
            "    the 1986 questions**: a stratified sample, seed 0, every category\n"
            "    represented. A full hybrid pass re-embeds ~1.19M records and measured\n"
            "    ~5.2 hours on the reference machine, so\n"
            "    [#530](https://github.com/tomcounsell/popoto/issues/530) re-ran it\n"
            "    sampled under gold-blind scoring rather than leave the superseded\n"
            "    full run standing. Treat the numbers as a bounded estimate: they\n"
            "    carry sampling error a full run does not, and they are not\n"
            "    interchangeable with the full-1986 lexical page.\n"
            "\n"
            '!!! info "Corrected 2026-08-07, supersedes an inflated, unweighted run"\n'
            "    The superseded artifact (`locomo_20260708_hybrid.json`, full 1986,\n"
            "    unweighted RRF) reported Recall@1 0.1667 / Recall@5 0.4235 /\n"
            "    Recall@10 0.5403 / MRR 0.2835 under the pre-correction scoring the\n"
            "    lexical page describes\n"
            "    ([#514](https://github.com/tomcounsell/popoto/issues/514)). It stays\n"
            "    committed. Two things changed between it and this page: gold-blind\n"
            "    scoring **and** the weighted/query-adaptive fusion of\n"
            "    [#457](https://github.com/tomcounsell/popoto/issues/457), so the\n"
            "    difference cannot be attributed to either one alone.\n"
            "\n"
            '!!! note "Hybrid no longer underperforms lexical here"\n'
            "    On the identical 250 questions, the corrected lexical run scores\n"
            "    Recall@1 0.3400 / Recall@5 0.5120 / Recall@10 0.5840 / MRR 0.4178\n"
            "    (re-aggregated from the committed full-1986 artifact over the same\n"
            "    item IDs). Hybrid matches it to within one Recall@10 question. The\n"
            "    query-shape discriminator routes LoCoMo's name/date-anchored queries\n"
            "    to the keyword-lean regime, so the fused order converges on the\n"
            "    lexical order. Detail in\n"
            "    [Benchmarking](../../benchmarks.md#retrieval-modes).\n"
        ),
    ),
    Spec(
        slug="locomo_vector",
        title="LoCoMo — Vector (Dense/Embedding) Retrieval",
        nav_title="LoCoMo (vector)",
        stem="external/locomo_latest_vector",
        kind="external",
        note=(
            '!!! note "Dense-arm diagnostic — read in the retrieval regime"\n'
            "    Vector retrieval ranks by **pure cosine** over the\n"
            "    all-MiniLM-L6-v2 embedding (no BM25, no Reciprocal Rank Fusion). It\n"
            "    isolates the **dense arm only** — *not* the graph/co-occurrence arm\n"
            "    that also lives inside hybrid — so a weak number here does not by\n"
            "    itself explain the hybrid-vs-lexical gap. Read it against the same\n"
            "    10-dialogue / 5-category / 1986-QA-pair LoCoMo variant used by the\n"
            "    lexical and hybrid pages: same retrieval-recall metric family\n"
            "    throughout, and the nearest published retrieval-measured reference\n"
            "    is MEMTIER (arXiv:2605.03675).\n"
        ),
    ),
    Spec(
        slug="rlt_redis",
        title="RLT — Retrieval Latency & Throughput (Redis)",
        nav_title="RLT (Redis)",
        stem="rlt/rlt_latest_redis",
        # ``rlt`` is neither an "external" (Recall@k/MRR) nor a "csr" (RSR) page:
        # its metric family is latency/throughput, so it never contributes a row
        # to the Overview's recall headline table (``_external_table`` filters on
        # kind == "external") and has no ``summary`` block for ``_read_summary``.
        # The page body (scaling / throughput / mixed-workload tables) is spliced
        # verbatim from the committed artifact, exactly like every other page.
        # Ordered before ``csr`` so the CSR page stays last (a pinned invariant
        # in test_gen_benchmark_pages.py).
        kind="rlt",
        note=(
            '!!! note "Latency metric family — not comparable to recall pages"\n'
            "    RLT measures **speed** (p50/p95/p99 retrieval latency, throughput,\n"
            "    scaling curve, live mixed-workload degradation), a different metric\n"
            "    family from the Recall@k/MRR retrieval pages above — the two are not\n"
            "    cross-comparable. These are **native Popoto (Redis)** numbers only;\n"
            "    the Valkey run and real Mem0/Zep/vector-DB comparators are a tracked\n"
            "    follow-up (see [Benchmarking How-To](../../benchmarks.md) →\n"
            "    RLT section). Absolute latency is machine-dependent — read the\n"
            "    *shape*, not the exact millisecond.\n"
        ),
    ),
    Spec(
        slug="csr",
        title="Deterministic CSR Regression Gate",
        nav_title="CSR Regression Gate",
        stem="csr/csr_latest",
        kind="csr",
        note=(
            '!!! info "Report-only CI gate — not a leaderboard metric"\n'
            "    The Constraint Satisfaction Rate (CSR) harness is a deterministic,\n"
            "    per-PR regression gate (bit-identical every run — no LLM judge, no\n"
            "    embeddings, no Redis module; identical on Redis and Valkey). Its RSR\n"
            "    (standard/adversarial) and Adversarial Gap numbers are report-only\n"
            '    signals for catching regressions, **not** a "higher is better"\n'
            "    leaderboard score.\n"
        ),
    ),
)


def _resolve(stem: str, ext: str) -> Path:
    """Return the artifact path for ``<stem>.<ext>`` under RESULTS_ROOT."""
    return RESULTS_ROOT / f"{stem}.{ext}"


def _strip_leading_h1(md_text: str) -> str:
    """Drop the artifact's own leading ``# ...`` heading, if present.

    The generator supplies its own H1 per spec, so we splice only the body
    below the artifact's top-level heading to avoid two ``# `` headings (and
    the toc/anchor-duplication warnings that ``--strict`` would raise).
    """
    lines = md_text.splitlines()
    out: list[str] = []
    dropped = False
    for line in lines:
        if not dropped and line.strip() == "":
            # Preserve leading blank lines only after we've decided.
            if out:
                out.append(line)
            continue
        if not dropped and line.lstrip().startswith("# "):
            dropped = True
            continue
        out.append(line)
    return "\n".join(out).lstrip("\n")


def _read_summary(stem: str) -> dict | None:
    """Read the artifact ``summary`` block, or None if unavailable.

    A missing/malformed JSON is non-fatal: the page still renders from the
    ``.md``; only the index headline row for this dataset is dropped.
    """
    json_path = _resolve(stem, "json")
    if not json_path.exists():
        print(
            f"[gen_benchmark_pages] no json for {stem}: index headline skipped",
            file=sys.stderr,
        )
        return None
    try:
        return json.loads(json_path.read_text()).get("summary")
    except (ValueError, OSError) as exc:
        print(
            f"[gen_benchmark_pages] malformed json for {stem}: {exc!r}; "
            "index headline skipped",
            file=sys.stderr,
        )
        return None


def _write_page(spec: Spec) -> dict | None:
    """Emit one results page. Returns the summary dict (for the index) or None.

    Returns None (and writes no page) when the ``.md`` body artifact is
    missing or a dangling symlink — graceful degradation, never raised.
    """
    md_path = _resolve(spec.stem, "md")
    if not md_path.exists():
        print(
            f"[gen_benchmark_pages] skipping {spec.slug}: "
            f"missing artifact {md_path}",
            file=sys.stderr,
        )
        return None

    body = _strip_leading_h1(md_path.read_text())
    out_path = OUTPUT_DIR / f"{spec.slug}.md"
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write(f"# {spec.title}\n\n")
        fd.write(spec.note)
        fd.write("\n")
        fd.write(body)
        fd.write("\n")
    # "Edit this page" points back at the artifact the numbers came from.
    mkdocs_gen_files.set_edit_path(out_path, REPO_RESULTS_PREFIX / f"{spec.stem}.md")

    summary = _read_summary(spec.stem)
    return {"spec": spec, "summary": summary}


INDEX_FRAMING = """\
# Benchmarks — Overview

Popoto's memory retrieval is evaluated against published datasets. Every page in
this section is **auto-generated at build time from the committed benchmark
artifacts** under `tests/benchmarks/results/` — a new benchmark run only needs
to commit its refreshed `_latest` artifact and these pages update on the next
deploy, with no hand-edited tables to drift.

!!! danger "Metric families are not convertible"
    Popoto reports **retrieval recall** (any-hit Recall@k / MRR): did the
    correct evidence appear in the top-k retrieved memories? The widely-cited
    "LoCoMo leaderboard" systems (Hindsight, Backboard, Dakera, Memori,
    ByteRover, RGMem, Mem0, Zep) report **LLM-as-judge answer accuracy**: did a
    language model produce a correct final answer? These are **different metric
    families and are not convertible in either direction**, so Popoto's recall
    is never tabulated beside a judge-accuracy percentage. (Note: Dakera
    advertises "88.2% recall", but their methodology is judge-scored: it is
    not retrieval recall despite the name.)

    Popoto's own judged answer accuracy is published, with its N, its interval,
    and its protocol, in
    [Benchmarking → Judged-Answer Accuracy](../../benchmarks.md#judged-answer-accuracy-tier-5).

## LongMemEval-S is the headline

LongMemEval-S is the retrieval benchmark we lead with — MEMTIER
(arXiv:2605.03675) argues it is the more appropriate retrieval benchmark, and
the hybrid win over the agentmemory reference is a **like-for-like** comparison
in the same metric family on the same dataset.

{longmemeval_table}

## LoCoMo — read in the retrieval regime

**Correction (2026-08-07):** every LoCoMo number below was re-measured after a
scoring defect that consulted the answer key when collapsing retrieved turns to
result IDs was fixed ([#514](https://github.com/tomcounsell/popoto/issues/514)).
The corrected figures are lower. All four arms (lexical, hybrid, graph, judged)
now run under gold-blind scoring
([#530](https://github.com/tomcounsell/popoto/issues/530)), but their **coverage
differs and is not uniform**: lexical is the full 1986 questions, hybrid is a
250-question stratified sample (a full hybrid pass measured ~5.2 h), graph is
the full 282-question multi-hop slice, and judged is 100 questions sampled from
a 2-dialogue subset. Each page states its own sample mode and limit in the run
header; read that before comparing two pages.

!!! note "MEMTIER anchor (arXiv:2605.03675)"
    MEMTIER is the nearest published work that measures LoCoMo *as retrieval*
    rather than as judged answers, which makes it the right anchor for latency
    and for the shape of the regime. It reports hybrid-RRF retrieval at
    **96.7 ms/query** on comparable hardware. Its own LoCoMo retrieval scores
    are baselines its authors describe as uninformative, so they are not used
    here to place Popoto on a scale.

    **Variant:** this is the LoCoMo variant with **10 dialogues, 5 categories
    (including adversarial), 1986 QA pairs** (as described in Omni-SimpleMem,
    arXiv:2604.01007) — *not* the common leaderboard variant of 1,540 QA pairs /
    50 dialogues / 4 categories (adversarial excluded). The two are easy to
    cross-read; they are not comparable.

{locomo_table}

!!! warning "Category-5 (adversarial) caveat"
    Adversarial is historically the hardest category industry-wide (the original
    LoCoMo paper reports humans ≈89 F1 vs LLMs ≈2 F1). Popoto's category-5
    scoring *comparably to* the other categories (corrected lexical Recall@1
    0.3341) is
    presented here as a **factual observation, not a strength**: it may indicate
    the harness matches populated evidence spans rather than exercising refusal
    behavior. An evidence-matching audit is filed separately; until it resolves,
    read cat-5 numbers with this caveat.

{csr_section}
## Further reading

- [Benchmarking](../../benchmarks.md) — how the harnesses work and how to run them.
- [Query-blind retrieval](../../guides/query-blind-retrieval.md): when composite
  mode is the right ranking and when it is the wrong one.
"""


def _fmt(summary: dict | None, key: str) -> str:
    """Format a summary float to 4 decimals, or ``—`` when unavailable."""
    if not summary or summary.get(key) is None:
        return "—"
    try:
        return f"{float(summary[key]):.4f}"
    except (TypeError, ValueError):
        return "—"


def _external_table(rows: list[dict]) -> str:
    """Render a Recall@k/MRR headline table for the external retrieval rows."""
    external = [
        r for r in rows if r["spec"].kind == "external" and r["summary"] is not None
    ]
    if not external:
        return "_No LongMemEval-S / LoCoMo artifacts available._"
    header = (
        "| Benchmark | Recall@1 | Recall@5 | Recall@10 | MRR |\n"
        "|---|---|---|---|---|\n"
    )
    lines = []
    for r in external:
        spec, summary = r["spec"], r["summary"]
        link = f"[{spec.title}]({spec.slug}.md)"
        lines.append(
            f"| {link} "
            f"| {_fmt(summary, 'recall_at_1')} "
            f"| {_fmt(summary, 'recall_at_5')} "
            f"| {_fmt(summary, 'recall_at_10')} "
            f"| {_fmt(summary, 'mrr')} |"
        )
    return header + "\n".join(lines)


def _dataset_table(rows: list[dict], prefix: str) -> str:
    """Headline table filtered to specs whose slug starts with ``prefix``."""
    subset = [r for r in rows if r["spec"].slug.startswith(prefix)]
    return _external_table(subset)


def _csr_section(rows: list[dict]) -> str:
    """Render the CSR framing section, only if the CSR page was generated.

    The section links to ``csr.md``; emitting it unconditionally would leave a
    dangling link (and fail ``--strict``) whenever the CSR artifact is absent.
    """
    if not any(r["spec"].slug == "csr" for r in rows):
        return ""
    return (
        "## Deterministic CSR regression gate\n\n"
        "The [CSR page](csr.md) reports the deterministic Constraint Satisfaction\n"
        "Rate harness — a per-PR CI regression gate whose scores are bit-identical\n"
        "every run (no LLM judge, no embeddings, no Redis module). Its numbers are\n"
        "**report-only regression signals, not a leaderboard metric.**\n\n"
    )


def _write_index(rows: list[dict]) -> None:
    """Write the Overview page with headline tables spliced into the framing."""
    text = INDEX_FRAMING.format(
        longmemeval_table=_dataset_table(rows, "longmemeval_s"),
        locomo_table=_dataset_table(rows, "locomo"),
        csr_section=_csr_section(rows),
    )
    out_path = OUTPUT_DIR / "index.md"
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write(text)


def _write_summary(rows: list[dict]) -> None:
    """Write the literate-nav SUMMARY.md for the Benchmarks results section.

    Lists the always-present Overview plus one entry per page that was actually
    generated, in spec order. Pages skipped for a missing artifact are simply
    not listed here — that is what keeps ``mkdocs build --strict`` green when an
    artifact is absent (no dangling nav reference). ``literate-nav`` resolves
    the ``benchmarks/results/`` nav entry against this file.
    """
    out_path = OUTPUT_DIR / "SUMMARY.md"
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write("* [Overview](index.md)\n")
        for r in rows:
            spec = r["spec"]
            fd.write(f"* [{spec.nav_title}]({spec.slug}.md)\n")


def _warn_orphan_artifacts(
    specs: tuple[Spec, ...], root: Path = RESULTS_ROOT
) -> list[str]:
    """Warn (never raise) about ``external/`` artifacts no ``Spec`` publishes.

    The published set is an *explicit* ``SPECS`` list (not a directory glob), by
    design, so page order and per-dataset framing stay deterministic. The cost
    of that design is a **silent** gap: an ``external/*_latest*`` artifact that
    no ``Spec`` references is skipped without a trace, so a new retrieval mode's
    results can land in the repo yet never appear on the docs site.

    This scan converts that silent gap into a **loud** one. It globs
    ``root/external/*_latest*.md`` (the artifact whose presence would drive a
    page), and for every artifact whose ``RESULTS_ROOT``-relative stem is not the
    ``stem`` of some ``Spec``, prints a build-time WARNING to stderr. It never
    raises — ``mkdocs build --strict`` stays green — and it never publishes; a
    human still authors the ``Spec`` (with deterministic order and framing). The
    warning simply makes the omission visible in the deploy log.

    ``root`` is injectable (defaults to ``RESULTS_ROOT``) so tests can point it
    at a ``tmp_path`` and never touch the committed ``results/external/`` tree.
    Returns the list of orphan stems it warned about (for testability).

    Only the ``.md`` artifact is scanned: the ``.md`` is what drives a page body,
    so a lone ``.json`` without an ``.md`` would be skipped by ``_write_page``
    anyway and is not an "unpublished results page" in the sense this guards.
    """
    mapped = {spec.stem for spec in specs}
    external_dir = root / "external"
    orphans: list[str] = []
    for md_path in sorted(external_dir.glob("*_latest*.md")):
        # Stem relative to RESULTS_ROOT, preserving the ``external/`` prefix and
        # dropping the ``.md`` suffix, so it compares equal to a ``Spec.stem``
        # such as ``external/longmemeval_s_latest_vector``.
        stem = md_path.relative_to(root).with_suffix("").as_posix()
        if stem not in mapped:
            orphans.append(stem)
            print(
                f"[gen_benchmark_pages] WARNING: unmapped artifact {stem!r} "
                f"({md_path}) — no Spec publishes it, so it is absent from the "
                "docs site. Add a Spec to SPECS to publish it.",
                file=sys.stderr,
            )
    return orphans


def main() -> None:
    rows: list[dict] = []
    for spec in SPECS:
        result = _write_page(spec)
        if result is not None:
            rows.append(result)
    _write_index(rows)
    _write_summary(rows)
    _warn_orphan_artifacts(SPECS)


if __name__ in ("__main__", "<run_path>"):
    # mkdocs-gen-files execs this file via ``runpy.run_path``, which sets
    # ``__name__`` to ``"<run_path>"``; running it directly sets ``"__main__"``.
    # Guarding on both keeps the generator running at build time while letting a
    # unit test ``import`` the module (to exercise SPECS / _warn_orphan_artifacts)
    # without triggering the full build-time generation.
    main()
