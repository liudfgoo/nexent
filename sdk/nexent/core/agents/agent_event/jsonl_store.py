"""JSONL-backed implementation of EventStore.

Directory layout::

    <root>/<session_id>/
        events.jsonl   — append-only event log, one JSON object per line
        meta.json      — Session object (rewritable)
        runs.jsonl     — Run records
        blobs/<sha256> — content-addressed blob storage
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from .models import BranchResult, Event, Run, Session
from .store import EventStore

logger = logging.getLogger("agent_event.jsonl_store")

_IS_WIN = platform.system() == "Windows"


class JsonlEventStore(EventStore):
    """File-system event store using one JSONL file per session."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

        # In-process write lock + seq cache per session
        self._lock = threading.Lock()
        self._seq_cache: Dict[UUID, int] = {}  # session_id -> next_seq
        # In-memory idempotency index keyed by event_id (unique)
        self._seen_event_ids: Dict[UUID, bool] = {}
        # Secondary composite-key index for (run_id, step_index, type, tool_call_id) dedup
        self._append_index: Dict[Tuple, bool] = {}
        # In-memory event_id -> Event for branch traversal
        self._event_index: Dict[UUID, Event] = {}

    # -- helpers -------------------------------------------------------------

    def _session_dir(self, session_id: UUID) -> Path:
        d = self._root / str(session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _event_to_line(event: Event) -> str:
        return event.model_dump_json(by_alias=True, exclude_none=True)

    @staticmethod
    def _line_to_event(line: str) -> Event:
        return Event.model_validate_json(line)

    def _load_seq(self, session_id: UUID) -> int:
        """Determine the next seq by reading the last line of events.jsonl."""
        events_path = self._session_dir(session_id) / "events.jsonl"
        if not events_path.exists():
            return 0
        with open(events_path, "rb") as f:
            # Seek to end, scan backwards for last non-empty line
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return 0
            pos = size - 1
            # Skip trailing newlines / carriage returns
            while pos > 0:
                f.seek(pos)
                ch = f.read(1)
                if ch not in (b"\n", b"\r"):
                    break
                pos -= 1
            if pos == 0:
                return 0
            # Now pos points to last non-newline char; scan back to find
            # the start of this line (previous newline or beginning of file)
            line_end = pos + 1
            while pos > 0:
                f.seek(pos)
                if f.read(1) == b"\n":
                    break
                pos -= 1
            line_start = pos + 1 if pos > 0 else 0
            f.seek(line_start)
            last_line = f.read(line_end - line_start).decode("utf-8").strip()
            if not last_line:
                return 0
            try:
                ev = self._line_to_event(last_line)
                return ev.seq + 1
            except Exception:
                logger.warning("Failed to parse last line for seq recovery")
                return 0

    def _ensure_index(self, session_id: UUID) -> None:
        """Load events.jsonl into _event_index and _append_index if not yet loaded."""
        events_path = self._session_dir(session_id) / "events.jsonl"
        if not events_path.exists():
            return
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = self._line_to_event(line)
                    self._event_index[ev.event_id] = ev
                    self._seen_event_ids[ev.event_id] = True
                    if ev.run_id is not None and ev.step_index is not None:
                        tc_id = ""
                        if ev.type.value == "tool_call":
                            tc_id = ev.payload.tool_call_id  # type: ignore[union-attr]
                        key = (ev.session_id, ev.run_id, ev.step_index, ev.type.value, tc_id)
                        self._append_index[key] = True
                except Exception:
                    logger.warning("Failed to parse event line during index build")

    # -- write ---------------------------------------------------------------

    def append(self, event: Event) -> None:
        session_id = event.session_id
        # Primary dedup: event_id (UUID)
        if event.event_id in self._seen_event_ids:
            logger.debug("Duplicate event_id skipped: %s", event.event_id)
            return

        # Secondary dedup: (run_id, step_index, type, tool_call_id) composite key
        tc_id = ""
        if event.type.value == "tool_call" and event.run_id is not None:
            tc_id = event.payload.tool_call_id  # type: ignore[union-attr]
            key = (session_id, event.run_id, event.step_index, event.type.value, tc_id)
            if key in self._append_index:
                logger.debug("Duplicate composite key skipped: %s", key)
                return

        session_dir = self._session_dir(session_id)
        events_path = session_dir / "events.jsonl"

        with self._lock:
            # Allocate seq inside lock
            if session_id not in self._seq_cache:
                self._seq_cache[session_id] = self._load_seq(session_id)
                self._ensure_index(session_id)
            # Re-check after lock acquisition
            if event.event_id in self._seen_event_ids:
                return

            # Patch event seq (caller may pass 0 as placeholder)
            if event.seq == 0:
                event.seq = self._seq_cache[session_id]
            self._seq_cache[session_id] = event.seq + 1

            line = self._event_to_line(event) + "\n"
            try:
                with open(events_path, "a", encoding="utf-8") as f:
                    # Cross-process file lock (best-effort)
                    if not _IS_WIN:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    else:
                        # Windows: msvcrt.locking for byte-range lock
                        try:
                            import msvcrt
                            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        except (ImportError, OSError):
                            pass
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                logger.error("Failed to append event seq=%d: %s", event.seq, line[:200])
                raise
            else:
                self._seen_event_ids[event.event_id] = True
                # Only add composite key when run_id is present (meaningful dedup)
                if event.run_id is not None and event.step_index is not None:
                    if event.type.value == "tool_call":
                        tc_id = event.payload.tool_call_id  # type: ignore[union-attr]
                    else:
                        tc_id = ""
                    key = (session_id, event.run_id, event.step_index, event.type.value, tc_id)
                    self._append_index[key] = True
                self._event_index[event.event_id] = event

    # -- read ----------------------------------------------------------------

    def next_seq(self, session_id: UUID) -> int:
        with self._lock:
            if session_id not in self._seq_cache:
                self._seq_cache[session_id] = self._load_seq(session_id)
                self._ensure_index(session_id)
            return self._seq_cache[session_id]

    def read_session(self, session_id: UUID) -> BranchResult:
        self._ensure_index(session_id)
        events_path = self._session_dir(session_id) / "events.jsonl"
        events: List[Event] = []
        if events_path.exists():
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(self._line_to_event(line))
                    except Exception:
                        logger.warning("Skipping malformed line in read_session")
        events.sort(key=lambda e: e.seq)
        return self._detect_gaps(events)

    def read_branch(self, leaf_event_id: UUID) -> BranchResult:
        # Ensure index is populated for the session that contains leaf_event_id
        if leaf_event_id not in self._event_index:
            # Scan all session dirs to find the event
            for session_dir in self._root.iterdir():
                if session_dir.is_dir():
                    sid = UUID(session_dir.name)
                    self._ensure_index(sid)
                    if leaf_event_id in self._event_index:
                        break

        # Build branch by walking parent_event_id chain
        chain: List[Event] = []
        current_id: Optional[UUID] = leaf_event_id
        visited: set = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            ev = self._event_index.get(current_id)
            if ev is None:
                # Gap: parent_event_id points to missing event
                return BranchResult(
                    events=list(reversed(chain)),
                    has_gap=True,
                    gap_event_id=current_id,
                )
            chain.append(ev)
            current_id = ev.parent_event_id

        events = list(reversed(chain))
        return self._detect_gaps(events)

    # -- session / run -------------------------------------------------------

    def get_session(self, session_id: UUID) -> Session:
        meta_path = self._session_dir(session_id) / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Session {session_id} not found")
        return Session.model_validate_json(meta_path.read_text(encoding="utf-8"))

    def upsert_session(self, s: Session) -> None:
        meta_path = self._session_dir(s.session_id) / "meta.json"
        meta_path.write_text(
            s.model_dump_json(by_alias=True, exclude_none=True) + "\n",
            encoding="utf-8",
        )

    def get_run(self, run_id: UUID) -> Run:
        # Scan runs.jsonl across all sessions
        for session_dir in self._root.iterdir():
            if not session_dir.is_dir():
                continue
            runs_path = session_dir / "runs.jsonl"
            if not runs_path.exists():
                continue
            with open(runs_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = Run.model_validate_json(line)
                        if r.run_id == run_id:
                            return r
                    except Exception:
                        continue
        raise FileNotFoundError(f"Run {run_id} not found")

    def upsert_run(self, r: Run) -> None:
        session_dir = self._session_dir(r.session_id)
        runs_path = session_dir / "runs.jsonl"

        # Read existing runs, replace if same run_id, else append
        existing: List[Run] = []
        replaced = False
        if runs_path.exists():
            with open(runs_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing_run = Run.model_validate_json(line)
                        if existing_run.run_id == r.run_id:
                            existing.append(r)
                            replaced = True
                        else:
                            existing.append(existing_run)
                    except Exception:
                        continue

        with open(runs_path, "w" if replaced else "a", encoding="utf-8") as f:
            if replaced:
                for run in existing:
                    f.write(run.model_dump_json(by_alias=True, exclude_none=True) + "\n")
            else:
                f.write(r.model_dump_json(by_alias=True, exclude_none=True) + "\n")

    # -- blob storage --------------------------------------------------------

    def put_blob(self, data: str) -> str:
        sha = hashlib.sha256(data.encode("utf-8")).hexdigest()
        # Blobs are stored under a global blobs dir to enable cross-session sharing
        blobs_dir = self._root / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        blob_path = blobs_dir / sha
        if not blob_path.exists():
            blob_path.write_text(data, encoding="utf-8")
        return f"blob://{sha}"

    def get_blob(self, content_ref: str) -> str:
        # Accept both "blob://<sha>" and raw sha
        if content_ref.startswith("blob://"):
            sha = content_ref[7:]
        else:
            sha = content_ref
        blob_path = self._root / "blobs" / sha
        if not blob_path.exists():
            raise FileNotFoundError(f"Blob {sha} not found")
        return blob_path.read_text(encoding="utf-8")

    # -- gap detection -------------------------------------------------------

    @staticmethod
    def _detect_gaps(events: List[Event]) -> BranchResult:
        """Check for seq holes and broken parent_event_id chains."""
        if not events:
            return BranchResult(events=events)

        # Check seq monotonicity (holes)
        for i in range(1, len(events)):
            if events[i].seq != events[i - 1].seq + 1:
                return BranchResult(
                    events=events,
                    has_gap=True,
                    gap_event_id=events[i].event_id,
                )

        # Check parent_event_id chain integrity
        event_ids = {ev.event_id for ev in events}
        for ev in events:
            if ev.parent_event_id is not None and ev.parent_event_id not in event_ids:
                return BranchResult(
                    events=events,
                    has_gap=True,
                    gap_event_id=ev.event_id,
                )

        return BranchResult(events=events)
