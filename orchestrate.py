"""
Оркестрация одной сессии измерения: обнаружение/открытие приборов, прогон
двусторонней характеристики и (необязательно) запись первичных данных в Excel.

Вынесено отдельно, чтобы один и тот же код использовали и CLI (run.py), и
GUI (gui.py) — без дублирования логики работы с железом. Вся коммуникация с
пользователем идёт через колбэк log(text), по умолчанию — print. Это
позволяет GUI перехватывать прогресс в свой журнал, не меняя ядро.

Запись файла отделена от измерения: CLI передаёт xlsx_path и получает файл
сразу, а GUI сохраняет данные отдельной кнопкой «Сохранить в Excel», чтобы
оператор мог сам выбрать папку и имя.
"""
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from apppaths import multimeter_cfg_dir, current_source_cfg_dir
from instruments import Multimeter, CurrentSource, discover_instruments, find_config_for_idn
from relay import RelayController, discover_relay_port
from measurement import run_measurement
from report import prepare_data, write_report_xlsx


LogFn = Callable[[str], None]
StopFn = Optional[Callable[[], bool]]


def _config_by_idn(rm, addr: str, cfg_dir: Path, what: str) -> Path:
    """Опрашивает *IDN? по заданному адресу и подбирает json-конфиг прибора."""
    instr = rm.open_resource(addr)
    try:
        instr.encoding = 'utf-8'
        idn = instr.query('*IDN?').strip()
    finally:
        try:
            instr.close()
        except Exception:
            pass

    cfg = find_config_for_idn(idn, cfg_dir)
    if cfg is None:
        raise RuntimeError(f"Не удалось подобрать конфиг {what} для IDN: {idn}")
    return cfg


def _resolve_instruments(rm, dmm_addr: Optional[str], src_addr: Optional[str], log: LogFn):
    """
    Возвращает (dmm_addr, dmm_cfg, src_addr, src_cfg).

    Каждый заданный вручную адрес опрашивается через *IDN? для подбора
    конфига; автообнаружение запускается только для того прибора, адрес
    которого не задан. Раньше при одном заданном адресе код целиком уходил
    в автопоиск и молча игнорировал введённое значение.
    """
    source_cfg_dir = current_source_cfg_dir()
    dmm_cfg = src_cfg = None

    if dmm_addr:
        log(f"Мультиметр: задан адрес {dmm_addr}, определяю модель по *IDN?...")
        dmm_cfg = _config_by_idn(rm, dmm_addr, multimeter_cfg_dir(), "мультиметра")
    if src_addr:
        log(f"Источник тока: задан адрес {src_addr}, определяю модель по *IDN?...")
        src_cfg = _config_by_idn(rm, src_addr, source_cfg_dir, "источника тока")

    if dmm_cfg is not None and src_cfg is not None:
        return dmm_addr, dmm_cfg, src_addr, src_cfg

    # Ищем только то, чего не хватает (discover_instruments печатает через
    # print; в GUI это перехватывается редиректом stdout — см. gui.py).
    found_dmm_addr, found_dmm_cfg, found_src_addr, found_src_cfg = discover_instruments(
        multimeter_cfg_dir(), source_cfg_dir, rm=rm, source_label="источник тока",
        require_multimeter=dmm_cfg is None, require_source=src_cfg is None,
    )

    return (
        dmm_addr or found_dmm_addr, dmm_cfg or found_dmm_cfg,
        src_addr or found_src_addr, src_cfg or found_src_cfg,
    )


def _open_instruments(dmm_addr, dmm_cfg, src_addr, src_cfg, relay_port, rm):
    """
    Открывает вольтметр, источник и реле. Если открытие второго или третьего
    прибора падает, уже открытые закрываются — иначе VISA-сессия остаётся
    висеть и прибор недоступен до перезапуска программы.
    """
    opened = []
    try:
        dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
        opened.append(dmm)
        src = CurrentSource(src_addr, src_cfg, rm=rm)
        opened.append(src)
        relay = RelayController(relay_port)
        opened.append(relay)
        return dmm, src, relay
    except Exception:
        for handle in reversed(opened):
            try:
                handle.close()
            except Exception:
                pass
        raise


def run_measurement_session(
    rm,
    params: dict,
    xlsx_path: Optional[Path] = None,
    dmm_addr: Optional[str] = None,
    src_addr: Optional[str] = None,
    relay_port: Optional[str] = None,
    log: LogFn = print,
    should_stop: StopFn = None,
) -> pd.DataFrame:
    """
    Полный цикл: подобрать/открыть приборы и реле, снять обе ветви.
    Возвращает DataFrame первичных измерений (ток задания + напряжение с
    датчика). Ничего производного не считается — погрешность оператор
    считает вручную по ТЗ/ТУ.

    rm             — уже созданный pyvisa.ResourceManager (см. visa_backend).
    params         — словарь параметров из cli.resolve_measure_params
                     (обязательно содержит 'i_nom' — номинальный ток модели датчика).
    xlsx_path      — если задан, результат сразу пишется в этот .xlsx (путь CLI).
                     Если None, файл не пишется: GUI сохраняет данные отдельно.
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

    dmm, src, relay = _open_instruments(dmm_addr, dmm_cfg, src_addr, src_cfg, relay_port, rm)

    log("Приборы и реле инициализированы. Начинаю измерения...")

    try:
        results = run_measurement(
            dmm, src, relay,
            I_start=params['I_start'], I_stop=params['I_stop'], I_step=params['I_step'],
            V_limit=params['V_limit'], delay=params['delay'], cooling_delay=params['cooling_delay'],
            should_stop=should_stop,
        )
    finally:
        for handle in (dmm, src, relay):
            try:
                handle.close()
            except Exception:
                pass

    log("Измерения завершены, источник и реле выключены.")

    df = prepare_data(pd.DataFrame(results))

    if xlsx_path is not None:
        write_report_xlsx(xlsx_path, df, params)
        log(f"Данные сохранены в {xlsx_path}")

    return df
