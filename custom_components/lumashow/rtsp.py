"""A small RTSP client (RTSP 1.0 over TCP, RTP interleaved on the same connection) and the H.264 and H.265 depacketizers (RFC 6184, RFC 7798),
for reading go2rtc's restream of a camera inside Home Assistant and handing whole access units to a device.

Why here and not a library: the stream is read from go2rtc on the loopback, in one well-known shape (one video track, interleaved RTP over
TCP, no authentication); what is needed is a few hundred lines that are easy to read and to test, with no dependency a HACS install would
have to carry. Audio is not read (the device does not play it).

``open`` does DESCRIBE, SETUP and PLAY and returns a session; ``session.units()`` yields access units (``AccessUnit``: the RTP timestamp in
90 kHz ticks, whether it holds a key picture, the NAL units) as they complete; ``session.close()`` sends TEARDOWN. The parameter sets
(SPS, PPS, and VPS for HEVC) come from the SDP's ``sprop-*`` attributes and from the stream itself (go2rtc sends them before every key
picture of an RTSP restream).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)

RTSP_TIMEOUT: Final = 8.0
KEEPALIVE_EVERY: Final = 25.0
MAX_UNIT_BYTES: Final = 4 << 20
START_CODE: Final = b"\x00\x00\x00\x01"


class RtspError(Exception):
    """The server did not do what a client of go2rtc's restream expects."""


@dataclass
class Track:
    codec: str  # "h264" or "h265"
    payload_type: int
    control: str
    clock: int = 90000
    parameter_sets: list[bytes] = field(default_factory=list)


@dataclass
class AccessUnit:
    timestamp: int  # RTP ticks (90 kHz)
    key: bool
    nals: list[bytes]

    def annex_b(self) -> bytes:
        return b"".join(START_CODE + nal for nal in self.nals)


# -- SDP ----------------------------------------------------------------------------------------------------------------------------------


def parse_sdp(text: str, base: str) -> Track:
    """The first H.264 or H.265 video track of an SDP, with its control address resolved against ``base``."""
    lines = [line.strip() for line in text.splitlines()]
    sections: list[list[str]] = [[]]
    for line in lines:
        if line.startswith("m="):
            sections.append([line])
        else:
            sections[-1].append(line)
    session_control = next((line[len("a=control:") :] for line in sections[0] if line.startswith("a=control:")), "")
    for section in sections[1:]:
        media = section[0]
        if not media.startswith("m=video"):
            continue
        payload_types = media.split()[3:]
        rtpmap: dict[int, str] = {}
        fmtp: dict[int, str] = {}
        control = ""
        for line in section[1:]:
            if line.startswith("a=rtpmap:"):
                pt, _, name = line[len("a=rtpmap:") :].partition(" ")
                with contextlib.suppress(ValueError):
                    rtpmap[int(pt)] = name.split("/")[0].upper()
            elif line.startswith("a=fmtp:"):
                pt, _, params = line[len("a=fmtp:") :].partition(" ")
                with contextlib.suppress(ValueError):
                    fmtp[int(pt)] = params
            elif line.startswith("a=control:"):
                control = line[len("a=control:") :]
        for pt in payload_types:
            with contextlib.suppress(ValueError):
                number = int(pt)
                name = rtpmap.get(number, "")
                if name in ("H264", "H265", "HEVC"):
                    codec = "h264" if name == "H264" else "h265"
                    track = Track(codec=codec, payload_type=number, control=_join(base, session_control, control))
                    track.parameter_sets = _sprop(fmtp.get(number, ""))
                    return track
    raise RtspError("the stream has no H.264 or H.265 video")


def _join(base: str, session_control: str, control: str) -> str:
    """RTSP control addresses: absolute, or relative to the session's (itself relative to the request's)."""
    root = base if session_control in ("", "*") else _join(base, "", session_control)
    if control in ("", "*"):
        return root
    if "://" in control:
        return control
    return root.rstrip("/") + "/" + control.lstrip("/")


def _sprop(params: str) -> list[bytes]:
    out: list[bytes] = []
    for item in params.split(";"):
        key, _, value = item.strip().partition("=")
        if key in ("sprop-parameter-sets", "sprop-vps", "sprop-sps", "sprop-pps"):
            for piece in value.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                with contextlib.suppress(ValueError):
                    out.append(base64.b64decode(piece + "=" * (-len(piece) % 4)))
    return out


# -- RTP ----------------------------------------------------------------------------------------------------------------------------------


@dataclass
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    marker: bool
    payload: bytes


