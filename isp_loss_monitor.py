#!/usr/bin/env python3
"""
Uplink Ledger

A standard-library-only packet-loss, latency, and jitter monitor designed to
run on a LAN client or monitoring VM. It exposes a small JSON/CSV HTTP API and
serves a bundled browser dashboard while also rendering terminal status.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import dataclasses
import datetime as dt
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.3.0"
TARGET_ORDER = ("gateway", "isp_hop", "cloudflare", "google", "quad9")
TARGET_LABELS = {
    "gateway": "Router",
    "isp_hop": "First Hop",
    "cloudflare": "Cloudflare",
    "google": "Google",
    "quad9": "Quad9",
}
PUBLIC_TARGETS = {
    "cloudflare": ("Cloudflare", "1.1.1.1"),
    "google": ("Google", "8.8.8.8"),
    "quad9": ("Quad9", "9.9.9.9"),
}
METRIC_FIELDS = (
    "sent",
    "received",
    "loss_pct",
    "rtt_min_ms",
    "rtt_avg_ms",
    "rtt_max_ms",
    "jitter_ms",
)
CSV_BASE_FIELDS = (
    "schema_version",
    "interval_start_utc",
    "interval_end_utc",
    "duration_seconds",
    "status_code",
    "status_message",
)
CSV_FIELDS = list(CSV_BASE_FIELDS)
for _key in TARGET_ORDER:
    CSV_FIELDS.append(f"{_key}_address")
    CSV_FIELDS.extend(f"{_key}_{field}" for field in METRIC_FIELDS)

PING_COUNTS_RE = re.compile(
    r"(?P<sent>\d+)\s+packets transmitted,\s*"
    r"(?P<received>\d+)\s+(?:packets\s+)?received",
    re.IGNORECASE,
)
PING_RTT_RE = re.compile(r"\btime[=<](?P<rtt>\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
ROUTE_FIELD_RE = re.compile(r"^\s*(gateway|interface):\s*(\S+)", re.MULTILINE)
TRACE_LINE_RE = re.compile(r"^\s*\d+\s+(.*)$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def next_interval_boundary(
    value: dt.datetime,
    interval_seconds: int,
) -> dt.datetime:
    """Return the next wall-clock boundary for an interval."""
    if value.tzinfo is None:
        raise ValueError("interval boundary timestamps must be timezone-aware")
    epoch_seconds = value.timestamp()
    boundary = (math.floor(epoch_seconds / interval_seconds) + 1) * interval_seconds
    return dt.datetime.fromtimestamp(boundary, tz=dt.timezone.utc)


def finite_round(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def parse_optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def parse_optional_int(value: str | None) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


@dataclasses.dataclass(frozen=True)
class Target:
    key: str
    label: str
    address: str


@dataclasses.dataclass
class BurstResult:
    sent: int
    received: int
    rtts_ms: list[float]
    error: str | None = None


class StatsAccumulator:
    """Aggregates ping bursts without retaining all samples."""

    def __init__(self) -> None:
        self.sent = 0
        self.received = 0
        self.rtt_count = 0
        self.rtt_sum = 0.0
        self.rtt_min: float | None = None
        self.rtt_max: float | None = None
        self.jitter_sum = 0.0
        self.jitter_pairs = 0
        self.last_rtt: float | None = None
        self.errors = 0

    def add(self, result: BurstResult) -> None:
        self.sent += max(0, result.sent)
        self.received += max(0, min(result.received, result.sent))
        if result.error:
            self.errors += 1
        for rtt in result.rtts_ms:
            if not math.isfinite(rtt) or rtt < 0:
                continue
            self.rtt_count += 1
            self.rtt_sum += rtt
            self.rtt_min = rtt if self.rtt_min is None else min(self.rtt_min, rtt)
            self.rtt_max = rtt if self.rtt_max is None else max(self.rtt_max, rtt)
            if self.last_rtt is not None:
                self.jitter_sum += abs(rtt - self.last_rtt)
                self.jitter_pairs += 1
            self.last_rtt = rtt

    def snapshot(self) -> dict[str, Any]:
        loss = None
        if self.sent:
            loss = max(0.0, min(100.0, (self.sent - self.received) * 100 / self.sent))
        avg = self.rtt_sum / self.rtt_count if self.rtt_count else None
        jitter = self.jitter_sum / self.jitter_pairs if self.jitter_pairs else None
        return {
            "sent": self.sent,
            "received": self.received,
            "loss_pct": finite_round(loss),
            "rtt_min_ms": finite_round(self.rtt_min),
            "rtt_avg_ms": finite_round(avg),
            "rtt_max_ms": finite_round(self.rtt_max),
            "jitter_ms": finite_round(jitter),
            "errors": self.errors,
        }


class PingRunner:
    def __init__(
        self,
        command: str,
        count: int,
        ping_interval: float,
        reply_timeout: float,
    ) -> None:
        self.command = command
        self.count = count
        self.ping_interval = ping_interval
        self.reply_timeout = reply_timeout
        self.freebsd_style = sys.platform.startswith(("freebsd", "darwin"))

    def build_command(self, address: str) -> list[str]:
        wait_value = (
            str(max(1, round(self.reply_timeout * 1000)))
            if self.freebsd_style
            else str(max(1, math.ceil(self.reply_timeout)))
        )
        return [
            self.command,
            "-n",
            "-c",
            str(self.count),
            "-i",
            str(self.ping_interval),
            "-W",
            wait_value,
            address,
        ]

    def run(self, target: Target) -> BurstResult:
        command = self.build_command(target.address)
        max_runtime = (
            max(0, self.count - 1) * self.ping_interval
            + self.reply_timeout
            + 2.0
        )
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=max_runtime,
                check=False,
            )
            output = completed.stdout or ""
            result = parse_ping_output(output, fallback_sent=self.count)
            if completed.returncode not in (0, 1) and not result.error:
                result.error = f"ping exited {completed.returncode}"
            return result
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            result = parse_ping_output(partial, fallback_sent=self.count)
            result.error = "ping timed out"
            return result
        except OSError as exc:
            return BurstResult(
                sent=self.count,
                received=0,
                rtts_ms=[],
                error=f"could not run ping: {exc}",
            )


def parse_ping_output(output: str, fallback_sent: int) -> BurstResult:
    count_match = PING_COUNTS_RE.search(output)
    rtts = [float(match.group("rtt")) for match in PING_RTT_RE.finditer(output)]
    if count_match:
        sent = int(count_match.group("sent"))
        received = int(count_match.group("received"))
        return BurstResult(sent=sent, received=received, rtts_ms=rtts)
    # A killed ping may not print its summary. The reply lines are still useful,
    # while fallback_sent preserves a conservative loss figure.
    return BurstResult(
        sent=fallback_sent,
        received=min(len(rtts), fallback_sent),
        rtts_ms=rtts,
        error="ping summary was unavailable",
    )


@dataclasses.dataclass
class DiscoveryResult:
    gateway: str | None
    interface: str | None
    isp_hop: str | None
    warning: str | None = None
    public_ip: str | None = None
    isp_hop_source: str | None = None
    cache_updated_at: str | None = None


class DiscoveryCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if payload.get("schema_version") != 1:
            return {}
        for field in ("public_ip", "gateway", "isp_hop"):
            value = payload.get(field)
            if value is not None and not is_usable_ipv4(str(value)):
                payload[field] = None
        return payload

    def save(
        self,
        public_ip: str | None,
        gateway: str,
        interface: str | None,
        isp_hop: str,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "public_ip": public_ip,
            "gateway": gateway,
            "interface": interface,
            "isp_hop": isp_hop,
            "updated_at": iso_utc(utc_now()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(payload, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        return payload

    def seed(self, gateway: str, interface: str | None, isp_hop: str) -> None:
        existing = self.load()
        if existing.get("isp_hop"):
            return
        self.save(None, gateway, interface, isp_hop)


class NetworkDiscovery:
    def __init__(
        self,
        route_command: str = "route",
        netstat_command: str = "netstat",
        ip_command: str = "ip",
        curl_command: str = "curl",
        traceroute_command: str = "traceroute",
        trace_target: str = "1.1.1.1",
        public_ip_url: str = "https://ipv4.icanhazip.com/",
        gateway_address: str | None = None,
        cache_path: Path = Path("discovery-cache.json"),
    ) -> None:
        self.route_command = route_command
        self.netstat_command = netstat_command
        self.ip_command = ip_command
        self.curl_command = curl_command
        self.traceroute_command = traceroute_command
        self.trace_target = trace_target
        self.public_ip_url = public_ip_url
        self.gateway_address = gateway_address
        self.cache = DiscoveryCache(cache_path)

    def seed_cached_hop(
        self,
        gateway: str | None,
        interface: str | None,
        isp_hop: str | None,
    ) -> None:
        if gateway and isp_hop:
            self.cache.seed(gateway, interface, isp_hop)

    def discover(self) -> DiscoveryResult:
        discovered_gateway, interface = self.default_gateway()
        gateway = self.gateway_address or discovered_gateway
        cached = self.cache.load()
        public_ip = self.public_ipv4()
        if not gateway:
            return DiscoveryResult(
                gateway=None,
                interface=interface,
                isp_hop=None,
                warning="No IPv4 default gateway was found; discovery will retry.",
                public_ip=public_ip,
            )

        cached_hop = cached.get("isp_hop")
        cached_gateway = cached.get("gateway")
        cached_public_ip = cached.get("public_ip")
        same_gateway = cached_gateway in (None, gateway)
        public_ip_unchanged = bool(
            public_ip and cached_public_ip and public_ip == cached_public_ip
        )

        if cached_hop and same_gateway and (
            public_ip_unchanged or public_ip is None
        ):
            warning = None
            if public_ip is None:
                warning = (
                    "Public IPv4 check failed; continuing with the cached first hop."
                )
            return DiscoveryResult(
                gateway=gateway,
                interface=interface,
                isp_hop=cached_hop,
                warning=warning,
                public_ip=public_ip or cached_public_ip,
                isp_hop_source="cache",
                cache_updated_at=cached.get("updated_at"),
            )

        isp_hop = self.first_hop_after_gateway(gateway)
        if isp_hop:
            saved = self.cache.save(public_ip, gateway, interface, isp_hop)
            return DiscoveryResult(
                gateway=gateway,
                interface=interface,
                isp_hop=isp_hop,
                public_ip=public_ip,
                isp_hop_source="traceroute",
                cache_updated_at=saved.get("updated_at"),
            )

        if cached_hop:
            return DiscoveryResult(
                gateway=gateway,
                interface=interface,
                isp_hop=cached_hop,
                warning=(
                    "Traceroute found no responding first hop; continuing with "
                    "the last known hop and retrying later."
                ),
                public_ip=public_ip or cached_public_ip,
                isp_hop_source="stale-cache",
                cache_updated_at=cached.get("updated_at"),
            )

        return DiscoveryResult(
            gateway=gateway,
            interface=interface,
            isp_hop=None,
            warning=(
                "No responding hop beyond the gateway was found. "
                "The ISP may filter traceroute; public endpoint monitoring continues."
            ),
            public_ip=public_ip,
        )

    def public_ipv4(self) -> str | None:
        try:
            completed = subprocess.run(
                [
                    self.curl_command,
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--proto",
                    "=https",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "10",
                    self.public_ip_url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        candidate = (completed.stdout or "").strip()
        if completed.returncode == 0 and is_usable_ipv4(candidate):
            return candidate
        return None

    def default_gateway(self) -> tuple[str | None, str | None]:
        # Linux and Enterprise Linux use iproute2 and commonly have neither
        # route(8) nor netstat(8) installed.
        try:
            completed = subprocess.run(
                [self.ip_command, "-4", "route", "show", "default"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
            for line in (completed.stdout or "").splitlines():
                fields = line.split()
                if not fields or fields[0] != "default":
                    continue
                gateway = fields[fields.index("via") + 1] if "via" in fields else None
                interface = (
                    fields[fields.index("dev") + 1] if "dev" in fields else None
                )
                if gateway and is_usable_ipv4(gateway):
                    return gateway, interface
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass

        # FreeBSD and macOS.
        try:
            completed = subprocess.run(
                [self.route_command, "-n", "get", "default"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
            fields = dict(ROUTE_FIELD_RE.findall(completed.stdout or ""))
            gateway = fields.get("gateway")
            interface = fields.get("interface")
            if gateway and is_usable_ipv4(gateway):
                return gateway, interface
        except (OSError, subprocess.TimeoutExpired):
            pass

        # Fallback for unusual route output.
        try:
            completed = subprocess.run(
                [self.netstat_command, "-rn", "-f", "inet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
            for line in (completed.stdout or "").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0] in ("default", "0.0.0.0"):
                    if is_usable_ipv4(fields[1]):
                        return fields[1], fields[-1] if len(fields) >= 4 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None, None

    def first_hop_after_gateway(self, gateway: str) -> str | None:
        try:
            completed = subprocess.run(
                [
                    self.traceroute_command,
                    "-n",
                    "-m",
                    "12",
                    "-q",
                    "1",
                    "-w",
                    "2",
                    self.trace_target,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in (completed.stdout or "").splitlines():
            hop_match = TRACE_LINE_RE.match(line)
            if not hop_match:
                continue
            for token in hop_match.group(1).split():
                candidate = token.strip("()")
                if candidate in (gateway, self.trace_target):
                    continue
                if is_usable_ipv4(candidate):
                    return candidate
        return None


def is_usable_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.version == 4
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
    )


def targets_from_discovery(discovery: DiscoveryResult) -> list[Target]:
    targets: list[Target] = []
    if discovery.gateway:
        targets.append(Target("gateway", TARGET_LABELS["gateway"], discovery.gateway))
    if discovery.isp_hop:
        targets.append(Target("isp_hop", TARGET_LABELS["isp_hop"], discovery.isp_hop))
    for key, (label, address) in PUBLIC_TARGETS.items():
        targets.append(Target(key, label, address))
    return targets


def unavailable_metric() -> dict[str, Any]:
    return {
        "sent": 0,
        "received": 0,
        "loss_pct": None,
        "rtt_min_ms": None,
        "rtt_avg_ms": None,
        "rtt_max_ms": None,
        "jitter_ms": None,
        "errors": 0,
    }


def classify(metrics: dict[str, dict[str, Any]]) -> dict[str, str]:
    def loss(key: str) -> float | None:
        value = metrics.get(key, {}).get("loss_pct")
        return float(value) if value is not None else None

    public_losses = [
        value for value in (loss("cloudflare"), loss("google"), loss("quad9")) if value is not None
    ]
    affected_public = sum(value >= 1.0 for value in public_losses)
    gateway_loss = loss("gateway")
    isp_loss = loss("isp_hop")

    if not public_losses:
        return {
            "code": "collecting",
            "severity": "neutral",
            "message": "Collecting initial samples",
        }
    if (
        gateway_loss is not None
        and gateway_loss >= 1.0
        and affected_public >= 2
    ):
        return {
            "code": "gateway_loss",
            "severity": "critical",
            "message": "Loss is visible on the path to the Router",
        }
    if gateway_loss is not None and gateway_loss >= 1.0 and affected_public == 0:
        return {
            "code": "gateway_icmp_limited",
            "severity": "warning",
            "message": "Gateway ICMP loss observed; Internet forwarding is clean",
        }
    if affected_public >= 2:
        if isp_loss is not None and isp_loss >= 1.0:
            message = "Probable ISP-path packet loss"
            code = "isp_path_loss"
        else:
            message = "Packet loss reaches multiple Internet targets"
            code = "internet_loss"
        return {"code": code, "severity": "critical", "message": message}
    if isp_loss is not None and isp_loss >= 1.0 and affected_public == 0:
        return {
            "code": "hop_icmp_limited",
            "severity": "warning",
            "message": "First Hop likely de-prioritizes ICMP; forwarding is clean",
        }
    if affected_public == 1:
        return {
            "code": "single_target_loss",
            "severity": "warning",
            "message": "Loss is isolated to one Internet target",
        }
    public_rtts = [
        metrics.get(key, {}).get("rtt_avg_ms")
        for key in PUBLIC_TARGETS
        if metrics.get(key, {}).get("rtt_avg_ms") is not None
    ]
    if public_rtts and sum(public_rtts) / len(public_rtts) >= 150:
        return {
            "code": "high_latency",
            "severity": "warning",
            "message": "High Internet latency",
        }
    return {"code": "healthy", "severity": "healthy", "message": "Connection looks healthy"}


def create_record(
    start: dt.datetime,
    end: dt.datetime,
    targets: Iterable[Target],
    accumulators: dict[str, StatsAccumulator],
) -> dict[str, Any]:
    target_by_key = {target.key: target for target in targets}
    metrics: dict[str, dict[str, Any]] = {}
    for key in TARGET_ORDER:
        target = target_by_key.get(key)
        snapshot = accumulators[key].snapshot() if key in accumulators else unavailable_metric()
        snapshot["address"] = target.address if target else None
        snapshot["label"] = TARGET_LABELS[key]
        metrics[key] = snapshot
    diagnosis = classify(metrics)
    return {
        "start": iso_utc(start),
        "end": iso_utc(end),
        "duration_seconds": round((end - start).total_seconds(), 3),
        "targets": metrics,
        "diagnosis": diagnosis,
    }


class CsvStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("r", newline="", encoding="utf-8") as handle:
                    header = next(csv.reader(handle), [])
                if header != CSV_FIELDS:
                    raise RuntimeError(
                        f"{self.path} has an incompatible CSV header; "
                        "move it aside before starting this version"
                    )
                return
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                handle.flush()
                os.fsync(handle.fileno())

    def append(self, record: dict[str, Any]) -> None:
        row: dict[str, Any] = {
            "schema_version": "1",
            "interval_start_utc": record["start"],
            "interval_end_utc": record["end"],
            "duration_seconds": record["duration_seconds"],
            "status_code": record["diagnosis"]["code"],
            "status_message": record["diagnosis"]["message"],
        }
        for key in TARGET_ORDER:
            target = record["targets"][key]
            row[f"{key}_address"] = target.get("address") or ""
            for field in METRIC_FIELDS:
                value = target.get(field)
                row[f"{key}_{field}"] = "" if value is None else value
        with self._lock:
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())

    def load(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: deque[dict[str, Any]] = deque(maxlen=limit)
        for record in self.iter_records():
            records.append(record)
        return list(records)

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return
        with self._lock:
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    record = self._record_from_row(row)
                    if record:
                        yield record

    @staticmethod
    def _record_from_row(row: dict[str, str]) -> dict[str, Any] | None:
        if row.get("schema_version") != "1":
            return None
        metrics: dict[str, dict[str, Any]] = {}
        for key in TARGET_ORDER:
            target: dict[str, Any] = {
                "label": TARGET_LABELS[key],
                "address": row.get(f"{key}_address") or None,
                "sent": parse_optional_int(row.get(f"{key}_sent")),
                "received": parse_optional_int(row.get(f"{key}_received")),
                "errors": 0,
            }
            for field in METRIC_FIELDS[2:]:
                target[field] = parse_optional_float(row.get(f"{key}_{field}"))
            metrics[key] = target
        diagnosis = {
            "code": row.get("status_code") or "unknown",
            "severity": classify(metrics)["severity"],
            "message": row.get("status_message") or "Unknown",
        }
        return {
            "start": row.get("interval_start_utc"),
            "end": row.get("interval_end_utc"),
            "duration_seconds": parse_optional_float(row.get("duration_seconds")),
            "targets": metrics,
            "diagnosis": diagnosis,
        }


POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS isp_loss_intervals (
    interval_start timestamptz PRIMARY KEY,
    interval_end timestamptz NOT NULL,
    duration_seconds double precision,
    status_code text NOT NULL,
    status_message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS isp_loss_measurements (
    interval_start timestamptz NOT NULL
        REFERENCES isp_loss_intervals(interval_start) ON DELETE CASCADE,
    target_key text NOT NULL,
    label text NOT NULL,
    address inet,
    sent integer NOT NULL,
    received integer NOT NULL,
    loss_pct double precision,
    rtt_min_ms double precision,
    rtt_avg_ms double precision,
    rtt_max_ms double precision,
    jitter_ms double precision,
    errors integer NOT NULL DEFAULT 0,
    PRIMARY KEY (interval_start, target_key)
);

CREATE INDEX IF NOT EXISTS isp_loss_intervals_end_idx
    ON isp_loss_intervals (interval_end DESC);
CREATE INDEX IF NOT EXISTS isp_loss_measurements_target_idx
    ON isp_loss_measurements (target_key, interval_start DESC);
"""


