"""
Polls the NASDAQ trade halt RSS feed every N seconds.
Detects LULD pause → resume transitions and emits symbols ready to trade.

Fix (2026-05-07):
  - Detection is now non-blocking: resolved symbols are pushed onto a Queue
    immediately without waiting for order execution to complete.
  - Removed the requirement that the halt phase must be observed before the
    resumption fires. Previously, if the poll loop was frozen processing halt A
    and halt B completed its full halt+resume cycle during that freeze, halt B
    would be silently dropped because it was never added to _halted.
  - A dedicated worker thread drains the queue sequentially, keeping IBKR API
    calls single-threaded (ib_insync is not thread-safe for concurrent calls).
"""

import logging
import queue
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests

from config import HALT_RSS_URL, HALT_POLL_SECONDS, LULD_HALT_CODES

# NASDAQ RSS uses a namespace prefix on all custom fields
NDAQ = "http://www.nasdaqtrader.com/"

log = logging.getLogger(__name__)


@dataclass
class HaltEvent:
    symbol: str
    halt_code: str
    halt_time: str
    resumption_time: str
    reason: str
    is_resumed: bool


class HaltMonitor:
    """
    Polls NASDAQ halt RSS and calls on_resumption(symbol) when a LULD halt lifts.

    Architecture:
      - Poll thread: fetches RSS every HALT_POLL_SECONDS, pushes newly-resumed
        symbols onto _queue. Never blocks on order execution.
      - Worker thread: drains _queue one symbol at a time, calling on_resumption.
        Sequential processing keeps IBKR API calls safe.

    This means: even if 5 halts fire simultaneously, all 5 are detected and
    queued immediately. They're then processed one-by-one by the worker, each
    potentially 30+ seconds apart — but none are dropped.
    """

    def __init__(self, on_resumption: Callable[[str], None]):
        self.on_resumption = on_resumption
        self._fired: set[str] = set()    # symbols already queued/traded today
        self._queue: queue.Queue = queue.Queue()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "halt-trader/1.0"

        # Start worker thread
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="halt-worker"
        )
        self._worker_thread.start()

    def run(self) -> None:
        """Blocking poll loop — run in a background thread."""
        # Pre-populate _fired with all halts already resumed before we started.
        # The NASDAQ RSS feed shows the full day's halts, so without this the
        # first poll would queue every halt that fired since 9:30 AM.
        self._initial_scan()
        log.info("Halt monitor started, polling every %ds", HALT_POLL_SECONDS)
        while True:
            try:
                self._poll()
            except Exception as e:
                log.warning("Halt poll error: %s", e)
            time.sleep(HALT_POLL_SECONDS)

    def _initial_scan(self) -> None:
        """Mark all currently-resumed halts as already seen (don't trade them)."""
        try:
            resp = self._session.get(HALT_RSS_URL, timeout=10)
            resp.raise_for_status()
            root = _parse_rss(resp.content)
            pre_fired = []
            for item in root.findall(".//item"):
                symbol    = _text(item, f"{{{NDAQ}}}IssueSymbol") or _text(item, "title")
                halt_code = _text(item, f"{{{NDAQ}}}ReasonCode") or ""
                resume_time = _text(item, f"{{{NDAQ}}}ResumptionTradeTime") or ""
                if not symbol:
                    continue
                symbol = symbol.upper().strip()
                if halt_code not in LULD_HALT_CODES:
                    continue
                if resume_time and resume_time.strip():
                    self._fired.add(symbol)
                    pre_fired.append(symbol)
            if pre_fired:
                log.info("Initial scan: skipping %d already-resumed halts: %s",
                         len(pre_fired), pre_fired)
            else:
                log.info("Initial scan: no prior resumptions found")
        except Exception as e:
            log.warning("Initial scan failed: %s — will proceed anyway", e)

    def reset_daily(self) -> None:
        """Call at market open each day to clear state."""
        self._fired.clear()
        log.info("Halt monitor daily state reset")

    def _poll(self) -> None:
        """
        Fetch RSS, find resumed LULD halts, push new ones to the worker queue.
        This must return quickly — no sleeping, no IBKR calls here.
        """
        resp = self._session.get(HALT_RSS_URL, timeout=10)
        resp.raise_for_status()
        root = _parse_rss(resp.content)

        for item in root.findall(".//item"):
            symbol      = _text(item, f"{{{NDAQ}}}IssueSymbol") or _text(item, "title")
            halt_code   = _text(item, f"{{{NDAQ}}}ReasonCode") or ""
            resume_time = _text(item, f"{{{NDAQ}}}ResumptionTradeTime") or ""

            if not symbol:
                continue
            symbol = symbol.upper().strip()

            # Only care about LULD halt codes
            if halt_code not in LULD_HALT_CODES:
                continue

            is_resumed = bool(resume_time and resume_time.strip())

            if is_resumed and symbol not in self._fired:
                # Mark fired immediately (before worker picks it up) so
                # subsequent polls don't re-queue the same symbol.
                self._fired.add(symbol)
                log.info("LULD resumption queued: %s", symbol)
                self._queue.put(symbol)

    def _worker(self) -> None:
        """
        Drain the queue sequentially. All IBKR API calls happen here, on one
        thread, so there are no concurrency issues with ib_insync.
        """
        while True:
            symbol = self._queue.get()
            try:
                log.info("Worker processing resumption: %s  (queue depth after: %d)",
                         symbol, self._queue.qsize())
                self.on_resumption(symbol)
            except Exception as e:
                log.error("on_resumption error for %s: %s", symbol, e, exc_info=True)
            finally:
                self._queue.task_done()


def _parse_rss(content: bytes) -> ET.Element:
    """
    Parse RSS bytes robustly.
    NASDAQ sends UTF-8 with BOM (\xef\xbb\xbf). Passing a decoded string
    with an encoding="utf-8" XML declaration confuses Python's parser, so
    strip the BOM from bytes and pass bytes directly to ET.fromstring.
    """
    UTF8_BOM = b"\xef\xbb\xbf"
    if content.startswith(UTF8_BOM):
        content = content[len(UTF8_BOM):]
    try:
        return ET.fromstring(content)
    except ET.ParseError as e:
        raise ValueError(
            f"RSS feed did not return valid XML — market may be closed or feed is down ({e})"
        ) from e


def _text(elem: ET.Element, tag: str) -> str | None:
    child = elem.find(tag)
    return child.text.strip() if child is not None and child.text else None
