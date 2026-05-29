"""
Polls the NASDAQ trade halt RSS feed every N seconds.
Detects LULD pause → resume transitions and pushes symbols onto a Queue.

Architecture:
  - Poll thread: fetches RSS every HALT_POLL_SECONDS, pushes newly-resumed
    symbols onto _queue. Never blocks on order execution or IB calls.
  - Main thread: drains _queue each heartbeat tick and calls on_resumption.
    IB API calls (qualifyContracts, reqHistoricalData, placeOrder) MUST run
    on the thread that owns the ib_insync asyncio event loop — which is the
    main thread. A separate worker thread cannot make these calls safely in
    Python 3.10+ (raises "no current event loop in thread").
"""

import logging
import queue
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    Polls NASDAQ halt RSS and pushes resumed LULD symbols onto _queue.
    The caller (main thread) is responsible for draining _queue and acting on symbols.
    """

    def __init__(self):
        self._fired: set[str] = set()    # symbols already queued/traded today
        self._queue: queue.Queue = queue.Queue()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "halt-trader/1.0"

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
