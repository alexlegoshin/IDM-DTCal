#!/usr/bin/env python
"""
DTCal — снятие характеристики датчиков ДТ100А1/ДТ500А1 (вход — ток, выход —
напряжение). Работает и как CLI, и как GUI.

Запуск GUI (по умолчанию, без аргументов или подкомандой gui):
    python run.py
    python run.py gui

Измерение из CLI:
    python run.py measure --model DT100A1 --start 0 --stop 100 --step 5 \
        --vlimit 5 --delay 1 --cool 1 --label "Sensor1"

Измерение автоматически проходит обе полярности (forward/reverse) за один
запуск — переключение направления делает плата реле. В .xlsx пишутся только
первичные данные: заданный ток и измеренное напряжение. Погрешность
программа не считает — это делается вручную по ТЗ/ТУ на датчик.

Перед реальной работой с железом (measure/GUI) выполняется предполётная
проверка: (1) доступность NI-VISA и (2) виртуальные самотесты кода. При
провале любой из проверок измерение не запускается — это страховка
оборудования от повреждения из-за поломки кода.
"""
import sys

from apppaths import default_data_dir
from cli import build_parser, resolve_measure_params, make_result_filename
from config import ConfigManager


def preflight(skip_selftest: bool = False) -> tuple:
    """
    Предполётная проверка перед работой с железом.

    Возвращает (ok: bool, report: str). Проверяет:
      1. NI-VISA доступна и рабочая (visa_backend.check_visa);
      2. виртуальные самотесты кода проходят (selftest.run_selftests),
         если не отключены флагом.
    """
    from visa_backend import check_visa

    lines = []

    visa = check_visa()
    lines.append("[VISA] " + visa.summary_line())
    if not visa.ok:
        lines.append(visa.message)
        return False, "\n".join(lines)

    if skip_selftest:
        lines.append("[Самотесты] ПРОПУЩЕНЫ (--skip-selftest).")
        return True, "\n".join(lines)

    from selftest import run_selftests
    print("Выполняю самотесты (виртуальная проверка кода)...")
    st = run_selftests()
    lines.append("[Самотесты] " + ("OK — " if st.ok else "ПРОВАЛ — ") + st.summary)
    if not st.ok:
        lines.append(st.output)
        return False, "\n".join(lines)

    return True, "\n".join(lines)


def cmd_measure(args) -> int:
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    config_mgr = ConfigManager(data_dir / "dtcal_config.json")

    try:
        params = resolve_measure_params(args, config_mgr)
    except ValueError as e:
        print(f"Ошибка параметров: {e}")
        return 1

    xlsx_path = make_result_filename(data_dir, params['model'], params['label'])
    print(f"\nФайл результатов: {xlsx_path}")
    print(f"Датчик: {params['model']} (I ном. {params['i_nom']} А)")
    print(f"Возбуждение: ток, диапазон {params['I_start']}..{params['I_stop']} А, "
          f"шаг {params['I_step']} А (обе полярности через реле)")
    print(f"Ограничение напряжения источника: {params['V_limit']} В")
    print(f"Комментарий: {params['label']}")
    print(f"Задержка установки: {params['delay']} с, задержка охлаждения: {params['cooling_delay']} с\n")

    # --- Предполётная проверка (VISA + самотесты) ---
    ok, report = preflight(skip_selftest=args.skip_selftest)
    print(report)
    if not ok:
        print("\nПредполётная проверка не пройдена — измерение отменено.")
        return 1
    print()

    from visa_backend import make_resource_manager
    from orchestrate import run_measurement_session

    try:
        rm = make_resource_manager()
    except RuntimeError as e:
        print(f"Ошибка VISA: {e}")
        return 1

    try:
        df = run_measurement_session(
            rm, params, xlsx_path,
            dmm_addr=args.dmm_addr, src_addr=args.src_addr, relay_port=args.relay_port,
            log=print,
        )
    except RuntimeError as e:
        print(f"Ошибка измерения: {e}")
        return 1
    finally:
        try:
            rm.close()
        except Exception:
            pass

    print()
    if df.empty:
        print("Измерение не дало ни одной точки — файл содержит только метаданные.")
        return 1

    print(df.head(10).to_string(index=False))

    # NaN — это не «ноль вольт», а несостоявшееся чтение. Полный провал по
    # всем точкам означает проблему связи, а не результат измерения.
    readings = df['V_meas_V'].dropna()
    if readings.empty:
        print("\nНи одно чтение вольтметра не удалось: все точки NaN. Проверьте подключение прибора.")
        return 1
    print(f"\nЗаписано точек: {len(df)} (успешных чтений: {len(readings)})")
    print(f"Диапазон измеренного напряжения: {readings.min():+.4f} .. {readings.max():+.4f} В")
    return 0


def cmd_gui(args) -> int:
    from gui import launch_gui
    return launch_gui(args)


def cmd_selftest(args) -> int:
    """Диагностика: проверка NI-VISA + прогон виртуальных самотестов."""
    from visa_backend import check_visa
    from selftest import run_selftests

    visa = check_visa()
    print("=== Проверка NI-VISA ===")
    print(visa.message)
    print()

    print("=== Виртуальные самотесты ===")
    st = run_selftests(verbose=True)
    print(st.output.rstrip())
    print()
    print(("ИТОГ: самотесты OK — " if st.ok else "ИТОГ: САМОТЕСТЫ ПРОВАЛЕНЫ — ") + st.summary)
    return 0 if st.ok else 1


def main(argv=None) -> int:
    parser = build_parser(default_data_dir=default_data_dir())
    args = parser.parse_args(argv)

    # Без подкоманды или с 'gui' — запускаем графический интерфейс.
    if args.command is None or args.command == "gui":
        return cmd_gui(args)
    if args.command == "measure":
        return cmd_measure(args)
    if args.command == "selftest":
        return cmd_selftest(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
