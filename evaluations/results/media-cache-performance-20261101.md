# Media cache scan comparison — 2026-11-01

Status aggregation now streams directory metadata without materializing cache descriptors or reading LRU access rows. Eviction still reads fresh access times. No TTL, background worker or Node-detection change.

## Conditions

- Baseline `c4be2502419d1881d850293d7dda2005a45e03a0`; cache blobs before `43f805bf5c3dfd9a4aad5590fff459eb53262407`, after `5e2ea479373b45da5a0ffd11b59a9c42399bd2fe`.
- Both runs in the same isolated worktree on Windows-11-10.0.26300-SP0, Python 3.12.13, NTFS. Same project environment and PATH; optional song component `installed=false` / `running=false` on both sides for every size.
- Separate process and real Store/Runtime per 100/1000/5000 files; all content-hash-named 46-byte valid WAVs in online_music, one access row per file. Other pools/catalog/queue/tasks empty. Metadata/stat benchmark, no audio decode; Runtime.open never called, no inference or platform requests.
- Each method: 3 warmups + 25 uninstrumented timed calls. Tracemalloc (3 calls), SQL trace (1 call) and cProfile (1 call) measured separately. OS cache warm, no exclusive host/load control. Peak below is Python traced allocation, not model/native RSS. Before and after use the same original probe; the maintained CLI retains this measurement protocol and adds cleanup/options.

## Measured results

| Files | Method | p50 before → after (ms) | Max before → after (ms) | Python peak before → after (KiB) | SQL before → after |
| ---: | --- | ---: | ---: | ---: | ---: |
| 100 | MediaCache._files(music) | 15.09 → 3.95 | 18.01 → 4.65 | 147.39 → 93.47 | 2 → 2 |
| 100 | MediaCache.status | 17.17 → 3.51 | 20.68 → 4.86 | 147.67 → 8.64 | 18 → 6 |
| 100 | OnlineSongs.status | 25.09 → 9.72 | 32.70 → 10.89 | 148.48 → 14.99 | 7 → 6 |
| 100 | cache+online_back_to_back | 49.06 → 13.00 | 53.37 → 14.60 | 152.57 → 18.94 | 25 → 12 |
| 1000 | MediaCache._files(music) | 200.13 → 28.98 | 292.56 → 45.35 | 1381.45 → 860.01 | 2 → 2 |
| 1000 | MediaCache.status | 217.81 → 12.04 | 267.29 → 15.76 | 1381.73 → 8.67 | 18 → 6 |
| 1000 | OnlineSongs.status | 227.61 → 18.36 | 318.43 → 22.41 | 1382.54 → 14.99 | 7 → 6 |
| 1000 | cache+online_back_to_back | 446.24 → 28.05 | 593.12 → 37.42 | 1386.73 → 18.97 | 25 → 12 |
| 5000 | MediaCache._files(music) | 794.83 → 207.47 | 1012.16 → 244.92 | 8551.60 → 4077.58 | 2 → 2 |
| 5000 | MediaCache.status | 690.13 → 40.04 | 945.92 → 47.09 | 6674.57 → 8.67 | 18 → 6 |
| 5000 | OnlineSongs.status | 684.43 → 42.03 | 756.65 → 51.32 | 6675.38 → 14.99 | 7 → 6 |
| 5000 | cache+online_back_to_back | 1910.26 → 79.80 | 2305.92 → 102.33 | 8556.57 → 18.97 | 25 → 12 |

## Observed work reduction

Full status access-table scans: 4 → 0; online status: 1 → 0; LRU _files: 1 → 1. Full status shares one policy snapshot. Each request still enumerates files freshly and recomputes protections; write/clear/configure limits retain their original checks.

5000-file cProfile native metadata calls (Windows; profile is a separate instrumented pass):

| Method | nt.stat before → after | nt.lstat before → after |
| --- | ---: | ---: |
| MediaCache._files(music) | 15007 → 4 | 5004 → 4 |
| MediaCache.status | 15014 → 8 | 5008 → 8 |
| OnlineSongs.status | 15009 → 6 | 5004 → 4 |
| cache+online_back_to_back | 30023 → 14 | 10012 → 12 |

## Validation and scope

- 144 related tests passed: media cache + online songs (34), browsing evidence/store/workflows (82), live media + singing + runtime (28).
- New regressions cover summaries without LRU SQL, fresh lease expiry, latest touch affecting eviction after a status read, temporary-only recursion, ignored unrecognized source names, and root/unrecognized-entry/nested-temporary directory symlink or Windows junction refusal. Windows run had no skipped link tests.
- Rerunnable standalone tool: `uv run python evaluations/media_cache_benchmark.py --output-dir tmp/cache-benchmark` (new output directory required). CLI smoke passed; its owned fixture runtimes/tokens are removed after closure. Raw timed samples, peaks, SQL counters and top cProfile entries are in the adjacent JSON; full .prof remains in the isolated worktree tmp for review.
- This is a Windows metadata workload, not a Jetson/HTTP/OBS/audio/GPU benchmark or a production latency guarantee. DirEntry metadata caching differs by OS; Linux/Jetson gains need their own measurements. Existing capacity, LRU, protection and link-boundary behavior remain the acceptance criteria.
