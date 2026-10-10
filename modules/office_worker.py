from core.interfaces import SessionContext, TaskResult


def run(context: SessionContext) -> TaskResult:
    """
    Execute the office worker workflow.

    Required:
    - Open an office application.
    - Enter content using context.behaviour.
    - Include pauses and occasional corrections.
    - Save the document.
    - Close the application.
    - Return a TaskResult.
    """
    raise NotImplementedError("Member 1 implementation pending")
