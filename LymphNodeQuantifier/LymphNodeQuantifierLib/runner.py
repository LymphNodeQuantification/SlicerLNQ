"""QProcess wrapper that runs `lnq-segmenter predict --json-progress` and
re-emits its structured progress events as Qt signals.

Subprocess management is pure Qt — no Python `subprocess` module, no threads.
The QProcess plugs into Slicer's Qt event loop, so stderr arrives as
readyReadStandardError signals on the GUI thread. Cancel uses QProcess.kill,
which sends SIGKILL on POSIX and TerminateProcess on Windows — what the user
asked for ('cancel with KILL on the subprocess')."""
from __future__ import annotations

import json
import os
import sys

import qt
import slicer


PROGRESS_PREFIX = "lnq> "


def slicer_python_executable():
    """Path to Slicer's PythonSlicer binary. Subprocesses launched with this
    inherit Slicer's site-packages, so `import lnq_segmenter` works without
    explicit PYTHONPATH wiring."""
    name = "PythonSlicer.exe" if sys.platform == "win32" else "PythonSlicer"
    return os.path.join(slicer.app.slicerHome, "bin", name)


class SegmenterRunner(qt.QObject):
    """Wraps a single `lnq-segmenter predict` run.

    Signals:
      started()                    process spawned
      progressEvent(dict)          one structured event from --json-progress
      logLine(str)                 non-event stderr/stdout passthrough
      finished(int, str)           exit code + status ('ok' | 'cancelled' | 'crashed')

    Typical lifecycle:
      r = SegmenterRunner(parent)
      r.progressEvent.connect(...); r.logLine.connect(...); r.finished.connect(...)
      r.start_predict('inguinal-v1', '/tmp/in.nrrd', '/tmp/out.nrrd')
      # ...later, on cancel button:
      r.cancel()
    """

    started = qt.Signal()
    # progressEvent carries a JSON-serialized dict because PythonQt's signal
    # binding can't route "PyQt_PyObject" — using str + json keeps the wire
    # format simple and slot-friendly.
    progressEvent = qt.Signal(str)
    logLine = qt.Signal(str)
    finished = qt.Signal(int, str)

    def __init__(self, parent=None):
        qt.QObject.__init__(self, parent)
        self._process = qt.QProcess(self)
        self._process.setProcessChannelMode(qt.QProcess.SeparateChannels)
        self._stderr_buf = ""
        self._stdout_buf = ""
        self._cancelled = False
        self._process.readyReadStandardError.connect(self._onStderr)
        self._process.readyReadStandardOutput.connect(self._onStdout)
        self._process.started.connect(self.started.emit)
        self._process.finished.connect(self._onFinished)
        self._process.errorOccurred.connect(self._onErrorOccurred)

    # ----- control -----

    def start_predict(self, model_spec, input_path, output_path,
                      device="cuda", folds=None, probability_output=None):
        """Spawn PythonSlicer -m lnq_segmenter predict … --json-progress."""
        args = [
            "-m", "lnq_segmenter", "predict", model_spec,
            "--input", input_path,
            "--output", output_path,
            "--device", device,
            "--json-progress",
        ]
        if folds:
            args.append("--folds")
            args.extend(str(f) for f in folds)
        if probability_output:
            args.extend(["--probability-output", probability_output])
        self._cancelled = False
        self._process.start(slicer_python_executable(), args)

    def start_download(self, model_spec):
        """Spawn the download subcommand (without running inference). Useful
        for a 'Pre-download weights' UI affordance."""
        args = ["-m", "lnq_segmenter", "download", model_spec, "--json-progress"]
        self._cancelled = False
        self._process.start(slicer_python_executable(), args)

    def cancel(self):
        """Send SIGKILL (POSIX) / TerminateProcess (Windows) to the subprocess.
        The finished signal will fire with status='cancelled'."""
        if self._process.state() != qt.QProcess.NotRunning:
            self._cancelled = True
            self._process.kill()

    def isRunning(self):
        return self._process.state() != qt.QProcess.NotRunning

    # ----- stream parsing -----

    def _onStderr(self):
        data = bytes(self._process.readAllStandardError().data())
        self._stderr_buf = self._dispatch_lines(
            self._stderr_buf + data.decode("utf-8", errors="replace"))

    def _onStdout(self):
        data = bytes(self._process.readAllStandardOutput().data())
        self._stdout_buf = self._dispatch_lines(
            self._stdout_buf + data.decode("utf-8", errors="replace"))

    def _dispatch_lines(self, buf):
        """Split `buf` on newlines, emit each complete line, return the
        trailing partial line for the next call."""
        while "\n" in buf:
            line, _, buf = buf.partition("\n")
            line = line.rstrip("\r")
            if not line:
                continue
            if line.startswith(PROGRESS_PREFIX):
                payload = line[len(PROGRESS_PREFIX):]
                # Verify it's JSON without deserializing; the slot does
                # json.loads itself so the dict stays a normal Python object
                # on the receiving side.
                try:
                    json.loads(payload)
                except ValueError:
                    self.logLine.emit(line)
                    continue
                self.progressEvent.emit(payload)
            else:
                self.logLine.emit(line)
        return buf

    # ----- termination -----

    def _onErrorOccurred(self, err):
        # FailedToStart fires *before* finished. Surface it so the UI doesn't
        # hang waiting on a process that never started.
        if err == qt.QProcess.FailedToStart:
            self.logLine.emit(
                f"[runner] failed to start "
                f"{slicer_python_executable()}: {self._process.errorString()}")
            self.finished.emit(-1, "crashed")

    def _onFinished(self, exit_code, exit_status=None):
        # Qt5 emits the (int) overload of QProcess.finished by default through
        # PythonQt, so exit_status arrives as None. Fall back to the live
        # exitStatus() reading so crash detection still works.
        if exit_status is None:
            exit_status = self._process.exitStatus()
        # Drain remaining buffered partials in case the process ended without
        # a final newline.
        for buf_name in ("_stderr_buf", "_stdout_buf"):
            tail = getattr(self, buf_name)
            if tail:
                setattr(self, buf_name, "")
                self._dispatch_lines(tail + "\n")
        if self._cancelled:
            status = "cancelled"
        elif exit_status == qt.QProcess.CrashExit:
            status = "crashed"
        elif exit_code == 0:
            status = "ok"
        else:
            status = "crashed"
        self.finished.emit(int(exit_code), status)
