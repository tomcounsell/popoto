"""Tests for popoto.__version__ (issue #370 item 3)."""

import re
from pathlib import Path

import popoto


def test_version_attribute_exists():
    """popoto.__version__ is a non-empty PEP 440-looking string."""
    assert hasattr(popoto, "__version__"), "popoto.__version__ is missing"
    assert isinstance(popoto.__version__, str)
    assert popoto.__version__, "popoto.__version__ is empty"
    # PEP 440 minimum: N.N.N... optionally followed by + local / .devN etc.
    assert re.match(
        r"^\d+\.\d+\.\d+", popoto.__version__
    ), f"popoto.__version__={popoto.__version__!r} does not match N.N.N prefix"


def test_version_matches_pyproject():
    """__version__ should begin with the pyproject.toml project version.

    Allows dev/post suffixes (e.g. 1.5.0.dev1) but rejects silent drift.
    If popoto is not installed from this source tree, the fallback
    "0.0.0+unknown" is acceptable — we only enforce agreement when the
    installed distribution is clearly this project.
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "Could not parse version from pyproject.toml"
    project_version = match.group(1)

    # If we hit the source-tree fallback, skip the equality assertion — the
    # package isn't installed from metadata, so importlib.metadata can't see
    # the pyproject version.
    if popoto.__version__ == "0.0.0+unknown":
        return

    assert popoto.__version__.startswith(project_version), (
        f"popoto.__version__={popoto.__version__!r} does not start with "
        f"pyproject version {project_version!r}"
    )
