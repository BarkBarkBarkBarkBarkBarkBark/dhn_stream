import math
import socket
import time

import psutil
from django.http import HttpResponse
from django.shortcuts import render

from .state import sender_state, receiver_state


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------

def _to_dbfs(v: float) -> str:
    """Convert a linear amplitude (int16 scale, 0–32767) to dBFS string."""
    if v <= 0:
        return "-inf dBFS"
    return f"{20.0 * math.log10(float(v) / 32767.0):+.1f} dBFS"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def _parse_arp() -> list[tuple[str, str, str]]:
    """Return [(ip, mac, iface)] from /proc/net/arp, sorted 192.168.3.x first."""
    entries: list[tuple[str, str, str]] = []
    try:
        with open("/proc/net/arp") as fh:
            next(fh)  # skip header
            for line in fh:
                parts = line.split()
                if len(parts) >= 6 and parts[2] == "0x2":  # 0x2 = complete entry
                    entries.append((parts[0], parts[3], parts[5]))
    except OSError:
        pass
    entries.sort(key=lambda e: (0 if e[0].startswith("192.168.3.") else 1, e[0]))
    return entries


def _parse_gateway() -> str:
    """Return default gateway IP from /proc/net/route, or 'unknown'."""
    try:
        with open("/proc/net/route") as fh:
            next(fh)
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":  # default route
                    gw_hex = parts[2]
                    gw_int = int(gw_hex, 16)
                    return socket.inet_ntoa(gw_int.to_bytes(4, "little"))
    except OSError:
        pass
    return "unknown"


def _parse_dns() -> list[str]:
    """Return nameserver IPs from /etc/resolv.conf."""
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        servers.append(parts[1])
    except OSError:
        pass
    return servers or ["(none found)"]


def _udp_proc_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "?"


# ---------------------------------------------------------------------------
# Diagnostic view
# ---------------------------------------------------------------------------

_OUR_PORTS = {26090}  # annotate these in the UDP socket list
_FULL_SCALE = 32767.0