def postgres_text(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def postgres_number(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return str(value)
    raise ValueError(f"invalid numeric database value: {value!r}")


class PostgresStore:
    def __init__(
        self,
        database_url: str = "postgresql:///isp_loss_monitor",
        psql_command: str = "psql",
    ) -> None:
        self.database_url = database_url
        self.psql_command = psql_command

    def _run(
        self,
        sql: str,
        *,
        capture_output: bool = False,
        timeout: int = 60,
    ) -> str:
        command = [
            self.psql_command,
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "--dbname",
            self.database_url,
        ]
        if capture_output:
            command.extend(["-A", "-t"])
        try:
            completed = subprocess.run(
                command,
                input=sql,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"PostgreSQL command failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "unknown psql error").strip()
            raise RuntimeError(f"PostgreSQL command failed: {detail[-1200:]}")
        return completed.stdout or ""

    def ensure(self) -> None:
        self._run(POSTGRES_SCHEMA_SQL)

    @staticmethod
    def record_sql(record: dict[str, Any]) -> str:
        start = postgres_text(record["start"])
        diagnosis = record["diagnosis"]
        statements = [
            (
                "INSERT INTO isp_loss_intervals "
                "(interval_start, interval_end, duration_seconds, "
                "status_code, status_message) VALUES "
                f"({start}::timestamptz, "
                f"{postgres_text(record['end'])}::timestamptz, "
                f"{postgres_number(record.get('duration_seconds'))}, "
                f"{postgres_text(diagnosis['code'])}, "
                f"{postgres_text(diagnosis['message'])}) "
                "ON CONFLICT (interval_start) DO UPDATE SET "
                "interval_end = EXCLUDED.interval_end, "
                "duration_seconds = EXCLUDED.duration_seconds, "
                "status_code = EXCLUDED.status_code, "
                "status_message = EXCLUDED.status_message;"
            )
        ]
        for key in TARGET_ORDER:
            target = record["targets"].get(key, unavailable_metric())
            address = target.get("address")
            address_sql = (
                "NULL" if not address else f"{postgres_text(address)}::inet"
            )
            statements.append(
                "INSERT INTO isp_loss_measurements "
                "(interval_start, target_key, label, address, sent, received, "
                "loss_pct, rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms, errors) "
                "VALUES "
                f"({start}::timestamptz, {postgres_text(key)}, "
                f"{postgres_text(target.get('label') or TARGET_LABELS[key])}, "
                f"{address_sql}, {postgres_number(target.get('sent', 0))}, "
                f"{postgres_number(target.get('received', 0))}, "
                f"{postgres_number(target.get('loss_pct'))}, "
                f"{postgres_number(target.get('rtt_min_ms'))}, "
                f"{postgres_number(target.get('rtt_avg_ms'))}, "
                f"{postgres_number(target.get('rtt_max_ms'))}, "
                f"{postgres_number(target.get('jitter_ms'))}, "
                f"{postgres_number(target.get('errors', 0))}) "
                "ON CONFLICT (interval_start, target_key) DO UPDATE SET "
                "label = EXCLUDED.label, address = EXCLUDED.address, "
                "sent = EXCLUDED.sent, received = EXCLUDED.received, "
                "loss_pct = EXCLUDED.loss_pct, rtt_min_ms = EXCLUDED.rtt_min_ms, "
                "rtt_avg_ms = EXCLUDED.rtt_avg_ms, "
                "rtt_max_ms = EXCLUDED.rtt_max_ms, "
                "jitter_ms = EXCLUDED.jitter_ms, errors = EXCLUDED.errors;"
            )
        return "\n".join(statements)

    def append(self, record: dict[str, Any]) -> None:
        self._run("BEGIN;\n" + self.record_sql(record) + "\nCOMMIT;")

    def import_records(
        self,
        records: Iterable[dict[str, Any]],
        batch_size: int = 200,
    ) -> int:
        batch: list[dict[str, Any]] = []
        imported = 0
        for record in records:
            batch.append(record)
            if len(batch) >= batch_size:
                self._write_batch(batch)
                imported += len(batch)
                batch.clear()
        if batch:
            self._write_batch(batch)
            imported += len(batch)
        return imported

    def _write_batch(self, records: list[dict[str, Any]]) -> None:
        sql = ["BEGIN;"]
        sql.extend(self.record_sql(record) for record in records)
        sql.append("COMMIT;")
        self._run("\n".join(sql), timeout=max(60, len(records)))

    def load(self, limit: int) -> list[dict[str, Any]]:
        sql = f"""
WITH recent AS (
    SELECT interval_start, interval_end, duration_seconds,
           status_code, status_message
    FROM isp_loss_intervals
    ORDER BY interval_start DESC
    LIMIT {int(limit)}
)
SELECT json_build_object(
    'start', to_char(recent.interval_start AT TIME ZONE 'UTC',
                     'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'end', to_char(recent.interval_end AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'duration_seconds', recent.duration_seconds,
    'diagnosis', json_build_object(
        'code', recent.status_code,
        'message', recent.status_message
    ),
    'targets', json_object_agg(
        measurements.target_key,
        json_build_object(
            'label', measurements.label,
            'address', host(measurements.address),
            'sent', measurements.sent,
            'received', measurements.received,
            'loss_pct', measurements.loss_pct,
            'rtt_min_ms', measurements.rtt_min_ms,
            'rtt_avg_ms', measurements.rtt_avg_ms,
            'rtt_max_ms', measurements.rtt_max_ms,
            'jitter_ms', measurements.jitter_ms,
            'errors', measurements.errors
        )
    )
)::text
FROM recent
JOIN isp_loss_measurements AS measurements
  ON measurements.interval_start = recent.interval_start
GROUP BY recent.interval_start, recent.interval_end, recent.duration_seconds,
         recent.status_code, recent.status_message
ORDER BY recent.interval_start;
"""
        output = self._run(sql, capture_output=True)
        records: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            for key, target in record["targets"].items():
                if key in TARGET_LABELS:
                    target["label"] = TARGET_LABELS[key]
            record["diagnosis"]["severity"] = classify(record["targets"])[
                "severity"
            ]
            records.append(record)
        return records

    def continuous_bounds(
        self,
        max_gap_seconds: int = 600,
    ) -> tuple[str | None, str | None]:
        if max_gap_seconds < 0 or max_gap_seconds > 86400:
            raise ValueError("continuous-runtime gap must be between 0 and 86400")
        sql = f"""
WITH ordered AS (
    SELECT interval_start, interval_end,
           lag(interval_end) OVER (ORDER BY interval_start) AS previous_end
    FROM isp_loss_intervals
),
grouped AS (
    SELECT interval_start, interval_end,
           sum(
               CASE
                   WHEN previous_end IS NOT NULL
                    AND extract(epoch FROM interval_start - previous_end)
                        > {int(max_gap_seconds)}
                   THEN 1
                   ELSE 0
               END
           ) OVER (ORDER BY interval_start) AS continuity_group
    FROM ordered
)
SELECT
    to_char(min(interval_start) AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    to_char(max(interval_end) AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS"Z"')
FROM grouped
WHERE continuity_group = (SELECT max(continuity_group) FROM grouped);
"""
        output = self._run(sql, capture_output=True).strip()
        if not output:
            return None, None
        start, separator, end = output.partition("|")
        if not separator or not start or not end:
            return None, None
        return start, end


def merge_history(
    csv_history: Iterable[dict[str, Any]],
    postgres_history: Iterable[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    merged = {record["start"]: record for record in csv_history if record.get("start")}
    merged.update(
        {
            record["start"]: record
            for record in postgres_history
            if record.get("start")
        }
    )
    return [merged[key] for key in sorted(merged)][-limit:]


def last_known_discovery(
    history: Iterable[dict[str, Any]],
) -> tuple[str | None, str | None]:
    records = list(history)
    for record in reversed(records):
        targets = record.get("targets", {})
        isp_hop = targets.get("isp_hop", {}).get("address")
        if isp_hop:
            gateway = targets.get("gateway", {}).get("address")
            return gateway, isp_hop
    return None, None


class MonitorState:
    def __init__(
        self,
        history: list[dict[str, Any]],
        history_limit: int,
        continuous_started_at: str | None = None,
        continuous_last_end: str | None = None,
        continuous_gap_seconds: int = 600,
    ) -> None:
        self._lock = threading.RLock()
        self.started_at = utc_now()
        self.discovery: DiscoveryResult | None = None
        self.current: dict[str, Any] | None = None
        self.next_interval_start: str | None = None
        self.continuous_started_at = parse_iso_utc(continuous_started_at)
        self.continuous_last_end = parse_iso_utc(continuous_last_end)
        self.continuous_gap_seconds = continuous_gap_seconds
        self.history: deque[dict[str, Any]] = deque(history, maxlen=history_limit)

    def set_discovery(self, discovery: DiscoveryResult) -> None:
        with self._lock:
            self.discovery = discovery

    def begin_interval(
        self,
        start: dt.datetime,
        deadline: dt.datetime,
        targets: Iterable[Target],
    ) -> None:
        with self._lock:
            self._continue_or_reset(start)
            self.next_interval_start = None
            self.current = {
                "start": iso_utc(start),
                "deadline": iso_utc(deadline),
                "progress_pct": 0.0,
                "bursts_completed": 0,
                "targets": {
                    target.key: {
                        **unavailable_metric(),
                        "address": target.address,
                        "label": target.label,
                    }
                    for target in targets
                },
            }

    def wait_for_interval(self, start: dt.datetime) -> None:
        with self._lock:
            self._continue_or_reset(start)
            self.current = None
            self.next_interval_start = iso_utc(start)

    def _continue_or_reset(self, start: dt.datetime) -> None:
        start = start.astimezone(dt.timezone.utc)
        if self.continuous_last_end is None:
            if self.continuous_started_at is None:
                self.continuous_started_at = start
            return
        gap = (start - self.continuous_last_end).total_seconds()
        if gap > self.continuous_gap_seconds:
            self.continuous_started_at = start
            self.continuous_last_end = None

    def update_current(
        self,
        targets: Iterable[Target],
        accumulators: dict[str, StatsAccumulator],
        progress_pct: float,
        bursts_completed: int,
    ) -> None:
        with self._lock:
            if self.current is None:
                return
            self.current["progress_pct"] = finite_round(
                max(0.0, min(100.0, progress_pct)), 1
            )
            self.current["bursts_completed"] = bursts_completed
            self.current["targets"] = {
                target.key: {
                    **accumulators[target.key].snapshot(),
                    "address": target.address,
                    "label": target.label,
                }
                for target in targets
            }
            self.current["diagnosis"] = classify(self.current["targets"])

    def finish_interval(self, record: dict[str, Any]) -> None:
        with self._lock:
            self.history.append(record)
            self.current = None
            record_start = parse_iso_utc(record.get("start"))
            record_end = parse_iso_utc(record.get("end"))
            if self.continuous_started_at is None and record_start:
                self.continuous_started_at = record_start
            if record_end:
                self.continuous_last_end = record_end

    def cancel_interval(self) -> None:
        with self._lock:
            self.current = None

    def snapshot(self, history_limit: int | None = None) -> dict[str, Any]:
        with self._lock:
            now = utc_now()
            history = list(self.history)
            if history_limit is not None:
                history = history[-history_limit:]
            discovery = dataclasses.asdict(self.discovery) if self.discovery else None
            continuous_runtime = (
                max(0, round((now - self.continuous_started_at).total_seconds()))
                if self.continuous_started_at
                else 0
            )
            return {
                "version": VERSION,
                "server_time": iso_utc(now),
                "started_at": iso_utc(self.started_at),
                "runtime_seconds": round((now - self.started_at).total_seconds()),
                "continuous_started_at": (
                    iso_utc(self.continuous_started_at)
                    if self.continuous_started_at
                    else None
                ),
                "continuous_runtime_seconds": continuous_runtime,
                "discovery": discovery,
                "current": self.current,
                "next_interval_start": self.next_interval_start,
                "latest": history[-1] if history else None,
                "history": history,
            }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        state: MonitorState,
        csv_path: Path,
        web_root: Path,
        is_tls: bool = False,
    ) -> None:
        super().__init__(address, handler)
        self.monitor_state = state
        self.csv_path = csv_path
        self.web_root = web_root
        self.is_tls = is_tls


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "UplinkLedger"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = max(1, min(2016, int(query.get("limit", ["288"])[0])))
            except ValueError:
                limit = 288
            self.send_json(self.server.monitor_state.snapshot(limit))
            return
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "version": VERSION})
            return
        if parsed.path == "/export.csv":
            self.send_csv()
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        item = static_files.get(parsed.path)
        if item:
            self.send_static(*item)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_json(self, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(HTTPStatus.OK)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self) -> None:
        try:
            body = self.server.csv_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "CSV log is not available")
            return
        self.send_response(HTTPStatus.OK)
        self.security_headers()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            'attachment; filename="isp-packet-loss.csv"',
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename: str, content_type: str) -> None:
        path = self.server.web_root / filename
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Dashboard asset is missing: {filename}",
            )
            return
        self.send_response(HTTPStatus.OK)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.server.is_tls:
            self.send_header(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Dashboard polling should not flood the service log.
        return


class RedirectServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        secure_port: int,
        fallback_host: str,
    ) -> None:
        super().__init__(address, handler)
        self.secure_port = secure_port
        self.fallback_host = fallback_host


