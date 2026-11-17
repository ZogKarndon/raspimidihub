"""Minimal OSC 1.0 codec and the MIDI ↔ OSC message map.

UDP only (no SLIP). Enough of the spec for the hub's Ethernet
transport: strings, int32, float32, float64, blobs, the 4-byte MIDI
typetag, and true/false/nil. Bundles are unpacked on receive so a
peer that wraps replies still works. Outbound traffic is one message
per datagram.

MIDI on the wire becomes named messages a SuperCollider OSCFunc can
match directly:

    /midi/note_on   i i i    channel note velocity
    /midi/note_off  i i i    channel note velocity
    /midi/cc        i i i    channel controller value
    /midi/program   i i      channel program
    /midi/pressure  i i      channel pressure
    /midi/key_pressure  i i i  channel note pressure
    /midi/pitchbend i i      channel unsigned-14-bit (center 8192)
    /midi/clock
    /midi/start
    /midi/continue
    /midi/stop
    /midi/songpos   i        14-bit song position
    /midi/sysex     b        raw SysEx bytes, including F0/F7 when present

Channel is 0–15. Inbound, the same addresses are accepted, and so is
a single `/midi` message with the OSC MIDI typetag (`m`, four bytes:
port, status, data1, data2).
"""

from __future__ import annotations

import struct

_INT = struct.Struct(">i")
_FLOAT = struct.Struct(">f")
_DOUBLE = struct.Struct(">d")


class OscError(ValueError):
    """A datagram was not a decodable OSC packet."""


def _pad4(data: bytes) -> bytes:
    extra = (4 - (len(data) % 4)) % 4
    if not extra:
        return data
    return data + (b"\x00" * extra)


def _encode_string(text: str) -> bytes:
    return _pad4(text.encode("utf-8") + b"\x00")


def _decode_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise OscError("unterminated OSC string")
    text = data[offset:end].decode("utf-8", errors="replace")
    # Advance to the next 4-byte boundary after the NUL.
    nxt = end + 1
    nxt += (4 - (nxt % 4)) % 4
    return text, nxt


def encode_message(address: str, args: list | tuple = ()) -> bytes:
    """Encode one OSC message. Floats go out as float64 so SuperCollider
    sample-rate fields round-trip; ints are int32."""
    if not address.startswith("/"):
        raise OscError(f"OSC address must start with /: {address!r}")
    tags = [","]
    body = bytearray()
    for arg in args:
        if isinstance(arg, bool):
            tags.append("T" if arg else "F")
        elif isinstance(arg, int):
            if not -2147483648 <= arg <= 2147483647:
                raise OscError(f"OSC int out of range: {arg}")
            tags.append("i")
            body += _INT.pack(arg)
        elif isinstance(arg, float):
            tags.append("d")
            body += _DOUBLE.pack(arg)
        elif isinstance(arg, str):
            tags.append("s")
            body += _encode_string(arg)
        elif isinstance(arg, (bytes, bytearray)):
            raw = bytes(arg)
            tags.append("b")
            body += _INT.pack(len(raw))
            body += _pad4(raw)
        elif isinstance(arg, MidiAtom):
            tags.append("m")
            body += bytes(arg.four)
        else:
            raise OscError(f"unsupported OSC argument {type(arg).__name__}")
    return _encode_string(address) + _encode_string("".join(tags)) + bytes(body)


class MidiAtom:
    """Four-byte OSC MIDI typetag payload: port, status, data1, data2."""

    def __init__(self, port: int, status: int, data1: int, data2: int):
        self.four = bytes((port & 0xFF, status & 0xFF, data1 & 0xFF, data2 & 0xFF))

    def __repr__(self) -> str:
        return f"MidiAtom({self.four.hex()})"


def decode_packet(data: bytes) -> list[tuple[str, list]]:
    """Return every message in a packet (a bare message, or a bundle)."""
    if not data:
        raise OscError("empty OSC packet")
    if data.startswith(b"#bundle"):
        return _decode_bundle(data)
    address, args = _decode_message(data, 0)[:2]
    return [(address, args)]


