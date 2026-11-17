"""Integration tests that drive a real mged process.

These are skipped automatically when an mged executable cannot be found. To run
them, ensure `mged` is on PATH, or set the MGED_EXE environment variable to its
full path, e.g. (Git Bash on Windows):

    MGED_EXE="/c/bvldsw/BRL-CAD_7.47.0_win64/bin/mged.exe" python -m unittest discover -s mged_session/tests -v

The core plumbing tests deliberately launch with NO database (Tcl `puts` works
without one) to avoid mged's interactive "create new database?" prompt.
"""

import os
import shutil
import sys
import tempfile
import unittest

_SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKSPACE_ROOT = os.path.dirname(_SOURCE_ROOT)
sys.path.insert(0, _SOURCE_ROOT)
import MgedSession as ms  # noqa: E402


def _find_test_db():
    configured = os.environ.get("MGED_TEST_DB")
    if configured:
        return os.path.abspath(configured) if os.path.exists(configured) else None
    for candidate in (
            os.path.join(_SOURCE_ROOT, "box.g"),
            os.path.join(_WORKSPACE_ROOT, "box.g")):
        if os.path.exists(candidate):
            return candidate
    return None


def _find_mged():
    configured = os.environ.get("MGED_EXE")
    if configured and os.path.exists(configured):
        return configured
    development = r"C:\bvldsw\BRL-CAD_7.47.0_win64\bin\mged.exe"
    if os.path.exists(development):
        return development
    on_path = shutil.which("mged")
    if on_path and os.path.exists(on_path):
        return on_path
    fallback = r"D:\BRL-CAD_7.43.0_win64\bin\mged.exe"
    return fallback if os.path.exists(fallback) else None


MGED = _find_mged()
TEST_DB = _find_test_db()


@unittest.skipUnless(MGED, "mged executable not found (set MGED_EXE to run)")
class TestMgedIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Absorb mged's cold-start cost once (DLL load, AV scan of the binary)
        # with a generous timeout so per-test handshakes don't hit the limit.
        warm = ms.MgedSession(MGED, ["-c"])
        try:
            warm.launch(handshake_timeout=90)
        finally:
            warm.shutdown()

    def _session(self, **kwargs):
        s = ms.MgedSession(MGED, ["-c"], **kwargs)
        self.addCleanup(s.shutdown)
        return s

    def test_handshake_and_puts_roundtrip(self):
        s = self._session()
        s.launch()  # no db -> no create prompt; blocks on handshake
        self.assertTrue(s.running)
        result = s.send_command('puts "roundtrip"').result(timeout=10)
        self.assertEqual(result, "roundtrip")

    def test_nonblocking_launch_then_command(self):
        # Mirrors the Blender addon's launch path: wait=False returns at once,
        # the process is already running, and commands work immediately.
        s = self._session()
        handshake = s.launch(wait=False)
        self.assertTrue(s.running)
        self.assertEqual(s.send_command('puts "nb"').result(timeout=10), "nb")
        self.assertIsNotNone(handshake.result(timeout=10))

    def test_multiple_commands_keep_fifo_order(self):
        s = self._session()
        s.launch()
        handles = [s.send_command(f'puts "msg{i}"') for i in range(5)]
        for i, h in enumerate(handles):
            self.assertEqual(h.result(timeout=10), f"msg{i}")

    def test_done_callback_fires(self):
        s = self._session()
        s.launch()
        got = []
        s.send_command('puts "cb"', callback=lambda f: got.append(f.result()))
        # Give the reader thread a moment by blocking on a follow-up command.
        s.send_command('puts "sync"').result(timeout=10)
        self.assertEqual(got, ["cb"])

    def test_shutdown_then_relaunch_reuses_object(self):
        s = self._session()
        s.launch()
        self.assertEqual(s.send_command('puts "first"').result(timeout=10), "first")
        s.shutdown()
        self.assertFalse(s.running)
        s.launch()  # same object, fresh process
        self.assertEqual(s.send_command('puts "second"').result(timeout=10), "second")

    def test_file_logging_during_real_session(self):
        path = os.path.join(tempfile.gettempdir(), "mged_integration.log")
        if os.path.exists(path):
            os.remove(path)
        s = self._session(log_file=path)
        # Cleanups run LIFO: close the log handle first, then remove the file.
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.addCleanup(s.disable_logging)
        s.launch()
        s.send_command('puts "logged"').result(timeout=10)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('-> MGED (user): puts "logged"', text)
        self.assertIn("MGED -> : logged", text)


