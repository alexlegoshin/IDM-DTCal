import argparse
import re
from datetime import datetime
from pathlib import Path

from config import ConfigManager
from sensors import (
    MODEL_CLI_ALIASES,
    SENSOR_MODELS,
    V_MINUS_DEFAULT,
    V_PLUS_DEFAULT,
    V_ZERO_DEFAULT,
    scale_from_params,
)


def build_parser(default_data_dir: Path = Path("data")) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DTCal",
        description="DTCal — снятие характеристики датчиков ДТ100А1/ДТ500А1 (вход — ток, выход — напряжение), "
                    "запись данных и погрешности в Excel. Без аргументов запускается графический интерфейс (GUI).",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=default_data_dir,
        help="Каталог для хранения xlsx и конфига (по умолчанию: ./data рядом с программой)",
    )
    parser.add_argument(
        "--skip-selftest", action="store_true",
        help="Пропустить предполётные самотесты (НЕ рекомендуется: тесты защищают оборудование от повреждения при поломке кода)",
    )
    # required=False: без подкоманды запускается GUI (см. run.main).
    subparsers = parser.add_subparsers(dest="command", required=False)

    # ---------------- gui ----------------
    subparsers.add_parser(
        "gui",
        help="Запустить графический интерфейс (то же, что запуск без аргументов)",
    )

    # ---------------- selftest ----------------
    subparsers.add_parser(
        "selftest",
        help="Прогнать виртуальные самотесты и проверку NI-VISA, вывести отчёт и выйти",
    )

    # ---------------- measure ----------------
    p_measure = subparsers.add_parser(
        "measure",
        help="Выполнить измерение характеристики датчика (обе полярности через реле), результат — в Excel",
    )
    model_help = "Модель датчика: " + ", ".join(f"{k} ({v} А ном.)" for k, v in MODEL_CLI_ALIASES.items())
    p_measure.add_argument("--model", choices=sorted(MODEL_CLI_ALIASES), default=None, help=model_help)
    p_measure.add_argument("--start", type=float, help="Начальное значение возбуждающего тока, А (обычно 0)")
    p_measure.add_argument("--stop", type=float, help="Конечное значение возбуждающего тока, А")
    p_measure.add_argument("--step", type=float, help="Шаг возбуждающего тока, А")
    p_measure.add_argument("--vlimit", type=float, help="Защитное ограничение напряжения на источнике тока, В")
    p_measure.add_argument("--delay", type=float, help="Задержка на установку возбуждения, с")
    p_measure.add_argument("--cool", type=float, help="Задержка на охлаждение между точками, с")
    p_measure.add_argument("--label", type=str, help="Комментарий (пометка)")
    # Точки номинальной выходной характеристики. Вынесены в параметры, а не
    # зашиты в код: паспортные значения уточняются по ТЗ/ТУ, и у ДТ500А1 ноль
    # может быть слегка смещён.
    p_measure.add_argument(
        "--v-minus", type=float, dest="v_minus", default=None,
        help=f"Выход датчика при I = -I ном., В (по умолчанию {V_MINUS_DEFAULT:g})",
    )
    p_measure.add_argument(
        "--v-zero", type=float, dest="v_zero", default=None,
        help=f"Выход датчика при I = 0, В (по умолчанию {V_ZERO_DEFAULT:g}; у ДТ500А1 может быть смещён)",
    )
    p_measure.add_argument(
        "--v-plus", type=float, dest="v_plus", default=None,
        help=f"Выход датчика при I = +I ном., В (по умолчанию {V_PLUS_DEFAULT:g})",
    )
    p_measure.add_argument(
        "--dmm-addr", type=str, default=None,
        help="VISA-адрес мультиметра (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--src-addr", type=str, default=None,
        help="VISA-адрес источника тока (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--relay-port", type=str, default=None,
        help="Serial-порт платы реле, например COM3 (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--yes", action="store_true",
        help="Не спрашивать подтверждения, использовать сохранённые/переданные параметры без диалога",
    )

    return parser


# ----------------------------------------------------------------------
# Интерактивный ввод параметров measure (с подсказками из сохранённого конфига)
# ----------------------------------------------------------------------

