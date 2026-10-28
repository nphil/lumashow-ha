"""A fake RTSP server playing ``fixtures/stream.h264`` (Annex B, 320x180, 15 fps, two seconds) as go2rtc's restream would: one H.264 track,
interleaved RTP over the RTSP connection, single-NAL and FU-A packets, the marker on a picture's last packet. Keeps when each picture was
sent, for the latency a test measures."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "stream.h264"
PAYLOAD_TYPE = 96
CLOCK = 90000
FPS = 15
MTU = 1400


def nal_units(data: bytes) -> list[bytes]:
    out: list[bytes] = []
    at = data.find(b"\x00\x00\x01")
    while at >= 0:
        at += 3
        nxt = data.find(b"\x00\x00\x01", at)
        end = len(data) if nxt < 0 else nxt
        nal = data[at:end]
        while nal.endswith(b"\x00"):
            nal = nal[:-1]
        out.append(nal)
        at = nxt
    return out


def access_units(nals: list[bytes]) -> list[list[bytes]]:
    """Pictures: a new one starts at an SPS, an SEI, or a slice whose first macroblock is 0 (ue(v) "1" is the first bit)."""
    units: list[list[bytes]] = []
    for nal in nals:
        kind = nal[0] & 0x1F
        starts = kind in (6, 7, 9) or (kind in (1, 5) and bool(nal[1] & 0x80))
        if not units or (starts and any(n[0] & 0x1F in (1, 5) for n in units[-1])):
            units.append([nal])
        else:
            units[-1].append(nal)
    return units


def rtp_packets(unit: list[bytes], seq: int, timestamp: int) -> tuple[list[bytes], int]:
    packets: list[bytes] = []
    payloads: list[bytes] = []
    for nal in unit:
        if len(nal) <= MTU:
            payloads.append(nal)
            continue
        head = nal[0]
        body = nal[1:]
        pieces = [body[i : i + MTU] for i in range(0, len(body), MTU)]
        for i, piece in enumerate(pieces):
            fu = (0x80 if i == 0 else 0) | (0x40 if i == len(pieces) - 1 else 0) | (head & 0x1F)
            payloads.append(bytes([(head & 0xE0) | 28, fu]) + piece)
    for i, payload in enumerate(payloads):
        marker = 0x80 if i == len(payloads) - 1 else 0
        header = bytes([0x80, marker | PAYLOAD_TYPE]) + (seq & 0xFFFF).to_bytes(2, "big") + timestamp.to_bytes(4, "big") + b"\x00\x00\x00\x01"
        packets.append(header + payload)
        seq += 1
    return packets, seq


class FakeRtsp:
    def __init__(self, *, pace: float = 1 / FPS, loop_forever: bool = False) -> None:
        self.pace = pace
        self.loop_forever = loop_forever
        self.port = 0
        self.requests: list[str] = []
        self.sent_at: dict[int, float] = {}
        self.units = access_units(nal_units(FIXTURE.read_bytes()))
        self.sps = next(n for n in nal_units(FIXTURE.read_bytes()) if n[0] & 0x1F == 7)
        self.pps = next(n for n in nal_units(FIXTURE.read_bytes()) if n[0] & 0x1F == 8)
        self._server: asyncio.Server | None = None
        self._players: set[asyncio.Task[None]] = set()
        self.torn_down = asyncio.Event()

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self.port}/camera.front_door"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in list(self._players):
            task.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def sdp(self) -> str:
        sprop = base64.b64encode(self.sps).decode() + "," + base64.b64encode(self.pps).decode()
        return (
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=go2rtc\r\nt=0 0\r\n"
            "m=video 0 RTP/AVP 96\r\na=control:trackID=0\r\na=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 packetization-mode=1;profile-level-id=42e01e;sprop-parameter-sets={sprop}\r\n"
            "m=audio 0 RTP/AVP 97\r\na=control:trackID=1\r\na=rtpmap:97 opus/48000/2\r\n"
        )

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        player: asyncio.Task[None] | None = None
        try:
            while True:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = await reader.read(1024)
                    if not chunk:
                        return
                    head += chunk
                text, _, _ = head.partition(b"\r\n\r\n")
                lines = text.decode().split("\r\n")
                method, url, _ = lines[0].split(" ", 2)
                headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
                self.requests.append(method)
                cseq = headers.get("cseq", "0")
                if method == "DESCRIBE":
                    body = self.sdp().encode()
                    writer.write(
                        f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Base: {url}/\r\nContent-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                elif method == "SETUP":
                    writer.write(
                        f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 4242;timeout=60\r\nTransport: {headers.get('transport', '')}\r\n\r\n".encode()
                    )
                elif method == "PLAY":
                    writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 4242\r\nRange: npt=0.000-\r\n\r\n".encode())
                    player = asyncio.get_running_loop().create_task(self._play(writer))
                    self._players.add(player)
                elif method == "TEARDOWN":
                    writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 4242\r\n\r\n".encode())
                    self.torn_down.set()
                    await writer.drain()
                    return
                else:
                    writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 4242\r\n\r\n".encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            return
        finally:
            if player is not None:
                player.cancel()
                self._players.discard(player)
            writer.close()

    async def _play(self, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        seq = 1
        index = 0
        try:
            while True:
                for unit in self.units:
                    timestamp = (index * CLOCK // FPS) & 0xFFFFFFFF
                    packets, seq = rtp_packets(unit, seq, timestamp)
                    self.sent_at[timestamp] = loop.time()
                    for packet in packets:
                        writer.write(b"$\x00" + len(packet).to_bytes(2, "big") + packet)
                    await writer.drain()
                    index += 1
                    if self.pace:
                        await asyncio.sleep(self.pace)
                if not self.loop_forever:
                    break
            writer.close()
        except (ConnectionError, OSError, asyncio.CancelledError):
            return
