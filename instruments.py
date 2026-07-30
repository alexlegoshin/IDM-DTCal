"""
Обёртки над приборами: вольтметр (выход датчика) и источник тока (возбуждение).

Политика диапазонов вольтметра (важно, отличается от IVTrace):
    В IVTrace измерялся ток в диапазоне нескольких декад, поэтому диапазон
    вёлся вручную по массиву ranges из конфига. В DTCal измеряемая величина —
    выходное напряжение датчика; оно лежит в пределах единиц вольт, но точная
    шкала зависит от подключённого датчика и программе неизвестна. Гоняться
    за диапазоном не нужно, а вот ошибиться в массиве ranges — легко
    (например, у Picotest/АКИП-B7-78/1 шкалы 0.1/1/10/100/1000, а не
    0.2/2/20/200/1000, как у Siglent и Rigol).

    Поэтому приоритет отдан аппаратному автодиапазону самого прибора, а наш
    массив ranges остаётся только резервом, если автодиапазон не поддержан.
    Все команды прибору идут через _write(), который не бросает исключений:
    один неподдержанный SCPI-запрос не должен ронять сессию измерения.
"""
import json
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pyvisa


# Резервный диапазон вольтметра подбирается под эту величину, В.
# Точный выход датчика заранее неизвестен (бывает биполярным ±4 В, бывает
# однополярным 2..10 В), поэтому берём заведомо покрывающее значение:
# ошибка в большую сторону стоит разрешения, в меньшую — уводит прибор в
# перегрузку. Используется, только если прибор не поддержал автодиапазон.
SENSOR_FULL_SCALE_V = 10.0


class Multimeter:
    """Обёртка над вольтметром/мультиметром, измеряющим выходное напряжение датчика (АКИП-2101, АКИП-B7-78/1, Rigol DM3068 и т.п.)."""

    def __init__(self, resource_addr: str, config_path: Path, rm: Optional[pyvisa.ResourceManager] = None):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.rm = rm or pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        # USBTMC-приборы (например Rigol DM3068) чувствительны к терминаторам.
        if self.config.get('write_termination') is not None:
            self.instr.write_termination = self.config['write_termination']
        if self.config.get('read_termination') is not None:
            self.instr.read_termination = self.config['read_termination']

        self.ranges: List[float] = sorted(self.config.get('ranges') or [])
        self.autorange_active = False
        self.active_range: Optional[float] = None
        self._init_device()

    # ------------------------------------------------------------------ низкий уровень
    def _write(self, cmd: str) -> bool:
        """
        Отправляет команду, никогда не бросая исключение. Возвращает True при успехе.

        Прибор может не поддерживать конкретный SCPI-запрос (набор команд у
        Siglent, Picotest и Rigol различается) — это не повод прерывать
        измерение, достаточно предупредить в журнал и работать дальше.
        """
        try:
            self.instr.write(cmd)
            return True
        except Exception as e:
            print(f"  [вольтметр] предупреждение: команда {cmd!r} не принята ({e})")
            return False

    @staticmethod
    def _parse_reading(raw: str) -> float:
        """
        Приборы отвечают по-разному: '+6.00000000E+00', с завершающим переводом
        строки, иногда несколькими значениями через запятую. Берём первое число.
        """
        s = (raw or '').strip()
        if not s:
            raise ValueError("пустой ответ прибора")
        return float(s.split(',')[0].strip())

    # ------------------------------------------------------------------ инициализация
    def _init_device(self):
        for cmd in self.config.get('init_commands', []):
            self._write(cmd)
            time.sleep(0.5 if cmd.strip() == '*RST' else 0.1)
        self._setup_ranging()

    def _setup_ranging(self):
        """Автодиапазон прибора — основной режим; фиксированный диапазон — резерв."""
        cmd = self.config.get('autorange_command')
        if cmd and self._write(cmd):
            self.autorange_active = True
            print("  [вольтметр] аппаратный автодиапазон включён")
            return

        fallback = self._fallback_range()
        if fallback is not None and self.set_range(fallback):
            print(f"  [вольтметр] автодиапазон недоступен, фиксированный диапазон {fallback:g} В")
            return

        print("  [вольтметр] диапазон задать не удалось, работаю на текущих настройках прибора")

    def _fallback_range(self) -> Optional[float]:
        """Наименьший диапазон из конфига, покрывающий SENSOR_FULL_SCALE_V."""
        for r in self.ranges:
            if r >= SENSOR_FULL_SCALE_V:
                return r
        return self.ranges[-1] if self.ranges else None

    def set_range(self, range_val: float) -> bool:
        """
        Ставит фиксированный диапазон. Возвращает True при успехе (не бросает).

        Приборы задают диапазон по-разному: Siglent/Picotest принимают значение
        в вольтах (SENS:VOLT:DC:RANG 20), Rigol — индекс шкалы
        (:MEASure:VOLTage:DC 2, где 2 = 20 В). Поэтому в шаблон подставляются
        оба варианта, а конфиг выбирает нужный плейсхолдер: {range} или {index}.
        """
        template = self.config.get('range_command', 'SENS:VOLT:DC:RANG {range}')
        try:
            index = self.ranges.index(range_val)
        except ValueError:
            index = max(len(self.ranges) - 1, 0)
        ok = self._write(template.format(range=range_val, index=index))
        if ok:
            self.autorange_active = False
            self.active_range = range_val
        return ok

    # ------------------------------------------------------------------ измерение
    def measure_voltage(self) -> float:
        """
        Читает выходное напряжение датчика, В.

        Пробует основную команду, затем резервную (наборы команд у приборов
        различаются). Бросает RuntimeError, только если не сработала ни одна —
        measurement.py трактует это как неудачное чтение точки.
        """
        commands = [self.config['measure_command']]
        fallback = self.config.get('fallback_measure_command')
        if fallback and fallback not in commands:
            commands.append(fallback)

        last_error = None
        for cmd in commands:
            try:
                return self._parse_reading(self.instr.query(cmd))
            except Exception as e:
                last_error = e
        raise RuntimeError(f"не удалось прочитать напряжение: {last_error}")

    def close(self):
        try:
            self.instr.close()
        except Exception:
            pass


