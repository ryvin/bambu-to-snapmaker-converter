"""/download must only serve files inside the upload folder."""
import os

import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    (uploads / 'ok.3mf').write_bytes(b'fine')
    (tmp_path / 'secret.txt').write_text('TOP-SECRET')
    monkeypatch.setitem(app_module.app.config, 'UPLOAD_FOLDER', str(uploads))
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def test_serves_a_file_inside_uploads(client):
    resp = client.get('/download/ok.3mf')
    assert resp.status_code == 200
    assert resp.data == b'fine'


@pytest.mark.parametrize('path', [
    '/download/../secret.txt',
    '/download/%2e%2e/secret.txt',
    '/download/..%2fsecret.txt',
    '/download/..%5csecret.txt',
    '/download/a/../../secret.txt',
    '/download/C:%2fWindows%2fwin.ini',
])
def test_refuses_paths_that_leave_uploads(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert b'TOP-SECRET' not in resp.data


def test_safe_upload_path_rules(tmp_path):
    base = str(tmp_path)
    joined = app_module.safe_upload_path(base, 'ok.3mf')
    assert os.path.normpath(joined) == os.path.join(base, 'ok.3mf')
    assert app_module.safe_upload_path(base, 'batch_x/out.3mf') is not None
    for bad in ['..', '../x', 'a/../../x', '/etc/passwd', '..\\x', 'C:x', 'a\0b']:
        assert app_module.safe_upload_path(base, bad) is None, bad


@pytest.mark.skipif(not hasattr(os, 'symlink'), reason='no symlinks')
def test_refuses_symlink_out_of_uploads(tmp_path):
    base = tmp_path / 'uploads'
    base.mkdir()
    (tmp_path / 'secret.txt').write_text('TOP-SECRET')
    try:
        os.symlink(tmp_path / 'secret.txt', base / 'link.txt')
    except OSError:
        pytest.skip('symlink creation not permitted')
    assert app_module.safe_upload_path(str(base), 'link.txt') is None
