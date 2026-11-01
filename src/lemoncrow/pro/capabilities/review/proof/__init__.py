"""Proof: a reproduction for each open P0/P1 finding, and whether the pull request's added tests fail on base.

Three steps, each its own process and, in GitHub Actions, its own job:

* :mod:`plan` holds the model key and runs no pull request code. It asks the model for one minimal failing
  test per finding, written for the repository's existing test framework (:mod:`frameworks`), and writes a
  plan file.
* :mod:`run` holds no secret. It runs each planned test on head and on base, and the pull request's own
  changed tests on base (red-on-base), sandboxed when bubblewrap works, and writes a results file.
* :mod:`report` may write to the pull request and runs no pull request code. It reads both files and posts a
  ``LemonCrow Proof`` check run plus a reply on each finding's thread.

The files are the only hand-off (:mod:`wire`).
"""
