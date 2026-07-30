"""
Тесты номинальной характеристики датчика.

Главный охраняемый факт: шкала БИПОЛЯРНАЯ (-4..+4 В по умолчанию) и проходит
через ноль. Предыдущая версия считала её однополярной (2 / 6 / 10 В), из-за
чего вся обратная ветвь и все погрешности были посчитаны неверно.
"""
import pytest

from sensors import (
    DEFAULT_SCALE,
    SENSOR_MODELS,
    OutputScale,
    error_percent,
    expected_voltage,
    get_model,
    scale_from_params,
)


# ----------------------------------------------------------------------
# Шкала по умолчанию
# ----------------------------------------------------------------------

def test_default_scale_is_bipolar_and_centred_on_zero():
    assert DEFAULT_SCALE.v_minus == pytest.approx(-4.0)
    assert DEFAULT_SCALE.v_zero == pytest.approx(0.0)
    assert DEFAULT_SCALE.v_plus == pytest.approx(4.0)


def test_default_span_is_full_swing():
    """Нормирующее значение — размах шкалы, а не верхний предел."""
    assert DEFAULT_SCALE.span == pytest.approx(8.0)


def test_max_abs_drives_voltmeter_range():
    """Для выбора диапазона важен максимум модуля, а не верхний предел со знаком."""
    assert DEFAULT_SCALE.max_abs == pytest.approx(4.0)
    assert OutputScale(-3.96, 0.04, 4.04).max_abs == pytest.approx(4.04)


# ----------------------------------------------------------------------
# Ожидаемое напряжение
# ----------------------------------------------------------------------

@pytest.mark.parametrize("i_set,expected", [
    (0.0, 0.0),
    (100.0, 4.0),
    (-100.0, -4.0),
    (50.0, 2.0),
    (-50.0, -2.0),
    (25.0, 1.0),
])
def test_expected_voltage_symmetric_scale(i_set, expected):
    assert expected_voltage(i_set, 100.0) == pytest.approx(expected)


def test_expected_voltage_is_negative_on_reverse_branch():
    """Регрессия: при старой шкале 2..10 В обратная ветвь давала +2 В вместо -4 В."""
    assert expected_voltage(-500.0, 500.0) < 0


def test_expected_voltage_with_shifted_zero_hits_all_three_points():
    """Случай ДТ500А1: ноль смещён, ветви имеют разный наклон."""
    scale = OutputScale(v_minus=-3.96, v_zero=0.04, v_plus=4.04)

    assert expected_voltage(0.0, 500.0, scale) == pytest.approx(0.04)
    assert expected_voltage(500.0, 500.0, scale) == pytest.approx(4.04)
    assert expected_voltage(-500.0, 500.0, scale) == pytest.approx(-3.96)


def test_expected_voltage_branches_have_independent_slopes():
    """
    Излом в нуле: если ноль смещён вверх, положительная ветвь короче
    отрицательной, и на равных по модулю токах отклонения от V(0) разные.
    """
    scale = OutputScale(v_minus=-3.0, v_zero=0.0, v_plus=5.0)

    assert expected_voltage(50.0, 100.0, scale) == pytest.approx(2.5)
    assert expected_voltage(-50.0, 100.0, scale) == pytest.approx(-1.5)


# ----------------------------------------------------------------------
# Погрешность
# ----------------------------------------------------------------------

def test_error_percent_keeps_sign():
    assert error_percent(2.1, 2.0) == pytest.approx(1.25)
    assert error_percent(1.9, 2.0) == pytest.approx(-1.25)


def test_error_percent_normalises_by_span_not_by_reading():
    """
    Приведённая, а не относительная: в нуле шкалы (U ожид. = 0) относительная
    погрешность обратилась бы в бесконечность, приведённая — считается.
    """
    assert error_percent(0.08, 0.0) == pytest.approx(1.0)


def test_error_percent_accepts_custom_span():
    assert error_percent(2.1, 2.0, span=4.0) == pytest.approx(2.5)


# ----------------------------------------------------------------------
# Валидация шкалы
# ----------------------------------------------------------------------

@pytest.mark.parametrize("v_minus,v_zero,v_plus", [
    (4.0, 0.0, -4.0),    # пределы перепутаны местами
    (-4.0, 5.0, 4.0),    # ноль вне диапазона
    (0.0, 0.0, 4.0),     # вырожденная отрицательная ветвь
    (-4.0, 4.0, 4.0),    # вырожденная положительная ветвь
])
def test_output_scale_rejects_non_monotonic_points(v_minus, v_zero, v_plus):
    with pytest.raises(ValueError):
        OutputScale(v_minus=v_minus, v_zero=v_zero, v_plus=v_plus)


def test_output_scale_rejects_non_numeric():
    with pytest.raises(ValueError):
        OutputScale(v_minus=None, v_zero=0.0, v_plus=4.0)


# ----------------------------------------------------------------------
# Разбор параметров сессии
# ----------------------------------------------------------------------

def test_scale_from_params_reads_session_values():
    scale = scale_from_params({'V_minus': -3.96, 'V_zero': 0.04, 'V_plus': 4.04})

    assert scale.v_minus == pytest.approx(-3.96)
    assert scale.v_zero == pytest.approx(0.04)
    assert scale.v_plus == pytest.approx(4.04)


@pytest.mark.parametrize("params", [None, {}, {'model': 'ДТ100А1'}, {'V_zero': None}])
def test_scale_from_params_falls_back_to_default(params):
    """Старые конфиги без этих ключей не должны ронять расчёт."""
    assert scale_from_params(params) == DEFAULT_SCALE


def test_scale_from_params_propagates_invalid_scale():
    with pytest.raises(ValueError):
        scale_from_params({'V_minus': 4.0, 'V_zero': 0.0, 'V_plus': -4.0})


# ----------------------------------------------------------------------
# Модели
# ----------------------------------------------------------------------

def test_get_model_carries_nominal_current_and_scale():
    scale = OutputScale(-3.96, 0.04, 4.04)
    model = get_model('ДТ500А1', scale)

    assert model.i_nom == SENSOR_MODELS['ДТ500А1'] == 500.0
    assert model.scale is scale


def test_get_model_rejects_unknown_name():
    with pytest.raises(ValueError):
        get_model('ДТ999А1')
