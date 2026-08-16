import pytest

from app import _resize_filament_settings, U1_PER_FILAMENT_EXTRA_KEYS, ConversionError


def _template(n=4):
    """Minimal U1-shaped project_settings with per-filament (n), per-extruder (4),
    per-plate (1), and flush structures (n*n matrix, 2n vector)."""
    return {
        'filament_colour': [f'#{i:06d}FF' for i in range(n)],
        'filament_type': ['PLA'] * n,
        'nozzle_temperature': ['220'] * n,            # per-filament, no 'filament_' prefix
        'required_nozzle_HRC': ['0'] * n,             # per-filament, no prefix
        'nozzle_diameter': ['0.4'] * 4,               # per-extruder -> stays 4
        'extruder_colour': ['#000000'] * 4,           # per-extruder -> stays 4
        'wipe_tower_x': ['165.0'],                    # per-plate -> untouched
        'wiping_volumes_extruders': ['70'] * 10,      # legacy -> untouched
        'flush_volumes_matrix': ['0' if i == j else '140'
                                 for i in range(n) for j in range(n)],
        'flush_volumes_vector': ['140'] * (2 * n),
        'flush_multiplier': 0.3,                      # scalar -> untouched
        'empty_list': [],                             # len-0 -> untouched
    }


def test_grows_per_filament_arrays_to_n():
    ps = _template(4)
    _resize_filament_settings(ps, 6)
    for k in ('filament_colour', 'filament_type', 'nozzle_temperature', 'required_nozzle_HRC'):
        assert len(ps[k]) == 6, k
    # extend-with-last preserved values
    assert ps['nozzle_temperature'] == ['220'] * 6


def test_per_extruder_and_per_plate_untouched():
    ps = _template(4)
    _resize_filament_settings(ps, 6)
    assert len(ps['nozzle_diameter']) == 4
    assert len(ps['extruder_colour']) == 4
    assert ps['wipe_tower_x'] == ['165.0']
    assert ps['wiping_volumes_extruders'] == ['70'] * 10
    assert ps['flush_multiplier'] == 0.3
    assert ps['empty_list'] == []


def test_flush_matrix_rebuilt_n_by_n_diagonal_zero():
    ps = _template(4)
    _resize_filament_settings(ps, 6)
    m = ps['flush_volumes_matrix']
    assert len(m) == 36                       # 6*6
    assert all(m[i * 6 + i] == '0' for i in range(6))          # zero diagonal
    # preserved top-left 4x4 (all '140' off-diagonal here), new cells filled '280'
    assert m[0 * 6 + 1] == '140'              # inside old 4x4
    assert m[0 * 6 + 5] == '280'              # new filament pair
    assert m[5 * 6 + 0] == '280'
    assert len(ps['flush_volumes_vector']) == 12               # 2*6


def test_noop_when_already_n():
    ps = _template(4)
    before = {k: (list(v) if isinstance(v, list) else v) for k, v in ps.items()}
    _resize_filament_settings(ps, 4)
    assert ps == before                       # 4->4 changes nothing


def test_truncates_when_fewer():
    ps = _template(6)
    _resize_filament_settings(ps, 5)
    assert len(ps['filament_colour']) == 5
    assert len(ps['nozzle_temperature']) == 5
    assert len(ps['flush_volumes_matrix']) == 25
    assert len(ps['flush_volumes_vector']) == 10


def test_non_square_matrix_fails_loud():
    ps = _template(4)
    ps['flush_volumes_matrix'] = ['0'] * 15   # not a perfect square
    with pytest.raises(ConversionError):
        _resize_filament_settings(ps, 6)


def test_every_extra_key_is_recognized_per_filament():
    # A dict with each extra key at length 4 must all become length N.
    ps = {k: ['x'] * 4 for k in U1_PER_FILAMENT_EXTRA_KEYS}
    _resize_filament_settings(ps, 7)
    assert all(len(v) == 7 for v in ps.values())
