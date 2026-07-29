import json

import pytest


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _multimeter_configs(instruments_dir):
    return sorted((instruments_dir / "multimeters").glob("*.json"))


def _current_source_configs(instruments_dir):
    return sorted((instruments_dir / "current_sources").glob("*.json"))


def test_all_json_configs_are_valid_json(instruments_dir):
    files = list(instruments_dir.glob("**/*.json"))
    assert files, "Не найдено ни одного конфига приборов"
    for f in files:
        _load(f)  # не должно бросить исключение


@pytest.mark.parametrize("get_files", [_multimeter_configs])
def test_multimeter_configs_have_required_keys(instruments_dir, get_files):
    required = (
        "model_name", "keywords", "init_commands", "measure_command", "ranges",
        "autorange_command", "range_command",
    )
    for f in get_files(instruments_dir):
        cfg = _load(f)
        for key in required:
            assert key in cfg, f"{f.name}: отсутствует ключ '{key}'"
        assert isinstance(cfg["keywords"], list) and cfg["keywords"], f"{f.name}: keywords пуст"
        assert isinstance(cfg["ranges"], list) and cfg["ranges"], f"{f.name}: ranges пуст"


def test_multimeter_ranges_are_strictly_ascending(instruments_dir):
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        ranges = cfg["ranges"]
        assert ranges == sorted(ranges), f"{f.name}: ranges должны быть отсортированы по возрастанию"
        assert len(set(ranges)) == len(ranges), f"{f.name}: ranges содержат дубликаты"
        assert all(r > 0 for r in ranges), f"{f.name}: диапазоны должны быть положительными"


def test_multimeter_measure_command_is_a_query(instruments_dir):
    """
    Команда чтения обязана быть запросом (оканчиваться на '?'), иначе
    instr.query() зависнет в ожидании ответа до таймаута.
    """
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        cmd = cfg["measure_command"].strip()
        assert cmd.endswith("?"), f"{f.name}: measure_command должен быть запросом, получено {cmd!r}"
        fallback = cfg.get("fallback_measure_command")
        if fallback:
            assert fallback.strip().endswith("?"), \
                f"{f.name}: fallback_measure_command должен быть запросом, получено {fallback!r}"


def test_multimeter_init_does_not_disable_autorange(instruments_dir):
    """
    Инверсия политики IVTrace (осознанная, не регресс).

    В IVTrace измерялся ток в диапазоне нескольких декад, диапазон вёлся
    вручную, поэтому init_commands обязаны были содержать RANG:AUTO OFF, а
    measure_command — быть READ?/FETC?, чтобы MEAS? не сбрасывал шкалу.

    В DTCal измеряется выход датчика, всегда лежащий в 2..10 В. Ручное
    ведение диапазона не нужно, а ошибиться в массиве ranges легко (у
    Picotest шкалы 0.1/1/10/100/1000, у Siglent и Rigol — 0.2/2/20/200/1000).
    Поэтому основной режим — аппаратный автодиапазон прибора, и явное
    отключение автодиапазона в init_commands запрещено: оно бы его гасило.
    Фиксированный диапазон остаётся резервом (Multimeter._setup_ranging).
    """
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        for cmd in cfg["init_commands"]:
            upper = cmd.upper()
            assert not ("RANG" in upper and "AUTO" in upper and "OFF" in upper), \
                f"{f.name}: init_commands не должны отключать автодиапазон ({cmd!r})"


def test_multimeter_range_command_has_a_substitution_placeholder(instruments_dir):
    """
    range_command подставляется через str.format(range=..., index=...).
    Шаблон без плейсхолдера молча отправлял бы одну и ту же команду.
    """
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        template = cfg["range_command"]
        assert "{range}" in template or "{index}" in template, \
            f"{f.name}: range_command должен содержать {{range}} или {{index}}, получено {template!r}"
        # Шаблон обязан быть форматируемым без KeyError.
        template.format(range=10.0, index=2)


def test_current_source_configs_have_required_keys(instruments_dir):
    for f in _current_source_configs(instruments_dir):
        cfg = _load(f)
        for key in ("model_name", "keywords", "init_commands", "setup_commands", "output_on", "output_off"):
            assert key in cfg, f"{f.name}: отсутствует ключ '{key}'"
        for key in ("voltage_limit", "current"):
            assert key in cfg["setup_commands"], f"{f.name}: setup_commands.{key} отсутствует"


def test_all_configs_have_distinct_nonempty_keywords_within_their_directory(instruments_dir):
    """keywords не должны пересекаться внутри одной папки — иначе find_config_for_idn неоднозначен."""
    for get_files in (_multimeter_configs, _current_source_configs):
        files = get_files(instruments_dir)
        seen = {}
        for f in files:
            cfg = _load(f)
            for kw in cfg["keywords"]:
                kw_norm = kw.upper()
                assert kw_norm not in seen, (
                    f"keyword {kw!r} встречается и в {seen.get(kw_norm)!r}, и в {f.name!r} — "
                    f"find_config_for_idn будет неоднозначным"
                )
                seen[kw_norm] = f.name
