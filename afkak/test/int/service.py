# -*- coding: utf-8 -*-
# Copyright (C) 2015 Cyan, Inc.

import logging
import os
import select
import subprocess
import threading
from datetime import datetime, timedelta
from pprint import pformat

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

__all__ = [
    "ExternalService",
    "SpawnedService",
]

_STOP_TIMEOUT = timedelta(seconds=60)


class ExternalService(object):  # pragma: no cover
    def __init__(self, host, port):
        log.info("Using already running service at %s:%d", host, port)
        self.host = host
        self.port = port

    def open(self):
        pass

    def close(self):
        pass


class SpawnedService(object):
    _thread = None

    def __init__(self, name, log, args, env, start_re):
        self._name = name
        self._log = log
        self._args = args
        self._env = env
        self._start_re = start_re
        self._started = threading.Event()
        self._should_die = threading.Event()

    def _run(self):
        self._log.debug("Starting args=%r env=%s", self._args, pformat(self._env))
        proc = subprocess.Popen(
            self._args,
            env=self._env,
            bufsize=1,  # Line buffered.
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        stop_deadline = None
        closed = False
        killed = False
        while True:
            if closed:
                proc.wait()
            else:
                (rds, _, _) = select.select([proc.stdout], [], [], 0.01)

                if proc.stdout in rds:
                    line = proc.stdout.readline().decode("utf-8", "backslashescape")
                    if line:
                        self._log.debug(line.rstrip("\r\n"))
                        if self._start_re.search(line):
                            self._log.debug("Marking subprocess started")
                            self._started.set()
                    else:
                        proc.stdout.close()
                        closed = True
                        self._log.debug("Subprocess stdout closed")
                    continue  # Loop around to read any more output.

            returncode = proc.poll()
            if stop_deadline is None:
                if self._should_die.is_set():
                    self._log.info("Terminating subprocess")
                    proc.terminate()
                    stop_deadline = datetime.utcnow() + _STOP_TIMEOUT

                if returncode is not None:
                    self._log.critical(
                        "Subprocess with args=%r, env=%r has died unexpectedly: returncode=%d",
                        self._args,
                        self._env,
                        returncode,
                    )
                    raise Exception("Subprocess died unexpectedly with status {!r}".format(returncode))
            else:
                if returncode is not None:
                    self._log.info("Subprocess exited: returncode=%d", returncode)
                    break

                if not killed and datetime.utcnow() > stop_deadline:
                    log.error(
                        "Child process %r failed to exit within %d. Resorting to kill.",
                        proc,
                        _STOP_TIMEOUT,
                    )
                    proc.kill()
                    killed = True

    def start(self, timeout=10):
        assert self._thread is None

        def run():
            try:
                self._run()
            except BaseException:
                self._log.exception("Unhandled exception in fixture thread %r", self._thread)
                os.kill(os.getpid(), 9)

        self._thread = threading.Thread(target=run, name=self._name)
        self._thread.start()
        started = self._started.wait(timeout)
        if not started:
            self._log.error("Waiting for start timed out after %.2fs: terminating.", timeout)
            self.stop()
            raise Exception("start() timed out after {:.2f}s".format(timeout))

    def stop(self):
        assert self._thread is not None
        self._should_die.set()
        self._thread.join()