def diag(request):  # noqa: C901  (long but intentionally monolithic)
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []

    def h(title: str) -> None:
        lines.append("")
        lines.append("=" * 72)
        lines.append(f"  {title}")
        lines.append("=" * 72)

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<30} {value}")

    def blank() -> None:
        lines.append("")

    lines.append(f"  DHN Network Diagnostic — {now_ts}  (auto-refresh 3 s)")

    # ── HOST ──────────────────────────────────────────────────────────────
    h("HOST")
    hostname = socket.gethostname()
    try:
        fqdn = socket.getfqdn()
    except Exception:
        fqdn = hostname
    row("Hostname", hostname)
    row("FQDN", fqdn)
    blank()
    addrs = psutil.net_if_addrs()
    stats_map = psutil.net_if_stats()
    for iface, addr_list in sorted(addrs.items()):
        st = stats_map.get(iface)
        status = "UP" if (st and st.isup) else "DOWN"
        speed = f"{st.speed} Mbps" if (st and st.speed > 0) else "n/a"
        mtu = str(st.mtu) if st else "?"
        lines.append(f"  {iface}  [{status}  speed={speed}  mtu={mtu}]")
        for a in addr_list:
            if a.family == socket.AF_INET:
                mask = a.netmask or ""
                lines.append(f"    IPv4  {a.address:<18} mask={mask}")
            elif a.family == socket.AF_INET6:
                lines.append(f"    IPv6  {a.address}")
            elif a.family == psutil.AF_LINK:
                lines.append(f"    MAC   {a.address}")

    # ── GATEWAY / DNS ─────────────────────────────────────────────────────
    h("GATEWAY / DNS")
    row("Default gateway", _parse_gateway())
    row("DNS nameservers", "  ".join(_parse_dns()))

    # ── ARP TABLE ────────────────────────────────────────────────────────
    h("ARP TABLE  (kernel cache — /proc/net/arp)")
    gw = _parse_gateway()
    arp_entries = _parse_arp()
    if arp_entries:
        lines.append(f"  {'IP Address':<18} {'MAC Address':<20} {'Iface':<10} Notes")
        lines.append("  " + "-" * 60)
        for ip, mac, iface in arp_entries:
            notes = ""
            if ip == gw:
                notes = "← gateway"
            if ip.startswith("192.168.3."):
                notes = (notes + "  target subnet").strip()
            lines.append(f"  {ip:<18} {mac:<20} {iface:<10} {notes}")
    else:
        lines.append("  (no complete ARP entries — run some traffic first)")

    # ── NETWORK I/O ───────────────────────────────────────────────────────
    h("NETWORK I/O  (cumulative since boot)")
    io = psutil.net_io_counters(pernic=True)
    lines.append(f"  {'Interface':<12} {'Sent':>12} {'Recv':>12} {'PktOut':>10} {'PktIn':>10} {'ErrOut':>7} {'ErrIn':>7} {'DropOut':>8} {'DropIn':>8}")
    lines.append("  " + "-" * 90)
    for iface, c in sorted(io.items()):
        lines.append(
            f"  {iface:<12} {_fmt_bytes(c.bytes_sent):>12} {_fmt_bytes(c.bytes_recv):>12}"
            f" {c.packets_sent:>10} {c.packets_recv:>10}"
            f" {c.errout:>7} {c.errin:>7}"
            f" {c.dropout:>8} {c.dropin:>8}"
        )

    # ── UDP SOCKETS ───────────────────────────────────────────────────────
    h("UDP SOCKETS  (active on this host)")
    try:
        conns = psutil.net_connections(kind="udp")
        lines.append(f"  {'Local address':<26} {'Remote address':<26} {'PID':>7} {'Process':<20} Notes")
        lines.append("  " + "-" * 88)
        for c in sorted(conns, key=lambda x: (x.laddr.port if x.laddr else 0)):
            laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "*"
            raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "*"
            pid = c.pid or 0
            proc = _udp_proc_name(pid) if pid else "-"
            note = ""
            if c.laddr and c.laddr.port in _OUR_PORTS:
                note = "← OUR STREAM"
            lines.append(f"  {laddr:<26} {raddr:<26} {pid:>7} {proc:<20} {note}")
    except psutil.AccessDenied:
        lines.append("  (permission denied — run as root for full socket list)")

    # ── SENDER ────────────────────────────────────────────────────────────
    h("SENDER  (sine wave harmonic generator)")
    with sender_state._lock:
        s = dict(sender_state.stats)
        s_running = sender_state.running

    if not s:
        lines.append("  Status: STOPPED  (start sender from the dashboard)")
    else:
        status = "RUNNING" if s_running else "STOPPED"
        elapsed = float(s.get("elapsed", 0) or 0)
        pkt_rate = float(s.get("packet_rate", 0) or 0)
        pkts_sent = int(s.get("packets_sent", 0) or 0)
        bytes_sent = int(s.get("bytes_sent", 0) or 0)
        underruns = int(s.get("underruns", 0) or 0)
        throughput = float(s.get("throughput_mbps", 0) or 0)
        n_ch = int(s.get("channels", 0) or 0)
        sr = int(s.get("sample_rate_hz", 0) or 0)
        fund = float(s.get("fundamental_hz", 0) or 0)
        fpp = int(s.get("frames_per_packet", 1) or 1)
        tpeak = int(s.get("target_peak", 8000) or 8000)
        pkt_bytes = n_ch * fpp * 2  # int16 = 2 bytes

        # Packet loss estimate: compare actual pkts with what a lossless tx would give
        expected_pkts = int(pkt_rate * elapsed) if elapsed > 0 else 0
        loss_pct = max(0.0, (expected_pkts - pkts_sent) / expected_pkts * 100) if expected_pkts > 0 else 0.0

        row("Status", status)
        row("Destination", f"{s.get('channels','?')} ch  {sr} Hz  fundamental={fund} Hz")
        row("Elapsed", f"{elapsed:.1f} s")
        row("Packet rate", f"{pkt_rate:.1f} pkt/s")
        row("Packet size", f"{pkt_bytes} bytes  ({fpp} frame/pkt × {n_ch} ch × 2 B)")
        row("Throughput", f"{throughput:.4f} Mbps")
        row("Packets sent", f"{pkts_sent:,}")
        row("Bytes sent", _fmt_bytes(bytes_sent))
        row("Underruns", str(underruns))
        row("Est. packet loss", f"{loss_pct:.2f}%  (vs expected at current rate)")
        row("Target peak", f"{tpeak}  ({_to_dbfs(tpeak)})")
        blank()
        peaks = s.get("channel_peaks_raw", [])
        rms_vals = s.get("channel_rms_raw", [])
        if peaks:
            lines.append(f"  {'Ch':<5} {'Peak (int16)':>14} {'Peak dBFS':>12} {'RMS (int16)':>14} {'RMS dBFS':>12}")
            lines.append("  " + "-" * 60)
            for i, (p, r) in enumerate(zip(peaks, rms_vals)):
                lines.append(f"  {i:<5} {p:>14} {_to_dbfs(p):>12} {r:>14.1f} {_to_dbfs(r):>12}")

    # ── RECEIVER ──────────────────────────────────────────────────────────
    h("RECEIVER  (UDP decoder + FFT verifier)")
    with receiver_state._lock:
        r = dict(receiver_state.stats)
        sp = dict(receiver_state.spectrum)
        r_running = receiver_state.running

    if not r:
        lines.append("  Status: STOPPED  (start receiver from the dashboard)")
    else:
        status = "RUNNING" if r_running else "STOPPED"
        pkts_total = int(r.get("packets_total", 0) or 0)
        bytes_total = int(r.get("bytes_total", 0) or 0)
        lat = r.get("latency") or {}
        lat_mean = lat.get("mean_ms")
        lat_jitter = lat.get("jitter_ms")
        lat_since = lat.get("since_last_ms")
        cfg = r.get("config") or {}
        sr_r = int(cfg.get("sample_rate_hz", 0) or 0)
        fpp_r = int(cfg.get("frames_per_packet", 1) or 1)
        fund_r = float(cfg.get("fundamental_hz", 0) or 0)
        bind_r = cfg.get("bind_host", "?")
        port_r = cfg.get("recv_port", "?")

        # Compute packet rate from latency inter-arrival mean
        recv_pkt_rate = (1000.0 / lat_mean) if (lat_mean and lat_mean > 0) else 0.0
        avg_pkt_bytes = (bytes_total / pkts_total) if pkts_total > 0 else 0.0

        # Compare with sender for loss estimate
        with sender_state._lock:
            s_pkts = int(sender_state.stats.get("packets_sent", 0) or 0)
        loss_pct_r = max(0.0, (s_pkts - pkts_total) / s_pkts * 100) if s_pkts > 0 else 0.0

        row("Status", status)
        row("Binding", f"{bind_r}:{port_r}")
        row("Signal config", f"{sr_r} Hz  fundamental={fund_r} Hz  fpp={fpp_r}")
        blank()
        row("Packets received", f"{pkts_total:,}")
        row("Bytes received", _fmt_bytes(bytes_total))
        row("Avg packet size", f"{avg_pkt_bytes:.1f} bytes")
        row("Recv pkt rate", f"{recv_pkt_rate:.1f} pkt/s  (from inter-arrival mean)")
        row("Latency mean", f"{lat_mean} ms" if lat_mean is not None else "n/a")
        row("Jitter", f"{lat_jitter} ms" if lat_jitter is not None else "n/a")
        row("Since last packet", f"{lat_since} ms" if lat_since is not None else "n/a")
        row("Est. packet loss", f"{loss_pct_r:.2f}%  (sent vs received)")
        blank()
        peaks_r = r.get("channel_peaks_raw", [])
        rms_r = r.get("channel_rms_raw", [])
        if peaks_r:
            lines.append(f"  {'Ch':<5} {'Peak (int16)':>14} {'Peak dBFS':>12} {'RMS (int16)':>14} {'RMS dBFS':>12}")
            lines.append("  " + "-" * 60)
            for i, (p, rv) in enumerate(zip(peaks_r, rms_r)):
                lines.append(f"  {i:<5} {p:>14} {_to_dbfs(p):>12} {rv:>14.1f} {_to_dbfs(rv):>12}")

        # Harmonic verification from 1Hz FFT push
        blank()
        matches = sp.get("channel_matches", [])
        fund_sp = sp.get("fundamental_hz")
        expected_h = sp.get("expected_harmonics", [])
        if matches:
            lines.append(f"  Harmonic verification  (fundamental = {fund_sp} Hz):")
            for i, (ok, exp) in enumerate(zip(matches, expected_h)):
                label = "MATCH   " if ok else "MISMATCH"
                exp_str = "  ".join(f"{h:.0f} Hz" for h in exp)
                lines.append(f"    Ch {i}  {label}  expected: {exp_str}")
        else:
            lines.append("  (FFT verification pending — awaits 1 s window)")

    # ── SIGNAL QUALITY SUMMARY ───────────────────────────────────────────
    h("SIGNAL QUALITY SUMMARY")
    with sender_state._lock:
        s_snap = dict(sender_state.stats)
    with receiver_state._lock:
        r_snap = dict(receiver_state.stats)
        sp_snap = dict(receiver_state.spectrum)

    s_active = bool(s_snap)
    r_active = bool(r_snap)

    if not (s_active or r_active):
        lines.append("  No active streams.  Start sender and receiver from the dashboard.")
    else:
        # Harmonic match score
        m_list = sp_snap.get("channel_matches", [])
        if m_list:
            n_match = sum(m_list)
            n_total = len(m_list)
            row("Harmonic match", f"{n_match}/{n_total} channels")
        else:
            row("Harmonic match", "pending FFT window")

        # Avg RMS dBFS from receiver
        rms_r2 = r_snap.get("channel_rms_raw", [])
        if rms_r2:
            avg_rms = sum(rms_r2) / len(rms_r2)
            row("Avg RMS (recv)", _to_dbfs(avg_rms))

        # Peak headroom from receiver
        peaks_r2 = r_snap.get("channel_peaks_raw", [])
        if peaks_r2:
            max_pk = max(peaks_r2)
            headroom = _FULL_SCALE - max_pk
            row("Peak headroom", f"{headroom:.0f}  ({_to_dbfs(max_pk)} peak, {headroom/_FULL_SCALE*100:.1f}% below clip)")

        # Sender vs receiver RMS delta
        rms_s2 = s_snap.get("channel_rms_raw", [])
        if rms_s2 and rms_r2 and len(rms_s2) == len(rms_r2):
            deltas = [abs(rs - rr) for rs, rr in zip(rms_s2, rms_r2)]
            avg_delta = sum(deltas) / len(deltas)
            row("Avg RMS delta (send vs recv)", f"{avg_delta:.1f} int16 units")

        # Latency & jitter
        lat2 = r_snap.get("latency") or {}
        if lat2.get("mean_ms") is not None:
            row("Packet latency mean", f"{lat2['mean_ms']} ms")
            row("Packet jitter", f"{lat2['jitter_ms']} ms")

        # Loss
        s_pkts2 = int(s_snap.get("packets_sent", 0) or 0)
        r_pkts2 = int(r_snap.get("packets_total", 0) or 0)
        if s_pkts2 > 0:
            loss2 = max(0.0, (s_pkts2 - r_pkts2) / s_pkts2 * 100)
            row("Packet loss", f"{loss2:.3f}%  ({s_pkts2 - r_pkts2:,} pkts)")

    lines.append("")
    lines.append("=" * 72)
    lines.append("")

    # ── Assemble minimal HTML ─────────────────────────────────────────────
    body = "\n".join(lines)
    html = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='3'>"
        "<title>DHN Diagnostics</title>"
        "<style>"
        "html,body{margin:0;padding:0;background:#0d0d0d;color:#c8f0c8;"
        "font:13px/1.5 'Cascadia Code','Fira Mono','Consolas',monospace;}"
        "pre{white-space:pre;padding:12px 16px;}"
        "</style>"
        "</head><body>"
        f"<pre>{body}</pre>"
        "</body></html>"
    )
    return HttpResponse(html, content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# Default values shown in the UI on first load
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "dest_host": "127.0.0.1",
    "dest_port": 26090,
    "bind_host": "0.0.0.0",
    "receiver_port": 26090,
    "channels": 4,
    "sample_rate_hz": 32000,
    "fundamental_hz": 440,
    "frames_per_packet": 1,
    "target_peak": 8000,
    "send_buffer": 8388608,
}


def index(request):
    return render(request, "dashboard/index.html", {"defaults": _DEFAULTS})