class RedirectHandler(BaseHTTPRequestHandler):
    server: RedirectServer
    server_version = "UplinkLedgerRedirect"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _destination_host(self) -> str:
        raw_host = self.headers.get("Host", "").strip()
        try:
            hostname = urllib.parse.urlsplit(f"//{raw_host}").hostname
        except ValueError:
            hostname = None
        if hostname and ":" in hostname:
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                hostname = None
            else:
                hostname = f"[{hostname}]"
        elif hostname and not re.fullmatch(r"[A-Za-z0-9._-]+", hostname):
            hostname = None
        return hostname or self.server.fallback_host

    def _redirect(self) -> None:
        target = self.path if self.path.startswith("/") else "/"
        if any(ord(character) < 32 for character in target):
            target = "/"
        port_suffix = (
            "" if self.server.secure_port == 443 else f":{self.server.secure_port}"
        )
        location = (
            f"https://{self._destination_host()}{port_suffix}{target}"
        )
        self.send_response(HTTPStatus.PERMANENT_REDIRECT)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _redirect
    do_HEAD = _redirect
    do_POST = _redirect
    do_PUT = _redirect
    do_PATCH = _redirect
    do_DELETE = _redirect
    do_OPTIONS = _redirect

    def log_message(self, format: str, *args: Any) -> None:
        return


