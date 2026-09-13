"""macOS Seatbelt (``sandbox-exec``) isolation for the fixture runner.

V0 scope, stated precisely rather than oversold:

Enforced by the generated profile:
  * network access is denied entirely (no ``allow network*`` clause, and
    ``deny default`` covers it);
  * filesystem *writes* are confined to one disposable, per-run work
    directory (resolved with ``os.path.realpath`` — macOS aliases ``/tmp``
    and ``/var`` to ``/private/tmp``/``/private/var`` via symlinks, and a
    profile written against the unresolved path silently fails to match,
    which would make the sandbox reject the harness's own legitimate
    writes into that directory);
  * process execution is restricted to the exact interpreter binary used to
    run the hidden tests (a literal path match) — the candidate process can
    still ``fork()`` (Python's own startup does), but it cannot ``exec`` any
    other program, which blocks spawning shells, network tools, or other
    subprocesses.

Explicitly NOT enforced by the Seatbelt profile in V0:
  * read confidentiality. ``file-read*`` is intentionally left broad rather
    than allowlisted to a minimal set of paths. An early prototype of a
    tight allowlist (Python's own install prefix, ``/usr/lib``,
    ``/System/Library``, etc.) crashed ``dyld`` outright on this host,
    because dyld's shared-cache location varies across macOS releases and
    is not one of the paths a small, portable allowlist can name reliably.
    Rather than ship a profile that silently breaks (or silently passes
    the wrong thing) on a different macOS version, V0 accepts that
    candidate code able to guess an absolute path on this host could read
    it, and relies on process-level mitigations instead (below).

Process-level mitigations for what the sandbox profile does not cover:
  * the environment passed to the candidate process is a short explicit
    allowlist (``PATH``/``HOME``/``LANG``/etc.) — never the harness's own
    ``os.environ`` — so no ambient credential or token reaches the
    candidate process this way;
  * ``HOME`` points at the disposable work directory, so ``~``-relative
    lookups (``~/.ssh/...`` and similar) resolve inside the sandbox instead
    of the real home directory;
  * the repository's absolute path is never included in any prompt or
    fixture file shown to a model, so a model has no path to target even
    though the OS-level read restriction is not enforced;
  * ``RLIMIT_NPROC`` and the literal-exec restriction above jointly block
    spawning helper processes that might otherwise widen what a candidate
    can reach.

This is a real, accepted V0 gap, not a silent one — a candidate that already
knows (or successfully brute-forces) an absolute path on this specific host
could still read arbitrary files it has OS permission to read. Closing that
fully needs OS-level isolation stronger than Seatbelt provides here (a
container or VM with a filesystem view containing only the disposable copy),
which is out of scope for this V0.
"""

from __future__ import annotations

import os

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def sandbox_available() -> bool:
    """Whether this host can actually enforce the isolation above.

    The runner must fail closed when this is false — see
    ``runner.evaluate_submission``'s ``sandbox-unavailable`` status. It must
    never fall back to running untrusted code unsandboxed and claim the
    isolation guarantees documented above.
    """
    return os.access(SANDBOX_EXEC, os.X_OK)


def build_seatbelt_profile(real_workdir: str, python_bin: str) -> str:
    """Build a Seatbelt profile scoped to one disposable work directory.

    ``real_workdir`` and ``python_bin`` must already be resolved with
    ``os.path.realpath`` by the caller — see the module docstring for why.
    """
    return f"""(version 1)
(deny default)
(allow process-exec (literal "{python_bin}"))
(allow process-fork)
(allow file-read*)
(allow file-write* (subpath "{real_workdir}"))
(allow file-write-data (literal "/dev/null"))
(allow sysctl-read)
(allow mach-lookup)
(allow iokit-open)
"""
