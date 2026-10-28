"""The RTSP client's parts: the SDP's track and control address, RTP headers, and the H.264/H.265 depacketizers (single, aggregated,
fragmented, a lost fragment)."""

from __future__ import annotations

import pytest

from custom_components.lumashow import rtsp
from custom_components.lumashow.rtsp import AccessUnit, Depacketizer, RtpPacket, parse_rtp, parse_sdp


def test_the_sdp_gives_the_video_track_its_control_address_and_parameter_sets() -> None:
    sdp = (
        "v=0\r\ns=x\r\na=control:*\r\n"
        "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 opus/48000/2\r\na=control:trackID=1\r\n"
        "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1;sprop-parameter-sets=Z0IAHtoCgL/lwA==,aM4G4g==\r\na=control:trackID=0\r\n"
    )
    track = parse_sdp(sdp, "rtsp://127.0.0.1:18554/cam/")
    assert track.codec == "h264" and track.payload_type == 96 and track.control == "rtsp://127.0.0.1:18554/cam/trackID=0"
    assert [s[0] & 0x1F for s in track.parameter_sets] == [7, 8]
    hevc = "m=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=control:rtsp://host/abs\r\na=fmtp:98 sprop-vps=QAEMAf//;sprop-sps=QgEB;sprop-pps=RAHA\r\n"
    track = parse_sdp(hevc, "rtsp://host/cam")
    assert track.codec == "h265" and track.control == "rtsp://host/abs" and len(track.parameter_sets) == 3
    with pytest.raises(rtsp.RtspError):
        parse_sdp("m=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\n", "rtsp://host/cam")


def test_rtp_headers_with_csrcs_extension_and_padding_are_read() -> None:
    header = bytes([0xB1, 0x80 | 96]) + (7).to_bytes(2, "big") + (9000).to_bytes(4, "big") + b"\x00\x00\x00\x01"
    csrc = b"\x00\x00\x00\x02"
    extension = b"\xbe\xde\x00\x01\x01\x02\x03\x04"
    payload = b"\x65abc"
    packet = parse_rtp(header + csrc + extension + payload + b"\x00\x00\x03")
    assert packet == RtpPacket(payload_type=96, sequence=7, timestamp=9000, marker=True, payload=payload)
    assert parse_rtp(b"\x00" * 12) is None and parse_rtp(b"\x80") is None


def _pkt(payload: bytes, seq: int, ts: int, marker: bool = False) -> RtpPacket:
    return RtpPacket(payload_type=96, sequence=seq, timestamp=ts, marker=marker, payload=payload)


def test_h264_pictures_are_put_together_from_single_aggregated_and_fragmented_packets() -> None:
    d = Depacketizer("h264")
    sps, pps = b"\x67\x42\x00\x1e", b"\x68\xce\x06\xe2"
    stap = b"\x18" + len(sps).to_bytes(2, "big") + sps + len(pps).to_bytes(2, "big") + pps
    idr = b"\x65" + bytes(range(256)) * 12
    body = idr[1:]
    fu = [bytes([0x7C, (0x80 if i == 0 else 0) | (0x40 if i == 2 else 0) | 5]) + body[i * 1024 : (i + 1) * 1024] for i in range(3)]
    assert d.push(_pkt(stap, 1, 0)) == []
    assert d.push(_pkt(fu[0], 2, 0)) == [] and d.push(_pkt(fu[1], 3, 0)) == []
    assert d.push(_pkt(fu[2], 4, 0, marker=True)) == [AccessUnit(timestamp=0, key=True, nals=[sps, pps, idr])]
    # a non-key picture, complete at the next timestamp when the marker was lost
    assert d.push(_pkt(b"\x41pic", 5, 6000)) == []
    assert d.push(_pkt(b"\x41next", 6, 12000)) == [AccessUnit(timestamp=6000, key=False, nals=[b"\x41pic"])]
    # a lost fragment drops the picture it was part of, nothing else
    assert d.push(_pkt(fu[0], 7, 18000)) == [AccessUnit(timestamp=12000, key=False, nals=[b"\x41next"])]
    assert d.push(_pkt(fu[2], 9, 18000, marker=True)) == []
    assert d.lost == 1
    assert d.push(_pkt(b"\x41after", 10, 24000, marker=True)) == [AccessUnit(timestamp=24000, key=False, nals=[b"\x41after"])]
    assert AccessUnit(timestamp=0, key=False, nals=[b"\x41a", b"\x41b"]).annex_b() == b"\x00\x00\x00\x01\x41a\x00\x00\x00\x01\x41b"


def test_h265_pictures_are_put_together_too() -> None:
    d = Depacketizer("h265")
    vps, sps, pps = b"\x40\x01\x0c", b"\x42\x01\x01", b"\x44\x01\xc0"
    ap = b"\x60\x01" + b"".join(len(n).to_bytes(2, "big") + n for n in (vps, sps, pps))
    idr = b"\x26\x01" + bytes(3000)  # type 19 (IDR_W_RADL)
    body = idr[2:]
    fu = [b"\x62\x01" + bytes([(0x80 if i == 0 else 0) | (0x40 if i == 1 else 0) | 19]) + body[i * 1500 : (i + 1) * 1500] for i in range(2)]
    assert d.push(_pkt(ap, 1, 0)) == [] and d.push(_pkt(fu[0], 2, 0)) == []
    assert d.push(_pkt(fu[1], 3, 0, marker=True)) == [AccessUnit(timestamp=0, key=True, nals=[vps, sps, pps, idr])]
    assert rtsp.parameter_sets_of("h265", [vps, sps, pps, idr]) == [vps, sps, pps]
