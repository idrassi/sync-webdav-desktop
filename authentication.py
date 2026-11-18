"""Browser-only Sync authentication; raw CLI output never reaches GUI logs."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
from typing import Callable


class AuthenticationError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class CredentialCapture:
    """Parse only the one-time local credential block, discarding all other text."""
    def __init__(self, on_credential: Callable[[str], None] | None = None):
        self.on_credential = on_credential
        self.pending = ""
        self.password: str | None = None
        self.username: str | None = None
        self.saw_browser = False

    def feed(self, text: str, final: bool = False) -> None:
        self.pending += text
        if len(self.pending) > 65536:
            self.pending = self.pending[-65536:]
        lines = self.pending.split("\n")
        self.pending = "" if final else lines.pop()
        for line in lines:
            line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
            # Exact documented credential format; never interpret arbitrary
            # tokens or account password prompts as the local password.
            user = re.fullmatch(r"Username:\s*(\S+)", line, flags=re.IGNORECASE)
            password = re.fullmatch(r"Password:\s*(\S+)", line, flags=re.IGNORECASE)
            if user:
                self.username = user.group(1)
            if password:
                # Keep the documented username-first handoff; an unpaired
                # Password line might be an unrelated prompt. Consume each pair.
                if self.username == "sync" and password.group(1) != self.password:
                    self.password = password.group(1)
                    if self.on_credential:
                        self.on_credential(self.password)
                self.username = None
            if "browser" in line.lower() or "https://" in line:
                self.saw_browser = True

    def clear(self) -> None:
        self.password = None
        self.username = None
        self.pending = ""


def terminate_group(child: subprocess.Popen) -> None:
    # The group leader may have exited while its children still own pipes or
    # modify files. Signal our private session independently of leader state.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        child.poll()  # Reap the leader so its zombie does not keep the group.
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            child.wait()
            return
        time.sleep(0.05)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def browser_login(binary: Path, profile: str, cancel: threading.Event,
                  progress: Callable[[str], None], reset: bool = False,
                  timeout: int = 600,
                  on_credential: Callable[[str], None] | None = None) -> str | None:
    """Return only a newly issued LOCAL password; never log/copy raw output.

    The Sync program itself opens the normal browser and saves its own session.
    stdout/stderr are private anonymous pipes, used solely for the documented
    one-time WebDAV credential handoff. No account password is requested here.
    """
    args = [str(binary), "credentials", "--reset", "--profile", profile] if reset else [
        str(binary), "login", "--profile", profile]
    env = {key: val for key, val in os.environ.items()
           if key not in {"WEBDAV_USERNAME", "WEBDAV_PASSWORD", "SYNC_WEBDAV_DIR"}}
    env["LC_ALL"] = "C"
    progress("Complete sign-in in the browser opened by Sync…")
    capture = CredentialCapture(on_credential)
    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True, env=env)
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                raise AuthenticationError("Browser sign-in timed out. Try again and finish sign-in within ten minutes.")
            ready = selector.select(timeout=0.2)
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if chunk:
                    capture.feed(chunk.decode("utf-8", "replace"))
                else:
                    selector.unregister(key.fileobj)
            if child.poll() is not None and not selector.get_map():
                break
            if cancel.is_set():
                raise Cancelled("Sign-in cancelled. Services remain stopped until you reconnect.")
        capture.feed("", final=True)
        if child.returncode:
            raise AuthenticationError("Sync browser sign-in failed or was declined. Retry Connect; no credentials were logged.")
        return capture.password
    finally:
        terminate_group(child)
        selector.close()
        child.stdout.close()
        capture.clear()
