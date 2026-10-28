"""Portable Pages output must retain public evidence and exclude local runtime."""
import hashlib
from pathlib import Path
import shutil
import zipfile

import pytest
from panda_alpha import __version__

from scripts.build_site import ROOT, PUBLIC_FILES, PAGES, archive_site, build, validate_site


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("public-site") / "output"
    return root, build(root, revision="a" * 40)


def test_site_retains_all_six_paths_sources_and_working_subdirectory_links(built):
    root, manifest = built
    assert validate_site(root)
    assert manifest["case_id"] == "ecb-fx-momentum-v2" and manifest["version"] == __version__
    page = (root / "case/index.html").read_text(encoding="utf-8")
    assert page.count("<tr>") == 7 and "-15.84" in page
    assert (root / "case/data/rates.json").read_bytes() == (ROOT / "quantfactra/cases/fx_momentum/rates.json").read_bytes()
    assert (root / "case/data/result-v1.json").read_bytes() == (ROOT / "docs/public-case-result-v1.json").read_bytes()
    assert "https://yanxxzz.github.io/quantfactra/case/" in page
    assert "Sitemap: https://yanxxzz.github.io/quantfactra/sitemap.xml" in (root / "robots.txt").read_text()


def test_portable_archive_has_only_manifest_files_and_original_data(built, tmp_path):
    root, manifest = built
    archive = tmp_path / "demo.zip"
    receipt = archive_site(root, archive)
    with zipfile.ZipFile(archive) as saved:
        assert set(saved.namelist()) == {*manifest["files"], "site-manifest.json"}
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(saved.read(name)).hexdigest() == expected
        assert "Source: European Central Bank" in saved.read("OFFLINE-README.txt").decode()
        assert saved.testzip() is None
    assert receipt["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_site_manifest_uses_portable_relative_name_order(built):
    _, manifest = built
    assert list(manifest['files']) == sorted(manifest['files'])


def test_builder_never_globs_or_copies_private_local_files(tmp_path):
    source = tmp_path / "source"
    required = {*PUBLIC_FILES, *PAGES, "panda_alpha/__init__.py", "quantfactra/bt_engine.py", "quantfactra/adapters/bt.py", "quantfactra/published_factor.py"}
    for relative in required:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    private = source / ".runtime/account.local.json"
    private.parent.mkdir()
    private.write_text('{"private":"sentinel-do-not-publish"}')
    output = tmp_path / "site"
    manifest = build(output, root=source)
    assert all(".runtime" not in name and ".local.json" not in name for name in manifest["files"])
    assert not any("sentinel-do-not-publish" in p.read_text(encoding="utf-8", errors="ignore") for p in output.rglob("*") if p.is_file())


def test_changed_source_receipt_stops_before_any_output_is_created(tmp_path):
    resources = tmp_path / "source/quantfactra/cases/fx_momentum"
    resources.mkdir(parents=True)
    for name in ("source.json", "rates.json", "protocol.json", "protocol-v2.json", "execution-revision.json"):
        shutil.copyfile(ROOT / "quantfactra/cases/fx_momentum" / name, resources / name)
    (resources / "rates.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="frozen receipts"):
        build(tmp_path / "not-created", root=tmp_path / "source")
    assert not (tmp_path / "not-created").exists()


def test_builder_refuses_overwrite_and_reports_broken_links(built, tmp_path):
    root, _ = built
    with pytest.raises(FileExistsError):
        build(root)
    clone = tmp_path / "links"
    shutil.copytree(root, clone)
    page = clone / "index.html"
    page.write_text(page.read_text(encoding="utf-8").replace('href="case/"', 'href="missing/"'), encoding="utf-8")
    with pytest.raises(ValueError, match="Broken site link"):
        validate_site(clone)


def test_momentum_case_keeps_cost_risk_scope_and_does_not_redistribute_monthly_data(built):
    root, manifest = built
    page = (root/'momentum/index.html').read_text(encoding='utf-8')
    assert page.count('<tr>') == 7 and '6.45%' in page and '-78.44%' in page
    assert '1994-01' in page and '25 bps / 月' in page
    assert '组合收益级' in page and '200%' in page and '不是逐笔交易成本' in page
    assert 'french-momentum-1927-2016-v1' in manifest['case_ids']
    assert not any(name.endswith(('.zip', '.csv')) or 'monthly-index' in name for name in manifest['files'])
