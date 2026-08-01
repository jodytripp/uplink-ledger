import csv
import datetime as dt
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "uplink_ledger.py"
SPEC = importlib.util.spec_from_file_location("uplink_ledger", MODULE_PATH)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


FREEBSD_PING = """
PING 1.1.1.1 (1.1.1.1): 56 data bytes
64 bytes from 1.1.1.1: icmp_seq=0 ttl=58 time=11.100 ms
64 bytes from 1.1.1.1: icmp_seq=1 ttl=58 time=12.300 ms
64 bytes from 1.1.1.1: icmp_seq=3 ttl=58 time=14.700 ms

--- 1.1.1.1 ping statistics ---
5 packets transmitted, 3 packets received, 40.0% packet loss
round-trip min/avg/max/stddev = 11.100/12.700/14.700/1.497 ms
"""

LINUX_PING = """
PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=18.2 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=117 time=19.8 ms

--- 8.8.8.8 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 201ms
rtt min/avg/max/mdev = 18.200/19.000/19.800/0.800 ms
"""


class PingParsingTests(unittest.TestCase):
    def test_freebsd_ping(self):
        result = monitor.parse_ping_output(FREEBSD_PING, fallback_sent=5)
        self.assertEqual((result.sent, result.received), (5, 3))
        self.assertEqual(result.rtts_ms, [11.1, 12.3, 14.7])
        self.assertIsNone(result.error)

    def test_linux_ping(self):
        result = monitor.parse_ping_output(LINUX_PING, fallback_sent=2)
        self.assertEqual((result.sent, result.received), (2, 2))
        self.assertEqual(result.rtts_ms, [18.2, 19.8])

    def test_missing_summary_is_conservative(self):
        result = monitor.parse_ping_output(
            "64 bytes: icmp_seq=0 time=5.0 ms\n", fallback_sent=5
        )
        self.assertEqual((result.sent, result.received), (5, 1))
        self.assertIsNotNone(result.error)

    def test_accumulator_loss_rtt_and_jitter(self):
        stats = monitor.StatsAccumulator()
        stats.add(monitor.BurstResult(5, 3, [10.0, 12.0, 15.0]))
        stats.add(monitor.BurstResult(5, 5, [14.0, 12.0, 11.0, 10.0, 10.0]))
        value = stats.snapshot()
        self.assertEqual(value["sent"], 10)
        self.assertEqual(value["received"], 8)
        self.assertEqual(value["loss_pct"], 20.0)
        self.assertEqual(value["rtt_avg_ms"], 11.75)
        self.assertAlmostEqual(value["jitter_ms"], 10 / 7, places=3)


