"""In-sandbox probe driver. Copied verbatim into the disposable work directory.

This is the only verifier-authored code that runs on the same side of the
trust boundary as a candidate submission, and it is deliberately incapable
of deciding anything: it executes one probe, reports what it observed, and
exits. The comparison against the expected value happens in the parent
process, outside the sandbox, where candidate code cannot reach it.

Consequences that matter, and are tested:

* if the candidate exits (``os._exit``), hangs, or crashes, no observation
  line is written and the parent scores the probe as ``no-observation`` --
  silence is a failure, never a pass;
* nothing here compares, asserts, or scores, so there is no assertion
  machinery for a candidate to rebind into a no-op;
* the expected value is not present in this process, in ``argv``, in the
  environment, or anywhere in the work directory.

Ordering of marker lines is not a security property. A candidate can emit a
marker of its own; to be accepted it would have to carry the correct answer
for the input it was given, which is the task itself.

Run as: ``python _angel_probe.py <probe-source-file>``
"""

import json
import sys

OBSERVATION_MARKER = "@@ANGEL-OBSERVED@@ "
ERROR_MARKER = "@@ANGEL-ERROR@@ "


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: _angel_probe.py <probe-source-file>\n")
        return 2
    with open(argv[1], "r", encoding="utf-8") as stream:
        source = stream.read()

    namespace = {"__name__": "__angel_probe__"}
    try:
        exec(compile(source, "<angel-probe>", "exec"), namespace)  # noqa: S102 - the probe body is verifier-authored
    except BaseException as exc:  # noqa: BLE001 - a candidate may raise anything at all
        sys.stdout.write(ERROR_MARKER + json.dumps(f"{type(exc).__name__}: {exc}"[:2000]) + "\n")
        sys.stdout.flush()
        return 1

    if "OBSERVED" not in namespace:
        sys.stdout.write(ERROR_MARKER + json.dumps("probe body assigned no OBSERVED value") + "\n")
        sys.stdout.flush()
        return 1

    try:
        payload = json.dumps(namespace["OBSERVED"], sort_keys=True, default=repr)
    except (TypeError, ValueError) as exc:
        sys.stdout.write(ERROR_MARKER + json.dumps(f"unserialisable observation: {exc}"[:2000]) + "\n")
        sys.stdout.flush()
        return 1

    sys.stdout.write(OBSERVATION_MARKER + payload + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
