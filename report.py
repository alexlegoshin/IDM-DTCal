"""
Запись результата измерения в Excel.

DTCal выдаёт ТОЛЬКО первичные данные: какой ток задали и какое напряжение
показал вольтметр. Ожидаемое напряжение и погрешность здесь намеренно не
считаются — выходная характеристика зависит от конкретного подключённого
датчика (может быть биполярной, может быть однополярной 2..10 В), поэтому
расчёт делается вручную по ТЗ/ТУ вне программы.

Книга состоит из двух листов:
    «Инфо»   — метаданные сессии (что, чем и с какими параметрами снимали);
    «Данные» — измерения: время, направление, ток задания, напряжение.

Оформление листов делается здесь же (ширина колонок, форматы чисел,
закреплённая шапка), чтобы готовый файл не приходилось подгонять руками.
"""
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# Внутренние (англоязычные) имена колонок -> заголовки для Excel.
# В DataFrame имена остаются латиницей: по ним работают тесты и код GUI,
# а кириллица нужна только в готовом отчёте для оператора.
COLUMN_TITLES = {
    'Timestamp': 'Время',
    'Branch': 'Направление',
    'I_set_A': 'I задан., А',
    'V_meas_V': 'U измер., В',
}

# Порядок колонок в файле. Это и есть полный состав данных: ничего
# производного не добавляется.
DATA_COLUMNS = ['Timestamp', 'Branch', 'I_set_A', 'V_meas_V']

BRANCH_TITLES = {'forward': 'прямое', 'reverse': 'обратное'}

NUMBER_FORMATS = {
    'I_set_A': '0.00',
    # Знак напряжения значим: на обратной ветви выход может быть
    # отрицательным, и «-» не должен потеряться при форматировании.
    'V_meas_V': '0.0000',
}

_HEADER_FILL = PatternFill('solid', fgColor='EFEFEF')
_HEADER_FONT = Font(bold=True)


def prepare_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Приводит сырой результат измерения к финальному составу колонок.

    Пустой DataFrame (измерение остановили до первой точки) не является
    ошибкой: возвращаем пустую таблицу с полным набором колонок, иначе
    последующее обращение к df['I_set_A'] упало бы с KeyError.
    """
    df = df.copy()

    if df.empty:
        for col in DATA_COLUMNS:
            if col not in df.columns:
                df[col] = pd.Series(dtype='float64')

    return df.reindex(columns=[c for c in DATA_COLUMNS if c in df.columns or df.empty])


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


def _format_data_sheet(worksheet, columns):
    _style_header(worksheet, len(columns))

    widths = {}
    for idx, name in enumerate(columns, start=1):
        widths[idx] = len(COLUMN_TITLES.get(name, name)) + 4
        fmt = NUMBER_FORMATS.get(name)
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
    """Пишет .xlsx с листом 'Данные' (первичные измерения) и листом 'Инфо' (метаданные сессии)."""
    xlsx_path = Path(xlsx_path)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

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
        ("Время измерения", datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ("Всего точек", len(df)),
        ("Содержимое файла", "только первичные данные измерения"),
        ("Погрешность", "не рассчитывается — считается вручную по ТЗ/ТУ на датчик"),
    ]
    info_df = pd.DataFrame(info_rows, columns=["Параметр", "Значение"])

    data = prepare_data(df)
    columns = list(data.columns)
    if 'Branch' in data.columns:
        data['Branch'] = data['Branch'].map(lambda b: BRANCH_TITLES.get(b, b))
    data = data.rename(columns=COLUMN_TITLES)

    with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
        info_df.to_excel(writer, sheet_name='Инфо', index=False)
        data.to_excel(writer, sheet_name='Данные', index=False)

        _format_info_sheet(writer.sheets['Инфо'])
        _format_data_sheet(writer.sheets['Данные'], columns)
