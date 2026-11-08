# Capacity measurements and remaining limits

Measured locally on November 8, 2026 with Python 3.14 on macOS. These are synthetic measurements from this development machine, not a supported-device maximum or a memory ceiling. No live LAN devices or installed database were used by the benchmarks.

## Results

Each history benchmark runs in a fresh child process with a temporary SQLite database, 100 synthetic devices, eight numeric readings per device and 500,000 stored samples. Peak RSS includes the Python process and SQLite allocations; the CSV response is consumed in the child without retaining its body. Browser memory and concurrent requests are not included.

| Workload | Time | Peak process RSS | Response size |
| --- | ---: | ---: | ---: |
| Original CSV implementation (full rows, string and bytes in memory) | 1.867 s | 455.78 MiB | 38.98 MiB |
| Streaming CSV implementation | 1.309 s | 38.88 MiB | 38.98 MiB |
| Original all-metric chart query and JSON serialization | 1.133 s | 213.67 MiB | 37.16 MiB |
| Selected metric across 100 devices, with total point budget | 0.344 s | 51.69 MiB | 1.84 MiB |
| Selected metric for one of those devices | 0.004 s | 42.78 MiB | 0.05 MiB |
| Legacy unfiltered chart request, with total point budget | 0.673 s | 55.89 MiB | 1.84 MiB |
| Selected metric across 1,000 devices, 500,000 samples | 0.355 s | 57.27 MiB | 1.86 MiB |
| Collection: 120 synthetic unavailable devices, 3 seconds each | 30.042 s | 38.47 MiB | State only |

The history workers peaked near 36–41 MiB before their measured operation. The new selected-metric workload reduced peak RSS by about 76% and response size by about 95% compared with the original all-metric chart workload. The 1,000-device case still returned 12,000 points; it tests synthetic history queries, not 1,000 real device connections. CSV export now fetches 256 rows at a time using a separate read-only SQLite connection. It retains ordering, numeric and text samples, device identity filters, UTF-8 BOM and spreadsheet-formula escaping. An open export does not hold the collector lock; WAL allows collection to keep writing. Tests verify a consistent export snapshot while collection saves another reading, and verify that closing a partial export releases that snapshot.

## Limits to watch

- **Polling latency.** The collector has 12 workers. A rough device-read lower bound is `ceil(device_count / 12) × per-device read time` when devices take equally long. The 120-device/three-second case consumes the entire nominal 30-second budget. BACnet and Modbus have up to ten-second budgets, so a much smaller set of slow endpoints can be significant. Actual total time also includes network setup, storage and cleanup. The collector waits another 30 seconds after each cycle (plus backup/name work); a 30-second cycle therefore means roughly a minute between completed cycles. Settings displays the last cycle duration, and the dashboard warns when the current or previous cycle reaches 30 seconds. Overlapping manual polls are skipped.
- **Charts now have a total point budget.** The browser requests only the selected metric, with an optional inventory `device_id`. A compact numeric metric catalog preserves choices that exist only in historical samples, including offline/reconfigured devices. SQL adjusts time buckets for the number of requested device/metric series: up to 300 averaged points per series and 12,000 total points per response. Unfiltered legacy API requests use the same budget. All stored readings remain available through CSV. If an extreme series count exceeds the budget even with two buckets per series, the response sets `points_limited` and the browser shows a warning to choose one device or export CSV.
- **Chart reads use their own SQLite snapshot.** Catalog and averaged points come from one read-only WAL transaction, without taking the collector lock. This bounds Python sample/JSON allocations but does not bound SQLite sort work, concurrent HTTP requests, catalog size, or query time over arbitrarily large histories. Catalog size grows with distinct metric identifiers, rather than the full number of historical points. The benchmark includes catalog generation and JSON serialization; it excludes browser memory.
- **Browser work.** Switching metric, device or range cancels the earlier fetch and ignores late responses. The view keeps only the selected points. Chart bounds use an iterative scan rather than argument-spread arrays, and invisible hover targets are limited to views containing at most 1,200 points. The rendered series use the server's actual bucket duration to preserve visible gaps at the chosen resolution. Desktop/mobile tests covered 100 series, focused-device views, rapid selection changes, a historical-only metric and a known collection gap. Many simultaneous browsers and exceptionally large metric catalogs still need sustained testing.
- **History growth.** With eight changing readings every 30 seconds, 100 devices could generate about 2.3 million numeric rows per day (about 69 million over 30 days). That volume has not been benchmarked. Slow-changing supported fields may store less; do not extrapolate the 500,000-row result to that size. Retention can be shortened from Settings. No existing history was shortened during this work.
- **Concurrent and slow exports.** Streaming bounds Python's row/body allocation for each export, but SQLite caches/sorts, HTTP threads and multiple requests still use resources. A slow client holds a read snapshot and can prevent WAL truncation until it disconnects or finishes. CSV export now closes its connection after completion or a detected disconnect; it does not impose a download-duration limit.
- **Device requests.** IPP caps replies at 128 KiB, 512 attributes and 16 supply readings with a three-second socket timeout. NUT caps lists at 128 KiB, 512 variables and 4-KiB lines with a three-second read deadline. No new third-party runtime dependency was added. IPP's socket timeout is not an absolute deadline for a peer that continually trickles HTTP data.

No out-of-memory failure or machine-wide memory ceiling was reached. These tests reveal workloads worth constraining before scaling; they do not establish leak-free long-term operation. Sustained large-inventory collection, many simultaneous browsers/exports, physical printers/UPS hardware, and NUT driver behavior remain unverified.

## Reproduce locally

The benchmark requires macOS or Linux (`resource.getrusage`) and uses the standard library. It never opens a device connection or uses your real database. The slow-device case intentionally takes about 30 seconds.

```sh
python3 tools/benchmark_capacity.py --rows 500000 --devices 100 --mode legacy-csv
python3 tools/benchmark_capacity.py --rows 500000 --devices 100 --mode csv
python3 tools/benchmark_capacity.py --rows 500000 --devices 100 --mode legacy-chart
python3 tools/benchmark_capacity.py --rows 500000 --devices 100 --mode chart
python3 tools/benchmark_capacity.py --rows 500000 --devices 100 --mode chart-device
python3 tools/benchmark_capacity.py --rows 500000 --devices 100 --mode chart-all
python3 tools/benchmark_capacity.py --rows 500000 --devices 1000 --mode chart
python3 tools/benchmark_capacity.py --rows 1 --devices 120 --mode poll --delay 3
```

Run `python3 -m unittest discover -s tests -p 'test_capacity.py' -v` for export concurrency/correctness and collector-warning checks. Run `test_office_readers.py` for IPP/NUT loopback transport and full collector → SQLite → HTTP/history/CSV validation.
