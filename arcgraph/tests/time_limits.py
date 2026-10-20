"""Time limits shared by the tests.

Hosted CI runners are several times slower than development machines and their
speed varies by two times or more between runs. A limit that only guards
against a hang must therefore sit far above any normal duration, and it costs
nothing when the awaited work finishes early. CI prints the slowest tests
(``--durations``) to show how close the suite comes to these limits.
"""

SUBPROCESS_HANG_SECONDS = 600
"""Timeout for a child process that should finish; only a hang reaches it."""

WAIT_HANG_SECONDS = 60
"""Timeout for an in-process wait on an event that should happen."""
