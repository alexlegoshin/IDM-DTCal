"""
Номинальная амплитудная характеристика датчиков ДТ100А1 / ДТ500А1.

Вход — ток (номинал зависит от модели), выход — НАПРЯЖЕНИЕ БИПОЛЯРНОЕ:

    I = -I_ном   -> V = V_minus   (по умолчанию -4 В)
    I = 0        -> V = V_zero    (по умолчанию  0 В)
    I = +I_ном   -> V = V_plus    (по умолчанию +4 В)

ВАЖНО (исправление): раньше здесь была зашита однополярная шкала
2 / 6 / 10 В со «смещённым нулём». Это неверно — реальный выход датчиков
двуполярный, примерно -4..+4 В, и проходит через ноль. Все формулы
пересчитаны под биполярную шкалу.

Точки характеристики не константы, а параметр сессии (OutputScale):
паспортные значения уточняются по ТЗ/ТУ на конкретную партию, а у ДТ500А1
ноль может быть слегка смещён (например, -3.96 / +0.04 / +4.04). Поэтому
оператор задаёт их в GUI или ключами --v-minus/--v-zero/--v-plus, а здесь
лежат только значения по умолчанию.

Характеристика считается кусочно-линейной с изломом в нуле: две ветви
строятся независимо (0..+I_ном и 0..-I_ном). При симметричной шкале
(V_zero ровно посередине) это вырождается в одну прямую, а при смещённом
нуле — корректно описывает разный наклон ветвей.

Погрешность считается со знаком (важно направление отклонения) и
нормируется по ГОСТ 8.401-80: шкала двусторонняя, ноль лежит внутри
диапазона, поэтому нормирующее значение равно сумме модулей пределов,
то есть размаху V_plus - V_minus (по умолчанию 8 В).
"""
from dataclasses import dataclass

SENSOR_MODELS = {
    "ДТ100А1": 100.0,
    "ДТ500А1": 500.0,
}

# ASCII-псевдонимы для CLI (--model), чтобы не полагаться на ввод кириллицы
# в аргументах командной строки/bat-скриптах.
MODEL_CLI_ALIASES = {
    "DT100A1": "ДТ100А1",
    "DT500A1": "ДТ500А1",
}

# Значения по умолчанию для точек характеристики, В. Приняты симметричными
# ±4 В для обеих моделей; фактические (в т.ч. смещение нуля у ДТ500А1)
# задаются оператором и сверяются с ТЗ/ТУ.
V_MINUS_DEFAULT = -4.0
V_ZERO_DEFAULT = 0.0
V_PLUS_DEFAULT = 4.0


@dataclass(frozen=True)
class OutputScale:
    """
    Три точки номинальной выходной характеристики датчика, В.

    Требуется строгая монотонность v_minus < v_zero < v_plus: иначе шкала
    вырождена (нулевой наклон ветви) и погрешность посчитать нельзя.
    """
    v_minus: float = V_MINUS_DEFAULT
    v_zero: float = V_ZERO_DEFAULT
    v_plus: float = V_PLUS_DEFAULT

    def __post_init__(self):
        for name, value in (("v_minus", self.v_minus), ("v_zero", self.v_zero), ("v_plus", self.v_plus)):
            if value is None or not isinstance(value, (int, float)):
                raise ValueError(f"Точка характеристики {name} должна быть числом, получено {value!r}")
        if not (self.v_minus < self.v_zero < self.v_plus):
            raise ValueError(
                "Точки выходной характеристики должны строго возрастать: "
                f"V(-I ном.) < V(0) < V(+I ном.), получено "
                f"{self.v_minus:g} / {self.v_zero:g} / {self.v_plus:g} В"
            )

    @property
    def span(self) -> float:
        """Размах шкалы, В — нормирующее значение погрешности (ГОСТ 8.401-80)."""
        return self.v_plus - self.v_minus

    @property
    def max_abs(self) -> float:
        """Максимальный модуль выхода, В — по нему выбирается шкала вольтметра."""
        return max(abs(self.v_minus), abs(self.v_plus))

    def describe_points(self) -> str:
        return (f"V(-I ном.) = {self.v_minus:+.4f} В, V(0) = {self.v_zero:+.4f} В, "
                f"V(+I ном.) = {self.v_plus:+.4f} В")


DEFAULT_SCALE = OutputScale()

# Формулы в текстовом виде — печатаются в отчёт, чтобы способ расчёта был
# виден оператору прямо в файле, а не только в исходниках.
EXPECTED_FORMULA_TEXT = (
    "I ≥ 0:  U ожид. = V(0) + (V(+I ном.) − V(0)) × I / I ном.\n"
    "I < 0:  U ожид. = V(0) − (V(0) − V(−I ном.)) × |I| / I ном."
)
ERROR_FORMULA_TEXT = "γ = (U измер. − U ожид.) / U норм. × 100 %"
NORMALIZATION_TEXT = (
    "приведённая погрешность по ГОСТ 8.401-80; шкала двусторонняя, ноль внутри "
    "диапазона, поэтому нормирующее значение = сумма модулей пределов = "
    "V(+I ном.) − V(−I ном.)"
)


@dataclass(frozen=True)
class SensorModel:
    name: str
    i_nom: float
    scale: OutputScale = DEFAULT_SCALE


def get_model(name: str, scale: OutputScale = DEFAULT_SCALE) -> SensorModel:
    if name not in SENSOR_MODELS:
        raise ValueError(f"Неизвестная модель датчика: {name!r} (ожидается одна из {list(SENSOR_MODELS)})")
    return SensorModel(name=name, i_nom=SENSOR_MODELS[name], scale=scale)


def scale_from_params(params) -> OutputScale:
    """
    Достаёт точки характеристики из словаря параметров сессии (ключи
    V_minus/V_zero/V_plus). Отсутствующие и None-значения заменяются
    значениями по умолчанию: старые конфиги и вызовы без этих ключей
    продолжают работать.
    """
    if not params:
        return DEFAULT_SCALE

    def pick(key, default):
        value = params.get(key)
        return default if value is None else float(value)

    return OutputScale(
        v_minus=pick('V_minus', V_MINUS_DEFAULT),
        v_zero=pick('V_zero', V_ZERO_DEFAULT),
        v_plus=pick('V_plus', V_PLUS_DEFAULT),
    )


def expected_voltage(i_set: float, i_nom: float, scale: OutputScale = DEFAULT_SCALE) -> float:
    """
    Ожидаемое выходное напряжение датчика при токе i_set (со знаком).

    Ветви считаются раздельно от точки V(0): при симметричной шкале обе
    формулы дают одну прямую, при смещённом нуле — разный наклон.
    """
    if i_set >= 0:
        return scale.v_zero + (scale.v_plus - scale.v_zero) * (i_set / i_nom)
    return scale.v_zero + (scale.v_zero - scale.v_minus) * (i_set / i_nom)


def error_percent(v_meas: float, v_expected: float, span: float = None) -> float:
    """
    Приведённая погрешность со знаком, % от размаха шкалы (по умолчанию 8 В).

    Знак сохраняется: положительная погрешность — измеренное напряжение
    выше ожидаемого, отрицательная — ниже. Модуль не берётся умышленно,
    направление отклонения важно для калибровки.
    """
    if span is None:
        span = DEFAULT_SCALE.span
    return (v_meas - v_expected) / span * 100.0
