import math

import pytest

from measurement import run_measurement, _measure_branch


class FakeDMM:
    """
    Заглушка вольтметра: отдаёт заготовленные значения напряжения по порядку вызовов.

    Управления диапазоном здесь нет умышленно: в DTCal измеряемая величина
    всегда лежит в 2..10 В, диапазон настраивается один раз при инициализации
    прибора (аппаратный автодиапазон), а measurement.py его не трогает.
    """

    def __init__(self, readings):
        self.readings = list(readings)  # float либо инстанс исключения
        self.calls = 0

    def measure_voltage(self) -> float:
        self.calls += 1
        item = self.readings.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeSource:
    def __init__(self, fail_on=None):
        self.calls = []
        self.current_setpoints = []
        self._fail_on = fail_on  # имя метода, который должен бросить исключение

    def _maybe_fail(self, name):
        if self._fail_on == name:
            raise RuntimeError(f"FakeSource: сбой {name}")

    def setup(self, voltage_limit):
        self.calls.append(('setup', voltage_limit))

    def set_current(self, current):
        self.current_setpoints.append(current)
        self.calls.append(('set_current', current))

    def output_on(self):
        self.calls.append(('output_on',))

    def output_off(self):
        self.calls.append(('output_off',))

    def output_count(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def shutdown(self):
        self.calls.append(('shutdown',))
        self._maybe_fail('shutdown')


class FakeRelay:
    def __init__(self):
        self.calls = []

    def forward(self):
        self.calls.append('forward')
        return 'OK'

    def reverse(self):
        self.calls.append('reverse')
        return 'OK'

    def off(self):
        self.calls.append('off')
        return 'OK'


def test_measure_branch_averages_three_readings_and_signs_i_set():
    dmm = FakeDMM(readings=[6.0, 6.1, 6.2] * 3)  # 3 точки по 3 чтения
    src = FakeSource()

    results = _measure_branch(
        dmm, src,
        I_start=0, I_stop=2, I_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert len(results) == 3
    assert [r['I_set_A'] for r in results] == [0, 1, 2]
    assert results[0]['V_meas_V'] == pytest.approx((6.0 + 6.1 + 6.2) / 3)
    assert all(r['Branch'] == 'forward' for r in results)


def test_measure_branch_negative_sign_produces_negative_i_set():
    dmm = FakeDMM(readings=[6.5] * 9)
    src = FakeSource()

    results = _measure_branch(
        dmm, src,
        I_start=0, I_stop=2, I_step=1,
        delay=0, cooling_delay=0,
        sign=-1, branch_name='reverse',
    )

    assert [r['I_set_A'] for r in results] == [0, -1, -2]


def test_measure_branch_all_reads_failing_yields_nan_not_zero():
    """
    Регрессия: раньше при полном отказе чтения (3/3 исключения) точка
    молча записывалась как V_meas_V=0.0, что маскировало сбой связи под
    видом реального измерения. Должен быть NaN.
    """
    dmm = FakeDMM(readings=[Exception("comm error")] * 3)
    src = FakeSource()

    results = _measure_branch(
        dmm, src,
        I_start=0, I_stop=0, I_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert len(results) == 1
    assert math.isnan(results[0]['V_meas_V'])


def test_measure_branch_partial_failure_averages_successful_reads():
    dmm = FakeDMM(readings=[Exception("timeout"), 8.0, 8.2])
    src = FakeSource()

    results = _measure_branch(
        dmm, src,
        I_start=0, I_stop=0, I_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert results[0]['V_meas_V'] == pytest.approx((8.0 + 8.2) / 2)


def test_measure_branch_turns_output_off_for_every_point():
    dmm = FakeDMM(readings=[6.0] * 9)
    src = FakeSource()

    _measure_branch(
        dmm, src,
        I_start=0, I_stop=2, I_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
    )

    assert src.output_count('output_on') == 3
    assert src.output_count('output_off') == 3


def test_measure_branch_turns_output_off_when_reading_explodes():
    """
    Безопасность: неожиданная ошибка внутри точки не должна оставить ток
    включённым в датчике — output_off стоит в finally.
    """
    class ExplodingDMM:
        def measure_voltage(self):
            raise KeyboardInterrupt("оператор прервал")

    src = FakeSource()

    with pytest.raises(KeyboardInterrupt):
        _measure_branch(
            ExplodingDMM(), src,
            I_start=0, I_stop=0, I_step=1,
            delay=0, cooling_delay=0,
            sign=+1, branch_name='forward',
        )

    assert ('output_off',) in src.calls


def test_measure_branch_rejects_nonpositive_step():
    """Нулевой/отрицательный шаг раньше давал деление на ноль при расчёте числа точек."""
    dmm = FakeDMM(readings=[6.0] * 9)
    src = FakeSource()

    with pytest.raises(ValueError):
        _measure_branch(
            dmm, src,
            I_start=0, I_stop=2, I_step=0,
            delay=0, cooling_delay=0,
            sign=+1, branch_name='forward',
        )


def test_measure_branch_stops_on_request():
    dmm = FakeDMM(readings=[6.0] * 30)
    src = FakeSource()

    results = _measure_branch(
        dmm, src,
        I_start=0, I_stop=5, I_step=1,
        delay=0, cooling_delay=0,
        sign=+1, branch_name='forward',
        should_stop=lambda: True,
    )

    assert results == []


def test_run_measurement_runs_forward_then_reverse_and_shuts_down():
    dmm = FakeDMM(readings=[6.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    results = run_measurement(
        dmm, src, relay,
        I_start=0, I_stop=1, I_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )

    assert relay.calls == ['forward', 'reverse', 'off']
    assert ('shutdown',) in src.calls
    branches = [r['Branch'] for r in results]
    assert branches.count('forward') == 2
    assert branches.count('reverse') == 2
    signs = {r['Branch']: [] for r in results}
    for r in results:
        signs[r['Branch']].append(r['I_set_A'])
    assert signs['forward'] == [0, 1]
    assert signs['reverse'] == [0, -1]


def test_run_measurement_passes_v_limit_to_setup():
    dmm = FakeDMM(readings=[6.0] * 100)
    src = FakeSource()
    relay = FakeRelay()

    run_measurement(
        dmm, src, relay,
        I_start=0, I_stop=0, I_step=1,
        V_limit=7.5, delay=0, cooling_delay=0,
    )

    assert src.calls[0] == ('setup', 7.5)


def test_run_measurement_shuts_down_source_and_relay_even_on_failure():
    class ExplodingRelay(FakeRelay):
        def reverse(self):
            raise RuntimeError("relay comm failure")

    dmm = FakeDMM(readings=[6.0] * 100)
    src = FakeSource()
    relay = ExplodingRelay()

    with pytest.raises(RuntimeError):
        run_measurement(
            dmm, src, relay,
            I_start=0, I_stop=0, I_step=1,
            V_limit=5.0, delay=0, cooling_delay=0,
        )

    assert ('shutdown',) in src.calls
    assert 'off' in relay.calls


def test_run_measurement_switches_relay_off_even_if_source_shutdown_fails():
    """
    Регрессия: src.shutdown() и relay.off() стояли подряд в одном finally,
    поэтому исключение на выключении источника оставляло реле под
    напряжением. Теперь оба выключения независимы.
    """
    dmm = FakeDMM(readings=[6.0] * 100)
    src = FakeSource(fail_on='shutdown')
    relay = FakeRelay()

    run_measurement(
        dmm, src, relay,
        I_start=0, I_stop=0, I_step=1,
        V_limit=5.0, delay=0, cooling_delay=0,
    )

    assert 'off' in relay.calls
