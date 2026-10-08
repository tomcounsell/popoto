"""The ``popoto-migrate-redis-to-postgres`` console script (1.10.0).

The migration tool (#756) was reachable only as ``python -m
popoto.migrate_redis_to_postgres``. The release adds a console script. These
tests pin the three things that make the two forms equivalent: the entry
point resolves to a callable, ``--help`` names the form that was used, and the
operator's ``--mapping`` module is found from the working directory either
way (``-m`` puts the working directory on ``sys.path``; a console script puts
its own ``bin`` directory there instead).
"""

import sys
import tomllib
from pathlib import Path

import pytest

import popoto.migrate_redis_to_postgres as tool

REPO = Path(__file__).resolve().parents[1]


def _entry_point() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    return data["project"]["scripts"][tool.CONSOLE_SCRIPT]


def test_the_console_script_points_at_console_main():
    module_name, _, attribute = _entry_point().partition(":")
    # The package, never __main__.py: that file must only delegate (#792).
    assert module_name == "popoto.migrate_redis_to_postgres"
    assert attribute == "console_main"
    assert callable(getattr(tool, attribute))


@pytest.mark.parametrize(
    "argv0, prog",
    [
        ("/venv/bin/popoto-migrate-redis-to-postgres", tool.CONSOLE_SCRIPT),
        ("/x/popoto/migrate_redis_to_postgres/__main__.py", "python -m"),
    ],
)
def test_help_names_the_form_that_was_used(monkeypatch, capsys, argv0, prog):
    monkeypatch.setattr(sys, "argv", [argv0, "--help"])
    with pytest.raises(SystemExit) as done:
        tool.console_main(["--help"])
    assert done.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: {prog}")


def test_console_main_finds_a_mapping_module_in_the_working_directory(
    tmp_path, monkeypatch
):
    (tmp_path / "rel110_mapping_probe.py").write_text("MAPPINGS = []\n")
    monkeypatch.chdir(tmp_path)
    path = [p for p in sys.path if p not in ("", str(tmp_path))]
    monkeypatch.setattr(sys, "path", path)
    seen = {}

    def fake_main(argv=None):
        seen["obj"] = tool._import_object("rel110_mapping_probe:MAPPINGS")
        return 0

    monkeypatch.setattr(tool, "main", fake_main)
    try:
        assert tool.console_main([]) == 0
    finally:
        sys.modules.pop("rel110_mapping_probe", None)
    assert seen["obj"] == []
    assert sys.path[0] == str(tmp_path)


def test_an_unimportable_mapping_module_is_a_refusal_naming_pythonpath():
    with pytest.raises(tool.MigrationRefused, match="PYTHONPATH"):
        tool._import_object("rel110_no_such_module_anywhere:MAPPINGS")
