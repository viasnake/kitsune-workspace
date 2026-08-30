"""Hold a managed process behind a durable PID-ownership handshake."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path


def _process_start_time(pid: int) -> str:
    content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing_parenthesis = content.rfind(")")
    fields = content[closing_parenthesis + 2 :].split()
    if closing_parenthesis < 0 or len(fields) <= 19:
        raise RuntimeError("cannot read process start time")
    return fields[19]


def _write_launch_record(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = json.dumps(
            {"pid": os.getpid(), "process_start_time": _process_start_time(os.getpid())},
            separators=(",", ":"),
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _wait_for_exec_gate(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            time.sleep(0.01)
            continue
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RuntimeError("exec gate is not an owned regular file")
            return
        finally:
            os.close(descriptor)


def main() -> None:
    if len(sys.argv) < 5 or sys.argv[3] != "--":
        raise SystemExit("usage: process_launcher RECORD GATE -- COMMAND [ARG ...]")
    launch_record = Path(sys.argv[1])
    exec_gate = Path(sys.argv[2])
    command = sys.argv[4:]
    if not command:
        raise SystemExit("target command is required")
    _write_launch_record(launch_record)
    _wait_for_exec_gate(exec_gate)
    os.execvpe(  # noqa: S606 - execute the validated Manifest command after ownership commit
        command[0], command, os.environ
    )


if __name__ == "__main__":
    main()
