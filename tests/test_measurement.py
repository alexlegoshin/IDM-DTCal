import math

import pytest

from measurement import run_measurement, _measure_branch


class FakeDMM:
    """Заглушка Multimeter: отдаёт заготовленные значения напряжения по порядку вызовов."""

    def __init__(self, readings):
        self.readings = list(readings)  # каждый элемент — либо float, либо Exception-класс/инстанс
        self.ranges = [0.2, 2.0, 20.0, 200.0, 1000.0]
        self.range_idx = len(self.ranges) - 1
        self.set_range_calls = []
        self.auto_range_calls = []

    def measure_voltage(self) -> float:
        item = self.readings.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def auto_range(self, measured_voltage, is_first=False):
        self.auto_range_calls.append((measured_voltage, is_first))

    def set_range(self, r):
        self.set_range_calls.append(r)


class FakeSource:
    def __init__(self):
        self.calls = []
        self.current_setpoints = []

    def setup(self, voltage_limit):
        self.calls.append(('setup', voltage_limit))

    def set_current(self, current):
        self.current_setpoints.append(current)
        self.calls.append(('set_current', current))

    def output_on(self):
        self.calls.append(('output_on',))

    def output_off(self):
        self.calls.append(('output_off',))

    def shutdown(self):
        self.calls.append(('shutdown',))


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


def test_measure_branch_range_reset_starts_from_max_range():
    dmm = FakeDMM(readings=[6.0] * 3)
    dmm.range_idx = 0
    src = FakeSource()

    _measure_branch(
        dmm, src,
        I_start=0, I_stop=0, I_step=1,
        delay=0, cooling_delay=0,
        sign=-1, branch_name='reverse', range_reset=True,
    )

    assert dmm.range_idx == len(dmm.ranges) - 1
    assert dmm.set_range_calls[0] == dmm.ranges[-1]


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