class TerminalReporter:
    def __init__(self, mode: str, dashboard_url: str) -> None:
        self.mode = "dashboard" if mode == "auto" and sys.stdout.isatty() else mode
        if self.mode == "auto":
            self.mode = "lines"
        self.dashboard_url = dashboard_url

    def startup(
        self,
        discovery: DiscoveryResult,
        csv_path: Path,
        postgres_url: str,
        interval_seconds: int,
        burst_period: int,
        ping_count: int,
    ) -> None:
        print(f"Uplink Ledger {VERSION}")
        print(f"Dashboard: {self.dashboard_url}")
        print(f"CSV log:   {csv_path}")
        print(f"PostgreSQL: {postgres_url}")
        print(
            f"Sampling:  {ping_count} pings every {burst_period}s per target; "
            f"{interval_seconds}s intervals"
        )
        print(
            f"Router:    {discovery.gateway or 'not found'}"
            + (f" via {discovery.interface}" if discovery.interface else "")
        )
        print(f"Public IP: {discovery.public_ip or 'not available'}")
        hop_source = (
            f" ({discovery.isp_hop_source})"
            if discovery.isp_hop_source
            else ""
        )
        print(f"First Hop: {discovery.isp_hop or 'not found'}{hop_source}")
        if discovery.warning:
            print(f"Warning:   {discovery.warning}")
        print("Press Ctrl+C to stop.\n", flush=True)

    def render_live(
        self,
        current: dict[str, Any],
        latest: dict[str, Any] | None,
    ) -> None:
        if self.mode != "dashboard":
            return
        lines = [
            "\033[2J\033[H",
            f" ISP QUALITY MONITOR  {dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
            "=" * 92,
            f" Dashboard: {self.dashboard_url}",
            f" Interval:  {current['start']}  "
            f"[{progress_bar(current.get('progress_pct', 0), 24)}] "
            f"{current.get('progress_pct', 0):5.1f}%",
            "",
            f" {'Target':<14} {'Address':<16} {'Loss':>8} {'Avg RTT':>10} "
            f"{'Max RTT':>10} {'Jitter':>10} {'Recv/Sent':>11}",
            " " + "-" * 90,
        ]
        for key in TARGET_ORDER:
            metric = current["targets"].get(key)
            if not metric:
                continue
            lines.append(format_metric_line(metric))
        diagnosis = current.get("diagnosis", {}).get("message", "Collecting samples")
        lines.extend(
            [
                "",
                f" Current status: {diagnosis}",
                (
                    f" Last interval:  {latest['diagnosis']['message']}"
                    if latest
                    else " Last interval:  none completed yet"
                ),
                "=" * 92,
                " Ctrl+C stops the monitor cleanly.",
            ]
        )
        print("\n".join(lines), flush=True)

    def interval_complete(self, record: dict[str, Any]) -> None:
        if self.mode in ("quiet", "dashboard"):
            return
        parts = []
        for key in TARGET_ORDER:
            metric = record["targets"][key]
            if not metric.get("address"):
                continue
            loss = metric.get("loss_pct")
            avg = metric.get("rtt_avg_ms")
            parts.append(
                f"{key}={format_number(loss, '%')}/{format_number(avg, 'ms')}"
            )
        print(
            f"{record['end']}  {record['diagnosis']['message']}  " + "  ".join(parts),
            flush=True,
        )

    def stopped(self) -> None:
        if self.mode == "dashboard":
            print("\nMonitor stopped.", flush=True)
        elif self.mode != "quiet":
            print("Monitor stopped.", flush=True)


