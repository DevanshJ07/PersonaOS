from core.interfaces import SessionContext, TaskResult


def run(context: SessionContext) -> TaskResult:
    """
    Execute the browser workflow.

    Required:
    - Open a browser.
    - Visit websites from context.config.
    - Scroll and follow links.
    - Use context.behaviour.pause() between actions.
    - Close the browser.
    - Return a TaskResult.
    """
    raise NotImplementedError("Member 2 implementation pending")
