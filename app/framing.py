"""Byte-stream framing for binary messages split across arbitrary chunks.

Industrial links (UART, TCP...) deliver frames inside a continuous byte
stream that may be fragmented in arbitrary places and interleaved with
noise. This module reconstructs frames from that stream:

* chunks are appended in order and kept in a cross-chunk buffer;
* a frame starts with fixed **sync bytes**;
* an **unsigned length field** sits at a fixed offset from the frame start;
  the total frame length (sync included) is
  ``unsigned(length_field) + length_adjustment``;
* every complete frame is handed to a caller-supplied parser callback —
  parse/checksum failure is *not* fatal: the scanner resynchronizes by
  advancing one byte and searching for the next sync occurrence;
* bytes never consumed (partial sync, partial length field, partial frame)
  are retained for the next chunk and returned as ``incomplete_hex`` at the
  end, never reported as an error.

Hard limits protect the service from a stream that never synchronizes or
advertises an absurd length: the retained buffer may not exceed
``MAX_BUFFER_BYTES`` (1 MiB) and a single frame may not exceed
``MAX_FRAME_BYTES`` (64 KiB); both raise :class:`FramingError`, which the
API maps to HTTP 422.
"""

from dataclasses import dataclass, field
from typing import Callable, List

MAX_BUFFER_BYTES = 1024 * 1024  # 1 MiB of unconsumed buffered bytes
MAX_FRAME_BYTES = 64 * 1024  # 64 KiB per single frame


class FramingError(Exception):
    """A configured framing limit was exceeded (buffer/frame size)."""


@dataclass
class DiscardedRange:
    """A half-open ``[start, end)`` byte span skipped while resynchronizing."""

    start: int  # absolute byte offset (inclusive)
    end: int  # absolute byte offset (exclusive)
    hex: str
    reason: str  # why the bytes were discarded (noise, illegal length, ...)


@dataclass
class StreamFrame:
    """One successfully extracted frame and its parse result."""

    start: int  # absolute byte offset of the sync bytes (inclusive)
    end: int  # absolute byte offset just past the frame (exclusive)
    hex: str
    result: dict


@dataclass
class StreamDeframer:
    """Stateful, chunk-driven frame extractor.

    Offsets returned in frames/discarded ranges are absolute over the
    concatenation of every appended chunk (the first byte of the first
    chunk is offset 0).
    """

    sync: bytes
    length_field_offset: int
    length_field_bytes: int
    length_endianness: str  # "big" | "little"
    length_adjustment: int
    parse_frame: Callable[[bytes], dict]
    max_buffer_bytes: int = MAX_BUFFER_BYTES
    max_frame_bytes: int = MAX_FRAME_BYTES

    _buf: bytearray = field(default_factory=bytearray, init=False)
    # Absolute offset of _buf[0] in the whole chunk stream.
    _base: int = field(default=0, init=False)
    _frames: List[StreamFrame] = field(default_factory=list, init=False)
    _discarded: List[DiscardedRange] = field(default_factory=list, init=False)

    def add_chunk(self, chunk: bytes) -> None:
        """Append one chunk and extract every frame that becomes complete.

        Raises :class:`FramingError` if the retained buffer exceeds the
        configured limit or a length field advertises an oversized frame.
        """
        self._buf.extend(chunk)
        if len(self._buf) > self.max_buffer_bytes:
            raise FramingError(
                f"stream buffer exceeds {self.max_buffer_bytes} bytes "
                f"({len(self._buf)} buffered without a complete frame)"
            )
        self._pump()

    def finish(self) -> dict:
        """Flush after the last chunk.

        Everything still buffered is an unfinished frame (partial sync,
        partial length field or a frame whose bytes have not all arrived)
        and is returned as ``incomplete_hex`` — it is never an error.
        """
        return {
            "frames": [
                {
                    "start": f.start,
                    "end": f.end,
                    "hex": f.hex,
                    "result": f.result,
                }
                for f in self._frames
            ],
            "discarded_ranges": [
                {"start": d.start, "end": d.end, "hex": d.hex, "reason": d.reason}
                for d in self._discarded
            ],
            "incomplete_hex": self._buf.hex(),
            "frames_parsed": len(self._frames),
            "discarded_bytes": sum(d.end - d.start for d in self._discarded),
        }

    # --------------------------------------------------------------- internals

    def _discard(self, n: int, reason: str) -> None:
        """Drop ``n`` bytes from the buffer head as noise/bad frame data."""
        dropped = bytes(self._buf[:n])
        start, end = self._base, self._base + n
        if self._discarded and self._discarded[-1].end == start:
            last = self._discarded[-1]
            last.end = end
            last.hex += dropped.hex()
            if reason not in last.reason:
                last.reason = f"{last.reason}, {reason}"
        else:
            self._discarded.append(
                DiscardedRange(start=start, end=end, hex=dropped.hex(), reason=reason)
            )
        del self._buf[:n]
        self._base += n

    def _pump(self) -> None:
        """Extract all currently complete frames from the buffer."""
        while True:
            # 1. locate the sync bytes; everything before them is noise.
            idx = self._buf.find(self.sync)
            if idx == -1:
                # keep a possible sync prefix at the tail, discard the rest
                keep = len(self.sync) - 1
                if len(self._buf) > keep:
                    self._discard(len(self._buf) - keep, "leading noise")
                return

            if idx > 0:
                self._discard(idx, "leading noise")
                continue

            # 2. need the whole length field before the length is known
            header_end = self.length_field_offset + self.length_field_bytes
            if len(self._buf) < header_end:
                return  # incomplete header; wait for more bytes

            length_value = int.from_bytes(
                self._buf[
                    self.length_field_offset : self.length_field_offset
                    + self.length_field_bytes
                ],
                byteorder=self.length_endianness,
                signed=False,
            )
            total_length = length_value + self.length_adjustment

            # 3. a non-positive total length is illegal (the unsigned field
            #    plus the adjustment cannot describe a frame): the supposed
            #    sync is treated as noise -> resync one byte on.
            if total_length <= 0:
                self._discard(1, "illegal length field")
                continue

            # 4. a frame larger than the hard frame cap is a protocol-level
            #    failure (422), not resynchronizable noise.
            if total_length > self.max_frame_bytes:
                raise FramingError(
                    f"frame length {total_length} bytes exceeds the "
                    f"{self.max_frame_bytes} byte limit "
                    f"(length field value {length_value}, "
                    f"adjustment {self.length_adjustment})"
                )

            # 5. wait for the remaining frame bytes; the buffer cap is
            #    enforced by add_chunk when further data arrives.
            if len(self._buf) < total_length:
                return

            # 6. complete candidate: parse and verify it; on any parse or
            #    checksum failure resync by skipping the sync byte itself.
            frame_bytes = bytes(self._buf[:total_length])
            try:
                result = self.parse_frame(frame_bytes)
            except Exception:
                self._discard(1, "frame parse/checksum failure")
                continue

            self._frames.append(
                StreamFrame(
                    start=self._base,
                    end=self._base + total_length,
                    hex=frame_bytes.hex(),
                    result=result,
                )
            )
            del self._buf[:total_length]
            self._base += total_length
