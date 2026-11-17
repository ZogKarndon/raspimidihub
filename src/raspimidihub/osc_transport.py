"""OSC on the wired network, and SuperCollider server discovery.

The hub binds one UDP socket (default port 9000) and speaks OSC 1.0.
MIDI routed to the OSC device's IN port leaves as `/midi/...` messages
toward the selected target. OSC MIDI messages that arrive on the
socket are injected on the OUT port, so the routing matrix treats OSC
like any other device.

SuperCollider's server (scsynth / supernova) does not advertise itself
with mDNS. What it does do, when it is bound to a reachable address,
is answer `/status` with `/status.reply`. Discovery is that probe:

- subnet broadcast on every non-Wi-Fi interface (Ethernet, and USB
  Ethernet gadgets such as `usb0` / `enx…` — a second Pi cabled over
  USB shows up there);
- the other of `.1` / `.2` when we hold one of those addresses (the
  usual Pi USB-gadget pair, 192.168.7.1 ↔ 192.168.7.2);
- completed ARP neighbours on those interfaces;
- manual peers from the config.

Not discovered: a server bound to 127.0.0.1 (scsynth's default), a
server on another subnet, or anything only reachable over Wi-Fi (the
radio is not used). This is not a SuperCollider client — synthdefs,
node trees, and `/s_new` are the user's patch, not the hub's.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from pathlib import Path

from .osc import (
    encode_message,
    decode_packet,
    message_to_midi,
    midi_bytes_to_messages,
    parse_status_reply,
    OscError,
)

log = logging.getLogger(__name__)

DEFAULT_LISTEN_PORT = 9000
DEFAULT_SC_PORT = 57110
PROBE_INTERVAL = 5.0
SERVER_TTL = 16.0  # just over three missed probes

_PEER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SKIP_PREFIXES = ("docker", "br-", "veth", "virbr", "tailscale", "wg", "zt", "lo")


def is_wired_interface(name: str) -> bool:
    """Ethernet and USB-Ethernet. Wi-Fi and virtual bridges are not."""
    if not name or name.startswith(_SKIP_PREFIXES) or name.startswith("wlan"):
        return False
    return True


def parse_peer(text: str, default_port: int = DEFAULT_SC_PORT) -> tuple[str, int] | None:
    """Parse `host` or `host:port`. IPv6 is not accepted."""
    text = (text or "").strip()
    if not text or not _PEER_RE.match(text) or text.count(":") > 1:
        return None
    if ":" in text:
        host, port_s = text.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            return None
        if not host or not 1 <= port <= 65535:
            return None
        return host, port
    if not 1 <= default_port <= 65535:
        return None
    return text, default_port


def read_arp(path: str = "/proc/net/arp") -> list[tuple[str, str, int]]:
    """Return (ip, device, flags) rows from the kernel neighbour table."""
    rows: list[tuple[str, str, int]] = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return rows
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            flags = int(parts[2], 16)
        except ValueError:
            continue
        rows.append((parts[0], parts[5], flags))
    return rows


def probe_targets(interfaces: list[dict], manual_peers: list,
                  sc_port: int = DEFAULT_SC_PORT,
                  arp_rows: list[tuple[str, str, int]] | None = None
                  ) -> list[tuple[str, int]]:
    """Destinations for a SuperCollider `/status` probe.

    `interfaces` is the `get_all_interfaces()` shape: each item has
    `interface`, `addresses` (CIDR strings), and an optional `gateway`.
    """
    targets: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    own_ips: set[str] = set()

    def add(host: str, port: int) -> None:
        if not host or host in own_ips:
            return
        key = (host, port)
        if key in seen:
            return
        seen.add(key)
        targets.append(key)

    for info in interfaces or []:
        name = info.get("interface") or ""
        if not is_wired_interface(name):
            continue
        for cidr in info.get("addresses") or []:
            try:
                iface_ip = ipaddress.IPv4Address(str(cidr).split("/", 1)[0])
                net = ipaddress.IPv4Network(str(cidr), strict=False)
            except ValueError:
                continue
            own_ips.add(str(iface_ip))
            if net.prefixlen <= 30:
                add(str(net.broadcast_address), sc_port)
            last = int(iface_ip) & 0xFF
            # Pi USB-gadget convention: the two ends are .1 and .2.
            if last in (1, 2) and net.prefixlen <= 24:
                other = iface_ip + 1 if last == 1 else iface_ip - 1
                if other in net:
                    add(str(other), sc_port)
            if net.prefixlen >= 30:
                for host in net.hosts():
                    if host != iface_ip:
                        add(str(host), sc_port)
        gateway = (info.get("gateway") or "").strip()
        if gateway:
            try:
                ipaddress.IPv4Address(gateway)
            except ValueError:
                pass
            else:
                add(gateway, sc_port)

    for ip, device, flags in arp_rows or []:
        if not is_wired_interface(device):
            continue
        if flags & 0x2 == 0:  # NUD_REACHABLE / complete
            continue
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        add(ip, sc_port)

    for raw in manual_peers or []:
        parsed = parse_peer(str(raw), sc_port)
        if parsed:
            add(*parsed)
    return targets


def _emit_alsa(alsa, ev, source_port: int) -> None:
    from ctypes import pointer

    from .alsa_seq import (
        SND_SEQ_ADDRESS_SUBSCRIBERS,
        SND_SEQ_QUEUE_DIRECT,
        snd_seq_event_output_direct,
    )
    ev.source.client = alsa.client_id
    ev.source.port = source_port
    ev.dest.client = SND_SEQ_ADDRESS_SUBSCRIBERS
    ev.dest.port = 0
    ev.queue = SND_SEQ_QUEUE_DIRECT
    try:
        snd_seq_event_output_direct(alsa.handle, pointer(ev))
    except Exception as e:
        log.debug("osc: ALSA inject failed: %s", e)


class _Datagram(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self._callback = callback

    def datagram_received(self, data: bytes, addr) -> None:
        self._callback(data, addr)

    def error_received(self, exc) -> None:
        log.debug("osc: socket error: %s", exc)

    def connection_made(self, transport) -> None:
        pass

    def connection_lost(self, exc) -> None:
        pass


class OscTransport:
    """UDP OSC socket + optional ALSA bridge + SuperCollider probes."""

    def __init__(self, engine, config, server):
        self._engine = engine
        self._config = config
        self._server = server
        self._transport = None
        self._started = False
        self._probe_task: asyncio.Task | None = None
        self._servers: dict[str, dict] = {}
        self._alsa = None
        self._in_port = -1
        self._out_port = -1
        self._error = ""
        self._sent = 0
        self._received = 0

    @property
    def settings(self) -> dict:
        data = self._config.data
        block = data.get("osc")
        if not isinstance(block, dict):
            block = {}
            data["osc"] = block
        return block

    @property
    def alsa_client_id(self) -> int | None:
        return self._alsa.client_id if self._alsa else None

    def live_servers(self) -> list[dict]:
        now = time.monotonic()
        fresh = []
        for info in self._servers.values():
            age = now - info.get("last_seen", 0)
            if age > SERVER_TTL:
                continue
            public = {k: v for k, v in info.items() if k != "last_seen"}
            public["age_s"] = round(age, 1)
            fresh.append(public)
        fresh.sort(key=lambda s: (s["host"], s["port"]))
        return fresh

    def current_target(self) -> tuple[str, int] | None:
        """Where outbound MIDI-OSC goes.

        An explicit `osc.target` wins. With none set, a single
        discovered server is used. Several servers and no choice means
        nowhere — the UI asks for a target instead of guessing.
        """
        cfg = self.settings
        sc_port = int(cfg.get("sc_port") or DEFAULT_SC_PORT)
        raw = (cfg.get("target") or "").strip()
        if raw:
            return parse_peer(raw, sc_port)
        servers = self.live_servers()
        if len(servers) == 1:
            return servers[0]["host"], int(servers[0]["port"])
        return None

    def status(self) -> dict:
        cfg = self.settings
        target = self.current_target()
        return {
            "available": True,
            "enabled": bool(cfg.get("enabled", True)),
            "listen_port": int(cfg.get("listen_port") or DEFAULT_LISTEN_PORT),
            "sc_port": int(cfg.get("sc_port") or DEFAULT_SC_PORT),
            "bound": self._transport is not None,
            "error": self._error,
            "target": cfg.get("target") or "",
            "resolved_target": f"{target[0]}:{target[1]}" if target else "",
            "manual_peers": list(cfg.get("manual_peers") or []),
            "servers": self.live_servers(),
            "midi_bridge": self._alsa is not None,
            "sent": self._sent,
            "received": self._received,
            "discovery": {
                "method": "osc-status-probe",
                "real_supercollider_discovery": True,
                "not_supported": [
                    "mDNS / Bonjour (stock scsynth does not advertise)",
                    "servers bound to 127.0.0.1 (scsynth's default)",
                    "servers on a different subnet (no broadcast, no ARP)",
                    "Wi-Fi (the radio is disabled)",
                    "synthdef upload, node trees, or /s_new scheduling",
                ],
            },
        }

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._open_alsa()
        if self.settings.get("enabled", True):
            await self._bind()
            self._probe_task = asyncio.get_running_loop().create_task(self._probe_loop())
        self._rescan()
        log.info("osc: up (listen %s, bridge %s)",
                 self.settings.get("listen_port", DEFAULT_LISTEN_PORT),
                 "yes" if self._alsa else "no")

    async def stop(self) -> None:
        self._started = False
        if self._probe_task:
            self._probe_task.cancel()
            self._probe_task = None
        self._close_socket()
        self._close_alsa()

    async def reconfigure(self) -> None:
        """Re-bind after a settings change. Safe to call from a request."""
        if not self._started:
            return
        if self._probe_task:
            self._probe_task.cancel()
            self._probe_task = None
        self._close_socket()
        if self.settings.get("enabled", True):
            await self._bind()
            self._probe_task = asyncio.get_running_loop().create_task(self._probe_loop())

    def apply_patch(self, patch: dict) -> str | None:
        """Validate and store a settings patch. Returns an error string
        or None. Does not save — the route does, same as network MIDI."""
        cfg = self.settings
        if "enabled" in patch:
            cfg["enabled"] = bool(patch["enabled"])
        if "listen_port" in patch and patch["listen_port"] not in (None, ""):
            try:
                port = int(patch["listen_port"])
            except (TypeError, ValueError):
                return "listen_port must be an integer"
            if not 1 <= port <= 65535:
                return "listen_port out of range"
            cfg["listen_port"] = port
        if "sc_port" in patch and patch["sc_port"] not in (None, ""):
            try:
                port = int(patch["sc_port"])
            except (TypeError, ValueError):
                return "sc_port must be an integer"
            if not 1 <= port <= 65535:
                return "sc_port out of range"
            cfg["sc_port"] = port
        if "target" in patch:
            raw = str(patch.get("target") or "").strip()
            if raw and parse_peer(raw, int(cfg.get("sc_port") or DEFAULT_SC_PORT)) is None:
                return "target must be host or host:port"
            cfg["target"] = raw
        if "manual_peers" in patch:
            peers = patch["manual_peers"]
            if not isinstance(peers, list):
                return "manual_peers must be a list"
            cleaned = []
            for item in peers[:32]:
                parsed = parse_peer(str(item), int(cfg.get("sc_port") or DEFAULT_SC_PORT))
                if parsed is None:
                    return f"invalid peer {item!r}"
                host, port = parsed
                cleaned.append(f"{host}:{port}")
            cfg["manual_peers"] = cleaned
        return None

    # --- socket ---

    async def _bind(self) -> None:
        port = int(self.settings.get("listen_port") or DEFAULT_LISTEN_PORT)
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Datagram(self._on_datagram),
                local_addr=("0.0.0.0", port),
                allow_broadcast=True,
            )
            self._error = ""
            log.info("osc: listening on UDP %d", port)
        except OSError as e:
            self._transport = None
            self._error = f"could not bind UDP {port}: {e}"
            log.warning("osc: %s", self._error)

    def _close_socket(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _on_datagram(self, data: bytes, addr) -> None:
        try:
            messages = decode_packet(data)
        except OscError:
            log.debug("osc: ignoring non-OSC datagram from %s", addr)
            return
        for address, args in messages:
            if address == "/status.reply":
                self._note_server(addr, args)
                continue
            midi = message_to_midi(address, args)
            if midi:
                self._received += 1
                self._inject_midi(midi)

    def _note_server(self, addr, args: list) -> None:
        host = addr[0]
        try:
            port = int(addr[1])
        except (TypeError, ValueError):
            return
        # Ignore our own status traffic if a probe ever loops back.
        if host.startswith("127."):
            return
        key = f"{host}:{port}"
        before = set(self._servers)
        info = parse_status_reply(args)
        info["host"] = host
        info["port"] = port
        info["last_seen"] = time.monotonic()
        self._servers[key] = info
        if set(self._servers) != before:
            log.info("osc: SuperCollider server at %s (ugens=%s synths=%s)",
                     key, info.get("ugens"), info.get("synths"))
            self._notify()

    def _expire(self) -> None:
        now = time.monotonic()
        stale = [k for k, info in self._servers.items()
                 if now - info.get("last_seen", 0) > SERVER_TTL]
        if not stale:
            return
        for key in stale:
            self._servers.pop(key, None)
            log.info("osc: SuperCollider server %s went quiet", key)
        self._notify()

    def _notify(self) -> None:
        server = self._server
        if server is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(server.send_sse("osc-changed", {
            "servers": self.live_servers(),
            "resolved_target": self.status()["resolved_target"],
        }))

    # --- discovery ---

    async def _probe_loop(self) -> None:
        while self._started and self.settings.get("enabled", True):
            try:
                await self._probe_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("osc: probe failed", exc_info=True)
            try:
                await asyncio.sleep(PROBE_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _probe_once(self) -> None:
        self._expire()
        transport = self._transport
        if transport is None:
            return
        from .wifi import get_all_interfaces
        loop = asyncio.get_running_loop()
        interfaces = await loop.run_in_executor(None, get_all_interfaces)
        arp = await loop.run_in_executor(None, read_arp)
        sc_port = int(self.settings.get("sc_port") or DEFAULT_SC_PORT)
        targets = probe_targets(
            interfaces, self.settings.get("manual_peers") or [],
            sc_port, arp)
        payload = encode_message("/status", [])
        for host, port in targets:
            try:
                transport.sendto(payload, (host, port))
            except OSError:
                continue

    # --- MIDI bridge ---

    def _open_alsa(self) -> None:
        if self._alsa is not None:
            return
        try:
            from .alsa_seq import AlsaSeq
            alsa = AlsaSeq("OSC", default_ports=False)
        except OSError as e:
            log.warning("osc: ALSA client unavailable (%s); UDP only", e)
            return
        try:
            self._out_port = alsa.create_port("OUT", readable=True)
            self._in_port = alsa.create_port("IN", writable=True)
            asyncio.get_running_loop().add_reader(alsa.fileno(), self._on_alsa)
        except Exception:
            log.warning("osc: ALSA port setup failed", exc_info=True)
            alsa.close()
            return
        self._alsa = alsa

    def _close_alsa(self) -> None:
        if self._alsa is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self._alsa.fileno())
        except (OSError, ValueError, RuntimeError):
            pass
        self._alsa.close()
        self._alsa = None

    def _rescan(self) -> None:
        engine = self._engine
        if engine is None or self._alsa is None:
            return
        try:
            engine._scan_and_connect()
        except Exception:
            log.debug("osc: device rescan failed", exc_info=True)

    def _on_alsa(self) -> None:
        from .midi_codec import event_to_midi
        while self._alsa is not None:
            ev = self._alsa.read_event()
            if ev is None:
                return
            if ev.dest.port != self._in_port:
                continue
            midi = event_to_midi(ev)
            if midi:
                self._send_midi(midi)

    def _send_midi(self, midi: bytes) -> None:
        transport = self._transport
        target = self.current_target()
        if transport is None or target is None:
            return
        for address, args in midi_bytes_to_messages(midi):
            try:
                transport.sendto(encode_message(address, args), target)
                self._sent += 1
            except OSError as e:
                log.debug("osc: send to %s failed: %s", target, e)
                return

    def _inject_midi(self, midi: bytes) -> None:
        if self._alsa is None:
            return
        from .midi_codec import midi_to_event
        ev = midi_to_event(midi)
        if ev is None:
            return
        _emit_alsa(self._alsa, ev, self._out_port)