def parse_rtp(data: bytes) -> RtpPacket | None:
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    csrc = data[0] & 0x0F
    extension = bool(data[0] & 0x10)
    padding = bool(data[0] & 0x20)
    at = 12 + 4 * csrc
    if extension:
        if len(data) < at + 4:
            return None
        at += 4 + 4 * int.from_bytes(data[at + 2 : at + 4], "big")
    end = len(data)
    if padding and end > at:
        end -= data[-1]
    if end < at:
        return None
    return RtpPacket(
        payload_type=data[1] & 0x7F,
        sequence=int.from_bytes(data[2:4], "big"),
        timestamp=int.from_bytes(data[4:8], "big"),
        marker=bool(data[1] & 0x80),
        payload=data[at:end],
    )


def _h264_is_key(nal: bytes) -> bool:
    return bool(nal) and nal[0] & 0x1F == 5


def _h264_is_parameter_set(nal: bytes) -> bool:
    return bool(nal) and nal[0] & 0x1F in (7, 8)


def _h265_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F if nal else 0


def _h265_is_key(nal: bytes) -> bool:
    return 16 <= _h265_type(nal) <= 21


def _h265_is_parameter_set(nal: bytes) -> bool:
    return _h265_type(nal) in (32, 33, 34)


class Depacketizer:
    """RTP payloads in, access units out. One unit is every NAL unit with the same timestamp, complete at the marker (or when the
    timestamp moves on). A fragmented NAL unit (FU-A, FU) is put back together; one with a lost start is dropped with its unit marked."""

    def __init__(self, codec: str) -> None:
        self.codec = codec
        self._nals: list[bytes] = []
        self._timestamp: int | None = None
        self._fragment: bytearray | None = None
        self._expected: int | None = None
        self.lost = 0
        self._damaged = False

    def push(self, packet: RtpPacket) -> list[AccessUnit]:
        out: list[AccessUnit] = []
        if self._expected is not None and packet.sequence != self._expected:
            self.lost += 1
            self._fragment = None
            self._damaged = True
        self._expected = (packet.sequence + 1) & 0xFFFF
        if self._timestamp is not None and packet.timestamp != self._timestamp:
            out.extend(self._flush())
        self._timestamp = packet.timestamp
        for nal in self._nals_of(packet.payload):
            if nal:
                self._nals.append(nal)
        if packet.marker:
            out.extend(self._flush())
        return out

    def _flush(self) -> list[AccessUnit]:
        if not self._nals or self._timestamp is None:
            self._nals = []
            self._damaged = False
            return []
        nals, self._nals = self._nals, []
        damaged, self._damaged = self._damaged, False
        timestamp = self._timestamp
        if damaged:
            return []
        is_key = _h264_is_key if self.codec == "h264" else _h265_is_key
        return [AccessUnit(timestamp=timestamp, key=any(is_key(n) for n in nals), nals=nals)]

    def _nals_of(self, payload: bytes) -> list[bytes]:
        if not payload:
            return []
        if self.codec == "h264":
            kind = payload[0] & 0x1F
            if 1 <= kind <= 23:
                return [payload]
            if kind == 24:  # STAP-A
                return _aggregated(payload[1:])
            if kind == 28:  # FU-A
                head = bytes([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
                return self._fragment_of(payload[2:], start=bool(payload[1] & 0x80), end=bool(payload[1] & 0x40), head=head)
            return []
        kind = _h265_type(payload)
        if kind == 48:  # AP
            return _aggregated(payload[2:])
        if kind == 49 and len(payload) >= 3:  # FU
            fu = payload[2]
            head = bytes([(payload[0] & 0x81) | ((fu & 0x3F) << 1), payload[1]])
            return self._fragment_of(payload[3:], start=bool(fu & 0x80), end=bool(fu & 0x40), head=head)
        if kind < 48:
            return [payload]
        return []

    def _fragment_of(self, piece: bytes, *, start: bool, end: bool, head: bytes) -> list[bytes]:
        if start:
            self._fragment = bytearray(head)
        if self._fragment is None:
            self._damaged = True
            return []
        self._fragment += piece
        if len(self._fragment) > MAX_UNIT_BYTES:
            self._fragment = None
            self._damaged = True
            return []
        if end:
            whole, self._fragment = bytes(self._fragment), None
            return [whole]
        return []


def _aggregated(data: bytes) -> list[bytes]:
    out: list[bytes] = []
    at = 0
    while at + 2 <= len(data):
        size = int.from_bytes(data[at : at + 2], "big")
        at += 2
        out.append(data[at : at + size])
        at += size
    return out


def parameter_sets_of(codec: str, nals: list[bytes]) -> list[bytes]:
    pick = _h264_is_parameter_set if codec == "h264" else _h265_is_parameter_set
    return [n for n in nals if pick(n)]


# -- the client ---------------------------------------------------------------------------------------------------------------------------


class Session:
    """One RTSP session, playing; ``units()`` reads it until it ends or ``close()``."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, url: str, track: Track, session_id: str) -> None:
        self._reader = reader
        self._writer = writer
        self.url = url
        self.track = track
        self.session_id = session_id
        self._cseq = 10
        self.closed = False
        self.packets = 0
        self.depacketizer = Depacketizer(track.codec)

    async def units(self):
        """Access units as they complete; a keep-alive request goes out every ``KEEPALIVE_EVERY`` seconds of quiet."""
        last_alive = asyncio.get_running_loop().time()
        while not self.closed:
            try:
                head = await asyncio.wait_for(self._reader.readexactly(1), RTSP_TIMEOUT)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            except TimeoutError as err:
                raise RtspError("the stream went quiet") from err
            if head == b"$":
                channel_and_length = await self._reader.readexactly(3)
                length = int.from_bytes(channel_and_length[1:3], "big")
                data = await self._reader.readexactly(length)
                if channel_and_length[0] != 0:
                    continue
                packet = parse_rtp(data)
                if packet is None or packet.payload_type != self.track.payload_type:
                    continue
                self.packets += 1
                for unit in self.depacketizer.push(packet):
                    yield unit
            else:
                # an RTSP reply in the stream (to a keep-alive): read it whole and go on
                await self._read_response(first=head)
            now = asyncio.get_running_loop().time()
            if now - last_alive >= KEEPALIVE_EVERY:
                last_alive = now
                await self._send("GET_PARAMETER", self.url, {"Session": self.session_id})

    async def _send(self, method: str, url: str, headers: dict[str, str]) -> int:
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}", "User-Agent: lumashow-ha"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self._writer.drain()
        return self._cseq

    async def _read_response(self, first: bytes = b"") -> tuple[int, dict[str, str], bytes]:
        head = first
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(self._reader.read(1024), RTSP_TIMEOUT)
            if not chunk:
                raise RtspError("the server closed the connection")
            head += chunk
            if len(head) > 65536:
                raise RtspError("an answer that does not end")
        text, _, rest = head.partition(b"\r\n\r\n")
        lines = text.decode(errors="replace").split("\r\n")
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError) as err:
            raise RtspError(f"not an RTSP answer: {lines[0]!r}") from err
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        body = rest
        while len(body) < length:
            more = await asyncio.wait_for(self._reader.read(length - len(body)), RTSP_TIMEOUT)
            if not more:
                raise RtspError("the server closed the connection")
            body += more
        # bytes read past the body belong to the stream: put them back in front
        if len(body) > length:
            self._reader._buffer[0:0] = body[length:]  # asyncio gives no unread; the stream is ours
            body = body[:length]
        return status, headers, body

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._send("TEARDOWN", self.url, {"Session": self.session_id}), 1.0)
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


async def open(url: str, timeout: float = RTSP_TIMEOUT) -> Session:
    """DESCRIBE, SETUP (interleaved TCP) and PLAY the first video track of ``url``."""
    parts = urlsplit(url)
    if parts.scheme != "rtsp" or not parts.hostname:
        raise RtspError(f"not an rtsp address: {url}")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, parts.port or 554), timeout)
    session = Session(reader, writer, url, Track(codec="", payload_type=0, control=url), "")
    try:
        await session._send("DESCRIBE", url, {"Accept": "application/sdp"})
        status, headers, body = await session._read_response()
        if status != 200:
            raise RtspError(f"DESCRIBE answered {status}")
        base = headers.get("content-base") or headers.get("content-location") or url
        track = parse_sdp(body.decode(errors="replace"), base)
        session.track = track
        session.depacketizer = Depacketizer(track.codec)
        await session._send("SETUP", track.control, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        status, headers, _ = await session._read_response()
        if status != 200:
            raise RtspError(f"SETUP answered {status}")
        session.session_id = headers.get("session", "").split(";")[0].strip()
        if not session.session_id:
            raise RtspError("SETUP gave no session")
        await session._send("PLAY", url, {"Session": session.session_id, "Range": "npt=0.000-"})
        status, _, _ = await session._read_response()
        if status != 200:
            raise RtspError(f"PLAY answered {status}")
    except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError) as err:
        writer.close()
        raise RtspError(f"the stream could not be opened: {err!r}") from err
    except RtspError:
        writer.close()
        raise
    return session
