"""Wall-clock boundaries and monotonic elapsed times for collection receipts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from time import monotonic


logger = logging.getLogger("collect")


class CollectionTiming:
    def __init__(self, source="all"):
        self.source = source
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started = monotonic()
        self.stages_ms = {}
        logger.info("Collection started: source=%s at=%s", source, self.started_at)

    @contextmanager
    def measure(self, stage):
        started = monotonic()
        logger.info("Collection phase started: source=%s stage=%s", self.source, stage)
        try:
            yield
        finally:
            elapsed = (monotonic() - started) * 1000
            self.stages_ms[stage] = self.stages_ms.get(stage, 0) + elapsed
            logger.info(
                "Collection phase finished: source=%s stage=%s elapsed_ms=%s",
                self.source,
                stage,
                round(elapsed),
            )

    def snapshot(self):
        return {
            "started_at": self.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((monotonic() - self.started) * 1000),
            "stages_ms": {
                stage: round(value) for stage, value in self.stages_ms.items()
            },
        }

    def include(self, other):
        for stage, elapsed in other.stages_ms.items():
            self.stages_ms[stage] = self.stages_ms.get(stage, 0) + elapsed