def progress_bar(value: float, width: int) -> str:
    filled = round(max(0.0, min(100.0, value)) * width / 100)
    return "#" * filled + "-" * (width - filled)


def format_number(value: float | None, suffix: str) -> str:
    return "N/A" if value is None else f"{value:.1f}{suffix}"


def format_metric_line(metric: dict[str, Any]) -> str:
    label = metric["label"][:14]
    address = (metric.get("address") or "unavailable")[:16]
    recv_sent = f"{metric.get('received', 0)}/{metric.get('sent', 0)}"
    return (
        f" {label:<14} {address:<16} "
        f"{format_number(metric.get('loss_pct'), '%'):>8} "
        f"{format_number(metric.get('rtt_avg_ms'), 'ms'):>10} "
        f"{format_number(metric.get('rtt_max_ms'), 'ms'):>10} "
        f"{format_number(metric.get('jitter_ms'), 'ms'):>10} "
        f"{recv_sent:>11}"
    )


@dataclasses.dataclass
class MonitorConfig:
    listen: str
    port: int
    http_redirect_port: int | None
    csv_path: Path
    discovery_cache_path: Path
    web_root: Path
    tls_cert: Path | None
    tls_key: Path | None
    interval_seconds: int
    burst_period: int
    ping_count: int
    ping_interval: float
    reply_timeout: float
    discovery_period: int
    history_limit: int
    terminal_mode: str
    ping_command: str
    route_command: str
    netstat_command: str
    ip_command: str
    curl_command: str
    traceroute_command: str
    gateway_address: str | None
    public_ip_url: str
    postgres_url: str
    psql_command: str