def validate_measure_params(params: dict) -> list:
    """
    Проверяет числовые параметры измерения. Возвращает список текстовых
    описаний ошибок (пустой — если всё в порядке). Используется и в CLI
    (resolve_measure_params), и в GUI, чтобы правила были едиными.

    Защищает от I_step<=0 (деление на ноль в measurement.py) и
    I_stop < I_start (пустой проход измерения), а также от отрицательных
    задержек и неположительного V_limit источника тока.

    Отдельно проверяются точки выходной характеристики: невозрастающая
    тройка (например, перепутанные местами V(-I ном.) и V(+I ном.)) дала бы
    погрешность с обратным знаком, а нулевой размах — деление на ноль.
    """
    errors = []
    if params.get('I_step') is None or params['I_step'] <= 0:
        errors.append("Шаг возбуждающего тока должен быть положительным числом.")
    if params.get('I_start') is None or params.get('I_stop') is None or params['I_stop'] < params['I_start']:
        errors.append("Конечное значение тока должно быть не меньше начального.")
    if params.get('delay') is not None and params['delay'] < 0:
        errors.append("Задержка на установку не может быть отрицательной.")
    if params.get('cooling_delay') is not None and params['cooling_delay'] < 0:
        errors.append("Задержка на охлаждение не может быть отрицательной.")
    if params.get('V_limit') is None or params['V_limit'] <= 0:
        errors.append("Ограничение напряжения должно быть положительным числом.")
    try:
        scale_from_params(params)
    except (ValueError, TypeError) as e:
        errors.append(str(e))
    return errors


def _prompt_float(prompt: str, validator=None, error_msg: str = None) -> float:
    while True:
        try:
            value = float(input(prompt))
        except ValueError:
            print("Ошибка ввода: введите число. Попробуйте снова.")
            continue
        if validator is not None and not validator(value):
            print(error_msg or "Недопустимое значение. Попробуйте снова.")
            continue
        return value


def _prompt_model() -> str:
    names = list(SENSOR_MODELS)
    while True:
        choice = input(f"Модель датчика ({'/'.join(names)}): ").strip()
        if choice in SENSOR_MODELS:
            return choice
        # Разрешаем и ASCII-псевдоним на случай проблем с раскладкой/кодировкой.
        if choice.upper() in MODEL_CLI_ALIASES:
            return MODEL_CLI_ALIASES[choice.upper()]
        print(f"Введите одно из: {', '.join(names)}")


