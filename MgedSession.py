"""A general-purpose Python interface for driving a BRL-CAD MGED subprocess.

MGED is launched as a child process with its stdin/stdout connected to pipes.
Because stdin is redirected, MGED runs non-interactively (it prints no `mged>`
prompt) and simply executes whatever commands are piped to it. It therefore
emits nothing to indicate when a command has finished. To get a reliable
request/response model we append `puts "<sentinel>"` after every command: a
background reader thread accumulates stdout lines until it sees the sentinel,
then hands the collected output to the matching MgedCommand future (FIFO order).

The interface is designed to serve two kinds of consumer from one API:

* **Headless / data-science scripts** call ``future.result(timeout)``, which
  blocks until the command completes -- convenient and correct off the main
  thread.
* **GUI hosts such as Blender** must never block their single UI thread, so they
  either poll ``future.is_done`` from a timer or register
  ``future.add_done_callback(cb)`` / pass ``callback=`` to ``send_command``.
  ``send_command`` itself is a non-blocking pipe write and is safe to call from
  the UI thread.

Concurrency is handled with a single background reader thread (I/O-bound work
that releases the GIL); no multiprocessing is needed since MGED is already its
own OS process.

Logging to a file and/or the console can be toggled on and off at runtime and
streams each command and output line as the interaction progresses.
"""

import os
import queue
import shlex
import shutil
import subprocess
import threading
import time


class MgedException(Exception):
    """Raised for errors communicating with MGED."""
    pass


def parse_attach_args(args_str):
    """Split an mged option string, pulling out any ``-a <dm>`` display manager.

    Returns ``(options, dm)`` where ``options`` is the remaining argument list
    and ``dm`` is the display-manager type (or None). Attaching via
    :meth:`MgedSession.attach` after launch is more reliable than passing
    ``-a`` on the command line when stdin is piped, so callers typically feed
    ``dm`` to ``launch(attach=...)`` and ``options`` to the constructor.
    """
    args = shlex.split(args_str or "")
    dm = None
    if "-a" in args:
        idx = args.index("-a")
        if idx + 1 < len(args):
            dm = args[idx + 1]
            del args[idx:idx + 2]
        else:
            del args[idx]  # trailing "-a" with no value
    return args, dm


def _extract_brace_block(text, key):
    """Return the content inside the ``{...}`` that follows a standalone ``key``.

    e.g. for ``... V { { 0 0 0 } { 1 1 1 } } F {...}`` and key ``"V"`` returns
    ``" { 0 0 0 } { 1 1 1 } "``. Uses balanced-brace matching, so nested groups
    and the trailing ``}}`` are handled. Returns None if the key is absent.
    """
    idx = text.find(key)
    while idx != -1:
        before = text[idx - 1] if idx > 0 else " "
        after = text[idx + len(key)] if idx + len(key) < len(text) else " "
        if before in " \t{" and after in " \t{":
            break
        idx = text.find(key, idx + 1)
    if idx == -1:
        return None
    brace = text.find("{", idx)
    if brace == -1:
        return None
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1:i]
    return None


def _brace_groups(block):
    """Yield the content of each top-level ``{...}`` group within ``block``."""
    groups = []
    depth = 0
    start = None
    for i, c in enumerate(block):
        if c == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(block[start:i])
    return groups


def parse_search_names(text):
    """Parse MGED 'search'/'ls' output into a de-duplicated list of leaf names.

    'search . -type bot' prints one path/name per line; 'ls' prints several
    whitespace-separated names per line. Both are handled: each whitespace token
    is reduced to its leaf (after the last '/') and de-duplicated in order.
    """
    if not text:
        return []
    names = []
    seen = set()
    for line in text.splitlines():
        for token in line.split():
            leaf = token.rsplit("/", 1)[-1]
            if leaf and leaf not in seen:
                seen.add(leaf)
                names.append(leaf)
    return names


def parse_ls_l(text):
    """Parse 'ls -l' output into {name: object_type}.

    Each row is '<name> <obj_type> <major> <minor> <length>', whitespace
    separated. Names may contain spaces, so the last four tokens are the type
    fields and everything before them is the name.
    """
    types = {}
    if not text:
        return types
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 5:
            continue
        name = " ".join(tokens[:-4])
        types[name] = tokens[-4]
    return types


