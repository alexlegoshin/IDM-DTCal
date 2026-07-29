import math
import time
from datetime import datetime
from typing import Callable, List, Dict, Optional

import pyvisa

from instruments import CurrentSource
from instruments import Multimeter as DMM
from relay import RelayController


def _measure_branch(dmm: DMM, src: CurrentSource,
                     I_start: float, I_stop: float, I_step: float,
                     delay: float, cooling_delay: float,
                     sign: int, branch_name: str,
                     range_reset: bool = False,
                     should_stop: Optional[Callable[[], bool]] = None) -> List[Dict]:
    """
    Выполняет один проход измерения (0..I_max) для уже установленного реле
    (направление задаётся снаружи через relay.forward()/reverse()).

    Возбуждение всегда током (src.set_current). Измеряемая величина —
    выходное напряжение датчика (мультиметр в режиме SENS:VOLT:DC).
    sign используется для записи знака в I_set.

    should_stop — необязательный колбэк без аргументов; если он возвращает
    True, проход прерывается между точками (источник уже выключен на
    предыдущем шаге). Используется GUI для кнопки «Стоп»; при None (по
    умолчанию, как в CLI) поведение прежнее.
    """
    num_steps = int(round((I_stop - I_start) / I_step)) + 1
    results = []

    if range_reset:
        # При смене направления датчик перемагничивается заново, поэтому
        # выбор диапазона вольтметра лучше начать заново с первой точки.
        dmm.range_idx = len(dmm.ranges) - 1
        dmm.set_range(dmm.ranges[dmm.range_idx])

    for step in range(num_steps):
        if should_stop is not None and should_stop():
            print(f"  [{branch_name}] Остановка по запросу пользователя.")
            break

        abs_value = I_start + step * I_step
        signed_value = abs_value * sign

        src.set_current(abs_value)
        src.output_on()
        time.sleep(delay)

        voltages = []
        for _ in range(3):
            try:
                v = dmm.measure_voltage()
                voltages.append(v)
            except pyvisa.errors.VisaIOError:
                if dmm.range_idx < len(dmm.ranges) - 1:
                    dmm.range_idx += 1
                    dmm.set_range(dmm.ranges[dmm.range_idx])
                    try:
                        v = dmm.measure_voltage()
                        voltages.append(v)
                    except Exception:
                        pass
            except Exception:
                pass

        if voltages:
            v_avg = sum(voltages) / len(voltages)
            dmm.auto_range(v_avg, is_first=(step == 0))
        else:
            # Все попытки чтения провалились — точку помечаем NaN, а не
            # тихим нулём, чтобы не выдать сбой связи за реальный провал
            # характеристики. auto_range не трогаем: нет данных, по которым
            # выбирать диапазон.
            v_avg = math.nan

        src.output_off()
        time.sleep(cooling_delay)

        results.append({
            'Timestamp': datetime.now().isoformat(),
            'Branch': branch_name,
            'I_set_A': signed_value,
            'V_meas_V': v_avg,
        })

        print(f"  [{branch_name}] I_уст = {signed_value:+.4f} А  ->  V_изм = {v_avg:.6f} В")

    return results


def run_measurement(dmm: DMM, src: CurrentSource, relay: RelayController,
                     I_start: float, I_stop: float, I_step: float,
                     V_limit: float, delay: float, cooling_delay: float,
                     should_stop: Optional[Callable[[], bool]] = None) -> List[Dict]:
    """
    Полный двусторонний цикл измерения характеристики датчика ДТ100А1/ДТ500А1
    с автоматическим переключением полярности через плату реле:

        1) relay.forward() -> проход 0..I_max (положительная ветвь, sign=+1)
        2) relay.reverse() -> проход 0..I_max (отрицательная ветвь, sign=-1)
        3) relay.off()

    Возбуждение — источник тока (V_limit — защитное ограничение напряжения
    на источнике). Выход датчика (измеряемая величина) — напряжение,
    читается мультиметром независимо от направления.

    Направление (Branch) сохраняется в каждой записи результата.
    """
    src.setup(voltage_limit=V_limit)

    results = []

    try:
        print("\nПереключаю реле: прямое направление (IFW)...")
        resp = relay.forward()
        print(f"  Ответ реле: {resp}")
        results += _measure_branch(
            dmm, src, I_start, I_stop, I_step, delay, cooling_delay,
            sign=+1, branch_name='forward', should_stop=should_stop,
        )

        if should_stop is not None and should_stop():
            print("\nИзмерение прервано пользователем до обратной ветви.")
            return results

        print("\nПереключаю реле: обратное направление (IRW)...")
        resp = relay.reverse()
        print(f"  Ответ реле: {resp}")
        results += _measure_branch(
            dmm, src, I_start, I_stop, I_step, delay, cooling_delay,
            sign=-1, branch_name='reverse', range_reset=True, should_stop=should_stop,
        )
    finally:
        src.shutdown()
        relay.off()

    return results
