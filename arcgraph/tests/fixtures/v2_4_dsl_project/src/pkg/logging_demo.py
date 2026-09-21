import logging

logger = logging.getLogger(__name__)


class AuditService:
    logger = logging.getLogger("pkg.audit")

    def emit(self) -> None:
        self.logger.warning("audit")


def record_event() -> None:
    logger.info("stored")


def record_local_event() -> None:
    local_logger = logging.getLogger("pkg.local")
    local_logger.error("local")
