import math
import time
from datetime import datetime
from typing import Callable, List, Dict, Optional

from instruments import CurrentSource
from instruments import Multimeter as DMM
from relay import RelayController


READS_PER_POINT = 3

# Ток считается нулевым, если он меньше этого значения по модулю, А.
# Порог, а не сравнение с 0.0: I_start приходит от оператора и может прийти
# как -0.0 или как остаток округления.
ZERO_CURRENT_EPS_A = 1e-12


def _measure_branch(dmm: DMM, src: CurrentSource,
                     I_start: float, I_stop: float, I_step: float,
                     delay: float, cooling_delay: float,
                     sign: int, branch_name: str,
                     should_stop: Optional[Callable[[], bool]] = None,
                     descending: bool = False) -> List[Dict]:
    """
    Выполняет один проход измерения для уже установленного реле
    (направление задаётся снаружи через relay.forward()/reverse()).

    descending задаёт порядок обхода точек:
        False — от I_start к I_stop (0 … +I_max), используется прямой ветвью;
        True  — от I_stop к I_start (I_max … 0), используется обратной.

    Вместе это даёт непрерывный проход по току: 0 … +I_max, щелчок реле,
    -I_max … 0. Датчик намагничивается, поэтому важен именно связный путь по
    шкале, а не два одинаковых прохода от нуля.

    I_start/I_stop в обоих случаях остаются границами диапазона (start <= stop)
    — меняется только порядок обхода, поэтому валидация параметров и
    сохранённые конфиги прежние.

    Возбуждение всегда током (src.set_current). Измеряемая величина —
    выходное напряжение датчика; оно записывается как есть, включая знак:
    у биполярного датчика на обратной ветви значения отрицательные.
    Диапазоном вольтметра здесь не управляем — он настраивается один раз при
    инициализации прибора (аппаратный автодиапазон, см. instruments.py).
    sign используется для записи знака в I_set.

    should_stop — необязательный колбэк без аргументов; если он возвращает
    True, проход прерывается между точками (источник уже выключен на
    предыдущем шаге). Используется GUI для кнопки «Стоп»; при None (по
    умолчанию, как в CLI) поведение прежнее.
    """
    if I_step <= 0:
        raise ValueError(f"Шаг тока должен быть положительным, получено {I_step}")

    num_steps = int(round((I_stop - I_start) / I_step)) + 1
    results = []

    steps = range(num_steps - 1, -1, -1) if descending else range(num_steps)

    for step in steps:
        if should_stop is not None and should_stop():
            print(f"  [{branch_name}] Остановка по запросу пользователя.")
            break

        abs_value = I_start + step * I_step
        signed_value = abs_value * sign
        # 0.0 * -1 даёт -0.0, и в журнале/Excel это выглядит как «-0.0000».
        if signed_value == 0:
            signed_value = 0.0

        # Нулевую точку снимаем при ВЫКЛЮЧЕННОМ выходе источника: подавать
        # «ток 0 А» бессмысленно — включённый выход на нуле всё равно держит
        # датчик в контуре регулирования источника. Выход снимаем явно, чтобы
        # условие было гарантировано здесь, а не унаследовано от прошлой точки.
        drive_source = abs(abs_value) > ZERO_CURRENT_EPS_A

        if drive_source:
            src.set_current(abs_value)
            src.output_on()
        else:
            src.output_off()

        try:
            time.sleep(delay)

            voltages = []
            for _ in range(READS_PER_POINT):
                try:
                    voltages.append(dmm.measure_voltage())
                except Exception as e:
                    print(f"  [{branch_name}] чтение не удалось: {e}")
        finally:
            # Выход источника снимаем всегда, даже если чтение свалилось с
            # неожиданной ошибкой: оставить ток в датчике нельзя.
            if drive_source:
                src.output_off()

        if voltages:
            v_avg = sum(voltages) / len(voltages)
        else:
            # Все попытки чтения провалились — точку помечаем NaN, а не
            # тихим нулём, чтобы не выдать сбой связи за реальный провал
            # характеристики.
            v_avg = math.nan

        time.sleep(cooling_delay)

        results.append({
            'Timestamp': datetime.now().isoformat(),
            'Branch': branch_name,
            'I_set_A': signed_value,
            'V_meas_V': v_avg,
        })

        note = "" if drive_source else "   (выход источника снят)"
        print(f"  [{branch_name}] I_уст = {signed_value:+.4f} А  ->  V_изм = {v_avg:.6f} В{note}")

    return results


def run_measurement(dmm: DMM, src: CurrentSource, relay: RelayController,
                     I_start: float, I_stop: float, I_step: float,
                     V_limit: float, delay: float, cooling_delay: float,
                     should_stop: Optional[Callable[[], bool]] = None) -> List[Dict]:
    """
    Полный двусторонний цикл измерения характеристики датчика ДТ100А1/ДТ500А1
    с автоматическим переключением полярности через плату реле:

        1) relay.forward() -> проход 0 … +I_max (положительная ветвь, sign=+1)
        2) relay.reverse() -> проход -I_max … 0 (отрицательная ветвь, sign=-1)
        3) relay.off()

    Ветви идут навстречу друг другу, поэтому по току получается связный путь
    0 … +I_max, затем -I_max … 0. Датчик намагничивается, и такой проход
    физически корректнее двух одинаковых прогонов от нуля.

    Реле переключается между крайними точками (+I_max и -I_max), но ток в
    этот момент не течёт: выход источника снимается после каждой точки.

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
            sign=-1, branch_name='reverse', should_stop=should_stop,
            descending=True,
        )
    finally:
        # Оба выключения — независимо друг от друга: сбой снятия выхода
        # источника не должен оставить реле под напряжением, и наоборот.
        try:
            src.shutdown()
        except Exception as e:
            print(f"  ВНИМАНИЕ: сбой выключения источника: {e}")
        try:
            relay.off()
        except Exception as e:
            print(f"  ВНИМАНИЕ: сбой выключения реле: {e}")

    return results
