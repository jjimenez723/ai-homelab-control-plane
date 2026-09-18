"""The one seam evaluation and production both call (docs/20 section 4).

D-041's whole point is that "a model-role configuration is deployed using
the same versioned prompt definition and rendering path that was evaluated."
`render_for_task_class` is that path: a task class maps to exactly one active
prompt version, and both an ACB-v1 suite runner and a production caller
render through this same function. `tests/test_prompt_registry.py::
SharedPathTests` proves a suite runner and a production call produce
byte-identical rendered text for the same input, which is the only way "the
same prompt" is more than an assertion in a docstring.

`naming.semantic` now has a real production caller:
`tools/ingest/naming/semantic.py::SemanticProposer` renders through this
function when `naming.proposer: "semantic"` is configured (D-057), which
retires D-051's "still never exercised by a production caller" for that task
class and makes the shared-path claim above load-bearing rather than
aspirational -- a change to the prompt's text now changes both what the suite
measures and what production sends, which is the entire point.

`classification` and `routing` still have no production caller. That remains a
true statement rather than an oversight: the routing work they belong to is
Gate-2/attended-autonomous (D-051), not the file-intelligence pipeline.
"""

from __future__ import annotations

from tools.prompts.registry import PromptRegistry, UnknownPrompt, load_default_registry
from tools.prompts.renderer import ContextPack, EMPTY_CONTEXT, RenderedPrompt, render

# Task class (tools.models.task_classes) -> prompt_id. Only the three
# certifiable Gate-2 classes have a prompt today; a class with no entry here
# has no production prompt yet, which is a true statement, not an oversight.
TASK_CLASS_PROMPT_ID: dict[str, str] = {
    "classification": "acb-v1-routing-classification",
    "routing": "acb-v1-routing-classification",
    "naming.semantic": "acb-v1-semantic-naming",
}


class NoPromptForTaskClass(KeyError):
    """This task class has no registered production prompt yet."""


def prompt_id_for_task_class(task_class: str) -> str:
    try:
        return TASK_CLASS_PROMPT_ID[task_class]
    except KeyError as exc:
        raise NoPromptForTaskClass(
            f"task_class {task_class!r} has no prompt registered; known: "
            f"{sorted(TASK_CLASS_PROMPT_ID)}"
        ) from exc


def render_for_task_class(
    task_class: str,
    *,
    task_text: str,
    context: ContextPack = EMPTY_CONTEXT,
    extra: dict[str, str] | None = None,
    registry: PromptRegistry | None = None,
) -> RenderedPrompt:
    """Render the active prompt for a task class against one input.

    This is exactly what a suite runner calls for each item, and exactly
    what a production caller for that task class should call -- the shared
    path, not a description of one.
    """
    registry = registry or load_default_registry()
    prompt = registry.active(prompt_id_for_task_class(task_class))
    return render(prompt, task_text=task_text, context=context, extra=extra)
