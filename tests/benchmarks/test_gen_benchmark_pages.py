"""Tests for the benchmark docs generator ``docs/scripts/gen_benchmark_pages.py``.

The generator publishes committed ``*_latest*.{json,md}`` benchmark artifacts to
the docs site from an *explicit* ``SPECS`` list. These tests cover the #469
additions:

* vector-mode ``Spec``s exist, target the correct ``_vector`` artifact stems,
  and sit in deterministic dataset-then-mode order;
* the vector framing notes fabricate no metric numbers (benchmark doctrine:
  never publish results that do not exist as committed artifacts);
* ``_warn_orphan_artifacts`` turns the previously-silent "unmapped artifact"
  gap into a loud stderr warning, without raising and without touching the real
  ``results/external/`` tree (the scan root is injectable).

The generator lives under ``docs/scripts/`` (not an importable package) and its
``main()`` is guarded so ``import`` does not trigger the full build-time run.
We therefore load it by file path via ``importlib``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN_PATH = _REPO_ROOT / "docs" / "scripts" / "gen_benchmark_pages.py"

pytest.importorskip(
    "mkdocs_gen_files",
    reason="gen_benchmark_pages imports mkdocs_gen_files (docs extra)",
)


def _load_generator():
    """Import the generator module by file path without running ``main()``."""
    spec = importlib.util.spec_from_file_location("gen_benchmark_pages", _GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/refs resolve against a real module.
    sys.modules["gen_benchmark_pages"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


def test_import_does_not_run_main(gen):
    """The ``main()`` guard must keep plain import side-effect-free.

    If import triggered ``main()`` it would call ``mkdocs_gen_files.open`` outside
    a build and blow up; reaching this assertion at all proves the guard holds.
    """
    assert callable(gen.main)
    assert callable(gen._warn_orphan_artifacts)


def _specs_by_slug(gen):
    return {spec.slug: spec for spec in gen.SPECS}


def test_vector_specs_present_and_ordered(gen):
    by_slug = _specs_by_slug(gen)

    # Both vector specs exist with the correct artifact stems and kind.
    assert "longmemeval_s_vector" in by_slug
    assert "locomo_vector" in by_slug

    lme = by_slug["longmemeval_s_vector"]
    loc = by_slug["locomo_vector"]
    assert lme.stem == "external/longmemeval_s_latest_vector"
    assert loc.stem == "external/locomo_latest_vector"
    assert lme.kind == "external"
    assert loc.kind == "external"

    # Deterministic order: each vector spec follows its dataset's hybrid spec.
    slugs = [spec.slug for spec in gen.SPECS]
    assert slugs.index("longmemeval_s_vector") > slugs.index("longmemeval_s_hybrid")
    assert slugs.index("locomo_vector") > slugs.index("locomo_hybrid")
    # CSR stays last.
    assert slugs.index("csr") == len(slugs) - 1


def test_vector_specs_group_with_their_dataset_tables(gen):
    """Slug prefixes must keep the index headline tables grouping correctly.

    ``_dataset_table`` filters by ``slug.startswith(prefix)``; the vector slugs
    must share the dataset prefix so they land in the right table.
    """
    assert "longmemeval_s_vector".startswith("longmemeval_s")
    assert "locomo_vector".startswith("locomo")
    # Guard against the LoCoMo prefix accidentally swallowing longmemeval rows.
    assert not "longmemeval_s_vector".startswith("locomo")


def test_vector_notes_have_no_fabricated_numbers(gen):
    """Doctrine: publish only results that exist as committed artifacts.

    No vector artifact is committed yet, so the framing notes must not assert any
    concrete **Popoto** recall/MRR figures — those come from the committed
    artifact at build time. Citing an external published anchor (MEMTIER) is
    explicitly allowed and mirrors the existing lexical/hybrid notes, so we
    forbid attributed ``Recall@``/``MRR`` metric literals but permit a bare
    decimal only when the note cites MEMTIER.
    """
    import re

    # Fabricated Popoto results are written as attributed metric literals.
    attributed_metric = [
        re.compile(r"Recall@\d+\s*[:=]?\s*0?\.\d"),  # "Recall@1 0.89"
        re.compile(r"\bMRR\b\s*[:=]?\s*0?\.\d"),  # "MRR 0.93"
    ]
    bare_decimal = re.compile(r"\b0\.\d{2,}\b")

    for slug in ("longmemeval_s_vector", "locomo_vector"):
        note = _specs_by_slug(gen)[slug].note
        for pat in attributed_metric:
            match = pat.search(note)
            assert match is None, (
                f"{slug} note contains a fabricated metric literal "
                f"{match.group(0)!r}; vector numbers must come from the "
                f"committed artifact, not the framing note"
            )
        # Any bare decimal is permitted ONLY as the cited MEMTIER anchor band.
        if bare_decimal.search(note):
            assert "MEMTIER" in note, (
                f"{slug} note has a bare decimal that is not the cited MEMTIER "
                f"retrieval-regime anchor — that reads as a fabricated result"
            )


def test_metric_family_not_cross_compared_in_vector_notes(gen):
    """Vector notes must not tabulate recall beside LLM-judge accuracy."""
    for slug in ("longmemeval_s_vector", "locomo_vector"):
        note = _specs_by_slug(gen)[slug].note.lower()
        assert "judge" not in note
        assert "answer accuracy" not in note


def _all_framing_text(gen) -> str:
    """Every string of framing prose this generator can publish."""
    return "\n".join([spec.note for spec in gen.SPECS] + [gen.INDEX_FRAMING])


def test_memtier_band_claim_is_not_reintroduced(gen):
    """The "LoCoMo Recall@1 sits in the 0.10-0.30 band" claim stays dead.

    MEMTIER's own authors describe those LoCoMo retrieval baselines as
    uninformative, so quoting them as a band Popoto sits at the top of
    overstates the result. The MEMTIER *anchor* (96.7 ms/query latency, regime
    context) is retained deliberately; only the band framing is forbidden.
    """
    text = _all_framing_text(gen)
    for band in ("0.10–0.30", "0.10-0.30"):
        assert band not in text, (
            f"The MEMTIER retrieval band {band!r} reappeared in generated "
            f"framing text. Cite the MEMTIER anchor without the band."
        )
    assert "MEMTIER" in text, (
        "The MEMTIER anchor was removed entirely. The band claim is what was "
        "killed; the anchor is doctrine and must stay."
    )


def test_no_vendor_judged_accuracy_band_tabulated(gen):
    """Vendor judge-accuracy percentages are never quoted as a band.

    The metric-family warning names the vendors so a reader can recognise the
    boards it is warning about, but quoting their spread as a range invites the
    exact recall-vs-judged cross-read the warning exists to prevent.
    """
    text = _all_framing_text(gen)
    for band in ("52–92", "52-92"):
        assert band not in text, (
            f"Vendor judged-accuracy band {band!r} reappeared in generated "
            f"framing text."
        )


def _write_artifact(root: Path, name: str) -> None:
    external = root / "external"
    external.mkdir(parents=True, exist_ok=True)
    (external / name).write_text("# placeholder\n")


def test_orphan_artifact_scan_warns(gen, tmp_path, capsys):
    """An unmapped ``external/*_latest*.md`` triggers a loud stderr warning."""
    _write_artifact(tmp_path, "foo_latest_experimental.md")

    orphans = gen._warn_orphan_artifacts(gen.SPECS, root=tmp_path)

    assert "external/foo_latest_experimental" in orphans
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "foo_latest_experimental" in err


def test_orphan_scan_silent_when_all_mapped(gen, tmp_path, capsys):
    """No warning when every artifact corresponds to a Spec stem."""
    # Materialize the .md file for an existing external Spec stem.
    stem = "external/longmemeval_s_latest_vector"
    assert any(s.stem == stem for s in gen.SPECS)
    _write_artifact(tmp_path, "longmemeval_s_latest_vector.md")

    orphans = gen._warn_orphan_artifacts(gen.SPECS, root=tmp_path)

    assert orphans == []
    assert "WARNING" not in capsys.readouterr().err


def test_orphan_scan_silent_on_empty_root(gen, tmp_path, capsys):
    """An absent/empty ``external/`` dir yields no warning and does not raise."""
    orphans = gen._warn_orphan_artifacts(gen.SPECS, root=tmp_path)
    assert orphans == []
    assert "WARNING" not in capsys.readouterr().err


def test_orphan_scan_ignores_lone_json(gen, tmp_path, capsys):
    """A ``.json`` with no ``.md`` is not an unpublished page — no warning.

    ``_write_page`` drives the page off the ``.md`` body, so a lone ``.json``
    could never produce a page and must not be flagged as an orphan.
    """
    external = tmp_path / "external"
    external.mkdir(parents=True)
    (external / "bar_latest_experimental.json").write_text("{}")

    orphans = gen._warn_orphan_artifacts(gen.SPECS, root=tmp_path)

    assert orphans == []
    assert "WARNING" not in capsys.readouterr().err


def test_no_broken_symlinks_under_results():
    """Every committed ``results/`` symlink resolves (issues #511, #514).

    A dangling ``*_latest*`` symlink is invisible in a normal ``ls`` and makes
    the generator silently skip a page while ``_warn_orphan_artifacts`` still
    flags it as unmapped — two confusing signals for one missing file. Two of
    these (``locomo_latest_graph``, ``locomo_latest_ext-heuristic``) shipped
    pointing at artifacts that were never committed.
    """
    results_root = _REPO_ROOT / "tests" / "benchmarks" / "results"
    dangling = [
        str(p.relative_to(_REPO_ROOT))
        for p in results_root.rglob("*")
        if p.is_symlink() and not p.exists()
    ]
    assert dangling == []
