import builtins

import pytest

from cli import build_parser, resolve_measure_params, make_result_filename, validate_measure_params
from config import ConfigManager


def _good_params():
    return {'model': 'ДТ100А1', 'i_nom': 100.0,
            'I_start': 0.0, 'I_stop': 10.0, 'I_step': 1.0,
            'delay': 0.1, 'cooling_delay': 0.1, 'V_limit': 5.0, 'label': 'test'}


def test_validate_ok_for_valid_params():
    assert validate_measure_params(_good_params()) == []


def test_validate_rejects_zero_step():
    p = _good_params()
    p['I_step'] = 0
    errors = validate_measure_params(p)
    assert any('Шаг' in e for e in errors)


def test_validate_rejects_stop_below_start():
    p = _good_params()
    p['I_start'] = 10
    p['I_stop'] = 5
    errors = validate_measure_params(p)
    assert any('Конечное' in e for e in errors)


def test_validate_rejects_negative_delays():
    p = _good_params()
    p['delay'] = -1
    p['cooling_delay'] = -2
    errors = validate_measure_params(p)
    assert len(errors) >= 2


def test_validate_requires_positive_vlimit():
    p = _good_params()
    p['V_limit'] = 0
    errors = validate_measure_params(p)
    assert any('напряжения' in e for e in errors)


def _measure_args(parser, extra_args):
    return parser.parse_args(["measure"] + extra_args)


def test_make_result_filename_sanitizes_label(tmp_path):
    path = make_result_filename(tmp_path, "ДТ100А1", "VAC 4646X100 #test!")
    assert path.parent == tmp_path
    assert path.name.startswith("DTCal_ДТ100А1_VAC_4646X100_test_")
    assert path.suffix == ".xlsx"


def test_make_result_filename_empty_label_uses_nolabel(tmp_path):
    path = make_result_filename(tmp_path, "ДТ100А1", "")
    assert "nolabel" in path.name


def test_make_result_filename_none_label_uses_nolabel(tmp_path):
    path = make_result_filename(tmp_path, "ДТ100А1", None)
    assert "nolabel" in path.name


def test_resolve_measure_params_from_full_cli_args(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--model", "DT100A1",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "TestSensor", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    params = resolve_measure_params(args, mgr)

    assert params['model'] == 'ДТ100А1'
    assert params['i_nom'] == 100.0
    assert params['I_start'] == 0
    assert params['I_stop'] == 10
    assert params['I_step'] == 1
    assert params['V_limit'] == 5
    assert params['label'] == 'TestSensor'
    assert mgr.load() == params


def test_resolve_measure_params_step_zero_raises_value_error(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--model", "DT100A1",
        "--start", "0", "--stop", "10", "--step", "0",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "Bad", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    with pytest.raises(ValueError, match="Шаг"):
        resolve_measure_params(args, mgr)


def test_resolve_measure_params_stop_less_than_start_raises_value_error(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--model", "DT100A1",
        "--start", "10", "--stop", "5", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "Bad", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    with pytest.raises(ValueError, match="Конечное"):
        resolve_measure_params(args, mgr)


def test_resolve_measure_params_negative_vlimit_raises_value_error(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--model", "DT100A1",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "-1", "--delay", "0.1", "--cool", "0.1",
        "--label", "Bad", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    with pytest.raises(ValueError, match="напряжения"):
        resolve_measure_params(args, mgr)