class IntervalAlignmentTests(unittest.TestCase):
    def test_next_boundary_is_an_exact_five_minute_mark(self):
        current = dt.datetime(
            2026,
            7,
            28,
            12,
            3,
            47,
            821000,
            tzinfo=dt.timezone.utc,
        )
        self.assertEqual(
            monitor.next_interval_boundary(current, 300),
            dt.datetime(2026, 7, 28, 12, 5, tzinfo=dt.timezone.utc),
        )

    def test_exact_boundary_advances_to_the_next_complete_window(self):
        current = dt.datetime(
            2026,
            7,
            28,
            12,
            5,
            tzinfo=dt.timezone.utc,
        )
        self.assertEqual(
            monitor.next_interval_boundary(current, 300),
            dt.datetime(2026, 7, 28, 12, 10, tzinfo=dt.timezone.utc),
        )

    def test_interrupted_interval_is_not_kept(self):
        instance = object.__new__(monitor.Monitor)
        instance.stop_event = threading.Event()
        instance.stop_event.set()
        instance.state = monitor.MonitorState([], 10)
        instance.config = mock.Mock(
            interval_seconds=300,
            burst_period=10,
            ping_count=5,
        )
        start = dt.datetime.now(dt.timezone.utc)
        completed = monitor.Monitor.run_interval(
            instance,
            [monitor.Target("gateway", "Router", "192.0.2.1")],
            start,
            start + dt.timedelta(minutes=5),
        )
        self.assertFalse(completed)
        self.assertIsNone(instance.state.snapshot()["current"])

    def test_continuous_runtime_survives_a_ten_minute_gap(self):
        now = dt.datetime(2026, 7, 28, 13, 10, tzinfo=dt.timezone.utc)
        with mock.patch.object(monitor, "utc_now", return_value=now):
            state = monitor.MonitorState(
                [],
                10,
                continuous_started_at="2026-07-28T10:45:00Z",
                continuous_last_end="2026-07-28T13:00:00Z",
            )
            state.wait_for_interval(now)
            snapshot = state.snapshot()
        self.assertEqual(
            snapshot["continuous_started_at"],
            "2026-07-28T10:45:00Z",
        )
        self.assertEqual(snapshot["continuous_runtime_seconds"], 8700)

    def test_continuous_runtime_resets_after_more_than_ten_minutes(self):
        next_start = dt.datetime(
            2026,
            7,
            28,
            13,
            10,
            1,
            tzinfo=dt.timezone.utc,
        )
        with mock.patch.object(monitor, "utc_now", return_value=next_start):
            state = monitor.MonitorState(
                [],
                10,
                continuous_started_at="2026-07-28T10:45:00Z",
                continuous_last_end="2026-07-28T13:00:00Z",
            )
            state.wait_for_interval(next_start)
            snapshot = state.snapshot()
        self.assertEqual(
            snapshot["continuous_started_at"],
            "2026-07-28T13:10:01Z",
        )
        self.assertEqual(snapshot["continuous_runtime_seconds"], 0)