class CurrentSource:
    """Обёртка над источником тока (например ITECH IT-M)."""

    def __init__(self, resource_addr: str, config_path: Path, rm: Optional[pyvisa.ResourceManager] = None):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.rm = rm or pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        self._init_device()

    def _init_device(self):
        # Инициализация терпима к неподдержанным командам (наборы у моделей
        # различаются). Реальный обрыв связи всё равно вылезет на set_current(),
        # который умышленно НЕ глушится — молча промахнуться по току нельзя.
        for cmd in self.config['init_commands']:
            try:
                self.instr.write(cmd)
            except Exception as e:
                print(f"  [источник] предупреждение: команда {cmd!r} не принята ({e})")
            time.sleep(0.5 if cmd.strip() == '*RST' else 0.1)

    def setup(self, voltage_limit: float, slew_rate: float = 10.0):
        cmds = self.config['setup_commands']
        self.instr.write(cmds['voltage_limit'].format(voltage=voltage_limit))
        self.instr.write(cmds['current'].format(current=0))
        if 'slew_rate' in cmds:
            self.instr.write(cmds['slew_rate'].format(rate=slew_rate))

    def set_current(self, current: float):
        self.instr.write(self.config['setup_commands']['current'].format(current=current))

    def output_on(self):
        self.instr.write(self.config['output_on'])

    def output_off(self):
        self.instr.write(self.config['output_off'])

    def shutdown(self):
        """
        Безопасное выключение: обнулить ток и снять выход.

        Каждый шаг в своём try — если обнуление тока не прошло (обрыв связи,
        занятый прибор), выход всё равно обязан быть снят. Раньше исключение
        на set_current(0) оставляло выход источника включённым.
        """
        try:
            self.set_current(0)
        except Exception as e:
            print(f"  [источник] не удалось обнулить ток: {e}")
        try:
            self.output_off()
        except Exception as e:
            print(f"  [источник] НЕ УДАЛОСЬ СНЯТЬ ВЫХОД: {e} — проверьте источник вручную!")

    def close(self):
        try:
            self.instr.close()
        except Exception:
            pass


