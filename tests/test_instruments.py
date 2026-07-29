import json

import pytest

from instruments import (
    Multimeter, CurrentSource,
    find_config_for_idn, discover_instruments,
)
from tests.conftest import FakeVisaResource, FakeResourceManager


# ----------------------------------------------------------------------
# Подбор конфига по *IDN?
# ----------------------------------------------------------------------

def test_find_config_for_idn_matches_by_keyword(instruments_dir):
    cfg = find_config_for_idn("Instrument reply: SIGLENT,SDM3055,...", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akip2101.json"


def test_find_config_for_idn_case_insensitive(instruments_dir):
    cfg = find_config_for_idn("picotest model v7-78/1", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akipb778.json"


def test_find_config_for_idn_matches_rigol(instruments_dir):
    """Реальный ответ прибора из стенда IDM-DNKMeter."""
    cfg = find_config_for_idn("RIGOL TECHNOLOGIES,DM3068,DM3O000000001,01.01", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "rigol_dm3068.json"


def test_find_config_for_idn_matches_picotest_prist_idn(instruments_dir):
    """Проверенный вживую IDN АКИП-B7-78/1 начинается с 'Prist', а не 'Picotest'."""
    cfg = find_config_for_idn("Prist,V7-78/1,TW00053362,03.45-01-04", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akipb778.json"


def test_find_config_for_idn_no_match_returns_none(instruments_dir):
    cfg = find_config_for_idn("SOME,UNRELATED,DEVICE", instruments_dir / "multimeters")
    assert cfg is None


def test_find_config_for_idn_skips_broken_json(tmp_path, capsys):
    """
    Битый конфиг не должен ломать автопоиск: рядом лежащий валидный файл
    обязан быть найден (раньше json.loads падал и убивал весь discover).
    """
    (tmp_path / "broken.json").write_text("{not json", encoding='utf-8')
    (tmp_path / "good.json").write_text(json.dumps({"keywords": ["GOODDEV"]}), encoding='utf-8')

    cfg = find_config_for_idn("VENDOR,GOODDEV,1,1", tmp_path)

    assert cfg is not None and cfg.name == "good.json"
    assert "broken.json" in capsys.readouterr().out


# ----------------------------------------------------------------------
# Мультиметр: диапазоны и чтение
# ----------------------------------------------------------------------

@pytest.fixture
def akip2101_cfg(instruments_dir):
    return instruments_dir / "multimeters" / "akip2101.json"


@pytest.fixture
def rigol_cfg(instruments_dir):
    return instruments_dir / "multimeters" / "rigol_dm3068.json"


@pytest.fixture
def picotest_cfg(instruments_dir):
    return instruments_dir / "multimeters" / "akipb778.json"


def test_multimeter_init_prefers_hardware_autorange(akip2101_cfg, make_fake_rm):
    """
    Основное решение DTCal: доверяем аппаратному автодиапазону прибора, а не
    массиву ranges из конфига. Выход датчика всегда 2..10 В, гоняться за
    диапазоном не нужно, а ошибиться в массиве шкал легко.
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    cfg = json.loads(akip2101_cfg.read_text(encoding='utf-8'))
    for cmd in cfg['init_commands']:
        assert cmd in fake.written
    assert dmm.autorange_active is True
    assert fake.written[-1] == cfg['autorange_command']
    # Фиксированный диапазон не выставлялся.
    assert not any('RANG 2' in c for c in fake.written)


def test_multimeter_falls_back_to_fixed_range_when_autorange_unsupported(akip2101_cfg, make_fake_rm):
    """Если прибор не принял команду автодиапазона — ставим фиксированную шкалу, покрывающую 10 В."""
    fake = FakeVisaResource(fail_writes=["SENS:VOLT:DC:RANG:AUTO"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    assert dmm.autorange_active is False
    assert dmm.active_range == 20.0  # наименьшая шкала Siglent, покрывающая 10 В
    assert fake.written[-1] == 'SENS:VOLT:DC:RANG 20.0'


def test_multimeter_survives_when_everything_is_rejected(akip2101_cfg, make_fake_rm):
    """
    Дуракозащита: прибор, отвергающий вообще все команды настройки, не должен
    ронять конструктор — измерять всё равно можно на текущих настройках.
    """
    fake = FakeVisaResource(fail_writes=["*RST", "SENS", "CONF", "READ"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    assert dmm.autorange_active is False


def test_multimeter_measure_voltage_uses_configured_command(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource(query_responses=["10.001234"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    value = dmm.measure_voltage()

    assert value == pytest.approx(10.001234)
    assert fake.queried[-1] == "MEAS:VOLT:DC?"


def test_multimeter_parses_scientific_notation(rigol_cfg, make_fake_rm):
    """Rigol отвечает в формате '+6.00000000E+00\\n'."""
    fake = FakeVisaResource(query_responses=["+6.00000000E+00\n"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", rigol_cfg, rm=rm)

    assert dmm.measure_voltage() == pytest.approx(6.0)
    assert fake.queried[-1] == ":MEASure:VOLTage:DC?"


def test_multimeter_parses_first_field_of_comma_separated_reply(akip2101_cfg, make_fake_rm):
    """Некоторые приборы отдают несколько значений через запятую."""
    fake = FakeVisaResource(query_responses=["+8.1234E+00,+1.0E+00"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    assert dmm.measure_voltage() == pytest.approx(8.1234)


def test_multimeter_measure_voltage_uses_fallback_command(akip2101_cfg, make_fake_rm):
    """Если основная команда чтения не сработала, пробуем резервную."""
    fake = FakeVisaResource(query_responses=[RuntimeError("undefined header"), "7.5"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    value = dmm.measure_voltage()

    assert value == pytest.approx(7.5)
    assert fake.queried[-2:] == ["MEAS:VOLT:DC?", "READ?"]


def test_multimeter_measure_voltage_raises_when_all_commands_fail(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource(query_responses=[RuntimeError("timeout"), RuntimeError("timeout")])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    with pytest.raises(RuntimeError):
        dmm.measure_voltage()


def test_multimeter_empty_reply_is_an_error_not_zero(akip2101_cfg, make_fake_rm):
    """Пустой ответ прибора нельзя принять за 0 В — это скрыло бы сбой связи."""
    fake = FakeVisaResource(query_responses=["", "  "])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    with pytest.raises(RuntimeError):
        dmm.measure_voltage()


def test_multimeter_set_range_uses_value_template(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    assert dmm.set_range(20.0) is True
    assert fake.written[-1] == 'SENS:VOLT:DC:RANG 20.0'


def test_multimeter_set_range_uses_index_template_for_rigol(rigol_cfg, make_fake_rm):
    """
    Rigol задаёт диапазон ИНДЕКСОМ шкалы (:MEASure:VOLTage:DC 2 == 20 В),
    а не значением в вольтах — сверено по Programming Guide for DM3000.
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", rigol_cfg, rm=rm)

    assert dmm.set_range(20.0) is True
    assert fake.written[-1] == ':MEASure:VOLTage:DC 2'


def test_picotest_ranges_are_not_siglent_ranges(picotest_cfg):
    """
    Регрессия: в конфиг АКИП-B7-78/1 был скопирован массив шкал Siglent
    (0.2/2/20/200/1000). У Picotest M3500A шкалы 0.1/1/10/100/1000, из-за
    чего запрос 20 В прибор округлял вверх до 100 В, теряя разрешение.
    """
    cfg = json.loads(picotest_cfg.read_text(encoding='utf-8'))
    assert cfg['ranges'] == [0.1, 1.0, 10.0, 100.0, 1000.0]


def test_multimeter_fallback_range_covers_full_sensor_scale(picotest_cfg, make_fake_rm):
    """Резервный диапазон обязан покрывать 10 В — максимум выхода датчика."""
    fake = FakeVisaResource(fail_writes=["SENS:VOLT:DC:RANG:AUTO"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", picotest_cfg, rm=rm)

    assert dmm.active_range == 10.0


def test_multimeter_close_does_not_raise(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    dmm.close()
    assert fake.closed is True


# ----------------------------------------------------------------------
# Источник тока
# ----------------------------------------------------------------------

@pytest.fixture
def source_cfg(instruments_dir):
    return instruments_dir / "current_sources" / "akip1162.json"


def test_current_source_setup_sends_voltage_limit_and_zero_current(source_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", source_cfg, rm=rm)
    fake.written.clear()
    src.setup(voltage_limit=5.0)

    assert "SOUR:VOLT 5.0" in fake.written
    assert "SOUR:CURR 0" in fake.written


def test_current_source_set_current_and_output(source_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", source_cfg, rm=rm)
    src.set_current(2.5)
    src.output_on()
    src.output_off()

    assert "SOUR:CURR 2.5" in fake.written
    assert "OUTP ON" in fake.written
    assert "OUTP OFF" in fake.written


def test_current_source_shutdown_zeroes_and_turns_off(source_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", source_cfg, rm=rm)
    fake.written.clear()
    src.shutdown()

    assert fake.written == ["SOUR:CURR 0", "OUTP OFF"]


def test_current_source_shutdown_turns_output_off_even_if_zeroing_fails(source_cfg, make_fake_rm):
    """
    Безопасность: раньше исключение на обнулении тока прерывало shutdown и
    выход источника оставался включённым. Снятие выхода обязано выполниться.
    """
    fake = FakeVisaResource(fail_writes=["SOUR:CURR 0"])
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", source_cfg, rm=rm)
    fake.written.clear()
    src.shutdown()

    assert "OUTP OFF" in fake.written


def test_current_source_set_current_failure_is_not_silenced(source_cfg, make_fake_rm):
    """
    Обратная сторона: молча промахнуться по току нельзя — данные были бы
    сняты не при том токе, который записан в отчёт.
    """
    fake = FakeVisaResource(fail_writes=["SOUR:CURR"])
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", source_cfg, rm=rm)

    with pytest.raises(Exception):
        src.set_current(10.0)


# ----------------------------------------------------------------------
# Автообнаружение
# ----------------------------------------------------------------------

def test_discover_instruments_finds_dmm_and_source(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    src_res = FakeVisaResource(idn="ITECH,IT-M3122,SN002,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res, "SRC_ADDR": src_res})

    dmm_addr, dmm_cfg, src_addr, src_cfg = discover_instruments(
        instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm,
    )

    assert dmm_addr == "DMM_ADDR"
    assert dmm_cfg.name == "akip2101.json"
    assert src_addr == "SRC_ADDR"
    assert src_cfg.name == "akip1162.json"


def test_discover_instruments_closes_resources_even_on_query_error(instruments_dir):
    """Не опрошенный ресурс, оставленный открытым, блокирует прибор до перезапуска."""
    broken = FakeVisaResource(query_responses=[RuntimeError("no response")])
    broken.idn = None
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    src_res = FakeVisaResource(idn="ITECH,IT-M3122,SN002,1.0")
    rm = FakeResourceManager({"BROKEN": broken, "DMM_ADDR": dmm_res, "SRC_ADDR": src_res})

    discover_instruments(
        instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm,
    )

    assert broken.closed is True


def test_discover_instruments_raises_when_no_resources(instruments_dir):
    rm = FakeResourceManager({})
    with pytest.raises(RuntimeError):
        discover_instruments(instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm)


def test_discover_instruments_raises_when_source_missing(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res})

    with pytest.raises(RuntimeError, match="источник"):
        discover_instruments(instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm)


def test_discover_instruments_does_not_require_what_caller_already_has(instruments_dir):
    """
    Когда адрес мультиметра задан вручную, отсутствие мультиметра в
    автопоиске не является ошибкой — нужен только источник.
    """
    src_res = FakeVisaResource(idn="ITECH,IT-M3122,SN002,1.0")
    rm = FakeResourceManager({"SRC_ADDR": src_res})

    dmm_addr, dmm_cfg, src_addr, src_cfg = discover_instruments(
        instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm,
        require_multimeter=False,
    )

    assert dmm_addr is None and dmm_cfg is None
    assert src_addr == "SRC_ADDR"
