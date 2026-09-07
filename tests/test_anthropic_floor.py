"""The declared anthropic floor must match what the code actually calls.

Regression tests for #670: ``pyproject.toml`` declared ``anthropic>=0.40.0``
while three call sites passed ``output_config=`` to ``messages.create``, a
parameter that first appears in 0.77.0. An install satisfying the declared
floor raised ``TypeError`` inside ``ClaudeExtractionProvider.extract``'s
blanket ``except Exception``, which returned ``[]`` -- the same value that
means "this text contained no facts". The mismatch was therefore invisible.

These tests must pass with ``anthropic`` NOT installed, which is the CI
condition: it is an optional extra and is not in the ``dev`` extra. So the
call-site check (TC2) is an AST scan of the sources rather than a live
signature inspection; TC7 does the live check and skips when absent.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

from popoto.extraction._anthropic_compat import (
    MINIMUM_ANTHROPIC_VERSION,
    REQUIRED_CREATE_PARAMS,
    AnthropicVersionError,
    assert_messages_create_supported,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "popoto"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

CALL_SITE_FILES = [
    SRC / "extraction" / "claude.py",
    SRC / "extraction" / "resolution.py",
    SRC / "extraction" / "verdict.py",
]


def _declared_floor() -> str:
    """The ``anthropic>=X`` version declared in pyproject.toml's extra."""
    text = PYPROJECT.read_text()
    matches = re.findall(r'"anthropic>=([0-9][^"]*)"', text)
    assert matches, "no anthropic floor found in pyproject.toml"
    assert len(matches) == 1, f"expected exactly one anthropic floor, got {matches}"
    return matches[0]


def _messages_create_kwargs(path: Path):
    """Keyword names passed to any ``*.messages.create(...)`` call in a file.

    Returns a list with one entry (a set of keyword names) per call site
    found, so a caller can assert on how many were located as well as on
    what they pass.
    """
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "create":
            continue
        # ...<something>.messages.create(...)
        if not (
            isinstance(func.value, ast.Attribute) and func.value.attr == "messages"
        ):
            continue
        found.append({kw.arg for kw in node.keywords if kw.arg is not None})
    return found


# --------------------------------------------------------------------------
# TC1 -- the floor and the constant must not drift apart
# --------------------------------------------------------------------------


def test_declared_floor_matches_minimum_constant():
    """pyproject's anthropic floor equals MINIMUM_ANTHROPIC_VERSION.

    Fails in either direction, so neither can be edited alone.
    """
    assert _declared_floor() == MINIMUM_ANTHROPIC_VERSION


# --------------------------------------------------------------------------
# TC2 -- the issue's AC4: a call site gaining a parameter must fail here
# --------------------------------------------------------------------------


def test_call_sites_stay_within_the_declared_floors_surface():
    """No messages.create call passes a keyword outside REQUIRED_CREATE_PARAMS.

    This is the guard the issue asked for. Adding a parameter to any call
    site is a floor question -- the new parameter's own minimum version has
    to be established, and MINIMUM_ANTHROPIC_VERSION possibly raised,
    before it can ship. Failing here is what forces that.
    """
    for path in CALL_SITE_FILES:
        call_sites = _messages_create_kwargs(path)
        # Assert the scan actually found something: a refactor that builds
        # kwargs into a dict and splats them would otherwise make this test
        # silently vacuous instead of failing.
        assert call_sites, f"no messages.create call found in {path.name}"
        for kwargs in call_sites:
            assert kwargs, f"messages.create in {path.name} passes no keywords"
            unexpected = kwargs - REQUIRED_CREATE_PARAMS
            assert not unexpected, (
                f"{path.name} passes {sorted(unexpected)} to messages.create, "
                f"which is not covered by the declared floor "
                f"anthropic>={MINIMUM_ANTHROPIC_VERSION}. Establish the "
                f"minimum version providing it, update "
                f"MINIMUM_ANTHROPIC_VERSION and REQUIRED_CREATE_PARAMS, and "
                f"raise the floor in pyproject.toml."
            )