def _classify(token):
    """Map a tops/tree token to (name, kind) by its suffix marker.

    '<name>/R' -> region, '<name>/' -> comb, otherwise primitive.
    """
    if token.endswith("/R"):
        return token[:-2], "region"
    if token.endswith("/"):
        return token[:-1], "comb"
    return token, "primitive"


def parse_tops(text):
    """Parse 'tops' output into a list of (name, kind) for top-level objects."""
    if not text:
        return []
    items = []
    for line in text.splitlines():
        for token in line.split():
            items.append(_classify(token))
    return items


def parse_tree(text):
    """Parse 'tree <obj>' output into [(depth, op, name, kind), ...].

    Lines are tab-indented (depth = number of leading tabs); every non-root line
    begins with a boolean operator (u union, - subtraction, + intersection).
    Names carry the same suffix markers as tops ('/R' region, '/' comb).
    """
    if not text:
        return []
    nodes = []
    for line in text.splitlines():
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip("\t"))
        body = line.strip()
        op = ""
        if depth > 0 and body[:1] in "u-+" and (len(body) == 1 or body[1] == " "):
            op = body[0]
            body = body[1:].strip()
        name, kind = _classify(body)
        nodes.append((depth, op, name, kind))
    return nodes


def parse_attrs(text):
    """Parse ``attr get <obj>`` output into a {key: value} dict.

    MGED prints attributes as ``key {value} key {value} ...`` (values are always
    brace-wrapped, may be empty or contain spaces). Returns {} for empty/None.
    """
    attrs = {}
    if not text:
        return attrs
    i = 0
    n = len(text)
    while i < n:
        # Skip whitespace, then read a key up to the next space before '{'.
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        brace = text.find("{", i)
        if brace == -1:
            break
        key = text[i:brace].strip()
        # Balanced-brace match for the value (values rarely nest, but be safe).
        depth = 0
        j = brace
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= n:
            break
        if key:
            attrs[key] = text[brace + 1:j]
        i = j + 1
    return attrs


def parse_bot_geometry(get_output):
    """Parse vertices and faces out of MGED ``get <name>`` output for a BOT.

    MGED prints a BOT as ``bot mode ... V { {x y z} ... } F { {a b c} ... } ...``.
    Returns ``(verts, faces)`` where verts are ``(x, y, z)`` float tuples and
    faces are ``(a, b, c)`` int tuples. Missing/blank V or F yields an empty
    list for that part (so a non-BOT object returns ``([], [])``).
    """
    if not get_output:
        return [], []
    verts = []
    vblock = _extract_brace_block(get_output, "V")
    if vblock is not None:
        for g in _brace_groups(vblock):
            n = g.split()
            if len(n) >= 3:
                verts.append((float(n[0]), float(n[1]), float(n[2])))
    faces = []
    fblock = _extract_brace_block(get_output, "F")
    if fblock is not None:
        for g in _brace_groups(fblock):
            n = g.split()
            if len(n) >= 3:
                faces.append((int(n[0]), int(n[1]), int(n[2])))
    return verts, faces


class MgedCommand:
    """A handle (future) for a single command sent to MGED."""

    def __init__(self, text):
        self.text = text
        self._result = None
        self._completed = threading.Event()
        self._callbacks = []
        self._lock = threading.Lock()

    @property
    def is_done(self):
        return self._completed.is_set()

    def result(self, timeout=None):
        """Block until the command completes and return its output.

        Returns the command's output as a string, or None if the wait times out
        or the session ended before the command completed.
        """
        if self._completed.wait(timeout):
            return self._result
        return None

    def add_done_callback(self, callback):
        """Register ``callback(self)`` to run when the command completes.

        If the command is already done, the callback runs immediately on the
        calling thread; otherwise it runs on the reader thread. Callbacks must
        therefore be thread-safe and must not touch thread-confined GUI APIs
        directly (e.g. for Blender, push to a queue drained from a timer).
        """
        with self._lock:
            if not self._completed.is_set():
                self._callbacks.append(callback)
                return
        callback(self)

    def _set_result(self, output):
        with self._lock:
            self._result = output
            callbacks = self._callbacks
            self._callbacks = []
            self._completed.set()
        for cb in callbacks:
            try:
                cb(self)
            except Exception as e:
                print(f"MgedCommand callback error: {e}")


