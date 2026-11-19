import logging
import sys

from mcp_server_polymarket.redaction import scrub


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    # Stdout carries JSON-RPC under stdio, so every log line has to go to stderr.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for noisy in ("httpx2", "httpcore", "httpcore2", "mcp", "wsproto"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
