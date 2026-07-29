"""
Расчёт ожидаемого напряжения/погрешности и запись результата в Excel.

Заменяет связку CSV + отдельная команда analyze из IVTrace: DTCal сразу
после измерения знает модель датчика (и её I_ном), поэтому считает
погрешность на месте и пишет один .xlsx с двумя листами — данные и
метаданные сессии. Графики не строятся (не нужны для этой задачи).
"""
from datetime import datetime
from pathlib import Path

import pandas as pd

from sensors import expected_voltage, error_percent, SPAN


def build_report(df: pd.DataFrame, i_nom: float) -> pd.DataFrame:
    """Добавляет к сырым данным измерения колонки V_expected_V и Error_percent (со знаком)."""
    df = df.copy()
    df['V_expected_V'] = df['I_set_A'].apply(lambda i: expected_voltage(i, i_nom))
    df['Error_percent'] = df.apply(lambda r: error_percent(r['V_meas_V'], r['V_expected_V']), axis=1)
    return df


def write_report_xlsx(xlsx_path: Path, df: pd.DataFrame, params: dict) -> None:
    """Пишет .xlsx с листом 'Данные' (измерения + погрешность) и листом 'Инфо' (метаданные сессии)."""
    info_rows = [
        ("Датчик", params['model']),
        ("Номинальный ток, А", params['i_nom']),
        ("Диапазон возбуждения, А", f"{params['I_start']}..{params['I_stop']}, шаг {params['I_step']}"),
        ("Ограничение напряжения источника, В", params['V_limit']),
        ("Обе полярности", "сняты автоматически через плату реле (см. колонку Branch)"),
        ("Задержка установки, с", params['delay']),
        ("Задержка охлаждения, с", params['cooling_delay']),
        ("Комментарий", params.get('label', '')),
        ("Нормирующее значение погрешности, В", SPAN),
        ("Время измерения", datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ("Всего точек", len(df)),
    ]
    info_df = pd.DataFrame(info_rows, columns=["Параметр", "Значение"])

    with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
        info_df.to_excel(writer, sheet_name='Инфо', index=False)
        df.to_excel(writer, sheet_name='Данные', index=False)