class _Logger:
    """Thread-safe, runtime-toggleable logger that streams to a file/console."""

    def __init__(self):
        self._lock = threading.Lock()
        self._handle = None
        self._path = None
        self.log_to_console = False
        self.log_commands = True
        self.log_output = True

    def enable(self, path, *, log_commands=True, log_output=True, log_to_console=None):
        with self._lock:
            self.log_commands = log_commands
            self.log_output = log_output
            if log_to_console is not None:
                self.log_to_console = log_to_console
            if path:
                if self._handle is not None:
                    try:
                        self._handle.close()
                    except Exception:
                        pass
                # Append mode + per-write flush so the log streams live.
                self._handle = open(path, "a", encoding="utf-8")
                self._path = path

    def disable(self):
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:
                    pass
            self._handle = None
            self._path = None
            self.log_to_console = False

    def _write(self, message):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.write(line + "\n")
                    self._handle.flush()
                except Exception as e:
                    print(f"MgedSession log error: {e}")
            if self.log_to_console:
                print(line)

    def command(self, text, is_internal=False):
        if self.log_commands:
            label = "internal" if is_internal else "user"
            self._write(f"-> MGED ({label}): {text}")

    def output(self, line):
        if self.log_output:
            self._write(f"MGED -> : {line}")

    def event(self, message):
        self._write(message)


