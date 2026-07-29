"""
Расчёт ожидаемого напряжения/погрешности и запись результата в Excel.

Заменяет связку CSV + отдельная команда analyze из IVTrace: DTCal сразу
после измерения знает модель датчика (и её I_ном), поэтому считает
погрешность на месте и пишет один .xlsx с двумя листами — данные и
метаданные сессии. Графики не строятся (не нужны для этой задачи).

Оформление листов делается здесь же (ширина колонок, форматы чисел,
закреплённая шапка), чтобы готовый файл не приходилось подгонять руками.
Погрешность выводится с явным знаком — направление отклонения от
номинальной характеристики важно для калибровки.
"""
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from sensors import expected_voltage, error_percent, SPAN


# Внутренние (англоязычные) имена колонок -> заголовки для Excel.
# В DataFrame имена остаются латиницей: по ним работают тесты и код GUI,
# а кириллица нужна только в готовом отчёте для оператора.
COLUMN_TITLES = {
    'Timestamp': 'Время',
    'Branch': 'Направление',
    'I_set_A': 'I задан., А',
    'V_meas_V': 'U измер., В',
    'V_expected_V': 'U ожид., В',
    'Error_percent': 'Погрешность, %',
}

BRANCH_TITLES = {'forward': 'прямое', 'reverse': 'обратное'}

# Формат с явным знаком: секции Excel — положительные;отрицательные;ноль.
SIGNED_PERCENT_FORMAT = '+0.000;-0.000;0.000'

_HEADER_FILL = PatternFill('solid', fgColor='EFEFEF')
_HEADER_FONT = Font(bold=True)


def build_report(df: pd.DataFrame, i_nom: float) -> pd.DataFrame:
    """
    Добавляет к сырым данным измерения колонки V_expected_V и Error_percent (со знаком).

    Пустой DataFrame (измерение остановили до первой точки) не является
    ошибкой: возвращаем пустую таблицу с полным набором колонок, иначе
    обращение к df['I_set_A'] упало бы с KeyError.
    """
    if i_nom is None or i_nom <= 0:
        raise ValueError(f"Номинальный ток должен быть положительным, получено {i_nom!r}")

    df = df.copy()

    if df.empty:
        for col in list(COLUMN_TITLES):
            if col not in df.columns:
                df[col] = pd.Series(dtype='float64')
        return df

    df['V_expected_V'] = df['I_set_A'].apply(lambda i: expected_voltage(i, i_nom))
    df['Error_percent'] = df.apply(lambda r: error_percent(r['V_meas_V'], r['V_expected_V']), axis=1)
    return df


def _autosize(worksheet, df_like_widths: dict, min_width: int = 10, max_width: int = 22):
    for idx, width in df_like_widths.items():
        letter = get_column_letter(idx)
        worksheet.column_dimensions[letter].width = max(min_width, min(max_width, width))


def _style_header(worksheet, ncols: int):
    for col in range(1, ncols + 1):
        cell = worksheet.cell(row=1, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal='center', vertical='center')
    worksheet.freeze_panes = 'A2'


def _format_data_sheet(worksheet, df: pd.DataFrame):
    columns = list(df.columns)
    _style_header(worksheet, len(columns))

    # Числовые форматы по смыслу колонки: ток — 2 знака, напряжения — 4,
    # погрешность — 3 знака и обязательный знак «+»/«−».
    number_formats = {
        'I_set_A': '0.00',
        'V_meas_V': '0.0000',
        'V_expected_V': '0.0000',
        'Error_percent': SIGNED_PERCENT_FORMAT,
    }

    widths = {}
    for idx, name in enumerate(columns, start=1):
        header_len = len(COLUMN_TITLES.get(name, name))
        widths[idx] = header_len + 4
        fmt = number_formats.get(name)
        if fmt is None:
            continue
        for row in range(2, worksheet.max_row + 1):
            cell = worksheet.cell(row=row, column=idx)
            cell.number_format = fmt
            cell.alignment = Alignment(horizontal='right')

    if 'Timestamp' in columns:
        widths[columns.index('Timestamp') + 1] = 20
    _autosize(worksheet, widths)


def _format_info_sheet(worksheet):
    _style_header(worksheet, 2)
    for row in range(2, worksheet.max_row + 1):
        worksheet.cell(row=row, column=1).font = Font(bold=True)
    worksheet.column_dimensions['A'].width = 38
    worksheet.column_dimensions['B'].width = 46
    for row in range(1, worksheet.max_row + 1):
        worksheet.cell(row=row, column=2).alignment = Alignment(horizontal='left', wrap_text=False)


def write_report_xlsx(xlsx_path: Path, df: pd.DataFrame, params: dict) -> None:
    """Пишет .xlsx с листом 'Данные' (измерения + погрешность) и листом 'Инфо' (метаданные сессии)."""
    xlsx_path = Path(xlsx_path)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    finite_errors = df['Error_percent'].dropna() if 'Error_percent' in df.columns else pd.Series(dtype='float64')

    info_rows = [
        ("Датчик", params['model']),
        ("Номинальный ток, А", params['i_nom']),
        ("Диапазон возбуждения, А", f"{params['I_start']}..{params['I_stop']}, шаг {params['I_step']}"),
        ("Ограничение напряжения источника, В", params['V_limit']),
        ("Обе полярности", "сняты автоматически через плату реле (см. колонку «Направление»)"),
        ("Задержка установки, с", params['delay']),
        ("Задержка охлаждения, с", params['cooling_delay']),
        # Пустая строка в xlsx читается обратно как NaN — пишем прочерк.
        ("Комментарий", params.get('label') or "—"),
        ("Нормирующее значение погрешности, В", SPAN),
        ("Нормировка", "приведённая погрешность по ГОСТ 8.401-80 (шкала со смещённым нулём: 2..10 В)"),
        ("Знак погрешности", "«+» выход выше номинальной характеристики, «−» ниже"),
        ("Время измерения", datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ("Всего точек", len(df)),
        ("Макс. |погрешность|, %", round(float(finite_errors.abs().max()), 4) if not finite_errors.empty else "—"),
        ("Средняя погрешность (со знаком), %", round(float(finite_errors.mean()), 4) if not finite_errors.empty else "—"),
    ]
    info_df = pd.DataFrame(info_rows, columns=["Параметр", "Значение"])

    out = df.copy()
    if 'Branch' in out.columns:
        out['Branch'] = out['Branch'].map(lambda b: BRANCH_TITLES.get(b, b))
    out = out.rename(columns=COLUMN_TITLES)

    with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
        info_df.to_excel(writer, sheet_name='Инфо', index=False)
        out.to_excel(writer, sheet_name='Данные', index=False)

        _format_info_sheet(writer.sheets['Инфо'])
        _format_data_sheet(writer.sheets['Данные'], df)