class DiscoveryTests(unittest.TestCase):
    @mock.patch.object(monitor.subprocess, "run")
    def test_linux_route_and_first_hop_discovery(self, run):
        run.side_effect = [
            mock.Mock(
                stdout="default via 192.0.2.1 dev enp1s0 proto dhcp metric 100\n"
            ),
            mock.Mock(stdout="203.0.113.20\n", returncode=0),
            mock.Mock(
                stdout=(
                    "traceroute to 1.1.1.1, 12 hops max\n"
                    " 1  192.0.2.1  0.8 ms\n"
                    " 2  *\n"
                    " 3  198.51.100.9  8.2 ms\n"
                )
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            discovery = monitor.NetworkDiscovery(
                cache_path=Path(temporary) / "discovery.json"
            ).discover()
        self.assertEqual(discovery.gateway, "192.0.2.1")
        self.assertEqual(discovery.interface, "enp1s0")
        self.assertEqual(discovery.isp_hop, "198.51.100.9")
        self.assertEqual(discovery.public_ip, "203.0.113.20")
        self.assertEqual(discovery.isp_hop_source, "traceroute")

    @mock.patch.object(monitor.subprocess, "run")
    def test_explicit_router_address_overrides_default_route(self, run):
        run.side_effect = [
            mock.Mock(stdout="default via 192.0.2.254 dev enp1s0\n"),
            mock.Mock(stdout="203.0.113.20\n", returncode=0),
            mock.Mock(
                stdout=(
                    "traceroute to 1.1.1.1, 12 hops max\n"
                    " 1  192.0.2.1  0.8 ms\n"
                    " 2  198.51.100.9  8.2 ms\n"
                )
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            discovery = monitor.NetworkDiscovery(
                gateway_address="192.0.2.1",
                cache_path=Path(temporary) / "discovery.json",
            ).discover()
        self.assertEqual(discovery.gateway, "192.0.2.1")
        self.assertEqual(discovery.interface, "enp1s0")
        self.assertEqual(discovery.isp_hop, "198.51.100.9")

    @mock.patch.object(monitor.subprocess, "run")
    def test_same_public_ip_reuses_cached_hop_without_traceroute(self, run):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "discovery.json"
            monitor.DiscoveryCache(cache_path).save(
                "203.0.113.20",
                "192.0.2.1",
                "enp1s0",
                "198.51.100.9",
            )
            run.side_effect = [
                mock.Mock(stdout="default via 192.0.2.1 dev enp1s0\n"),
                mock.Mock(stdout="203.0.113.20\n", returncode=0),
            ]
            discovery = monitor.NetworkDiscovery(
                cache_path=cache_path
            ).discover()
        self.assertEqual(discovery.isp_hop, "198.51.100.9")
        self.assertEqual(discovery.isp_hop_source, "cache")
        self.assertIsNone(discovery.warning)
        self.assertEqual(run.call_count, 2)

    @mock.patch.object(monitor.subprocess, "run")
    def test_public_ip_change_retraces_and_updates_cache(self, run):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "discovery.json"
            cache = monitor.DiscoveryCache(cache_path)
            cache.save(
                "203.0.113.20",
                "192.0.2.1",
                "enp1s0",
                "198.51.100.9",
            )
            run.side_effect = [
                mock.Mock(stdout="default via 192.0.2.1 dev enp1s0\n"),
                mock.Mock(stdout="203.0.113.21\n", returncode=0),
                mock.Mock(
                    stdout=(
                        " 1  192.0.2.1  0.8 ms\n"
                        " 2  198.51.100.10  8.2 ms\n"
                    )
                ),
            ]
            discovery = monitor.NetworkDiscovery(
                cache_path=cache_path
            ).discover()
            saved = cache.load()
        self.assertEqual(discovery.isp_hop, "198.51.100.10")
        self.assertEqual(discovery.isp_hop_source, "traceroute")
        self.assertEqual(saved["public_ip"], "203.0.113.21")
        self.assertEqual(saved["isp_hop"], "198.51.100.10")

    @mock.patch.object(monitor.subprocess, "run")
    def test_failed_retrace_keeps_last_known_hop(self, run):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "discovery.json"
            monitor.DiscoveryCache(cache_path).save(
                "203.0.113.20",
                "192.0.2.1",
                "enp1s0",
                "198.51.100.9",
            )
            run.side_effect = [
                mock.Mock(stdout="default via 192.0.2.1 dev enp1s0\n"),
                mock.Mock(stdout="203.0.113.21\n", returncode=0),
                mock.Mock(stdout=" 1  192.0.2.1  0.8 ms\n 2  *\n"),
            ]
            discovery = monitor.NetworkDiscovery(
                cache_path=cache_path
            ).discover()
        self.assertEqual(discovery.isp_hop, "198.51.100.9")
        self.assertEqual(discovery.isp_hop_source, "stale-cache")
        self.assertIn("last known hop", discovery.warning)

    @mock.patch.object(monitor.subprocess, "run")
    def test_failed_public_ip_check_keeps_cached_hop(self, run):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "discovery.json"
            monitor.DiscoveryCache(cache_path).save(
                "203.0.113.20",
                "192.0.2.1",
                "enp1s0",
                "198.51.100.9",
            )
            run.side_effect = [
                mock.Mock(stdout="default via 192.0.2.1 dev enp1s0\n"),
                mock.Mock(stdout="", returncode=28),
            ]
            discovery = monitor.NetworkDiscovery(
                cache_path=cache_path
            ).discover()
        self.assertEqual(discovery.public_ip, "203.0.113.20")
        self.assertEqual(discovery.isp_hop, "198.51.100.9")
        self.assertEqual(discovery.isp_hop_source, "cache")
        self.assertIn("Public IPv4 check failed", discovery.warning)
        self.assertEqual(run.call_count, 2)

    def test_ipv4_filter(self):
        self.assertTrue(monitor.is_usable_ipv4("100.64.1.1"))
        self.assertFalse(monitor.is_usable_ipv4("127.0.0.1"))
        self.assertFalse(monitor.is_usable_ipv4("fe80::1"))
        self.assertFalse(monitor.is_usable_ipv4("*"))


def metrics_with_losses(gateway, isp, cloudflare, google, quad9):
    values = [gateway, isp, cloudflare, google, quad9]
    return {
        key: {
            **monitor.unavailable_metric(),
            "loss_pct": value,
            "rtt_avg_ms": 20.0,
        }
        for key, value in zip(monitor.TARGET_ORDER, values)
    }


class DiagnosisTests(unittest.TestCase):
    def test_correlated_loss_after_clean_gateway_is_probable_isp(self):
        diagnosis = monitor.classify(metrics_with_losses(0, 5, 5, 5, 5))
        self.assertEqual(diagnosis["code"], "isp_path_loss")

    def test_hop_only_loss_is_not_forwarding_loss(self):
        diagnosis = monitor.classify(metrics_with_losses(0, 30, 0, 0, 0))
        self.assertEqual(diagnosis["code"], "hop_icmp_limited")

    def test_gateway_only_loss_is_not_overstated(self):
        diagnosis = monitor.classify(metrics_with_losses(25, 0, 0, 0, 0))
        self.assertEqual(diagnosis["code"], "gateway_icmp_limited")


class CsvAndHttpTests(unittest.TestCase):
    def make_record(self):
        targets = [
            monitor.Target("gateway", "Router", "192.0.2.1"),
            monitor.Target("isp_hop", "First Hop", "198.51.100.9"),
            *[
                monitor.Target(key, label, address)
                for key, (label, address) in monitor.PUBLIC_TARGETS.items()
            ],
        ]
        accumulators = {}
        for target in targets:
            stats = monitor.StatsAccumulator()
            stats.add(monitor.BurstResult(5, 5, [10, 11, 12, 11, 10]))
            accumulators[target.key] = stats
        start = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)
        return monitor.create_record(
            start, start + dt.timedelta(minutes=5), targets, accumulators
        )

    def test_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "monitor.csv"
            store = monitor.CsvStore(path)
            store.ensure()
            store.append(self.make_record())
            loaded = store.load(10)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["targets"]["google"]["sent"], 5)
            self.assertEqual(loaded[0]["diagnosis"]["code"], "healthy")
            with path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(next(csv.reader(handle)), monitor.CSV_FIELDS)

    def test_health_and_status_http_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "monitor.csv"
            monitor.CsvStore(csv_path).ensure()
            web_root = MODULE_PATH.parent / "web"
            state = monitor.MonitorState([], 10)
            server = monitor.DashboardServer(
                ("127.0.0.1", 0),
                monitor.DashboardHandler,
                state,
                csv_path,
                web_root,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2
                ) as response:
                    payload = json.load(response)
                self.assertTrue(payload["ok"])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=2
                ) as response:
                    html = response.read()
                    self.assertIn(b"Uplink Ledger", html)
                    self.assertIn(b">Router<", html)
                    self.assertIn(b">First Hop<", html)
                    self.assertIn(b"rolling-loss-cloudflare", html)
                    self.assertIn(b"range-low-isp_hop", html)
                    self.assertIn(b"range-high-quad9", html)
                    self.assertIn(b'data-chart-range', html)
                    self.assertIn(b'data-chart-range="loss"', html)
                    self.assertIn(b'data-chart-range="latency"', html)
                    self.assertIn(b'<option value="24" selected>24 hours</option>', html)
                    self.assertIn(b'<option value="1">1 hour</option>', html)
                    self.assertIn(b'data-chart-latest', html)
                    self.assertIn(b"Zoomable packet-loss history chart", html)
                    self.assertNotIn(b"Past 24 hours or available history", html)
                    self.assertNotIn(b'id="rolling-window"', html)
                    self.assertNotIn(b"Client-side", html)
                    self.assertNotIn(b"connection-state", html)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/export.csv", timeout=2
                ) as response:
                    self.assertIn(
                        "attachment",
                        response.headers["Content-Disposition"],
                    )
                    self.assertTrue(
                        response.read().startswith(b"schema_version,")
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_redirect_preserves_host_path_and_query(self):
        server = monitor.RedirectServer(
            ("127.0.0.1", 0),
            monitor.RedirectHandler,
            secure_port=443,
            fallback_host="monitor.example.test",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            server.server_address[1],
            timeout=2,
        )
        try:
            connection.request(
                "GET",
                "/history?limit=288",
                headers={"Host": "monitor.example.com:80"},
            )
            response = connection.getresponse()
            self.assertEqual(
                response.status,
                monitor.HTTPStatus.PERMANENT_REDIRECT,
            )
            self.assertEqual(
                response.getheader("Location"),
                "https://monitor.example.com/history?limit=288",
            )
            self.assertEqual(response.read(), b"")
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class PostgresStoreTests(unittest.TestCase):
    def setUp(self):
        self.record = CsvAndHttpTests().make_record()

    def test_record_sql_is_relational_and_idempotent(self):
        sql = monitor.PostgresStore.record_sql(self.record)
        self.assertIn("INSERT INTO uplink_ledger_intervals", sql)
        self.assertIn("INSERT INTO uplink_ledger_measurements", sql)
        self.assertIn("ON CONFLICT (interval_start)", sql)
        self.assertEqual(sql.count("ON CONFLICT"), 6)

    def test_schema_migrates_legacy_table_and_index_names(self):
        schema = monitor.POSTGRES_SCHEMA_SQL
        self.assertIn(
            "ALTER TABLE isp_loss_intervals RENAME TO uplink_ledger_intervals",
            schema,
        )
        self.assertIn(
            "ALTER TABLE isp_loss_measurements RENAME TO uplink_ledger_measurements",
            schema,
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS uplink_ledger_intervals",
            schema,
        )

    def test_postgres_default_uses_product_database_name(self):
        self.assertEqual(
            monitor.PostgresStore().database_url,
            "postgresql:///uplink_ledger",
        )

    def test_import_batches_records(self):
        store = monitor.PostgresStore()
        store._run = mock.Mock(return_value="")
        count = store.import_records(
            [self.record, self.record, self.record],
            batch_size=2,
        )
        self.assertEqual(count, 3)
        self.assertEqual(store._run.call_count, 2)

    def test_load_rebuilds_dashboard_records(self):
        database_record = json.loads(json.dumps(self.record))
        database_record["diagnosis"].pop("severity", None)
        database_record["targets"]["isp_hop"]["label"] = "ISP Hop"
        store = monitor.PostgresStore()
        store._run = mock.Mock(return_value=json.dumps(database_record) + "\n")
        loaded = store.load(10)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["targets"]["quad9"]["address"], "9.9.9.9")
        self.assertEqual(loaded[0]["targets"]["isp_hop"]["label"], "First Hop")
        self.assertEqual(loaded[0]["diagnosis"]["severity"], "healthy")

    def test_sql_text_escaping(self):
        self.assertEqual(monitor.postgres_text("Spectrum's fault"), "'Spectrum''s fault'")

    def test_continuous_bounds_uses_latest_gap_free_group(self):
        store = monitor.PostgresStore()
        store._run = mock.Mock(
            return_value="2026-07-28T10:45:00Z|2026-07-28T14:00:00Z\n"
        )
        bounds = store.continuous_bounds(600)
        self.assertEqual(
            bounds,
            ("2026-07-28T10:45:00Z", "2026-07-28T14:00:00Z"),
        )
        sql = store._run.call_args.args[0]
        self.assertIn("lag(interval_end)", sql)
        self.assertIn("> 600", sql)

    def test_postgres_history_wins_when_merging_restart_data(self):
        csv_record = json.loads(json.dumps(self.record))
        database_record = json.loads(json.dumps(self.record))
        csv_record["diagnosis"]["message"] = "CSV copy"
        database_record["diagnosis"]["message"] = "PostgreSQL copy"
        merged = monitor.merge_history([csv_record], [database_record], 10)
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0]["diagnosis"]["message"],
            "PostgreSQL copy",
        )

    def test_upgrade_seed_finds_hop_before_newer_filtered_rows(self):
        with_hop = json.loads(json.dumps(self.record))
        filtered = json.loads(json.dumps(self.record))
        filtered["start"] = "2026-07-27T00:05:00Z"
        filtered["targets"]["isp_hop"]["address"] = None
        gateway, isp_hop = monitor.last_known_discovery(
            [with_hop, filtered]
        )
        self.assertEqual(gateway, "192.0.2.1")
        self.assertEqual(isp_hop, "198.51.100.9")


if __name__ == "__main__":
    unittest.main()
