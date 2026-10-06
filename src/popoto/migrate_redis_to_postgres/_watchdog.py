"""Supervisor for the migration's throwaway ``redis-server`` (#756).

Run by the migration tool as a separate process, by file path, before the
throwaway server exists::

    python -I _watchdog.py <tool pid>

It deliberately imports nothing but the standard library -- not popoto, not
redis-py -- so it can never open a connection to anything.

The tool talks to it over a pipe, one JSON object per line:

* ``{"op": "watch", "path": p}``: remove directory ``p`` when the run ends;
* ``{"op": "spawn", "argv": [...]}``: start ``argv`` as a child of this
  process and answer ``{"pid": n}``;
* ``{"op": "stop", "pid": n}``: stop that child and answer ``{"ok": true}``.

Whenever the tool goes away, for any reason, the supervisor stops every child
it spawned and removes every watched directory, then exits. The tool going
away is seen two independent ways: the command pipe reaches end of file (the
kernel closes it when the tool exits, ``SIGKILL`` included), and this
process's parent pid stops being the tool's (it is re-parented). A normal end
of run is the same event: the tool closes the pipe.

It runs in its own session, so the ``SIGHUP`` an SSH drop sends and the
``SIGINT`` of a Ctrl-C reach neither it nor the server it spawned; it ignores
both anyway, and treats ``SIGTERM`` as "clean up now".
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
from typing import Any

POLL_SECONDS = 0.2
STOP_SECONDS = 10.0


class _Supervisor:
    def __init__(self, tool_pid: int) -> None:
        self.tool_pid = tool_pid
        self.children: dict[int, subprocess.Popen[bytes]] = {}
        self.paths: list[str] = []
        self.buffer = b""

    def tool_alive(self) -> bool:
        return os.getppid() == self.tool_pid

    def reply(self, obj: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def handle(self, message: dict[str, Any]) -> None:
        op = message.get("op")
        if op == "watch":
            self.paths.append(str(message["path"]))
            self.reply({"ok": True})
        elif op == "spawn":
            child = subprocess.Popen(
                [str(a) for a in message["argv"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            self.children[child.pid] = child
            self.reply({"pid": child.pid})
        elif op == "stop":
            stopping = self.children.pop(int(message["pid"]), None)
            if stopping is not None:
                _stop(stopping)
            self.reply({"ok": True})
        else:
            self.reply({"error": f"unknown op {op!r}"})

    def reap(self) -> None:
        for child in self.children.values():
            child.poll()

    def cleanup(self) -> None:
        for child in list(self.children.values()):
            _stop(child)
        self.children.clear()
        for path in self.paths:
            shutil.rmtree(path, ignore_errors=True)

    def run(self) -> None:
        stdin = sys.stdin.buffer.fileno()
        self.reply({"ready": True})
        while self.tool_alive():
            readable, _, _ = select.select([stdin], [], [], POLL_SECONDS)
            self.reap()
            if not readable:
                continue
            chunk = os.read(stdin, 65536)
            if not chunk:
                return  # end of file: the tool closed the pipe, or died
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.strip():
                    try:
                        self.handle(json.loads(line))
                    except BrokenPipeError:
                        return
                    except Exception as exc:  # answer, never die mid-run
                        try:
                            self.reply({"error": repr(exc)})
                        except BrokenPipeError:
                            return


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=STOP_SECONDS)


def main(argv: list[str]) -> int:
    supervisor = _Supervisor(int(argv[1]))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def _terminate(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)
    try:
        supervisor.run()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        supervisor.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
