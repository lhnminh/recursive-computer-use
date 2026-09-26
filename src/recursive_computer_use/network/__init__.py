"""Learn website tasks from recorded network traffic and replay them as API calls.

- ``recipe``  : the ``Recipe`` type shared by the learner, store and runner.
- ``har``     : read and redact a recorded HAR file.
- ``learner`` : turn redacted traffic into a recipe; fill a recipe's params.
- ``capture`` : record a HAR while a person or computer use does the task.
- ``runner``  : replay a recipe over HTTP.
- ``loop``    : recipe first, computer-use fallback, relearn on failure.
"""

from .recipe import (
    Extract,
    Param,
    Recipe,
    RecipeError,
    Step,
    render,
    resolve_session_vars,
    template_vars,
)
from .runner import RunResult, StepResult, run_recipe

__all__ = [
    "Extract",
    "Param",
    "Recipe",
    "RecipeError",
    "RunResult",
    "Step",
    "StepResult",
    "render",
    "resolve_session_vars",
    "run_recipe",
    "template_vars",
]
