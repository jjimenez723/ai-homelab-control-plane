"""macOS Seatbelt (``sandbox-exec``) isolation for the fixture runner.

Scope, stated precisely rather than oversold.

Enforced by the generated profile:
  * network access is denied entirely (no ``allow network*`` clause, and
    ``deny default`` covers it);
  * filesystem *writes* are confined to one disposable, per-run work
    directory (resolved with ``os.path.realpath`` -- macOS aliases ``/tmp``
    and ``/var`` to ``/private/tmp``/``/private/var`` via symlinks, and a
    profile written against the unresolved path silently fails to match,
    which would make the sandbox reject the harness's own legitimate
    writes into that directory);
  * process execution is restricted to the exact interpreter binary used to
    run a probe (a literal path match) -- the candidate process can still
    ``fork()`` (Python's own startup does), but it cannot ``exec`` any
    other program, which blocks spawning shells, network tools, or other
    subprocesses;
  * reads under the invoking user's real home directory are denied, with a
    narrow re-allow for the interpreter's own install prefixes. See
    ``home_read_denial`` below for why this shape and not an allowlist.

Explicitly NOT enforced:
  * read confidentiality outside the home directory. ``file-read*`` stays
    broad for system paths. An early prototype of a tight allowlist
    (Python's own install prefix, ``/usr/lib``, ``/System/Library``, etc.)
    crashed ``dyld`` outright on this host, because dyld's shared-cache
    location varies across macOS releases and is not one of the paths a
    small, portable allowlist can name reliably. Candidate code can
    therefore still read world-readable system files. What it can no longer
    read is the owner's home directory -- which is where this project's
    actual assets live (``~/.ssh``, ``~/Library/Keychains``, ``~/.config``,
    the repository working copy itself, and the ``.state/`` telemetry).

Process-level mitigations layered on top:
  * the environment passed to a probe process is a short explicit allowlist
    (``PATH``/``HOME``/``LANG``/etc.) -- never the harness's own
    ``os.environ`` -- so no ambient credential or token reaches candidate
    code this way;
  * ``HOME`` points at the disposable work directory. Note this is a
    convenience, not a boundary: ``pwd.getpwuid(os.getuid()).pw_dir``
    returns the real home regardless of ``HOME``. The Seatbelt denial
    above, not the ``HOME`` value, is what actually stops the read.
  * ``RLIMIT_NPROC`` and the literal-exec restriction above jointly block
    spawning helper processes that might otherwise widen what a candidate
    can reach.

Integrity of the *verdict* is a separate concern from isolation and is not
this module's job: see ``tools/fixtures/runner.py``, which never executes
verifier-owned comparison logic inside the sandbox.
"""

from __future__ import annotations

import os
import pwd
import sys

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def sandbox_available() -> bool:
    """Whether this host can actually enforce the isolation above.

    The runner must fail closed when this is false -- see
    ``runner.evaluate_submission``'s ``sandbox-unavailable`` status. It must
    never fall back to running untrusted code unsandboxed and claim the
    isolation guarantees documented above.
    """
    return os.access(SANDBOX_EXEC, os.X_OK)


def real_home() -> str:
    """The invoking user's home directory, ignoring ``HOME``.

    ``HOME`` is set to the disposable work directory for candidate
    processes, so reading it here would return the wrong answer and build a
    profile that denies nothing. The password database is the authority,
    and it is also exactly the lookup a candidate uses to defeat a
    ``HOME``-only mitigation.
    """
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def interpreter_read_roots() -> tuple[str, ...]:
    """Paths under the home directory that the interpreter itself must read.

    A user-managed Python (uv, pyenv, Homebrew in a user prefix) and any
    virtualenv built from it commonly live under ``~``. Denying the whole
    home directory without re-allowing these makes the interpreter fail to
    start, which is the failure mode this module's history warns about. The
    re-allow is deliberately narrow: the interpreter prefixes only, never
    the rest of the home directory.
    """
    home = real_home()
    roots = {
        os.path.realpath(sys.prefix),
        os.path.realpath(sys.base_prefix),
        os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))),
    }
    return tuple(sorted(root for root in roots if root and root.startswith(home + os.sep)))


def home_read_denial() -> str:
    """Seatbelt clauses denying home reads, re-allowing interpreter prefixes.

    Ordering matters: Seatbelt applies the *last* matching rule, so the
    broad ``(allow file-read*)`` comes first, the home denial next, and the
    interpreter re-allow last.
    """
    lines = [f'(deny file-read* (subpath "{real_home()}"))']
    lines.extend(f'(allow file-read* (subpath "{root}"))' for root in interpreter_read_roots())
    return "\n".join(lines)


def build_seatbelt_profile(real_workdir: str, python_bin: str) -> str:
    """Build a Seatbelt profile scoped to one disposable work directory.

    ``real_workdir`` and ``python_bin`` must already be resolved with
    ``os.path.realpath`` by the caller -- see the module docstring for why.
    """
    return f"""(version 1)
(deny default)
(allow process-exec (literal "{python_bin}"))
(allow process-fork)
(allow file-read*)
{home_read_denial()}
(allow file-read* (subpath "{real_workdir}"))
(allow file-write* (subpath "{real_workdir}"))
(allow file-write-data (literal "/dev/null"))
(allow sysctl-read)
(allow mach-lookup)
(allow iokit-open)
"""
