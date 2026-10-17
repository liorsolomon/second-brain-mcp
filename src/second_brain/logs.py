"""Access logging without query strings.

OAuth endpoints carry single-use codes, state and transaction IDs in the URL
(/auth/callback?code=…, /consent?txn_id=…). Uvicorn's access log writes the full
URL, so those values would end up in the hosting provider's logs.
"""

from __future__ import annotations

import copy
import logging
from typing import Any


class StripQueryFilter(logging.Filter):
    """Drops the query string from uvicorn access-log records, keeping the path."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """Uvicorn's default logging config with the query-stripping filter on the access handler."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["strip_query"] = {"()": "second_brain.logs.StripQueryFilter"}
    config["handlers"]["access"].setdefault("filters", []).append("strip_query")
    return config