@unittest.skipUnless(MGED, "mged executable not found (set MGED_EXE to run)")
@unittest.skipUnless(TEST_DB,
                     "sample database not found (set MGED_TEST_DB to box.g)")
class TestMgedWithDatabase(unittest.TestCase):
    def test_open_box_g_and_list(self):
        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(TEST_DB)
        result = s.send_command("ls").result(timeout=10)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.strip(), "")

    def test_list_bots(self):
        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(TEST_DB)
        bots = s.list_bots()
        self.assertIn("tri", bots)
        self.assertIn("bot.mesh", bots)
        # 'cube' is an arb8 and 'asdf.r' is a region -- neither is a BOT.
        self.assertNotIn("cube", bots)
        self.assertNotIn("asdf.r", bots)

    def test_list_objects_is_complete_and_preserves_spaces(self):
        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(TEST_DB)
        objects = s.list_objects()
        self.assertEqual(len(objects), 16)
        self.assertIn("with space.s", objects)
        self.assertIn("a really long name to test", objects)

    def test_hierarchy_and_who(self):
        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(TEST_DB)
        nodes = s.hierarchy()
        self.assertIn(ms.HierarchyNode(
            "/all.g", 0, "", "all.g", "c", "comb"), nodes)
        self.assertIn(ms.HierarchyNode(
            "/all.g/Cube.r", 1, "u", "Cube.r", "r", "region"), nodes)
        # Cube/Cylinder/tri are BOTs; 'cube' is an arb8 (plain primitive).
        self.assertIn(ms.HierarchyNode(
            "/all.g/Cube.r/Cube", 2, "u", "Cube", "bot", "bot"), nodes)
        self.assertIn(ms.HierarchyNode(
            "/all.g/Cube.r/Cylinder", 2, "-", "Cylinder", "bot", "bot"), nodes)
        self.assertIn(ms.HierarchyNode(
            "/all.g/asdf.r/cube", 2, "u", "cube", "arb8", "primitive"), nodes)
        self.assertIn(ms.HierarchyNode(
            "/all.g/asdf.r/tri", 2, "u", "tri", "bot", "bot"), nodes)
        self.assertIn(ms.HierarchyNode(
            "/with space.s", 0, "", "with space.s", "tgc", "primitive"), nodes)
        self.assertEqual(sum(node.path == "/prims.r/tgc.s" for node in nodes), 2)
        # Nothing displayed initially; 'who' reflects what gets drawn.
        self.assertEqual(s.who(), [])
        s.send_command("draw tri").result(timeout=10)
        self.assertIn("tri", s.who())

    def test_get_bot_reads_geometry(self):
        # 'tri' in box.g is a BOT with 36 vertices / 68 faces (per 'l tri').
        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(TEST_DB)
        geom = s.get_bot("tri")
        self.assertIsNotNone(geom)
        verts, faces = geom
        self.assertEqual(len(verts), 36)
        self.assertEqual(len(faces), 68)
        self.assertTrue(all(len(v) == 3 for v in verts))
        self.assertTrue(all(len(f) == 3 for f in faces))

    def test_get_bot_missing_returns_none(self):
        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(TEST_DB)
        self.assertIsNone(s.get_bot("no_such_object_xyz"))

    def test_put_rejected_but_adjust_updates_existing(self):
        # Regression guard for the real-time update bug (see session.txt,
        # 2026-06-04): MGED rejects 'put' on an object that already exists, so
        # geometry updates to an existing primitive MUST use 'adjust'. We work
        # on a temp copy so the box.g fixture is never mutated.
        import shutil as _sh
        tmp = os.path.join(tempfile.gettempdir(), "box_adjust_test.g")
        _sh.copyfile(TEST_DB, tmp)
        self.addCleanup(lambda: os.path.exists(tmp) and os.remove(tmp))

        s = ms.MgedSession(MGED, ["-c"])
        self.addCleanup(s.shutdown)
        s.launch(tmp)
        self.assertIn("tri", (s.send_command("ls tri").result(timeout=10) or ""))

        # 'put' on the existing object is rejected ...
        put_out = (s.send_command(
            'put tri bot V { { 0 0 0 } } F { { 0 0 0 } }').result(timeout=10) or "")
        self.assertIn("already exists", put_out.lower())

        # ... but 'adjust' modifies it in place with no error.
        adj_out = (s.send_command(
            'adjust tri V { { 1 1 1 } { 2 2 2 } { 3 3 3 } } F { { 0 1 2 } }'
        ).result(timeout=10) or "")
        self.assertNotIn("already exists", adj_out.lower())
        self.assertNotIn("no such", adj_out.lower())
        self.assertNotIn("not found", adj_out.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
