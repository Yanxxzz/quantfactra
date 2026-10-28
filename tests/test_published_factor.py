"""Hand-calculated returns, strict source parsing and immutable reproduction."""
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from quantfactra import published_factor as factor


def zipped(text):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("F-F_Momentum_Factor.csv", text)
    return output.getvalue()


def protocol():
    p = factor.load_protocol()
    p.update(window={"start": "1930-01-01", "end": "1930-12-31"}, expected_months=12,
             periods=[{"label": "full", "start": "1930-01", "end": "1930-12"}])
    return p


def source_text():
    return 'Synthetic test fixture, not publisher data\n\n,Mom\n' + ''.join(f'1930{m:02},1.00\n' for m in range(1, 13)) + '\nAnnual Factors: January-December\n,Mom\n1930,99.0\n'


def test_parser_does_not_mix_annual_returns_or_parse_excluded_dates():
    text = source_text().replace('\nAnnual Factors', '201701,not-a-number\n\nAnnual Factors')
    rows, receipt = factor.parse_archive(zipped(text), protocol())
    assert len(rows) == 12 and all(value == Decimal(1) for _, value in rows)
    assert receipt['input_unit'] == 'percent per month'


@pytest.mark.parametrize('old,new', [('193005,1.00\n',''), ('193005,1.00','193004,1.00'),
                                  ('193005,1.00','193005,-99.99'), ('193005,1.00','193005,NaN'),
                                  (',Mom', ',Other'), ('193005,1.00','193005,1.00,2.00')])
def test_source_missingness_duplicates_and_wrong_schema_are_rejected(old, new):
    with pytest.raises(ValueError):
        factor.parse_archive(zipped(source_text().replace(old, new)), protocol())


def test_compounding_and_initial_wealth_drawdown_match_hand_calculation():
    rows = [('1930-01', Decimal('-20')), ('1930-02', Decimal('25'))]
    metrics, nav, dd, audit = factor.summarize(rows, 0)
    assert nav == pytest.approx([1, .8, 1])
    assert metrics['geometric_annual_return'] == pytest.approx(0, abs=1e-14)
    assert metrics['max_drawdown'] == pytest.approx(-.2)
    assert metrics['worst_year']['return'] == pytest.approx(0)
    assert audit['passed'] and audit['checked_months'] == 2
    deducted, _, _, _ = factor.summarize([('1930-01', Decimal(1))]*12, 10)
    assert deducted['terminal_index'] == pytest.approx(1.009**12)


@pytest.mark.parametrize('value', ['-100', '-101'])
def test_bankrupt_index_is_not_compounded(value):
    with pytest.raises(ValueError, match='-100%'):
        factor.summarize([('1930-01', Decimal(value))], 0)


@pytest.fixture
def fixture_source(tmp_path, monkeypatch):
    resources = tmp_path / 'resources'
    resources.mkdir()
    p = protocol()
    raw = json.dumps(p).encode()
    content = zipped(source_text())
    _, receipt = factor.parse_archive(content, p)
    (resources/'protocol.json').write_bytes(raw)
    (resources/'reference.json').write_text(json.dumps({'protocol_sha256': hashlib.sha256(raw).hexdigest(), 'selected_returns_sha256': receipt['selected_returns_sha256']}))
    (resources/'DATA_NOTICE.md').write_text('Synthetic test data')
    monkeypatch.setattr(factor, 'resources', lambda: resources)
    source = tmp_path/'source.zip'
    source.write_bytes(content)
    return source


def test_run_resume_and_tamper_detection(fixture_source, tmp_path):
    root = tmp_path/'case'
    first = factor.run_factor_case(root, source_path=fixture_source)
    assert first['historical_positive_evidence'] and not first['stock_level_replication']
    assert first['official_runs'] == 0
    assert (root/'report.html').is_file()
    assert factor.run_factor_case(root, resume_existing=True)['reused_existing']
    with pytest.raises(FileExistsError):
        factor.run_factor_case(root, source_path=fixture_source)
    with pytest.raises(ValueError, match='new source'):
        factor.run_factor_case(root, source_path=fixture_source, resume_existing=True)
    (root/'source.zip').write_bytes(b'changed')
    with pytest.raises(ValueError, match='hashes'):
        factor.run_factor_case(root, resume_existing=True)


def test_revised_vintage_stops_before_output(fixture_source, tmp_path):
    fixture_source.write_bytes(zipped(source_text().replace('193005,1.00','193005,2.00')))
    root = tmp_path/'not-created'
    with pytest.raises(ValueError, match='different data vintage'):
        factor.run_factor_case(root, source_path=fixture_source)
    assert not root.exists()


def test_sealed_window_rejected_before_any_download(monkeypatch, tmp_path):
    p = protocol()
    p['window'] = {'start': '2017-01-01', 'end': '2018-12-31'}
    (tmp_path/'protocol.json').write_text(json.dumps(p))
    monkeypatch.setattr(factor, 'resources', lambda: tmp_path)
    monkeypatch.setattr(factor, 'urlopen', lambda *a, **kw: pytest.fail('Network must not be reached'))
    with pytest.raises(ValueError, match='sealed OOS'):
        factor.run_factor_case(tmp_path/'case', download=True)


def test_cli_requires_source_without_creating_directory(tmp_path, capsys):
    from quant_research.cli import main
    root = tmp_path/'case'
    with pytest.raises(SystemExit) as error:
        main(['factor-case', '--output', str(root)])
    assert error.value.code == 2 and '--source' in capsys.readouterr().err
    assert not root.exists()
