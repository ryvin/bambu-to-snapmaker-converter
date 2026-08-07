import os

import dedup


def _write(p, data):
    with open(p, 'wb') as f:
        f.write(data)


def test_finds_identical_and_keeps_canonical(tmp_path):
    d = tmp_path
    _write(str(d / "580MM_U1.3mf"), b"AAAA")          # canonical
    _write(str(d / "580MM_U1_v2.3mf"), b"AAAA")       # identical version
    _write(str(d / "580MM_U1_v3.3mf"), b"AAAA")       # identical version
    _write(str(d / "other (1)_U1.3mf"), b"BBBB")      # copy-suffix
    _write(str(d / "other_U1.3mf"), b"BBBB")          # canonical for BBBB
    _write(str(d / "unique_U1.3mf"), b"CCCC")         # no duplicate

    groups = dedup.find_duplicate_groups(str(d))
    keeps = {os.path.basename(g[0]) for g in groups}
    assert keeps == {"580MM_U1.3mf", "other_U1.3mf"}   # canonical names kept
    assert len(groups) == 2


def test_dry_run_deletes_nothing(tmp_path):
    d = tmp_path
    _write(str(d / "a_U1.3mf"), b"X")
    _write(str(d / "a_U1_v2.3mf"), b"X")
    rep = dedup.dedup_folder(str(d), apply=False)
    assert rep['removed_count'] == 1 and not rep['applied']
    assert sorted(os.listdir(d)) == ["a_U1.3mf", "a_U1_v2.3mf"]  # still there


def test_apply_removes_extras_keeps_one(tmp_path):
    d = tmp_path
    _write(str(d / "a_U1.3mf"), b"X" * 100)
    _write(str(d / "a_U1_v2.3mf"), b"X" * 100)
    _write(str(d / "a_U1_v3.3mf"), b"X" * 100)
    rep = dedup.dedup_folder(str(d), apply=True)
    assert rep['removed_count'] == 2 and rep['bytes_freed'] == 200
    assert os.listdir(d) == ["a_U1.3mf"]                # only canonical remains
    # content preserved (lossless)
    assert (d / "a_U1.3mf").read_bytes() == b"X" * 100


def test_different_content_not_merged(tmp_path):
    d = tmp_path
    _write(str(d / "a_U1.3mf"), b"AAAA")
    _write(str(d / "b_U1.3mf"), b"BBBB")               # same size, different content
    rep = dedup.dedup_folder(str(d), apply=True)
    assert rep['removed_count'] == 0
    assert sorted(os.listdir(d)) == ["a_U1.3mf", "b_U1.3mf"]


def test_pattern_scopes_files(tmp_path):
    d = tmp_path
    _write(str(d / "a_U1.3mf"), b"X")
    _write(str(d / "a_U1_v2.3mf"), b"X")
    _write(str(d / "keep.txt"), b"X")                  # identical but not *.3mf
    rep = dedup.dedup_folder(str(d), pattern="*.3mf", apply=True)
    assert rep['removed_count'] == 1
    assert "keep.txt" in os.listdir(d)                 # untouched by pattern
