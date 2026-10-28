"""New users can find the public entry points and follow repository-local links."""
from pathlib import Path
import re

import pytest

from quantfactra.cli import main


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("args,label", [
    (["--help"], "quantfactra"),
    (["demo", "--help"], "quantfactra demo"),
    (["init", "--help"], "quantfactra init"),
    (["case", "--help"], "quantfactra case"),
    (["factor-case", "--help"], "quantfactra factor-case"),
    (["team", "--help"], "quantfactra team"),
])
def test_public_help_is_offline_and_does_not_need_config(args, label, capsys):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 0
    assert label in capsys.readouterr().out


@pytest.mark.parametrize("relative", ["README.md", "README_EN.md", "CONTRIBUTING.md", "docs/quickstart.md",
    "docs/backends.md", "docs/ai-integration.md", "docs/architecture.md", "docs/publication.md",
    "docs/public-research-case.md", "docs/bt-backend.md", "docs/model-providers.md",
    "docs/published-momentum-case.md", "docs/pages-and-release.md"])

def test_onboarding_document_local_links_exist(relative):
    path = ROOT / relative
    content = path.read_text(encoding="utf-8")
    urls = re.findall(r"\]\(([^)]+)\)|(?:src|href)=\"([^\"]+)\"", content)
    for markdown, attribute in urls:
        target = markdown or attribute
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0]
        assert (path.parent / target).exists(), f"{relative}: missing {target}"




@pytest.mark.parametrize("command", ["init", "demo"])
def test_repeating_first_run_preserves_existing_files_and_explains_recovery(command, tmp_path, capsys):
    output = tmp_path / "existing"
    output.mkdir()
    original = output / "keep.txt"
    original.write_text("original run", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main([command, "--output", str(output)])
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "Choose a new --output path" in message and "Traceback" not in message
    assert original.read_text(encoding="utf-8") == "original run"
    assert list(output.iterdir()) == [original]