def test_exception_is_reachable_from_the_package_namespace():
    """A caller can catch the error without importing a private module.

    ``AnthropicVersionError`` escapes from a public constructor, so it is
    part of the public surface and must be importable as such.
    """
    import popoto.extraction as extraction_pkg

    assert extraction_pkg.AnthropicVersionError is AnthropicVersionError
    assert extraction_pkg.MINIMUM_ANTHROPIC_VERSION == MINIMUM_ANTHROPIC_VERSION
    assert "AnthropicVersionError" in extraction_pkg.__all__


def test_output_config_is_actually_among_the_scanned_kwargs():
    """The scan sees output_config -- the parameter this whole issue is about.

    Guards against _messages_create_kwargs silently matching nothing
    useful and TC2 passing for the wrong reason.
    """
    for path in CALL_SITE_FILES:
        call_sites = _messages_create_kwargs(path)
        assert any("output_config" in kwargs for kwargs in call_sites), (
            f"{path.name} no longer passes output_config; if that is "
            f"intentional the floor may be loosenable"
        )


# --------------------------------------------------------------------------
# TC3 -- the issue's AC3: a version mismatch is not an empty result
# --------------------------------------------------------------------------


class _OldMessages:
    """messages.create as it existed before 0.77.0 -- no output_config."""

    def create(self, *, model, max_tokens, system, messages):  # pragma: no cover
        raise AssertionError("should never be called")


class _OldClient:
    def __init__(self, *args, **kwargs):
        self.messages = _OldMessages()


def test_too_old_client_raises_rather_than_extracting_nothing(monkeypatch):
    """Constructing the provider against a pre-0.77.0 client raises.

    The defect was that this configuration produced ``[]`` from
    ``extract()``, indistinguishable from "no facts found". Now it cannot
    reach ``extract()`` at all.
    """
    from popoto.extraction import claude as claude_mod

    fake_module = type("FakeAnthropicModule", (), {"Anthropic": _OldClient})
    monkeypatch.setattr(claude_mod, "anthropic_module", fake_module)
    monkeypatch.setattr(claude_mod, "_anthropic_available", True)

    with pytest.raises(AnthropicVersionError) as excinfo:
        claude_mod.ClaudeExtractionProvider(api_key="test-key")

    message = str(excinfo.value)
    assert "output_config" in message
    assert MINIMUM_ANTHROPIC_VERSION in message


def test_extract_still_never_raises_on_a_supported_client(monkeypatch):
    """The never-raises contract of extract() is unchanged by the new check."""
    from popoto.extraction import claude as claude_mod

    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("API exploded")

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            self.messages = _BoomMessages()

    fake_module = type("FakeAnthropicModule", (), {"Anthropic": _BoomClient})
    monkeypatch.setattr(claude_mod, "anthropic_module", fake_module)
    monkeypatch.setattr(claude_mod, "_anthropic_available", True)

    provider = claude_mod.ClaudeExtractionProvider(api_key="test-key")
    assert provider.extract("Alice met Bob in Paris.") == []


# --------------------------------------------------------------------------
# TC4/TC5 -- the two deliberate escape hatches
# --------------------------------------------------------------------------


def test_kwargs_accepting_client_is_allowed():
    """A create(**kwargs) signature passes -- it cannot be shown to be old.

    Every fake client in the existing test suite is this shape. Treating
    them as too-old would break tests that have nothing to do with versions.
    """

    class _KwargsMessages:
        def create(self, **kwargs):  # pragma: no cover
            return None

    class _KwargsClient:
        messages = _KwargsMessages()

    assert_messages_create_supported(_KwargsClient())


