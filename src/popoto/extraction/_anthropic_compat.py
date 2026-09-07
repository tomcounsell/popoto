"""Anthropic SDK capability checks for the extraction providers.

Private module. Holds the one fact every ``output_config`` call site in
this package depends on: the minimum ``anthropic`` version whose
``messages.create`` accepts the structured-output parameter, and a check
that fails loudly when the installed SDK is older than that.

Why this exists
---------------
``pyproject.toml`` declared ``anthropic>=0.40.0`` while three call sites
passed ``output_config=`` to ``messages.create``, a parameter that does
not exist that far back. An install satisfying the declared floor raised
``TypeError: create() got an unexpected keyword argument
'output_config'`` at request time -- and in
``ClaudeExtractionProvider.extract`` that landed in a blanket
``except Exception`` which logged a warning and returned ``[]``. Since
``[]`` is also the success value for "this text contained no extractable
facts", a too-old SDK presented as normal operation rather than as a
failure (see #670).

The remedy is to check at *client construction* instead. ``Anthropic()``
itself succeeds on every version from 0.40.0 through 1.2.0, so
construction is currently a silent pass-through and moving the failure
there costs nothing that works today. It also keeps
``ClaudeExtractionProvider.extract``'s documented "never raises"
contract intact -- the raise happens strictly earlier.

Measured minimum
----------------
``MINIMUM_ANTHROPIC_VERSION`` is not a guess. Every published release in
``[0.60.0, 0.100.0]`` (49 of them) was scanned for ``output_config`` in
``anthropic/resources/messages/messages.py``: absent through 0.76.0,
present from 0.77.0, monotonic with no re-introduction. The boundary was
then confirmed by installing the two adjacent versions into clean venvs
on Python 3.12.14 and inspecting the live signature:

===========  ==========================================
anthropic    ``output_config`` in ``messages.create``
===========  ==========================================
0.76.0       absent
0.77.0       present
===========  ==========================================
"""

import inspect
from typing import Any, FrozenSet

MINIMUM_ANTHROPIC_VERSION = "0.77.0"
"""Oldest anthropic release whose ``messages.create`` accepts ``output_config``.

Kept in lockstep with the ``anthropic`` floor in ``pyproject.toml``;
``tests/test_anthropic_floor.py`` fails if the two drift apart in either
direction.
"""

REQUIRED_CREATE_PARAMS: FrozenSet[str] = frozenset(
    {
        "model",
        "max_tokens",
        "system",
        "messages",
        "output_config",
    }
)
"""Every keyword this package passes to ``messages.create``.

The three call sites are ``extraction/claude.py``,
``extraction/resolution.py``, and ``extraction/verdict.py``. A call site
that gains a keyword outside this set is a floor question, not a local
change -- ``tests/test_anthropic_floor.py`` AST-scans the sources and
fails so the new parameter's minimum version gets established before it
ships.
"""


class AnthropicVersionError(RuntimeError):
    """Installed ``anthropic`` is too old for the calls this package makes.

    Deliberately distinct from the ``ImportError`` the providers raise
    when the package is absent entirely: a caller can tell "not
    installed" from "installed but too old", which need different
    remedies.
    """


def assert_messages_create_supported(client: Any) -> None:
    """Raise if ``client``'s ``messages.create`` can't take what we pass it.

    Args:
        client: An Anthropic-style client exposing ``messages.create``.

    Raises:
        AnthropicVersionError: If the signature is introspectable, does
            not accept arbitrary keywords, and is missing one or more of
            ``REQUIRED_CREATE_PARAMS``.

    Two shapes deliberately pass without inspection of individual
    parameters:

    * **A signature accepting ``**kwargs``.** A callable taking arbitrary
      keywords cannot be shown to reject ``output_config``. Every fake
      client in the test suite is of this shape, and treating them as
      too-old would break tests that have nothing to do with versions.
    * **A signature that cannot be introspected at all** -- a
      C-implemented or heavily proxied callable makes
      ``inspect.signature`` raise. This check exists to catch one
      specific known incompatibility, not to police client objects, so
      an unreadable signature is not evidence of anything.
    """
    try:
        create = client.messages.create
    except AttributeError:
        # Not a Messages-API-shaped client at all. Not this check's
        # business -- the call itself will say so far more clearly.
        return

    try:
        params = inspect.signature(create).parameters
    except (ValueError, TypeError):
        return

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return

    missing = sorted(REQUIRED_CREATE_PARAMS - set(params))
    if missing:
        raise AnthropicVersionError(
            "the installed anthropic package is too old for popoto's "
            "extraction providers: messages.create does not accept "
            f"{', '.join(missing)}. popoto requires anthropic>="
            f"{MINIMUM_ANTHROPIC_VERSION}. Upgrade it with: "
            "pip install --upgrade 'anthropic>="
            f"{MINIMUM_ANTHROPIC_VERSION}'"
        )
