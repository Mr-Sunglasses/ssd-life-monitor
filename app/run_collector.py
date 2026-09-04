"""Start the privileged collector on a safely recreated Unix socket."""

from __future__ import annotations

import os
import socket
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


def create_listener(socket_path: Path) -> socket.socket:
    """Bind a Unix listener without letting the ASGI server widen its mode."""

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        socket_path.chmod(0o660)
    except BaseException:
        listener.close()
        remove_stale_socket(socket_path)
        raise
    return listener


def main() -> None:
    socket_path = Path(os.getenv("COLLECTOR_SOCKET", "/run/ssd-life/collector.sock"))
    if not socket_path.is_absolute():
        raise RuntimeError("COLLECTOR_SOCKET must be an absolute path")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.chmod(0o755)
    remove_stale_socket(socket_path)

    # Binding the socket ourselves is intentional: Uvicorn's UDS path forces
    # mode 0666 after bind. Passing the descriptor preserves 0660 so only root
    # and the shared ssd-life group can reach the privileged internal API.
    listener = create_listener(socket_path)
    try:
        uvicorn.run(
            "app.collector_api:app",
            fd=listener.fileno(),
            access_log=False,
        )
    finally:
        listener.close()
        try:
            mode = socket_path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(mode):
                socket_path.unlink()


if __name__ == "__main__":
    main()