class Monitor:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.csv_store = CsvStore(config.csv_path)
        self.csv_store.ensure()
        csv_history = self.csv_store.load(config.history_limit)
        self.postgres_store = PostgresStore(
            database_url=config.postgres_url,
            psql_command=config.psql_command,
        )
        self.postgres_store.ensure()
        # This automatically carries recent pre-upgrade CSV history into a new
        # database. The standalone importer handles CSV archives larger than
        # the in-memory dashboard window.
        self.postgres_store.import_records(csv_history)
        continuous_start, continuous_end = self.postgres_store.continuous_bounds()
        postgres_history = self.postgres_store.load(config.history_limit)
        history = merge_history(
            csv_history, postgres_history, config.history_limit
        )
        self.state = MonitorState(
            history,
            config.history_limit,
            continuous_start,
            continuous_end,
        )
        self.discovery_client = NetworkDiscovery(
            route_command=config.route_command,
            netstat_command=config.netstat_command,
            ip_command=config.ip_command,
            curl_command=config.curl_command,
            traceroute_command=config.traceroute_command,
            public_ip_url=config.public_ip_url,
            gateway_address=config.gateway_address,
            cache_path=config.discovery_cache_path,
        )
        gateway, isp_hop = last_known_discovery(history)
        self.discovery_client.seed_cached_hop(gateway, None, isp_hop)
        self.ping_runner = PingRunner(
            config.ping_command,
            config.ping_count,
            config.ping_interval,
            config.reply_timeout,
        )
        shown_host = (
            socket.gethostname()
            if config.listen in ("0.0.0.0", "::")
            else config.listen
        )
        scheme = "https" if config.tls_cert else "http"
        standard_port = (scheme == "https" and config.port == 443) or (
            scheme == "http" and config.port == 80
        )
        port_suffix = "" if standard_port else f":{config.port}"
        self.dashboard_url = f"{scheme}://{shown_host}{port_suffix}/"
        self.reporter = TerminalReporter(config.terminal_mode, self.dashboard_url)
        self.http_server: DashboardServer | None = None
        self.http_thread: threading.Thread | None = None
        self.redirect_server: RedirectServer | None = None
        self.redirect_thread: threading.Thread | None = None
        self.discovery_thread: threading.Thread | None = None
        self._discovery_lock = threading.Lock()
        self._discovery: DiscoveryResult | None = None

    def start_http(self) -> None:
        self.http_server = DashboardServer(
            (self.config.listen, self.config.port),
            DashboardHandler,
            self.state,
            self.config.csv_path,
            self.config.web_root,
            is_tls=bool(self.config.tls_cert),
        )
        if self.config.tls_cert and self.config.tls_key:
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.load_cert_chain(
                certfile=self.config.tls_cert,
                keyfile=self.config.tls_key,
            )
            self.http_server.socket = tls_context.wrap_socket(
                self.http_server.socket,
                server_side=True,
            )
        if self.config.http_redirect_port is not None:
            fallback_host = (
                socket.gethostname()
                if self.config.listen in ("0.0.0.0", "::")
                else self.config.listen
            )
            self.redirect_server = RedirectServer(
                (self.config.listen, self.config.http_redirect_port),
                RedirectHandler,
                self.config.port,
                fallback_host,
            )
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            name="dashboard-http",
            daemon=True,
        )
        self.http_thread.start()
        if self.redirect_server:
            self.redirect_thread = threading.Thread(
                target=self.redirect_server.serve_forever,
                name="http-to-https-redirect",
                daemon=True,
            )
            self.redirect_thread.start()

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()

    def _set_discovery(self, discovery: DiscoveryResult) -> None:
        with self._discovery_lock:
            self._discovery = discovery
        self.state.set_discovery(discovery)

    def _targets(self) -> list[Target]:
        with self._discovery_lock:
            discovery = self._discovery
        return targets_from_discovery(discovery) if discovery else []

    def _rediscovery_loop(self) -> None:
        while not self.stop_event.wait(self.config.discovery_period):
            discovery = self.discovery_client.discover()
            self._set_discovery(discovery)

    def _wait_until(self, boundary: dt.datetime) -> bool:
        while not self.stop_event.is_set():
            remaining = (boundary - utc_now()).total_seconds()
            if remaining <= 0:
                return True
            if self.stop_event.wait(min(remaining, 1.0)):
                return False
        return False

    def run(self) -> None:
        discovery = self.discovery_client.discover()
        self._set_discovery(discovery)
        self.start_http()
        self.reporter.startup(
            discovery,
            self.config.csv_path,
            self.config.postgres_url,
            self.config.interval_seconds,
            self.config.burst_period,
            self.config.ping_count,
        )
        self.discovery_thread = threading.Thread(
            target=self._rediscovery_loop,
            name="network-discovery",
            daemon=True,
        )
        self.discovery_thread.start()
        scheduled_start = next_interval_boundary(
            utc_now(), self.config.interval_seconds
        )
        try:
            while not self.stop_event.is_set():
                self.state.wait_for_interval(scheduled_start)
                if not self._wait_until(scheduled_start):
                    break
                scheduled_end = scheduled_start + dt.timedelta(
                    seconds=self.config.interval_seconds
                )
                self.run_interval(
                    self._targets(),
                    scheduled_start,
                    scheduled_end,
                )
                scheduled_start = scheduled_end
                # Normal CSV/PostgreSQL finalization may take a fraction of a
                # second. A substantial delay means the beginning of this
                # window was missed, so wait for the next complete boundary.
                if utc_now() >= scheduled_start + dt.timedelta(
                    seconds=self.config.burst_period
                ):
                    scheduled_start = next_interval_boundary(
                        utc_now(), self.config.interval_seconds
                    )
        finally:
            if self.redirect_server:
                self.redirect_server.shutdown()
                self.redirect_server.server_close()
            if self.http_server:
                self.http_server.shutdown()
                self.http_server.server_close()
            if self.redirect_thread:
                self.redirect_thread.join(timeout=3)
            if self.http_thread:
                self.http_thread.join(timeout=3)
            if self.discovery_thread:
                self.discovery_thread.join(timeout=1)
            self.reporter.stopped()

    def run_interval(
        self,
        targets: list[Target],
        started_wall: dt.datetime,
        deadline_wall: dt.datetime,
    ) -> bool:
        started_monotonic = time.monotonic()
        window_seconds = (deadline_wall - started_wall).total_seconds()
        remaining_seconds = max(0.0, (deadline_wall - utc_now()).total_seconds())
        deadline_monotonic = started_monotonic + remaining_seconds
        accumulators = {target.key: StatsAccumulator() for target in targets}
        self.state.begin_interval(started_wall, deadline_wall, targets)
        bursts_completed = 0
        next_burst = started_monotonic

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(targets), thread_name_prefix="ping"
        ) as pool:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= deadline_monotonic:
                    break
                wait_for = max(0.0, next_burst - now)
                if self.stop_event.wait(wait_for):
                    break
                futures = {
                    pool.submit(self.ping_runner.run, target): target for target in targets
                }
                for future, target in futures.items():
                    try:
                        result = future.result()
                    except Exception as exc:  # defensive: keep other probes alive
                        result = BurstResult(
                            sent=self.config.ping_count,
                            received=0,
                            rtts_ms=[],
                            error=f"probe failed: {exc}",
                        )
                    accumulators[target.key].add(result)
                bursts_completed += 1
                remaining = max(0.0, (deadline_wall - utc_now()).total_seconds())
                progress = (window_seconds - remaining) * 100 / window_seconds
                self.state.update_current(
                    targets, accumulators, progress, bursts_completed
                )
                current = self.state.snapshot(history_limit=1)["current"]
                latest = self.state.snapshot(history_limit=1)["latest"]
                if current:
                    self.reporter.render_live(current, latest)
                # Skip missed slots rather than firing catch-up probe storms.
                next_burst += self.config.burst_period
                if next_burst < time.monotonic():
                    next_burst = time.monotonic()

        if bursts_completed == 0 or utc_now() < deadline_wall:
            self.state.cancel_interval()
            return False
        record = create_record(
            started_wall,
            deadline_wall,
            targets,
            accumulators,
        )
        self.csv_store.append(record)
        self.postgres_store.append(record)
        self.state.finish_interval(record)
        self.reporter.interval_complete(record)
        return True


