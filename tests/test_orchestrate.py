import pandas as pd
import pytest
from openpyxl import load_workbook

from report import DATA_COLUMNS, prepare_data, write_report_xlsx


def _params():
    return {
        'model': 'ДТ100А1',
        'i_nom': 100.0,
        'label': 'OrchSensor',
        'I_start': 0.0, 'I_stop': 10.0, 'I_step': 5.0,
        'V_limit': 5.0, 'delay': 0.1, 'cooling_delay': 0.2,
    }


def test_prepare_data_keeps_measurements_untouched():
    """Первичные данные проходят насквозь: никаких производных величин."""
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 0.0},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 2.0},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'reverse', 'I_set_A': -50.0, 'V_meas_V': -2.0},
    ])

    result = prepare_data(df)

    assert list(result.columns) == DATA_COLUMNS
    assert list(result['I_set_A']) == [0.0, 50.0, -50.0]
    assert list(result['V_meas_V']) == [0.0, 2.0, -2.0]


def test_prepare_data_does_not_compute_anything():
    """
    Расчёт погрешности вырезан осознанно: выходная характеристика зависит от
    подключённого датчика и программе неизвестна. Производные колонки не
    должны появиться снова.
    """
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 2.1},
    ])

    result = prepare_data(df)

    assert 'V_expected_V' not in result.columns
    assert 'Error_percent' not in result.columns


def test_prepare_data_on_empty_dataframe_returns_columns_not_crash():
    """
    Регрессия: если измерение остановили до первой точки, DataFrame пуст и
    обращение к df['I_set_A'] падало с KeyError, унося с собой весь сеанс.
    """
    result = prepare_data(pd.DataFrame())

    assert result.empty
    for col in DATA_COLUMNS:
        assert col in result.columns


def test_prepare_data_keeps_nan_readings_as_nan():
    """Неудачное чтение остаётся NaN и не превращается в 0 В."""
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': float('nan')},
    ])

    result = prepare_data(df)

    assert pd.isna(result.iloc[0]['V_meas_V'])


def test_write_report_xlsx_creates_file_with_two_sheets(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 0.0},
    ])
    xlsx_path = tmp_path / "test_report.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    assert xlsx_path.exists()

    xl = pd.ExcelFile(xlsx_path)
    assert xl.sheet_names == ['Инфо', 'Данные']

    info_df = pd.read_excel(xlsx_path, sheet_name='Инфо')
    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')

    assert len(info_df) > 0
    assert len(data_df) == 1
    # В отчёте заголовки человекочитаемые (внутри DataFrame остаются латиницей).
    assert list(data_df.columns) == ['Время', 'Направление', 'I задан., А', 'U измер., В']


def test_written_file_has_no_calculated_columns(tmp_path):
    """В выдаче не должно остаться ни ожидаемого напряжения, ни погрешности."""
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 2.1},
    ])
    xlsx_path = tmp_path / "raw_only.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')
    assert 'U ожид., В' not in data_df.columns
    assert 'Погрешность, %' not in data_df.columns

    info_df = pd.read_excel(xlsx_path, sheet_name='Инфо')
    assert "Нормирующее значение погрешности, В" not in list(info_df['Параметр'])


def test_info_sheet_states_that_error_is_not_computed(tmp_path):
    """Оператор должен видеть в файле, что погрешность считается вручную."""
    df = pd.DataFrame([{'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 0.0}])
    xlsx_path = tmp_path / "info.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    info_df = pd.read_excel(xlsx_path, sheet_name='Инфо')
    values = dict(zip(info_df['Параметр'], info_df['Значение']))

    assert "Датчик" in values
    assert "Всего точек" in values
    assert "не рассчитывается" in str(values["Погрешность"])


def test_write_report_xlsx_keeps_negative_voltage(tmp_path):
    """
    Выход датчика может быть биполярным — знак напряжения обязан дойти до
    файла без изменений.
    """
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'reverse', 'I_set_A': -100.0, 'V_meas_V': -3.96},
    ])
    xlsx_path = tmp_path / "bipolar.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')
    assert data_df.iloc[0]['U измер., В'] == pytest.approx(-3.96)


def test_write_report_xlsx_translates_branch_names(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 0.0},
        {'Timestamp': 'x', 'Branch': 'reverse', 'I_set_A': -50.0, 'V_meas_V': -2.0},
    ])
    xlsx_path = tmp_path / "branches.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    data_df = pd.read_excel(xlsx_path, sheet_name='Данные')
    assert list(data_df['Направление']) == ['прямое', 'обратное']


def test_write_report_xlsx_formats_sheet_for_reading(tmp_path):
    """
    Оформление должно быть готовым: закреплённая жирная шапка и заданные
    ширины — чтобы файл не подгоняли руками.
    """
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 50.0, 'V_meas_V': 2.1},
    ])
    xlsx_path = tmp_path / "formatted.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    ws = load_workbook(xlsx_path)['Данные']
    assert ws.freeze_panes == 'A2'
    assert ws.cell(row=1, column=1).font.bold is True

    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    v_col = headers.index('U измер., В') + 1
    assert ws.cell(row=2, column=v_col).number_format == '0.0000'

    # Ширина колонок задана (иначе кириллица обрезается многоточием).
    assert ws.column_dimensions['A'].width > 0


def test_write_report_xlsx_creates_missing_directory(tmp_path):
    """Оператор мог выбрать папку, которой уже нет — не падаем."""
    df = pd.DataFrame([{'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': 0.0}])
    xlsx_path = tmp_path / "nested" / "deeper" / "r.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    assert xlsx_path.exists()


def test_write_report_xlsx_handles_all_nan_data(tmp_path):
    """Полностью неудачное измерение всё равно должно записаться."""
    df = pd.DataFrame([
        {'Timestamp': 'x', 'Branch': 'forward', 'I_set_A': 0.0, 'V_meas_V': float('nan')},
    ])
    xlsx_path = tmp_path / "nan.xlsx"

    write_report_xlsx(xlsx_path, prepare_data(df), _params())

    assert xlsx_path.exists()
