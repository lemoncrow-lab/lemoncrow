"""The one implementation of each local tool's logic.

The thin client's executors (:mod:`lemoncrow_client.localtools`) and the main
``lemoncrow`` package both import these modules, so a tool behaves the same
wherever it runs and a fix lands once. Four rules keep that true, and
``tests/test_kit_boundary.py`` plus the packaging audit assert them:

* kit imports only the standard library, :mod:`lemoncrow_client.errors` and
  other kit modules -- never the client's session, transport, config or
  ``localtools``;
* kit takes plain inputs (a workspace root, argument mappings) and returns plain
  results; each caller renders them in its own format;
* the client's audit applies unchanged, and :mod:`.fsio` is kit's only module
  that writes files;
* behavior that needs something only the main package has (the code index,
  source projections, a third-party library) is an optional hook; without it
  kit uses a documented fallback.

A behavior a client tool needs is added here, never re-implemented beside it.
"""