def build_parser() -> argparse.ArgumentParser:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Continuously measure Internet-path packet loss, RTT, and jitter "
            "and serve the Uplink Ledger dashboard."
        )
    )
    parser.add_argument(
        "--listen",
        default="127.0.0.1",
        help="dashboard listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=443, help="dashboard TCP port (default: 443)"
    )
    parser.add_argument(
        "--http-redirect-port",
        type=int,
        help="plain-HTTP port that permanently redirects to HTTPS",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("isp-packet-loss.csv"),
        help="CSV log path (default: ./isp-packet-loss.csv)",
    )
    parser.add_argument(
        "--discovery-cache",
        type=Path,
        default=Path("discovery-cache.json"),
        help="persistent public-IP/ISP-hop cache path",
    )
    parser.add_argument(
        "--web-root",
        type=Path,
        default=base_dir / "web",
        help="directory containing index.html, app.js, and styles.css",
    )
    parser.add_argument(
        "--gateway-address",
        help=(
            "router LAN address; defaults to the host's IPv4 default gateway"
        ),
    )
    parser.add_argument(
        "--public-ip-url",
        default="https://ipv4.icanhazip.com/",
        help="HTTPS endpoint returning the network's public IPv4 address",
    )
    parser.add_argument(
        "--postgres-url",
        default="postgresql:///isp_loss_monitor",
        help=(
            "PostgreSQL connection URI "
            "(default: postgresql:///isp_loss_monitor via local peer auth)"
        ),
    )
    parser.add_argument(
        "--tls-cert",
        type=Path,
        help="PEM certificate or full-chain file for the HTTPS dashboard",
    )
    parser.add_argument(
        "--tls-key",
        type=Path,
        help="PEM private-key file for the HTTPS dashboard",
    )
    parser.add_argument(
        "--insecure-http",
        action="store_true",
        help="explicitly allow plain HTTP instead of TLS (testing only)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="aggregation interval (default: 300)",
    )
    parser.add_argument(
        "--burst-period",
        type=int,
        default=10,
        help="seconds between ping bursts (default: 10)",
    )
    parser.add_argument(
        "--ping-count",
        type=int,
        default=5,
        help="pings per target per burst (default: 5)",
    )
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=0.2,
        help="seconds between pings inside a burst (default: 0.2)",
    )
    parser.add_argument(
        "--reply-timeout",
        type=float,
        default=1.5,
        help="seconds to wait for a ping reply (default: 1.5)",
    )
    parser.add_argument(
        "--discovery-period",
        type=int,
        default=3600,
        help="seconds between gateway/hop rediscovery (default: 3600)",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=2016,
        help="maximum intervals kept in dashboard memory (default: 2016, seven days)",
    )
    parser.add_argument(
        "--terminal-mode",
        choices=("auto", "dashboard", "lines", "quiet"),
        default="auto",
        help="terminal output style (default: auto)",
    )
    parser.add_argument("--ping-command", default="ping", help=argparse.SUPPRESS)
    parser.add_argument("--route-command", default="route", help=argparse.SUPPRESS)
    parser.add_argument("--netstat-command", default="netstat", help=argparse.SUPPRESS)
    parser.add_argument("--ip-command", default="ip", help=argparse.SUPPRESS)
    parser.add_argument("--curl-command", default="curl", help=argparse.SUPPRESS)
    parser.add_argument("--psql-command", default="psql", help=argparse.SUPPRESS)
    parser.add_argument(
        "--traceroute-command", default="traceroute", help=argparse.SUPPRESS
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    checks = (
        (1 <= args.port <= 65535, "--port must be between 1 and 65535"),
        (
            args.http_redirect_port is None
            or 1 <= args.http_redirect_port <= 65535,
            "--http-redirect-port must be between 1 and 65535",
        ),
        (args.interval_seconds >= 10, "--interval-seconds must be at least 10"),
        (args.burst_period >= 1, "--burst-period must be at least 1"),
        (
            args.burst_period <= args.interval_seconds,
            "--burst-period cannot exceed --interval-seconds",
        ),
        (1 <= args.ping_count <= 100, "--ping-count must be between 1 and 100"),
        (args.ping_interval >= 0.1, "--ping-interval must be at least 0.1"),
        (args.reply_timeout >= 0.1, "--reply-timeout must be at least 0.1"),
        (args.discovery_period >= 30, "--discovery-period must be at least 30"),
        (args.history_limit >= 1, "--history-limit must be at least 1"),
    )
    for valid, message in checks:
        if not valid:
            parser.error(message)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if args.http_redirect_port == args.port:
        parser.error("--http-redirect-port must differ from --port")
    if args.http_redirect_port is not None and not args.tls_cert:
        parser.error("--http-redirect-port requires TLS")
    if not args.tls_cert and not args.insecure_http:
        parser.error(
            "TLS is required; supply --tls-cert and --tls-key "
            "(or use --insecure-http for local testing)"
        )
    for path_name in ("tls_cert", "tls_key"):
        path = getattr(args, path_name)
        if path and not path.expanduser().is_file():
            parser.error(
                f"--{path_name.replace('_', '-')} file was not found: {path}"
            )
    if args.gateway_address and not is_usable_ipv4(args.gateway_address):
        parser.error("--gateway-address must be a usable IPv4 address")
    public_ip_endpoint = urllib.parse.urlparse(args.public_ip_url)
    if public_ip_endpoint.scheme != "https" or not public_ip_endpoint.netloc:
        parser.error("--public-ip-url must be a valid HTTPS URL")
    postgres_endpoint = urllib.parse.urlparse(args.postgres_url)
    if postgres_endpoint.scheme not in ("postgresql", "postgres"):
        parser.error("--postgres-url must be a PostgreSQL connection URI")
    for command_name in (
        "ping_command",
        "traceroute_command",
        "curl_command",
        "psql_command",
    ):
        command = getattr(args, command_name)
        if os.path.sep not in command and not shutil.which(command):
            parser.error(f"required command was not found: {command}")
    required_assets = ("index.html", "app.js", "styles.css")
    missing = [name for name in required_assets if not (args.web_root / name).is_file()]
    if missing:
        parser.error(
            f"--web-root is missing dashboard assets: {', '.join(missing)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    config = MonitorConfig(
        listen=args.listen,
        port=args.port,
        http_redirect_port=args.http_redirect_port,
        csv_path=args.csv.expanduser().resolve(),
        discovery_cache_path=args.discovery_cache.expanduser().resolve(),
        web_root=args.web_root.expanduser().resolve(),
        tls_cert=args.tls_cert.expanduser().resolve() if args.tls_cert else None,
        tls_key=args.tls_key.expanduser().resolve() if args.tls_key else None,
        interval_seconds=args.interval_seconds,
        burst_period=args.burst_period,
        ping_count=args.ping_count,
        ping_interval=args.ping_interval,
        reply_timeout=args.reply_timeout,
        discovery_period=args.discovery_period,
        history_limit=args.history_limit,
        terminal_mode=args.terminal_mode,
        ping_command=args.ping_command,
        route_command=args.route_command,
        netstat_command=args.netstat_command,
        ip_command=args.ip_command,
        curl_command=args.curl_command,
        traceroute_command=args.traceroute_command,
        gateway_address=args.gateway_address,
        public_ip_url=args.public_ip_url,
        postgres_url=args.postgres_url,
        psql_command=args.psql_command,
    )
    try:
        monitor = Monitor(config)
        signal.signal(signal.SIGINT, monitor.request_stop)
        signal.signal(signal.SIGTERM, monitor.request_stop)
        monitor.run()
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