def resolve_measure_params(args, config_mgr: ConfigManager) -> dict:
    """
    Заполняет параметры измерения: сперва из аргументов командной строки,
    затем (если чего-то не хватает) — из сохранённого конфига или интерактивного ввода.
    Обновляет конфиг сохранёнными значениями.

    Модель датчика запрашивается в первую очередь, так как от неё зависит
    номинальный ток (I_ном) и, соответственно, ожидаемое напряжение/погрешность.

    Направление (ветвь) не запрашивается: плата реле сама выполняет проход
    в обе стороны (forward + reverse) в рамках одного запуска measure.
    """
    saved = config_mgr.load()

    # --- модель датчика — спрашиваем в первую очередь ---
    model = MODEL_CLI_ALIASES.get(args.model) if args.model else None
    if model is None:
        last_model = saved.get('model') if saved else None
        if last_model and args.yes:
            model = last_model
        elif last_model:
            use_prev = input(f"Последний раз использовалась модель: {last_model}. Использовать снова? (y/n, по умолчанию y): ").strip().lower()
            if use_prev != 'n':
                model = last_model
        if model is None:
            model = _prompt_model()

    i_nom = SENSOR_MODELS[model]

    params = {
        'model': model,
        'i_nom': i_nom,
        'I_start': args.start,
        'I_stop': args.stop,
        'I_step': args.step,
        'V_limit': args.vlimit,
        'delay': args.delay,
        'cooling_delay': args.cool,
        'label': args.label,
    }

    numeric_keys = ['I_start', 'I_stop', 'I_step', 'V_limit', 'delay', 'cooling_delay']
    have_all_numeric = all(params[k] is not None for k in numeric_keys)

    # Подсказки из сохранённого конфига валидны только если модель совпадает
    saved_matches_model = bool(saved) and saved.get('model') == model

    # --- точки выходной характеристики ---
    # Интерактивно не спрашиваем: у них есть осмысленный номинал (±4 В), а
    # уточнение по ТЗ/ТУ — редкая операция. Приоритет: флаг -> сохранённое
    # (той же модели) -> номинал.
    scale_defaults = {'V_minus': V_MINUS_DEFAULT, 'V_zero': V_ZERO_DEFAULT, 'V_plus': V_PLUS_DEFAULT}
    for key, arg_value in (('V_minus', args.v_minus), ('V_zero', args.v_zero), ('V_plus', args.v_plus)):
        if arg_value is not None:
            params[key] = arg_value
        elif saved_matches_model and saved.get(key) is not None:
            params[key] = saved[key]
        else:
            params[key] = scale_defaults[key]

    print(f"\nНоминальная выходная характеристика {model}: "
          f"V(-I ном.) = {params['V_minus']:+g} В, V(0) = {params['V_zero']:+g} В, "
          f"V(+I ном.) = {params['V_plus']:+g} В")
    print("  (при необходимости уточните по ТЗ/ТУ ключами --v-minus/--v-zero/--v-plus)")

    if not have_all_numeric:
        if saved_matches_model and not args.yes:
            print("\nНайдены сохранённые параметры:")
            print(f"  Возбуждение (А): {saved.get('I_start')} → {saved.get('I_stop')}, шаг {saved.get('I_step')} А")
            print(f"  Ограничение напряжения: {saved.get('V_limit')} В")
            print(f"  Задержка на установку: {saved.get('delay')} с")
            print(f"  Задержка на охлаждение: {saved.get('cooling_delay')} с")
            print(f"  Последний комментарий: {saved.get('label', '')}")
            use_prev = input("\nИспользовать эти параметры? (y/n, по умолчанию y): ").strip().lower()
            if use_prev != 'n':
                for k in numeric_keys:
                    if params[k] is None:
                        params[k] = saved.get(k)

        # Если всё ещё чего-то не хватает — спрашиваем интерактивно
        if not all(params[k] is not None for k in numeric_keys):
            print("\n=== Настройка измерения ===")
            if params['I_start'] is None:
                params['I_start'] = _prompt_float("Начальное значение возбуждающего тока (А): ")
            if params['I_stop'] is None:
                params['I_stop'] = _prompt_float(
                    "Конечное значение возбуждающего тока (А): ",
                    validator=lambda v: v >= params['I_start'],
                    error_msg=f"Конечное значение должно быть не меньше начального ({params['I_start']} А).",
                )
            if params['I_step'] is None:
                params['I_step'] = _prompt_float(
                    "Шаг возбуждающего тока (А): ",
                    validator=lambda v: v > 0,
                    error_msg="Шаг возбуждающего тока должен быть положительным числом.",
                )
            if params['V_limit'] is None:
                params['V_limit'] = _prompt_float(
                    "Ограничение напряжения на источнике (В): ",
                    validator=lambda v: v > 0,
                    error_msg="Ограничение напряжения должно быть положительным числом.",
                )
            if params['delay'] is None:
                params['delay'] = _prompt_float(
                    "Задержка на установку возбуждения (с): ",
                    validator=lambda v: v >= 0,
                    error_msg="Задержка не может быть отрицательной.",
                )
            if params['cooling_delay'] is None:
                params['cooling_delay'] = _prompt_float(
                    "Задержка на охлаждение между точками (с): ",
                    validator=lambda v: v >= 0,
                    error_msg="Задержка не может быть отрицательной.",
                )

    # Финальная проверка — покрывает и значения из --флагов/сохранённого
    # конфига (не проходившие через интерактивные валидаторы выше), и
    # защищает от I_step=0 (деление на ноль в measurement.py) и
    # I_stop < I_start (пустой проход измерения).
    errors = validate_measure_params(params)
    if errors:
        raise ValueError("Некорректные параметры измерения:\n  " + "\n  ".join(errors))

    # --- label ---
    if params['label'] is None:
        last_label = saved.get('label', '') if saved else ''
        hint = f" (Enter для '{last_label}')" if last_label else ""
        label = input(f"Комментарий{hint}: ").strip()
        params['label'] = label if label else last_label

    # Сохраняем итоговые параметры для следующего запуска
    config_mgr.save(params)

    return params


def make_result_filename(data_dir: Path, model: str, label: str) -> Path:
    """
    Имя файла не содержит ветвь (positive/negative) — один xlsx теперь
    содержит обе полярности, а различие фиксируется в колонке Branch.
    """
    label_safe = re.sub(r'[^a-zA-Zа-яА-Я0-9_\- ]', '', label).replace(' ', '_') if label else 'nolabel'
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / f"DTCal_{model}_{label_safe}_{timestamp_str}.xlsx"
