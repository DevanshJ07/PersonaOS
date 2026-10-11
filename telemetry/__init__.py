"""PersonaOS telemetry: passive session collector and behaviour instrumentation.

The evaluator is intentionally not imported here; import it as
``telemetry.evaluator`` (importing it here breaks ``python -m telemetry.evaluator``).
"""

from telemetry.collector import TelemetryCollector, instrument_behaviour

__all__ = ["TelemetryCollector", "instrument_behaviour"]
