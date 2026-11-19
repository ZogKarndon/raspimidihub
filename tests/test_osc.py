"""OSC codec, SuperCollider probe targets, and the UDP transport."""

import asyncio
import struct

from raspimidihub import osc
from raspimidihub.config import Config
from raspimidihub.osc_transport import (
    OscTransport,
    is_wired_interface,
    parse_peer,
    probe_targets,
)


def test_midi_note_roundtrip():
    messages = osc.midi_bytes_to_messages(bytes((0x90, 60, 100)))
    assert messages == [("/midi/note_on", [0, 60, 100])]
    packet = osc.encode_message(*messages[0])
    decoded = osc.decode_packet(packet)
    assert decoded == [("/midi/note_on", [0, 60, 100])]
    assert osc.message_to_midi(*decoded[0]) == bytes((0x90, 60, 100))


def test_midi_typetag_inbound():
    packet = osc.encode_message("/midi", [osc.MidiAtom(0, 0xB0, 7, 64)])
    address, args = osc.decode_packet(packet)[0]
    assert osc.message_to_midi(address, args) == bytes((0xB0, 7, 64))


def test_status_reply_doubles():
    # scsynth's /status.reply ends in two float64 sample rates.
    def osc_str(text: str) -> bytes:
        raw = text.encode() + b"\x00"
        raw += b"\x00" * ((4 - len(raw) % 4) % 4)
        return raw

    packet = (
        osc_str("/status.reply")
        + osc_str(",iiiiiffdd")
        + struct.pack(">iiiii", 1, 4, 2, 1, 3)
        + struct.pack(">ff", 1.5, 3.25)
        + struct.pack(">dd", 48000.0, 44100.0)
    )
    address, args = osc.decode_packet(packet)[0]
    assert address == "/status.reply"
    fields = osc.parse_status_reply(args)
    assert fields["ugens"] == 4
    assert fields["synths"] == 2
    assert fields["sample_rate"] == 44100.0
    assert fields["nominal_sample_rate"] == 48000.0


def test_bundle_is_unpacked():
    inner = osc.encode_message("/midi/clock", [])
    packet = b"#bundle\x00" + struct.pack(">Q", 1) + struct.pack(">i", len(inner)) + inner
    assert osc.decode_packet(packet) == [("/midi/clock", [])]


def test_probe_skips_wifi_and_finds_usb_gadget_peer():
    ifaces = [
        {"interface": "wlan0", "addresses": ["192.168.4.1/24"], "gateway": ""},
        {"interface": "eth0", "addresses": ["10.1.1.20/24"], "gateway": "10.1.1.1"},
        {"interface": "usb0", "addresses": ["192.168.7.2/24"], "gateway": ""},
        {"interface": "lo", "addresses": ["127.0.0.1/8"], "gateway": ""},
    ]
    arp = [("10.1.1.50", "eth0", 0x2), ("192.168.4.10", "wlan0", 0x2)]
    targets = probe_targets(ifaces, ["sc.local:57120"], 57110, arp)
    hosts = {host for host, _port in targets}
    assert "192.168.4.255" not in hosts          # wlan broadcast
    assert "192.168.4.10" not in hosts           # wlan neighbour
    assert "10.1.1.255" in hosts                 # eth broadcast
    assert "10.1.1.1" in hosts                   # gateway
    assert "10.1.1.50" in hosts                  # ARP on eth0
    assert "192.168.7.255" in hosts              # usb gadget subnet
    assert "192.168.7.1" in hosts                # the other end of .2
    assert ("sc.local", 57120) in targets
    assert not is_wired_interface("wlan0")
    assert is_wired_interface("enxaabbccddeeff")
    assert parse_peer("bad host", 57110) is None


def test_link_local_ethernet_is_probed():
    ifaces = [{
        "interface": "eth0",
        "addresses": ["169.254.40.12/16"],
        "gateway": "",
    }]
    targets = probe_targets(ifaces, [], 57110, [])
    assert ("169.254.255.255", 57110) in targets


def test_status_reply_records_a_server_and_ignores_loopback():
    cfg = Config()
    transport = OscTransport(engine=None, config=cfg, server=None)
    reply = osc.encode_message(
        "/status.reply", [1, 8, 1, 1, 2, 0.5, 1.0, 48000.0, 48000.0])
    transport._on_datagram(reply, ("192.168.7.1", 57110))
    servers = transport.live_servers()
    assert len(servers) == 1
    assert servers[0]["host"] == "192.168.7.1"
    assert servers[0]["port"] == 57110
    assert servers[0]["ugens"] == 8
    assert transport.current_target() == ("192.168.7.1", 57110)

    transport._on_datagram(reply, ("127.0.0.1", 57110))
    assert len(transport.live_servers()) == 1


def test_explicit_target_wins_over_discovery():
    cfg = Config()
    cfg.data["osc"] = dict(cfg.osc)
    cfg.data["osc"]["target"] = "10.0.0.9"
    transport = OscTransport(engine=None, config=cfg, server=None)
    reply = osc.encode_message("/status.reply", [1, 1, 0, 1, 0, 0.0, 0.0, 48000.0])
    transport._on_datagram(reply, ("192.168.7.1", 57110))
    assert transport.current_target() == ("10.0.0.9", 57110)


def test_two_servers_need_an_explicit_target():
    cfg = Config()
    cfg.data["osc"] = dict(cfg.osc)
    cfg.data["osc"]["manual_peers"] = list(cfg.data["osc"].get("manual_peers") or [])
    transport = OscTransport(engine=None, config=cfg, server=None)
    reply = osc.encode_message("/status.reply", [1, 1, 0, 1, 0, 0.0, 0.0, 48000.0])
    transport._on_datagram(reply, ("10.0.0.2", 57110))
    transport._on_datagram(reply, ("10.0.0.3", 57110))
    assert transport.current_target() is None
    assert transport.apply_patch({"target": "10.0.0.3:57110"}) is None
    assert transport.current_target() == ("10.0.0.3", 57110)


def test_udp_note_on_is_decoded():
    cfg = Config()
    cfg.data["osc"] = dict(cfg.osc)
    cfg.data["osc"]["listen_port"] = 0  # replaced below with a free port

    class Capture(OscTransport):
        def __init__(self):
            super().__init__(None, cfg, None)
            self.got = []

        def _inject_midi(self, midi: bytes) -> None:
            self.got.append(midi)

        def _open_alsa(self) -> None:
            return

    transport = Capture()

    async def run():
        import socket
        # Reserve a free UDP port, release it, then let the transport bind it.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        cfg.data["osc"]["listen_port"] = port
        await transport.start()
        try:
            sender = socket_send(port)
            sender(osc.encode_message("/midi/note_on", [2, 64, 90]))
            for _ in range(20):
                if transport.got:
                    break
                await asyncio.sleep(0.01)
        finally:
            await transport.stop()
        return transport.got

    def socket_send(port):
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        def send(payload):
            sock.sendto(payload, ("127.0.0.1", port))
            sock.close()
        return send

    got = asyncio.run(run())
    assert got == [bytes((0x92, 64, 90))]