def find_config_for_idn(idn: str, config_dir: Path) -> Optional[Path]:
    """
    Ищет json-конфиг в config_dir (нерекурсивно), у которого keywords
    встречаются в строке IDN.

    Битый или нечитаемый json пропускается с предупреждением: один
    испорченный файл в папке конфигов не должен ломать автопоиск приборов
    целиком (валидность всех конфигов отдельно проверяется самотестами).
    """
    idn_upper = (idn or '').upper()
    for json_file in sorted(Path(config_dir).glob("*.json")):
        try:
            cfg = json.loads(json_file.read_text(encoding='utf-8'))
        except Exception as e:
            print(f"  Предупреждение: конфиг {json_file.name} пропущен ({e})")
            continue
        keywords = cfg.get("keywords", [])
        if any(str(kw).upper() in idn_upper for kw in keywords):
            return json_file
    return None


def discover_instruments(
    multimeter_dir: Path,
    source_dir: Path,
    rm: Optional[pyvisa.ResourceManager] = None,
    query_timeout: int = 3000,
    source_label: str = "источник",
    require_multimeter: bool = True,
    require_source: bool = True,
) -> Tuple[str, Path, str, Path]:
    """
    Перебирает все доступные VISA-ресурсы, опрашивает *IDN? и сопоставляет
    каждый ответ с json-конфигами мультиметров и источников (тип источника —
    ток или напряжение — определяется тем, какая source_dir передана).

    Возвращает (dmm_addr, dmm_config_path, src_addr, src_config_path);
    ненайденные необязательные позиции возвращаются как None.

    require_multimeter/require_source — что обязательно должно быть найдено.
    Нужны, когда часть адресов задана вручную: тогда ненайденный прибор,
    адрес которого и так известен вызывающему коду, не должен считаться
    ошибкой (см. orchestrate._resolve_instruments).
    """
    rm = rm or pyvisa.ResourceManager()
    resources = rm.list_resources()

    if len(resources) == 0:
        raise RuntimeError("Не найдено ни одного VISA-ресурса. Проверьте подключение и драйверы NI-VISA.")

    dmm_addr = dmm_cfg = None
    src_addr = src_cfg = None

    print("Поиск приборов...")
    for res in resources:
        instr = None
        try:
            instr = rm.open_resource(res)
            instr.encoding = 'utf-8'
            instr.timeout = query_timeout
            idn = instr.query('*IDN?').strip()
            print(f"  {res}  ->  {idn}")

            if dmm_addr is None:
                cfg = find_config_for_idn(idn, multimeter_dir)
                if cfg is not None:
                    dmm_addr, dmm_cfg = res, cfg

            if src_addr is None:
                cfg = find_config_for_idn(idn, source_dir)
                if cfg is not None:
                    src_addr, src_cfg = res, cfg
        except Exception as e:
            print(f"  {res}  ->  Ошибка при опросе: {e}")
        finally:
            # Закрывать обязательно и на ошибке: не опрошенный ресурс,
            # оставленный открытым, блокирует прибор для следующего открытия.
            if instr is not None:
                try:
                    instr.close()
                except Exception:
                    pass

    missing = []
    if require_multimeter and not dmm_addr:
        missing.append("мультиметр")
    if require_source and not src_addr:
        missing.append(source_label)
    if missing:
        raise RuntimeError(
            f"Не удалось обнаружить: {', '.join(missing)}. Проверьте список ресурсов выше и json-конфиги."
        )

    if dmm_addr:
        print(f"\nМультиметр: {dmm_addr}  ({dmm_cfg.stem})")
    if src_addr:
        print(f"{source_label.capitalize()}: {src_addr}  ({src_cfg.stem})\n")

    return dmm_addr, dmm_cfg, src_addr, src_cfg
