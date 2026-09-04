import socket
import stat
import tempfile
from pathlib import Path

import pytest

from app.run_collector import create_listener, remove_stale_socket


def test_create_listener_preserves_group_only_socket_access():
    with tempfile.TemporaryDirectory(prefix="slm-", dir="/tmp") as directory:
        path = Path(directory) / "collector.sock"

        listener = create_listener(path)
        try:
            mode = path.lstat().st_mode
            assert stat.S_ISSOCK(mode)
            assert stat.S_IMODE(mode) == 0o660
        finally:
            listener.close()
            path.unlink()


def test_remove_stale_socket_removes_only_a_unix_socket():
    with tempfile.TemporaryDirectory(prefix="slm-", dir="/tmp") as directory:
        path = Path(directory) / "collector.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))

        remove_stale_socket(path)

        assert not path.exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_remove_stale_socket_refuses_non_socket_paths(tmp_path, kind):
    path = tmp_path / "collector.sock"
    target = tmp_path / "target"
    target.write_text("do not delete")
    if kind == "file":
        path.write_text("do not delete")
    else:
        path.symlink_to(target)

    with pytest.raises(RuntimeError, match="refusing to replace non-socket"):
        remove_stale_socket(path)

    assert path.exists()
    assert target.read_text() == "do not delete"
