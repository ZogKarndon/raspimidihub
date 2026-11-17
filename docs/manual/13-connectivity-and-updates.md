# Connectivity and Software Updates

How the hub reaches the network and gets new software. Networking
is **Ethernet only**. The Wi-Fi radio is turned off at startup;
there is no access point, no client mode, and no captive portal.

## Ethernet

Plug a cable into the RJ45 port. The Pi takes DHCP, or the static
address from **Settings → Network**. Open the UI at
`http://raspimidihub-<id>.local/` from any machine on that network.
`<id>` is the four-character code in the title bar.

A direct cable between two hubs needs no router: each hub keeps an
IPv4 link-local address (`169.254.x.y`) on `eth0` alongside any
DHCP lease, so the two always share a subnet.

USB Ethernet counts too. A second Raspberry Pi attached with a USB
gadget link (`usb0`, or an `enx…` interface) is on the wired
network for OSC and for SuperCollider discovery below. It is not
Wi-Fi.

## OSC

**Settings → Network → OSC** is how the hub speaks Open Sound
Control on that wire. The socket listens on UDP port 9000 (change
it on the same card).

An **OSC** device appears in the routing matrix:

- MIDI you route *into* its **IN** port is sent to the current
  target as OSC messages (`/midi/note_on`, `/midi/note_off`,
  `/midi/cc`, `/midi/program`, `/midi/pitchbend`, `/midi/clock`,
  `/midi/start`, `/midi/stop`, `/midi/continue`, `/midi/sysex`).
  Channel numbers are 0–15. Pitch bend is the unsigned 14-bit
  value (center 8192).
- OSC messages of the same shape that arrive on port 9000 come
  *out* of the device's **OUT** port as MIDI, and you route them
  like any other source. A `/midi` message with the OSC MIDI
  typetag is accepted too.

With no target chosen, a single discovered server is used. Several
servers and no choice means outbound MIDI is not sent until you
tap **Send here** or add a manual peer (`host` or `host:port`).

### SuperCollider servers

Discovery is real, and it is narrow. Stock `scsynth` does not
register with mDNS. It does answer `/status` with `/status.reply`
when the packet can reach it. Every few seconds the hub sends
`/status` to:

- the broadcast address of each Ethernet and USB-Ethernet subnet;
- the other of `.1` and `.2` when this Pi holds one of those
  (the usual USB-gadget pair, `192.168.7.1` and `192.168.7.2`);
- completed ARP neighbours on those interfaces;
- any manual peer.

A reply is listed on the Network page with synth and ugen counts.
That is the supported discovery.

What is **not** supported:

- a server left on the default bind address `127.0.0.1` (start it
  with `scsynth -u 57110 -B 0.0.0.0`, or in sclang
  `s.options.bindAddress = "0.0.0.0"` before `s.boot`);
- a server on another subnet, or only reachable over Wi-Fi;
- Bonjour / mDNS advertisements (scsynth does not send them);
- loading synthdefs, walking the node tree, or sending `/s_new`.
  The hub delivers MIDI-shaped OSC. The synth that listens is
  yours. A minimal receiver:

```
OSCFunc({ |msg|
    // msg[1] channel, msg[2] note, msg[3] velocity
}, '/midi/note_on');
```

## USB Tethering

A phone on a USB-A port with Personal Hotspot (iOS) or USB
Tethering (Android) enabled provides internet without an Ethernet
gateway. Settings shows the tethered URL as a clickable link.
Unplugging the phone returns the internet state to "none".

## mDNS

Each hub advertises a unique mDNS name: `raspimidihub-<id>.local`,
where `<id>` is the four-character hardware code shown in the
title bar (e.g. `raspimidihub-735C.local`) -- two hubs on one
network never collide. Resolution requirements:

- **macOS, iOS** -- native, no setup.
- **Linux** -- avahi-daemon must run (default on most
  distributions).
- **Modern Android** -- Android 12+ for most apps; some browsers
  still need the IP.
- **Windows** -- needs Apple Bonjour (free from Apple's site, or
  bundled with iTunes).

Without mDNS, use the address DHCP gave `eth0` (or the static
address from Settings → Network).

## Network MIDI -- Sharing Devices over the Network

**Settings → Network MIDI** exports any local MIDI device as a
standard **RTP-MIDI (AppleMIDI)** session, advertised over mDNS as
`"TX-7 @raspimidihub-<id>"`; the unique suffix means a peer always
resolves a device to the right hub. Anything that speaks RTP-MIDI
can connect:

- **a second RaspiMIDIHub** -- an Ethernet cable (up to 100 m) or
  any shared network;
- **macOS / iOS** -- Audio MIDI Setup's *MIDI Network Setup*
  (macOS) and RTP-MIDI-capable iOS apps, no extra software;
- **Linux** -- `rtpmidid` (and compatible tools).

Turn on the master toggle, then tick the devices to share -- one
plugin pair for a point-to-point tunnel, or everything. Several
clients can connect to one exported device at once. Exports are
saved in the config; the advert exists only while the device is
present -- unplug and the session leaves the network, replug and
it returns. Notes, CCs, clock and SysEx all cross the wire;
wired-LAN latency is well under a millisecond.

### Mirroring -- the two-hub scenario

When two RaspiMIDIHubs see each other, the peer's exported devices
**mirror automatically** into the local matrix as violet network
devices, grouped under a collapsible `@hubname` header (chapter 5,
*Remote Hub Groups*). No pairing flow: export on one side, route
on the other. Each side controls its own export list; the
receiving side can **Unmirror** per session (device's header menu)
and re-add from the Add menu.

Mirrored devices are full matrix citizens -- filters, mappings,
renames, clock routing -- and connections are saved under a stable
identity that survives reboots and IP changes on both ends. An
offline peer's devices show as offline rows and recover by
themselves.

Sessions from Macs, iPads or DAWs are **not** mirrored
automatically (a studio full of them would flood the matrix);
they are listed under Settings → Network MIDI and in the Add menu,
one tap to mirror.

Loops cannot form: a mirrored device cannot be re-exported, and a
hub never mirrors its own sessions.

### The direct cable, and life without mDNS

A direct Ethernet cable between two hubs needs no router: each hub
keeps an IPv4 link-local address (`169.254.x.y`) on `eth0` at all
times, in every mode, alongside any DHCP lease or static
address -- so two cabled hubs always share a subnet. The hub keeps
looking for a DHCP server; plugging into a real network later
picks up a lease automatically.

Where multicast is filtered (routed LANs, some managed switches),
add the other hub's IP or hostname under **Manual peers**; the hub
asks the peer directly for its exports and everything else behaves
as with discovery.

### Failure behaviour

A cable pull or peer power-cut is detected within ~30 seconds;
mirrored devices drop to offline and the hub keeps retrying --
replugging restores everything without a tap. Silently vanished
clients are dropped from an exported session after 60 seconds.

Transport is plain UDP on ports 5004 and up (one even/odd port
pair per exported device); discovery is the same mDNS as
`raspimidihub-<id>.local`. Needs `python3-zeroconf` (a standard
dependency of the deb); when missing, the Settings page says so
instead of offering the toggle.

## Software Updates

**Check GitHub for newer versions** in **Settings → Update** needs
the wired network to already reach the internet. There is no
Wi-Fi detour.

1. Resolve GitHub.
2. List the latest releases.
3. Download anything newer than the running build.
4. Keep the newest three `.deb` files on disk; delete older ones.
5. Install on demand from the local cache.

| Path | When |
|------|------|
| **Ethernet** | The cable's network can reach GitHub. The usual path. |
| **USB tethering** | A phone on a USB-A port is sharing its connection, and there is no Ethernet route. |

If neither path has internet, the check stops with that message.
Once a deb is on disk, **Install** needs no internet at all.

## Console Recovery

A console (USB keyboard + HDMI, or
`ssh user@raspimidihub-<id>.local`) reaches the underlying Pi OS.
The bootstrap image ships with **sshd enabled**, using the user
and key/password from the Pi Imager wizard -- which also makes a
failed first boot diagnosable (chapter 2). Commonly useful:

- `sudo reset-wifi` -- blocks the Wi-Fi radio and drops saved
  Wi-Fi profiles. The hub stays on Ethernet. Use it if an older
  image left a client connection that is fighting the cable.
- `journalctl -u raspimidihub -e` -- tails the routing service log
  (BLE issues, update failures, unexpected restarts).
- `sudo mount -o remount,rw / && sudo dpkg --configure -a && sudo
  mount -o remount,ro /` -- reconciles a half-applied dpkg state
  when **Install** keeps failing with `E: dpkg was interrupted…`;
  only needed on builds older than the one that runs this
  automatically before every install.

## Updating the rosetup Package

**Settings → Software Versions** lists both Debian packages:
`raspimidihub` (routing service + web UI) and
`raspimidihub-rosetup` (read-only filesystem hardening). rosetup
updates via the same install flow but needs a reboot to apply (the
read-only mount layer re-initialises at boot).
