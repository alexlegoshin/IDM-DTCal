import pandas as pd
import pytest

from report import build_report, write_report_xlsx
from sensors import SENSOR_MODELS


def _params():
    return {
        'model': 'ДТ100А1',
        'i_nom': 100.0,
        'label': 'OrchSensor',
        'I_start': 0.0, 'I_stop': 10.0, 'I_step': 5.0,
        'V_limit': 5.0, 'delay': 0.1, 'cooling_delay': 0.2,
    }


def test_build_report_adds_expected_and_error_columns():
    # For i_nom=100.0: V_expected(I) = 6.0 + (4.0/100) * I
    # At I=0: V_expected = 6.0V, at I=100: V_expected = 10.0V, at I=-100: V_expected = 2.0V
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 6.0},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 8.0},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'reverse', 'I_set_A': -50.0, 'V_meas_V': 4.0},
    ])

    result = build_report(df, i_nom=100.0)

    assert 'V_expected_V' in result.columns
    assert 'Error_percent' in result.columns
    assert len(result) == 3
    # At I=0: V_expected = 6V, V_meas = 6V -> Error = 0%
    assert result.iloc[0]['Error_percent'] == pytest.approx(0.0, abs=1e-9)
    # At I=+50A: V_expected = 8V, V_meas = 8V -> Error = 0%
    assert result.iloc[1]['Error_percent'] == pytest.approx(0.0, abs=1e-9)
    # At I=-50A: V_expected = 4V, V_meas = 4V -> Error = 0%
    assert result.iloc[2]['Error_percent'] == pytest.approx(0.0, abs=1e-9)


def test_build_report_error_has_sign():
    """Error should preserve sign: positive if measured > expected, negative otherwise."""
    # For i_nom=100: V_expected(50) = 6 + (4/100)*50 = 8.0V
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 8.1},   # +0.1V above expected 8V
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 7.9},   # -0.1V below expected 8V
    ])

    result = build_report(df, i_nom=100.0)

    # Error = (V_meas - V_expected) / SPAN * 100, SPAN = 8V
    # First: (8.1 - 8.0) / 8 * 100 = +1.25%
    # Second: (7.9 - 8.0) / 8 * 100 = -1.25%
    assert result.iloc[0]['Error_percent'] > 0
    assert result.iloc[1]['Error_percent'] < 0


def test_write_report_xlsx_creates_file_with_two_sheets(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 6.0},
    ])
    params = _params()
    xlsx_path = tmp_path / "test_report.xlsx"

    df_with_error = build_report(df, i_nom=params['i_nom'])
    write_report_xlsx(xlsx_path, df_with_error, params)

    assert xlsx_path.exists()

    # Read and verify both sheets
    xl = pd.ExcelFile(xlsx_path)
    assert 'Инфо' in xl.sheet_names
    assert 'Данные' in xl.sheet_names

    info_df = pd.read_excel(xlsx_path, sheet_name='Инфо')
    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')

    # Check info sheet has metadata
    assert len(info_df) > 0
    assert len(data_df) == 1
    assert 'I_set_A' in data_df.columns
    assert 'V_meas_V' in data_df.columns
    assert 'Error_percent' in data_df.columns
