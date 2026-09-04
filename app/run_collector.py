"""Start the privileged collector on a safely recreated Unix socket."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import uvicorn


def remove_stale_socket(socket_path: Path) -> None:
    """Remove only a pre-existing Unix socket, including safe symlink handling."""

    try:
        mode = socket_path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"refusing to replace non-socket path: {socket_path}")
    socket_path.unlink()


def main() -> None:
    socket_path = Path(os.getenv("COLLECTOR_SOCKET", "/run/ssd-life/collector.sock"))
    if not socket_path.is_absolute():
        raise RuntimeError("COLLECTOR_SOCKET must be an absolute path")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.chmod(0o755)
    remove_stale_socket(socket_path)

    # Uvicorn creates the socket as root:ssd-life in the container. 0660 lets
    # the unprivileged web process connect without making the internal API
    # available to unrelated users that can traverse the runtime directory.
    previous_umask = os.umask(0o117)
    try:
        uvicorn.run(
            "app.collector_api:app",
            uds=str(socket_path),
            access_log=False,
        )
    finally:
        os.umask(previous_umask)
        try:
            mode = socket_path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(mode):
                socket_path.unlink()


if __name__ == "__main__":
    main()
