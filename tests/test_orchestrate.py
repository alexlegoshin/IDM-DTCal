import pandas as pd
import pytest
from openpyxl import load_workbook

from report import build_report, write_report_xlsx, SIGNED_PERCENT_FORMAT


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
    assert result.iloc[0]['Error_percent'] == pytest.approx(1.25)
    assert result.iloc[1]['Error_percent'] == pytest.approx(-1.25)


def test_build_report_on_empty_dataframe_returns_columns_not_crash():
    """
    Регрессия: если измерение остановили до первой точки, DataFrame пуст и
    обращение к df['I_set_A'] падало с KeyError, унося с собой весь сеанс.
    """
    result = build_report(pd.DataFrame(), i_nom=100.0)

    assert result.empty
    for col in ('I_set_A', 'V_meas_V', 'V_expected_V', 'Error_percent'):
        assert col in result.columns


def test_build_report_rejects_nonpositive_i_nom():
    """i_nom=0 дал бы деление на ноль в expected_voltage()."""
    df = pd.DataFrame([{'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 6.0}])

    with pytest.raises(ValueError):
        build_report(df, i_nom=0.0)


def test_build_report_keeps_nan_readings_as_nan():
    """Неудачное чтение (NaN) не должно превратиться в погрешность-число."""
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': float('nan')},
    ])

    result = build_report(df, i_nom=100.0)

    assert pd.isna(result.iloc[0]['Error_percent'])


def test_write_report_xlsx_creates_file_with_two_sheets(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 6.0},
    ])
    params = _params()
    xlsx_path = tmp_path / "test_report.xlsx"

    df_with_error = build_report(df, i_nom=params['i_nom'])
    write_report_xlsx(xlsx_path, df_with_error, params)

    assert xlsx_path.exists()

    xl = pd.ExcelFile(xlsx_path)
    assert 'Инфо' in xl.sheet_names
    assert 'Данные' in xl.sheet_names

    info_df = pd.read_excel(xlsx_path, sheet_name='Инфо')
    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')

    assert len(info_df) > 0
    assert len(data_df) == 1
    # В отчёте заголовки человекочитаемые (внутри DataFrame остаются латиницей).
    assert 'I задан., А' in data_df.columns
    assert 'U измер., В' in data_df.columns
    assert 'Погрешность, %' in data_df.columns


def test_write_report_xlsx_translates_branch_names(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 6.0},
        {'Timestamp': 'x', 'Branch': 'reverse', 'I_set_A': -50.0, 'V_meas_V': 4.0},
    ])
    xlsx_path = tmp_path / "branches.xlsx"

    write_report_xlsx(xlsx_path, build_report(df, i_nom=100.0), _params())

    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')
    assert list(data_df['Направление']) == ['прямое', 'обратное']


def test_write_report_xlsx_formats_sheet_for_reading(tmp_path):
    """
    Оформление должно быть готовым: закреплённая жирная шапка и формат
    погрешности с явным знаком — чтобы файл не подгоняли руками.
    """
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 8.1},
    ])
    xlsx_path = tmp_path / "formatted.xlsx"

    write_report_xlsx(xlsx_path, build_report(df, i_nom=100.0), _params())

    ws = load_workbook(xlsx_path)['Данные']
    assert ws.freeze_panes == 'A2'
    assert ws.cell(row=1, column=1).font.bold is True

    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    err_col = headers.index('Погрешность, %') + 1
    assert ws.cell(row=2, column=err_col).number_format == SIGNED_PERCENT_FORMAT

    # Ширина колонок задана (иначе кириллица обрезается многоточием).
    assert ws.column_dimensions['A'].width > 0


def test_write_report_xlsx_info_sheet_contains_error_summary(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 8.1},
        {'Timestamp': 'x', 'Branch': 'reverse', 'I_set_A': -50.0, 'V_meas_V': 3.9},
    ])
    xlsx_path = tmp_path / "info.xlsx"

    write_report_xlsx(xlsx_path, build_report(df, i_nom=100.0), _params())

    info_df = pd.read_excel(xlsx_path, sheet_name='Инфо')
    keys = list(info_df['Параметр'])
    assert "Датчик" in keys
    assert "Нормирующее значение погрешности, В" in keys
    assert any("Макс" in k for k in keys)


def test_write_report_xlsx_creates_missing_directory(tmp_path):
    """Оператор мог выбрать папку, которой уже нет — не падаем."""
    df = pd.DataFrame([{'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 6.0}])
    xlsx_path = tmp_path / "nested" / "deeper" / "r.xlsx"

    write_report_xlsx(xlsx_path, build_report(df, i_nom=100.0), _params())

    assert xlsx_path.exists()


def test_write_report_xlsx_handles_all_nan_data(tmp_path):
    """Полностью неудачное измерение всё равно должно записаться (со сводкой «—»)."""
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': float('nan')},
    ])
    xlsx_path = tmp_path / "nan.xlsx"

    write_report_xlsx(xlsx_path, build_report(df, i_nom=100.0), _params())

    assert xlsx_path.exists()
