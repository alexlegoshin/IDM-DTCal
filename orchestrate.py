"""
Оркестрация одной сессии измерения: обнаружение/открытие приборов, прогон
двусторонней характеристики и запись результата в Excel.

Вынесено отдельно, чтобы один и тот же код использовали и CLI (run.py), и
GUI (gui.py) — без дублирования логики работы с железом. Вся коммуникация с
пользователем идёт через колбэк log(text), по умолчанию — print. Это
позволяет GUI перехватывать прогресс в свой журнал, не меняя ядро.
"""
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from apppaths import multimeter_cfg_dir, current_source_cfg_dir
from instruments import Multimeter, CurrentSource, discover_instruments, find_config_for_idn
from relay import RelayController, discover_relay_port
from measurement import run_measurement
from report import build_report, write_report_xlsx


LogFn = Callable[[str], None]
StopFn = Optional[Callable[[], bool]]


def _resolve_instruments(rm, dmm_addr: Optional[str], src_addr: Optional[str], log: LogFn):
    """
    Возвращает (dmm_addr, dmm_cfg, src_addr, src_cfg).

    Если оба адреса заданы вручную — опрашивает *IDN? по каждому, чтобы
    подобрать json-конфиг. Иначе запускает полное автообнаружение.
    """
    source_cfg_dir = current_source_cfg_dir()

    if dmm_addr and src_addr:
        log("Открываю приборы по заданным адресам, определяю модели по *IDN?...")

        dmm_instr = rm.open_resource(dmm_addr)
        dmm_instr.encoding = 'utf-8'
        dmm_idn = dmm_instr.query('*IDN?').strip()
        dmm_instr.close()
        dmm_cfg = find_config_for_idn(dmm_idn, multimeter_cfg_dir())
        if dmm_cfg is None:
            raise RuntimeError(f"Не удалось подобрать конфиг мультиметра для IDN: {dmm_idn}")

        src_instr = rm.open_resource(src_addr)
        src_instr.encoding = 'utf-8'
        src_idn = src_instr.query('*IDN?').strip()
        src_instr.close()
        src_cfg = find_config_for_idn(src_idn, source_cfg_dir)
        if src_cfg is None:
            raise RuntimeError(f"Не удалось подобрать конфиг источника тока для IDN: {src_idn}")

        return dmm_addr, dmm_cfg, src_addr, src_cfg

    # Полное автообнаружение (discover_instruments печатает через print;
    # в GUI это перехватывается редиректом stdout — см. gui.py).
    return discover_instruments(
        multimeter_cfg_dir(), source_cfg_dir, rm=rm, source_label="источник тока",
    )


def run_measurement_session(
    rm,
    params: dict,
    xlsx_path: Path,
    dmm_addr: Optional[str] = None,
    src_addr: Optional[str] = None,
    relay_port: Optional[str] = None,
    log: LogFn = print,
    should_stop: StopFn = None,
) -> pd.DataFrame:
    """
    Полный цикл: подобрать/открыть приборы и реле, снять обе ветви, посчитать
    ожидаемое напряжение и погрешность, записать .xlsx по пути xlsx_path.
    Возвращает DataFrame результатов (уже с колонками V_expected_V, Error_percent).

    rm             — уже созданный pyvisa.ResourceManager (см. visa_backend).
    params         — словарь параметров из cli.resolve_measure_params
                     (обязательно содержит 'i_nom' — номинальный ток модели датчика).
    dmm/src/relay  — необязательные ручные адреса (иначе автообнаружение).
    log            — колбэк вывода (по умолчанию print).
    should_stop    — колбэк кооперативной остановки (для GUI).

    Гарантирует выключение источника и реле в блоке finally, даже при ошибке.
    """
    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(rm, dmm_addr, src_addr, log)

    if relay_port:
        log(f"Плата реле: используется заданный порт {relay_port}")
    else:
        relay_port = discover_relay_port()

    dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
    src = CurrentSource(src_addr, src_cfg, rm=rm)
    relay = RelayController(relay_port)

    log("Приборы и реле инициализированы. Начинаю измерения...")

    try:
        results = run_measurement(
            dmm, src, relay,
            I_start=params['I_start'], I_stop=params['I_stop'], I_step=params['I_step'],
            V_limit=params['V_limit'], delay=params['delay'], cooling_delay=params['cooling_delay'],
            should_stop=should_stop,
        )
    finally:
        dmm.close()
        src.close()
        relay.close()

    log("Измерения завершены, источник и реле выключены.")

    df = pd.DataFrame(results)
    df = build_report(df, params['i_nom'])
    write_report_xlsx(xlsx_path, df, params)
    log(f"Данные сохранены в {xlsx_path}")

    return df
