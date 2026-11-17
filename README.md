# Standalone MGED Session

This is a general-purpose MGED controller for python.  It talks over a pipe
to an MGED process.

## Ownership boundary

- `MgedSession.py` owns subprocess lifecycle, asynchronous command/future I/O,
  MGED response parsing, hierarchy/BOT data, logging, and its executable example.
  It has no Blender dependency.
- `tests/test_mged_session.py` and `tests/test_integration.py` test that standalone
  behavior and travel with `MgedSession.py`.