def _decode_bundle(data: bytes) -> list[tuple[str, list]]:
    # "#bundle" + NUL + padding, then an 8-byte timetag, then size-prefixed
    # elements. Nested bundles are flattened.
    if len(data) < 16:
        raise OscError("truncated OSC bundle")
    offset = 16  # 8-byte header padded to 8, plus 8-byte timetag
    out: list[tuple[str, list]] = []
    while offset + 4 <= len(data):
        (size,) = _INT.unpack_from(data, offset)
        offset += 4
        if size < 0 or offset + size > len(data):
            raise OscError("truncated OSC bundle element")
        element = data[offset:offset + size]
        offset += size
        if element.startswith(b"#bundle"):
            out.extend(_decode_bundle(element))
        else:
            address, args = _decode_message(element, 0)[:2]
            out.append((address, args))
    return out


def _decode_message(data: bytes, offset: int) -> tuple[str, list, int]:
    address, offset = _decode_string(data, offset)
    if not address.startswith("/"):
        raise OscError(f"not an OSC address: {address!r}")
    tags, offset = _decode_string(data, offset)
    if not tags.startswith(","):
        raise OscError("OSC typetag string must start with a comma")
    args: list = []
    for tag in tags[1:]:
        if tag == "i":
            if offset + 4 > len(data):
                raise OscError("truncated OSC int")
            (value,) = _INT.unpack_from(data, offset)
            offset += 4
            args.append(value)
        elif tag == "f":
            if offset + 4 > len(data):
                raise OscError("truncated OSC float")
            (value,) = _FLOAT.unpack_from(data, offset)
            offset += 4
            args.append(value)
        elif tag == "d":
            if offset + 8 > len(data):
                raise OscError("truncated OSC double")
            (value,) = _DOUBLE.unpack_from(data, offset)
            offset += 8
            args.append(value)
        elif tag == "s":
            value, offset = _decode_string(data, offset)
            args.append(value)
        elif tag == "b":
            if offset + 4 > len(data):
                raise OscError("truncated OSC blob")
            (size,) = _INT.unpack_from(data, offset)
            offset += 4
            if size < 0 or offset + size > len(data):
                raise OscError("truncated OSC blob")
            blob = data[offset:offset + size]
            offset += size
            offset += (4 - (offset % 4)) % 4
            args.append(blob)
        elif tag == "m":
            if offset + 4 > len(data):
                raise OscError("truncated OSC midi")
            raw = data[offset:offset + 4]
            offset += 4
            args.append(MidiAtom(raw[0], raw[1], raw[2], raw[3]))
        elif tag in ("T", "F", "N", "I"):
            args.append({"T": True, "F": False, "N": None, "I": float("inf")}[tag])
        else:
            raise OscError(f"unsupported OSC typetag {tag!r}")
    return address, args, offset


def _clamp7(value: int) -> int:
    if value < 0:
        return 0
    if value > 127:
        return 127
    return value


def _as_int(value) -> int | None:
    if isinstance(value, bool) or isinstance(value, MidiAtom):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def midi_bytes_to_messages(msg: bytes) -> list[tuple[str, list]]:
    """Map one MIDI message to OSC messages. Returns [] when the
    message has no OSC form (empty, or active sensing)."""
    if not msg or not (msg[0] & 0x80):
        return []
    status = msg[0]
    if status == 0xFE:
        return []
    if status == 0xF0:
        return [("/midi/sysex", [bytes(msg)])]
    if status == 0xF8:
        return [("/midi/clock", [])]
    if status == 0xFA:
        return [("/midi/start", [])]
    if status == 0xFB:
        return [("/midi/continue", [])]
    if status == 0xFC:
        return [("/midi/stop", [])]
    if status == 0xF2 and len(msg) >= 3:
        pos = (msg[1] & 0x7F) | ((msg[2] & 0x7F) << 7)
        return [("/midi/songpos", [pos])]
    if status < 0xF0 and len(msg) >= 2:
        ch = status & 0x0F
        kind = status & 0xF0
        if kind in (0x80, 0x90, 0xA0, 0xB0) and len(msg) >= 3:
            a, b = _clamp7(msg[1]), _clamp7(msg[2])
            address = {
                0x80: "/midi/note_off",
                0x90: "/midi/note_on",
                0xA0: "/midi/key_pressure",
                0xB0: "/midi/cc",
            }[kind]
            return [(address, [ch, a, b])]
        if kind == 0xC0:
            return [("/midi/program", [ch, _clamp7(msg[1])])]
        if kind == 0xD0:
            return [("/midi/pressure", [ch, _clamp7(msg[1])])]
        if kind == 0xE0 and len(msg) >= 3:
            bend = (msg[1] & 0x7F) | ((msg[2] & 0x7F) << 7)
            return [("/midi/pitchbend", [ch, bend])]
    return []


