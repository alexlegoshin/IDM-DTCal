import json

import pytest

from instruments import (
    Multimeter, CurrentSource,
    find_config_for_idn, discover_instruments,
)
from tests.conftest import FakeVisaResource, FakeResourceManager


def test_find_config_for_idn_matches_by_keyword(instruments_dir):
    cfg = find_config_for_idn("Instrument reply: SIGLENT,SDM3055,...", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akip2101.json"


def test_find_config_for_idn_case_insensitive(instruments_dir):
    cfg = find_config_for_idn("picotest model v7-78/1", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akipb778.json"


def test_find_config_for_idn_no_match_returns_none(instruments_dir):
    cfg = find_config_for_idn("SOME,UNRELATED,DEVICE", instruments_dir / "multimeters")
    assert cfg is None


@pytest.fixture
def akip2101_cfg(instruments_dir):
    return instruments_dir / "multimeters" / "akip2101.json"


def test_multimeter_init_sends_init_commands_and_max_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    cfg = json.loads(akip2101_cfg.read_text(encoding='utf-8'))
    for cmd in cfg['init_commands']:
        assert cmd in fake.written

    max_range = cfg['ranges'][-1]
    assert dmm.range_idx == len(cfg['ranges']) - 1
    assert fake.written[-1] == f'SENS:VOLT:DC:RANG {max_range}'


def test_multimeter_measure_command_is_read_not_meas(akip2101_cfg):
    """
    Регрессия: MEAS:VOLT:DC?/CONF? по SCPI сбрасывают диапазон обратно в
    AUTO при каждом вызове, из-за чего ручной auto_range()/set_range()
    переставали иметь эффект. Конфиг обязан использовать READ?/FETC?.
    """
    cfg = json.loads(akip2101_cfg.read_text(encoding='utf-8'))
    cmd = cfg['measure_command'].upper()
    assert not cmd.startswith('MEAS'), "MEAS? сбрасывает диапазон прибора в AUTO при каждом чтении"
    assert not cmd.startswith('CONF'), "CONF? сбрасывает диапазон прибора в AUTO при каждом чтении"


def test_multimeter_measure_voltage_uses_configured_command(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource(query_responses=["10.001234"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    value = dmm.measure_voltage()

    assert value == pytest.approx(10.001234)
    assert fake.queried[-1] == "READ?"


def test_auto_range_is_first_picks_smallest_covering_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.auto_range(1.5, is_first=True)
    assert dmm.ranges[dmm.range_idx] == 2.0
    assert fake.written[-1] == 'SENS:VOLT:DC:RANG 2.0'


def test_auto_range_is_first_falls_back_to_max_when_over_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.auto_range(999.0, is_first=True)
    assert dmm.range_idx == len(dmm.ranges) - 1


def test_auto_range_steps_up_above_95_percent(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.range_idx = 2  # range 20.0
    dmm.auto_range(19.5, is_first=False)  # > 95% of 20.0
    assert dmm.range_idx == 3
    assert fake.written[-1] == f'SENS:VOLT:DC:RANG {dmm.ranges[3]}'


def test_auto_range_steps_down_below_10_percent(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.range_idx = 3  # range 200.0
    dmm.auto_range(1.0, is_first=False)  # < 10% of 200.0 -> шаг вниз
    assert dmm.ranges[dmm.range_idx] == 20.0


def test_auto_range_stays_put_within_normal_band(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.range_idx = 3  # range 200.0
    before = dmm.range_idx
    dmm.auto_range(50.0, is_first=False)  # between 10% and 95% of 200.0
    assert dmm.range_idx == before


def test_multimeter_close_does_not_raise(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    dmm.close()
    assert fake.closed is True


def test_current_source_setup_sends_voltage_limit_and_zero_current(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "current_sources" / "akip1162.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", cfg_path, rm=rm)
    fake.written.clear()
    src.setup(voltage_limit=5.0)

    assert "SOUR:VOLT 5.0" in fake.written
    assert "SOUR:CURR 0" in fake.written


def test_current_source_set_current_and_output(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "current_sources" / "akip1162.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", cfg_path, rm=rm)
    src.set_current(2.5)
    src.output_on()
    src.output_off()

    assert "SOUR:CURR 2.5" in fake.written
    assert "OUTP ON" in fake.written
    assert "OUTP OFF" in fake.written


def test_current_source_shutdown_zeroes_and_turns_off(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "current_sources" / "akip1162.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", cfg_path, rm=rm)
    fake.written.clear()
    src.shutdown()

    assert fake.written == ["SOUR:CURR 0", "OUTP OFF"]


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


def test_discover_instruments_raises_when_no_resources(instruments_dir):
    rm = FakeResourceManager({})
    with pytest.raises(RuntimeError):
        discover_instruments(instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm)


def test_discover_instruments_raises_when_source_missing(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res})

    with pytest.raises(RuntimeError, match="источник"):
        discover_instruments(instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm)