class MgedSession:
    """Manages a single background MGED process with request/response I/O."""

    # Marker printed after each command so the reader knows where its output
    # ends. Made distinctive to avoid colliding with real command output.
    SENTINEL = "MGED_CMD_DONE 919"

    def __init__(self, mged_path, command_line_options=None, *,
                 log_file=None, log_to_console=False,
                 on_output=None, buffer_output=False):
        self.mged_path = mged_path
        self.command_line_options = list(command_line_options or [])
        self.process = None
        self.thread = None
        self._pending_futures = queue.Queue()
        # Serializes "enqueue future + write to stdin" so concurrent senders
        # cannot interleave and desync the FIFO correlation with output.
        self._io_lock = threading.Lock()
        # Live output tee: optional per-line callback and optional buffer queue.
        self._on_output = on_output
        self._output_queue = queue.Queue() if buffer_output else None
        self._logger = _Logger()
        if log_file or log_to_console:
            self._logger.enable(log_file, log_to_console=log_to_console)

    @property
    def running(self):
        return bool(self.process and self.process.poll() is None)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown()
        return False

    # -- background reader -------------------------------------------------

    def _manager_thread(self):
        """Read stdout, split on the sentinel, and fulfill futures FIFO."""
        buffer = []
        try:
            for line in iter(self.process.stdout.readline, ''):
                clean_line = line.rstrip('\n')
                if clean_line.startswith(self.SENTINEL):
                    output = "\n".join(buffer)
                    buffer = []
                    try:
                        future = self._pending_futures.get_nowait()
                    except queue.Empty:
                        # Output with no waiting command (e.g. startup banner).
                        continue
                    future._set_result(output)
                else:
                    self._tee_line(clean_line)
                    buffer.append(clean_line)
        finally:
            self._release_pending_futures()

    def _tee_line(self, line):
        """Forward one raw output line to the log, callback, and queue."""
        self._logger.output(line)
        callback = self._on_output
        if callback is not None:
            try:
                callback(line)
            except Exception as e:
                print(f"MgedSession on_output error: {e}")
        if self._output_queue is not None:
            self._output_queue.put(line)

    def _release_pending_futures(self):
        """Wake any callers still waiting; the session has ended."""
        try:
            self.process.stdout.close()
        except Exception:
            pass
        while True:
            try:
                future = self._pending_futures.get_nowait()
            except queue.Empty:
                break
            if not future.is_done:
                future._set_result(None)

    # -- lifecycle ---------------------------------------------------------

    def launch(self, db_path=None, *, attach=None, wait=True, handshake_timeout=30):
        """Start MGED and the reader thread.

        ``attach`` issues ``attach <dm>`` after startup (the reliable way to
        attach a display manager when stdin is piped -- prefer this over putting
        ``-a <dm>`` in ``command_line_options``). With ``wait=True`` (default)
        this blocks until MGED answers a handshake and raises on timeout; with
        ``wait=False`` it returns the handshake future immediately so a GUI host
        can poll ``is_done`` for readiness without stalling. Returns the
        handshake :class:`MgedCommand`.
        """
        if self.running:
            raise MgedException("MGED is already running.")

        commandline = [self.mged_path] + self.command_line_options
        if db_path:
            commandline.append(db_path)

        self._logger.event(f"Launching: {commandline}")
        try:
            self.process = subprocess.Popen(
                commandline,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        except OSError as e:
            raise MgedException(f"MGED failed to launch: {e}") from e

        self.thread = threading.Thread(target=self._manager_thread, daemon=True)
        self.thread.start()

        # The handshake also absorbs MGED's startup banner and confirms it is
        # alive and responding before we hand control back to the caller.
        handshake = self.send_command('puts "hello"', is_internal=True)
        if attach:
            self.attach(attach)
        if wait:
            if handshake.result(timeout=handshake_timeout) is None:
                self.shutdown()
                raise MgedException("MGED failed to respond to handshake.")
        return handshake

    def attach(self, dm_type):
        """Attach a display manager (e.g. 'wgl', 'ogl', 'X')."""
        return self.send_command(f"attach {dm_type}", is_internal=True)

    def send_command(self, command_text, *, callback=None, is_internal=False):
        """Send a command and return an MgedCommand future for its output.

        Non-blocking: returns as soon as the command is written. ``callback`` is
        a shortcut for ``future.add_done_callback``. ``is_internal`` only affects
        the log label (internal vs user).
        """
        with self._io_lock:
            if not self.running:
                raise MgedException("MGED process is not running.")
            future = MgedCommand(command_text)
            if callback is not None:
                future.add_done_callback(callback)
            self._pending_futures.put(future)
            self._logger.command(command_text, is_internal=is_internal)
            # The trailing sentinel marks the end of this command's output.
            payload = f'{command_text}\n'
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise MgedException(f"Failed to send command to MGED: {e}") from e
        return future

    def list_bots(self, timeout=10):
        """Return the names of all BOT objects in the open database.

        Uses ``search . -type bot`` and reduces results to de-duplicated leaf
        names. Returns [] if the query is empty or times out.
        """
        result = self.send_command("search . -type bot").result(timeout=timeout)
        return parse_search_names(result)

    def tops(self, timeout=10):
        """Return top-level objects as a list of (name, kind)."""
        return parse_tops(self.send_command("tops").result(timeout=timeout))

    def tree(self, name, timeout=10):
        """Return the subtree of `name` as [(depth, op, name, kind), ...]."""
        result = self.send_command(f"tree {shlex.quote(name)}").result(timeout=timeout)
        return parse_tree(result)

    def who(self, timeout=10):
        """Return the names of objects currently displayed in the MGED view."""
        return parse_search_names(self.send_command("who").result(timeout=timeout))

    def object_types(self, timeout=10):
        """Return {name: object_type} for every object in the database ('ls -l')."""
        return parse_ls_l(self.send_command("ls -l").result(timeout=timeout))

    def hierarchy(self, timeout=10):
        """Return the whole database hierarchy from tops, flattened depth-first.

        Each entry is (depth, op, name, kind), where kind is 'comb', 'region',
        'bot' (a BOT primitive), or 'primitive' (any other solid). A leaf top
        with no subtree yields a single depth-0 node.
        """
        nodes = []
        for name, kind in self.tops(timeout=timeout):
            sub = self.tree(name, timeout=timeout)
            nodes.extend(sub if sub else [(0, "", name, kind)])
        # Distinguish BOT primitives from other solids (the tree output can't);
        # 'ls -l' gives the actual object type for every name.
        types = self.object_types(timeout=timeout)
        return [(d, op, n, "bot" if (k == "primitive" and types.get(n) == "bot") else k)
                for (d, op, n, k) in nodes]

    def get_bot(self, name, timeout=10):
        """Read a BOT's geometry from MGED. Returns (verts, faces) or None.

        Sends ``get <name>`` and parses the result. Returns None if the command
        times out or the object does not exist; returns ``([], [])`` if the
        object exists but is not a BOT (no V/F blocks).
        """
        result = self.send_command(f"get {shlex.quote(name)}").result(timeout=timeout)
        if result is None:
            return None
        if "no such" in result.lower() or "not found" in result.lower():
            return None
        return parse_bot_geometry(result)

    def attr_set(self, name, key, value, *, callback=None):
        """Set a single attribute on an object (``attr set <name> <key> <value>``).

        Non-blocking; returns the command future. Values are quoted so spaces are
        safe.
        """
        return self.send_command(
            f"attr set {shlex.quote(name)} {shlex.quote(str(key))} {shlex.quote(str(value))}",
            callback=callback, is_internal=True)

    def attr_get(self, name, timeout=10):
        """Return all attributes of an object as a {key: value} dict, or {}.

        Returns {} if the object has no attributes, does not exist, or the query
        times out.
        """
        result = self.send_command(f"attr get {shlex.quote(name)}").result(timeout=timeout)
        if result is None:
            return {}
        if "no such" in result.lower() or "not found" in result.lower():
            return {}
        return parse_attrs(result)

    def shutdown(self, timeout=3):
        """Ask MGED to quit, escalating to terminate/kill, then join the reader.

        Resets internal state so the same object can be relaunched afterwards.
        """
        if self.process and self.process.poll() is None:
            self._logger.event("Shutting down MGED.")
            try:
                with self._io_lock:
                    self.process.stdin.write("q\n")
                    self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        if self.thread:
            self.thread.join(timeout=timeout)
        # Close the pipes (the reader closes stdout on EOF, but stdin is ours)
        # to avoid leaking file handles across relaunches.
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        # Reset for potential reuse.
        self.process = None
        self.thread = None
        self._pending_futures = queue.Queue()
        if self._output_queue is not None:
            self._output_queue = queue.Queue()

    # -- logging (toggleable at runtime) -----------------------------------

    def enable_logging(self, path, *, log_commands=True, log_output=True, log_to_console=None):
        """Start (or re-point) streaming logs to ``path``; works mid-session."""
        self._logger.enable(path, log_commands=log_commands,
                            log_output=log_output, log_to_console=log_to_console)

    def disable_logging(self):
        """Stop logging and close the log file."""
        self._logger.disable()

    # -- live output stream (optional) -------------------------------------

    def set_output_callback(self, callback):
        """Set a per-line callback ``callback(line)`` fired on the reader thread."""
        self._on_output = callback

    def has_output(self):
        """True if buffered output is available (requires buffer_output=True)."""
        return self._output_queue is not None and not self._output_queue.empty()

    def get_output(self):
        """Pop one buffered output line, or None if empty/disabled."""
        if self._output_queue is None:
            return None
        try:
            return self._output_queue.get_nowait()
        except queue.Empty:
            return None


def main():
    mged_exe = shutil.which('mged') or r"D:\BRL-CAD_7.43.0_win64\bin\mged.exe"
    if not os.path.exists(mged_exe):
        print(f"mged not found: {mged_exe}")
        return
    if not os.access(mged_exe, os.X_OK):
        print(f"mged not accessible: {mged_exe}")
        return

    print(f'using {mged_exe}')

    # `result()` blocks until each command completes; the context manager
    # guarantees shutdown even on error. Attach the display manager via attach=
    # rather than '-a wgl' in the options (more reliable with piped stdin).
    with MgedSession(mged_exe, ['-c'], log_to_console=True) as session:
        session.launch('d:/box.g', attach='wgl')

        for cmd_text in ['title', 'e asdf.r', 'tops']:
            cmd = session.send_command(cmd_text)
            print(f'{cmd_text!r} -> {cmd.result(timeout=10)!r}')


if __name__ == '__main__':
    main()