def message_to_midi(address: str, args: list) -> bytes | None:
    """Map one OSC message back to a MIDI byte string, or None."""
    if address == "/midi" and args and isinstance(args[0], MidiAtom):
        raw = args[0].four
        status = raw[1]
        if status >= 0xF8:
            return bytes((status,))
        if status >= 0xF0:
            return None
        kind = status & 0xF0
        if kind in (0xC0, 0xD0):
            return bytes((status, raw[2] & 0x7F))
        return bytes((status, raw[2] & 0x7F, raw[3] & 0x7F))

    def chan_byte(base: int) -> int | None:
        ch = _as_int(args[0]) if args else None
        if ch is None or not 0 <= ch <= 15:
            return None
        return base | ch

    if address == "/midi/note_on" and len(args) >= 3:
        st = chan_byte(0x90)
        note, vel = _as_int(args[1]), _as_int(args[2])
        if st is None or note is None or vel is None:
            return None
        return bytes((st, _clamp7(note), _clamp7(vel)))
    if address == "/midi/note_off" and len(args) >= 2:
        st = chan_byte(0x80)
        note = _as_int(args[1])
        vel = _as_int(args[2]) if len(args) >= 3 else 0
        if st is None or note is None or vel is None:
            return None
        return bytes((st, _clamp7(note), _clamp7(vel)))
    if address == "/midi/cc" and len(args) >= 3:
        st = chan_byte(0xB0)
        cc, val = _as_int(args[1]), _as_int(args[2])
        if st is None or cc is None or val is None:
            return None
        return bytes((st, _clamp7(cc), _clamp7(val)))
    if address == "/midi/key_pressure" and len(args) >= 3:
        st = chan_byte(0xA0)
        note, val = _as_int(args[1]), _as_int(args[2])
        if st is None or note is None or val is None:
            return None
        return bytes((st, _clamp7(note), _clamp7(val)))
    if address == "/midi/program" and len(args) >= 2:
        st = chan_byte(0xC0)
        prog = _as_int(args[1])
        if st is None or prog is None:
            return None
        return bytes((st, _clamp7(prog)))
    if address == "/midi/pressure" and len(args) >= 2:
        st = chan_byte(0xD0)
        val = _as_int(args[1])
        if st is None or val is None:
            return None
        return bytes((st, _clamp7(val)))
    if address == "/midi/pitchbend" and len(args) >= 2:
        st = chan_byte(0xE0)
        bend = _as_int(args[1])
        if st is None or bend is None:
            return None
        bend = max(0, min(0x3FFF, bend))
        return bytes((st, bend & 0x7F, (bend >> 7) & 0x7F))
    if address == "/midi/clock":
        return b"\xf8"
    if address == "/midi/start":
        return b"\xfa"
    if address == "/midi/continue":
        return b"\xfb"
    if address == "/midi/stop":
        return b"\xfc"
    if address == "/midi/songpos" and args:
        pos = _as_int(args[0])
        if pos is None:
            return None
        pos &= 0x3FFF
        return bytes((0xF2, pos & 0x7F, (pos >> 7) & 0x7F))
    if address == "/midi/sysex" and args and isinstance(args[0], (bytes, bytearray)):
        blob = bytes(args[0])
        if blob.startswith(b"\xf0"):
            return blob
    return None


def parse_status_reply(args: list) -> dict:
    """Pull the fields SuperCollider's `/status.reply` carries.

    Layout (scsynth): unused int, ugens, synths, groups, synthdefs,
    average CPU, peak CPU, nominal sample rate, actual sample rate.
    Missing fields stay None — a short reply still counts as a server.
    """
    def at(i):
        return args[i] if i < len(args) else None

    return {
        "ugens": at(1),
        "synths": at(2),
        "groups": at(3),
        "synthdefs": at(4),
        "avg_cpu": at(5),
        "peak_cpu": at(6),
        "nominal_sample_rate": at(7),
        "sample_rate": at(8) if len(args) > 8 else at(7),
    }
