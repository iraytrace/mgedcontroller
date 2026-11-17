"""Unit tests for MgedSession that do NOT require a live mged process.

These exercise the pure-Python machinery -- the MgedCommand future/callbacks,
the toggleable logger, the live output tee, and the reader thread's
sentinel-based correlation -- by driving the reader with a fake subprocess.

Run from the repository root with:

    python -m unittest discover -s mged_session/tests -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

# Import the standalone source directly, matching its authoritative repository.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import MgedSession as ms  # noqa: E402


# --- Fakes used to drive the reader thread without launching mged ---------

class _FakeStream:
    """Minimal stand-in for a subprocess pipe (stdout or stdin)."""

    def __init__(self, lines=None):
        self._lines = list(lines or [])
        self.written = []
        self.closed = False

    def readline(self):
        return self._lines.pop(0) if self._lines else ''  # '' == EOF

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProcess:
    """Minimal stand-in for subprocess.Popen. poll()==None means 'running'."""

    def __init__(self, out_lines=None, returncode=None):
        self.stdout = _FakeStream(out_lines)
        self.stdin = _FakeStream()
        self._returncode = returncode

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9


class TestMgedCommand(unittest.TestCase):
    def test_pending_then_result(self):
        c = ms.MgedCommand("title")
        self.assertFalse(c.is_done)
        self.assertIsNone(c.result(timeout=0))
        c._set_result("My Box")
        self.assertTrue(c.is_done)
        self.assertEqual(c.result(), "My Box")

    def test_callback_fires_on_completion(self):
        c = ms.MgedCommand("title")
        fired = []
        c.add_done_callback(lambda f: fired.append(f.text))
        self.assertEqual(fired, [])
        c._set_result("ok")
        self.assertEqual(fired, ["title"])

    def test_callback_added_after_completion_runs_immediately(self):
        c = ms.MgedCommand("title")
        c._set_result("ok")
        fired = []
        c.add_done_callback(lambda f: fired.append(f.result()))
        self.assertEqual(fired, ["ok"])

    def test_multiple_callbacks_in_order_and_isolated(self):
        c = ms.MgedCommand("x")
        order = []
        c.add_done_callback(lambda f: order.append(1))
        c.add_done_callback(lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
        c.add_done_callback(lambda f: order.append(3))
        # The raising callback must not break the others; its error is reported
        # to stdout, which we swallow here to keep test output clean.
        with contextlib.redirect_stdout(io.StringIO()):
            c._set_result("done")
        self.assertEqual(order, [1, 3])


class TestLogger(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.gettempdir(), "mged_unit_test.log")
        self.path2 = self.path + "2"
        self._remove_files()
        # Registered first, so it runs LAST (after each logger's disable()).
        self.addCleanup(self._remove_files)

    def _remove_files(self):
        for p in (self.path, self.path2):
            if os.path.exists(p):
                os.remove(p)

    def _read(self, p):
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def _logger(self):
        log = ms._Logger()
        self.addCleanup(log.disable)  # always close the file handle
        return log

    def test_streams_live_with_labels(self):
        log = self._logger()
        log.enable(self.path)
        log.command("title", is_internal=False)
        log.command("attach wgl", is_internal=True)
        log.output("My Box")
        # Readable before close -> writes are flushed as they happen.
        text = self._read(self.path)
        self.assertIn("-> MGED (user): title", text)
        self.assertIn("-> MGED (internal): attach wgl", text)
        self.assertIn("MGED -> : My Box", text)

    def test_disable_stops_writes(self):
        log = self._logger()
        log.enable(self.path)
        log.output("before")
        snapshot = self._read(self.path)
        log.disable()
        log.output("after")
        self.assertEqual(self._read(self.path), snapshot)

    def test_repoint_to_new_path(self):
        log = self._logger()
        log.enable(self.path)
        log.event("first")
        log.enable(self.path2)
        log.event("second")
        self.assertIn("first", self._read(self.path))
        self.assertIn("second", self._read(self.path2))
        self.assertNotIn("second", self._read(self.path))

    def test_selective_suppression(self):
        log = self._logger()
        log.enable(self.path, log_commands=False, log_output=True)
        log.command("hidden", is_internal=False)
        log.output("shown")
        text = self._read(self.path)
        self.assertNotIn("hidden", text)
        self.assertIn("shown", text)

    def test_console_logging(self):
        log = self._logger()
        log.enable(None, log_to_console=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            log.event("hello console")
        self.assertIn("hello console", buf.getvalue())


class TestOutputTee(unittest.TestCase):
    def test_callback_and_buffer_queue(self):
        seen = []
        s = ms.MgedSession("mged", buffer_output=True, on_output=seen.append)
        s._tee_line("line one")
        s._tee_line("line two")
        self.assertEqual(seen, ["line one", "line two"])
        drained = []
        while s.has_output():
            drained.append(s.get_output())
        self.assertEqual(drained, ["line one", "line two"])
        self.assertIsNone(s.get_output())
        self.assertFalse(s.has_output())

    def test_buffer_disabled_by_default(self):
        s = ms.MgedSession("mged")
        s._tee_line("ignored")
        self.assertFalse(s.has_output())
        self.assertIsNone(s.get_output())

    def test_set_output_callback_swaps(self):
        a, b = [], []
        s = ms.MgedSession("mged", on_output=a.append)
        s._tee_line("to a")
        s.set_output_callback(b.append)
        s._tee_line("to b")
        self.assertEqual(a, ["to a"])
        self.assertEqual(b, ["to b"])


class TestReaderCorrelation(unittest.TestCase):
    """Drive _manager_thread synchronously with a fake stdout to EOF."""

    def test_fifo_correlation_and_tee(self):
        S = ms.MgedSession.SENTINEL
        lines = ["banner\n", f"{S}\n", "hello\n", f"{S}\n"]
        s = ms.MgedSession("mged", buffer_output=True)
        s.process = _FakeProcess(out_lines=lines)
        f1, f2 = ms.MgedCommand("c1"), ms.MgedCommand("c2")
        s._pending_futures.put(f1)
        s._pending_futures.put(f2)

        s._manager_thread()  # runs to EOF

        self.assertEqual(f1.result(timeout=0), "banner")
        self.assertEqual(f2.result(timeout=0), "hello")
        # Sentinel lines are filtered out of the tee; data lines are kept.
        drained = []
        while s.has_output():
            drained.append(s.get_output())
        self.assertEqual(drained, ["banner", "hello"])
        self.assertTrue(s.process.stdout.closed)

    def test_multiline_output_is_joined(self):
        S = ms.MgedSession.SENTINEL
        lines = ["a\n", "b\n", "c\n", f"{S}\n"]
        s = ms.MgedSession("mged")
        f = ms.MgedCommand("c")
        s.process = _FakeProcess(out_lines=lines)
        s._pending_futures.put(f)
        s._manager_thread()
        self.assertEqual(f.result(timeout=0), "a\nb\nc")

    def test_pipe_status_suffix_completes_command(self):
        S = ms.MgedSession.SENTINEL
        lines = ["command failed\n", f"{S} 920\n"]
        s = ms.MgedSession("mged")
        f = ms.MgedCommand("bad command")
        s.process = _FakeProcess(out_lines=lines)
        s._pending_futures.put(f)
        s._manager_thread()
        self.assertEqual(f.result(timeout=0), "command failed")

    def test_pending_futures_released_on_eof(self):
        lines = ["orphan\n"]  # no sentinel, then EOF
        s = ms.MgedSession("mged")
        f = ms.MgedCommand("c")
        s.process = _FakeProcess(out_lines=lines)
        s._pending_futures.put(f)
        s._manager_thread()
        self.assertTrue(f.is_done)
        self.assertIsNone(f.result(timeout=0))


class TestSendCommand(unittest.TestCase):
    def test_writes_command(self):
        s = ms.MgedSession("mged")
        s.process = _FakeProcess()  # poll()==None -> running
        fut = s.send_command("title")
        self.assertEqual(fut.text, "title")
        payload = "".join(s.process.stdin.written)
        self.assertEqual(payload, "title\n")
        self.assertEqual(s._pending_futures.qsize(), 1)

    def test_raises_when_not_running(self):
        s = ms.MgedSession("mged")  # no process
        with self.assertRaises(ms.MgedException):
            s.send_command("title")

    def test_callback_argument_registered(self):
        s = ms.MgedSession("mged")
        s.process = _FakeProcess()
        got = []
        fut = s.send_command("t", callback=got.append)
        fut._set_result("r")
        self.assertEqual(got, [fut])

    def test_running_property(self):
        s = ms.MgedSession("mged")
        self.assertFalse(s.running)
        s.process = _FakeProcess()
        self.assertTrue(s.running)
        s.process = _FakeProcess(returncode=0)  # exited
        self.assertFalse(s.running)


class TestParseAttachArgs(unittest.TestCase):
    def test_extracts_display_manager(self):
        self.assertEqual(ms.parse_attach_args("-c -a wgl"), (["-c"], "wgl"))

    def test_dm_first(self):
        self.assertEqual(ms.parse_attach_args("-a ogl -c"), (["-c"], "ogl"))

    def test_no_attach(self):
        self.assertEqual(ms.parse_attach_args("-c"), (["-c"], None))

    def test_empty(self):
        self.assertEqual(ms.parse_attach_args(""), ([], None))
        self.assertEqual(ms.parse_attach_args(None), ([], None))

    def test_trailing_attach_without_value(self):
        self.assertEqual(ms.parse_attach_args("-c -a"), (["-c"], None))


class TestParseSearchNames(unittest.TestCase):
    def test_one_per_line(self):
        self.assertEqual(ms.parse_search_names("Cube\nCylinder\ntri\n"),
                         ["Cube", "Cylinder", "tri"])

    def test_names_with_spaces_are_preserved(self):
        self.assertEqual(ms.parse_search_names(
            "a really long name to test\nwith space.s\n"),
            ["a really long name to test", "with space.s"])

    def test_verbose_summary_is_ignored(self):
        self.assertEqual(ms.parse_search_names(
            "Cube\n[1] items found in search\n"), ["Cube"])

    def test_empty(self):
        self.assertEqual(ms.parse_search_names(""), [])
        self.assertEqual(ms.parse_search_names(None), [])


class TestParseSearchHierarchy(unittest.TestCase):
    SAMPLE = ("/u with space.s(tgc)\n"
              "/u prims.r(r)\n"
              "/u prims.r(r)/- tgc.s(tgc)\n"
              "/u prims.r(r)/- tgc.s(tgc)\n"
              "/u all.g(c)\n"
              "/u all.g(c)/u Cube.r(r)\n"
              "/u all.g(c)/u Cube.r(r)/- Cylinder(bot)\n"
              "/u a really long name to test(ell)\n"
              "[8] items found in search\n")

    def test_parses_order_depth_operator_type_and_path(self):
        nodes = ms.parse_search_hierarchy(self.SAMPLE)
        self.assertEqual(nodes[0], ms.HierarchyNode(
            path="/with space.s", depth=0, operator="",
            name="with space.s", object_type="tgc", kind="primitive"))
        self.assertEqual(nodes[2], ms.HierarchyNode(
            path="/prims.r/tgc.s", depth=1, operator="-",
            name="tgc.s", object_type="tgc", kind="primitive"))
        self.assertEqual(nodes[5], ms.HierarchyNode(
            path="/all.g/Cube.r", depth=1, operator="u",
            name="Cube.r", object_type="r", kind="region"))
        self.assertEqual(nodes[6], ms.HierarchyNode(
            path="/all.g/Cube.r/Cylinder", depth=2, operator="-",
            name="Cylinder", object_type="bot", kind="bot"))
        self.assertEqual(nodes[7].name, "a really long name to test")

    def test_preserves_duplicate_occurrences(self):
        nodes = ms.parse_search_hierarchy(self.SAMPLE)
        self.assertEqual(nodes[2], nodes[3])
        self.assertEqual(len(nodes), 8)

    def test_combination_kind_and_name_parentheses(self):
        nodes = ms.parse_search_hierarchy("/u a name (draft)(c)\n")
        self.assertEqual(nodes, [ms.HierarchyNode(
            path="/a name (draft)", depth=0, operator="",
            name="a name (draft)", object_type="c", kind="comb")])

    def test_ignores_empty_summary_and_malformed_lines(self):
        text = "[0] items found in search\nnot a path\n/u malformed\n"
        self.assertEqual(ms.parse_search_hierarchy(text), [])
        self.assertEqual(ms.parse_search_hierarchy(""), [])
        self.assertEqual(ms.parse_search_hierarchy(None), [])


class TestParseWhoNames(unittest.TestCase):
    def test_default_whitespace_behavior(self):
        self.assertEqual(ms.parse_who_names("Cube tri "), ["Cube", "tri"])

    def test_known_names_preserve_spaces_and_paths(self):
        known = {"with space.s", "a really long name to test",
                 "/all.g/Cube.r/Cube"}
        text = "with space.s /all.g/Cube.r/Cube a really long name to test "
        self.assertEqual(ms.parse_who_names(text, known), [
            "with space.s", "/all.g/Cube.r/Cube",
            "a really long name to test"])

    def test_empty(self):
        self.assertEqual(ms.parse_who_names(""), [])
        self.assertEqual(ms.parse_who_names(None), [])


class TestParseLsL(unittest.TestCase):
    def test_types_table(self):
        text = ("Cube        bot     1 30 1712\n"
                "asdf.r      region  1 31 88\n"
                "cube        arb8    1  4 208\n")
        self.assertEqual(ms.parse_ls_l(text),
                         {"Cube": "bot", "asdf.r": "region", "cube": "arb8"})

    def test_name_with_spaces(self):
        # Last four tokens are the type fields; the rest is the (spaced) name.
        self.assertEqual(ms.parse_ls_l("my widget  bot 1 30 1712"),
                         {"my widget": "bot"})

    def test_empty_and_short_lines(self):
        self.assertEqual(ms.parse_ls_l(""), {})
        self.assertEqual(ms.parse_ls_l(None), {})
        self.assertEqual(ms.parse_ls_l("garbage line"), {})


class TestParseTops(unittest.TestCase):
    def test_classify_suffixes(self):
        text = "Cyl-tri.bot         Cyl-tri.r/R         all.g/              bot.mesh"
        self.assertEqual(ms.parse_tops(text), [
            ("Cyl-tri.bot", "primitive"), ("Cyl-tri.r", "region"),
            ("all.g", "comb"), ("bot.mesh", "primitive")])

    def test_empty(self):
        self.assertEqual(ms.parse_tops(""), [])
        self.assertEqual(ms.parse_tops(None), [])


class TestParseTree(unittest.TestCase):
    SAMPLE = ("all.g/\n\tu Cube.r/R\n\t\tu Cube\n\t\t- Cylinder\n"
              "\tu asdf.r/R\n\t\tu cube\n\t\tu tri")

    def test_parse(self):
        self.assertEqual(ms.parse_tree(self.SAMPLE), [
            (0, "", "all.g", "comb"),
            (1, "u", "Cube.r", "region"),
            (2, "u", "Cube", "primitive"),
            (2, "-", "Cylinder", "primitive"),
            (1, "u", "asdf.r", "region"),
            (2, "u", "cube", "primitive"),
            (2, "u", "tri", "primitive"),
        ])

    def test_empty(self):
        self.assertEqual(ms.parse_tree(""), [])
        self.assertEqual(ms.parse_tree(None), [])


class TestParseBotGeometry(unittest.TestCase):
    # Mirrors real MGED 'get <bot>' output: attributes, then V {...}} F {...}}.
    SAMPLE = ("bot mode volume orient rh flags {} "
              "V { { 0 0 0 } { 1 2 3 } { 4.5 -6.5 7.25 }} "
              "F { { 0 1 2 } { 2 1 0 }}")

    def test_parses_verts_and_faces(self):
        verts, faces = ms.parse_bot_geometry(self.SAMPLE)
        self.assertEqual(verts, [(0.0, 0.0, 0.0), (1.0, 2.0, 3.0), (4.5, -6.5, 7.25)])
        self.assertEqual(faces, [(0, 1, 2), (2, 1, 0)])

    def test_handles_adjacent_closing_braces(self):
        # The "}}" where the last element's brace meets the block's must parse.
        verts, faces = ms.parse_bot_geometry("V { { 9 9 9 }} F { { 0 0 0 }}")
        self.assertEqual(verts, [(9.0, 9.0, 9.0)])
        self.assertEqual(faces, [(0, 0, 0)])

    def test_missing_blocks(self):
        self.assertEqual(ms.parse_bot_geometry("comb region no"), ([], []))
        self.assertEqual(ms.parse_bot_geometry(""), ([], []))
        self.assertEqual(ms.parse_bot_geometry(None), ([], []))

    def test_v_not_confused_by_lowercase_words(self):
        # 'volume'/'flags' contain v/f but only capital V/F mark the blocks.
        verts, faces = ms.parse_bot_geometry(
            "bot mode volume flags {} V { { 1 1 1 }} F { { 0 0 0 }}")
        self.assertEqual(verts, [(1.0, 1.0, 1.0)])
        self.assertEqual(faces, [(0, 0, 0)])


class TestConstructorLogging(unittest.TestCase):
    def test_log_file_enabled_from_constructor(self):
        path = os.path.join(tempfile.gettempdir(), "mged_ctor_test.log")
        if os.path.exists(path):
            os.remove(path)
        s = ms.MgedSession("mged", log_file=path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.addCleanup(s.disable_logging)
        s._logger.event("constructed")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("constructed", fh.read())

    def test_console_only_from_constructor(self):
        s = ms.MgedSession("mged", log_to_console=True)
        self.assertTrue(s._logger.log_to_console)


class TestParseAttrs(unittest.TestCase):
    def test_box_g_region_line(self):
        # Real 'attr get Cube.r' output from box.g.
        text = "los {100} material_id {1} region {R} region_id {1000} "
        self.assertEqual(ms.parse_attrs(text), {
            "los": "100", "material_id": "1", "region": "R", "region_id": "1000"})

    def test_empty(self):
        self.assertEqual(ms.parse_attrs(""), {})
        self.assertEqual(ms.parse_attrs(None), {})

    def test_value_with_spaces(self):
        self.assertEqual(ms.parse_attrs("title {hello world} units {1.0}"),
                         {"title": "hello world", "units": "1.0"})

    def test_empty_value(self):
        self.assertEqual(ms.parse_attrs("comment {} region_id {5}"),
                         {"comment": "", "region_id": "5"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