def test_uninspectable_client_is_allowed():
    """A signature inspect cannot read passes rather than failing closed."""

    class _Uninspectable:
        # A callable whose signature raises when introspected.
        def __call__(self, *args, **kwargs):  # pragma: no cover
            return None

        @property
        def __signature__(self):
            raise ValueError("no signature for you")

    class _WeirdMessages:
        create = _Uninspectable()

    class _WeirdClient:
        messages = _WeirdMessages()

    assert_messages_create_supported(_WeirdClient())


def test_non_messages_client_is_allowed():
    """An object that is not Messages-API-shaped is not this check's business."""

    class _NotAClient:
        pass

    assert_messages_create_supported(_NotAClient())


def test_modern_signature_is_allowed():
    """An explicit signature that does list output_config passes."""

    class _ModernMessages:
        def create(
            self, *, model, max_tokens, system, messages, output_config
        ):  # pragma: no cover
            return None

    class _ModernClient:
        messages = _ModernMessages()

    assert_messages_create_supported(_ModernClient())


# --------------------------------------------------------------------------
# TC6 -- the sibling seams, whose caller contracts must not change
# --------------------------------------------------------------------------


def test_resolution_default_client_rejects_old_sdk(monkeypatch):
    from popoto.extraction import resolution as resolution_mod

    fake_module = type("FakeAnthropicModule", (), {"Anthropic": _OldClient})
    monkeypatch.setattr(resolution_mod, "anthropic_module", fake_module)
    monkeypatch.setattr(resolution_mod, "_anthropic_available", True)

    with pytest.raises(AnthropicVersionError):
        resolution_mod._default_client()


def test_verdict_default_client_rejects_old_sdk(monkeypatch):
    from popoto.extraction import verdict as verdict_mod

    fake_module = type("FakeAnthropicModule", (), {"Anthropic": _OldClient})
    monkeypatch.setattr(verdict_mod, "anthropic_module", fake_module)
    monkeypatch.setattr(verdict_mod, "_anthropic_available", True)

    with pytest.raises(AnthropicVersionError):
        verdict_mod._default_client()


def test_verdict_still_degrades_rather_than_raising(monkeypatch):
    """llm_verdict's caller contract is unchanged: it still returns, not raises.

    The new check makes the log message precise; it does not change what
    callers see.
    """
    from popoto.extraction import verdict as verdict_mod
    from popoto.extraction.candidates import Candidate

    fake_module = type("FakeAnthropicModule", (), {"Anthropic": _OldClient})
    monkeypatch.setattr(verdict_mod, "anthropic_module", fake_module)
    monkeypatch.setattr(verdict_mod, "_anthropic_available", True)

    candidate = Candidate(
        text="Alice met Bob in Paris.",
        turn_id="t1",
        candidate_id="t1:sentence:0",
        start=0,
        end=23,
        generator_rule="sentence",
    )
    result = verdict_mod.llm_verdict(candidate)
    assert result.verdict == verdict_mod.Verdict.REJECT
    assert result.reason_code == verdict_mod.ReasonCode.LLM_UNAVAILABLE


# --------------------------------------------------------------------------
# TC7 -- live agreement, when anthropic happens to be installed
# --------------------------------------------------------------------------


def test_installed_anthropic_satisfies_the_declared_floor():
    """If anthropic is installed, it is >= the floor and has every parameter."""
    anthropic_mod = pytest.importorskip("anthropic")

    from packaging.version import Version

    installed = Version(anthropic_mod.__version__)
    assert installed >= Version(MINIMUM_ANTHROPIC_VERSION), (
        f"installed anthropic {installed} is below the declared floor "
        f"{MINIMUM_ANTHROPIC_VERSION}"
    )

    client = anthropic_mod.Anthropic(api_key="test-key-not-used")
    params = inspect.signature(client.messages.create).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        missing = sorted(REQUIRED_CREATE_PARAMS - set(params))
        assert not missing, (
            f"anthropic {installed} is missing {missing} from "
            f"messages.create despite satisfying the declared floor"
        )
