# DATAPixx USB failure harness

This harness makes DATAPixx USB failures observable without opening a display. It runs digital-output conditions in a child process, logs attempts and recovery, and provides controlled hardware provocations.

> **Safety:** Run soak and provocation tests without a participant in the scanner or helmet room. These tests do not qualify safety or timing.

The harness targets PGL commit `55ae664c632af828a100d8e94dd6d20832ad1ad3` and does not modify `pgl/`.

## Requirements

Run from the repository root with Python 3.9+. Simulation uses the standard library and `pytest`; it needs no display or DATAPixx. Hardware runs require the intended Mac, USB chain, `pypixxlib`, and attached DATAPixx. Visual examples also require NumPy, Pillow, the macOS PGL build, and a Metal-capable display.

```bash
mkdir -p logs
python3 -m pytest harness/tests -v
python3 -m harness.usb_topology \
  --input-json harness/tests/fixtures/system_profiler_usb.json
```

## Simulate first

```bash
# Normal trigger sequence.
python3 -m harness.soak --simulate --interval 0.1 --max-triggers 10 \
  --csv logs/simulated.csv --text-log logs/simulated.log

# Inject a recoverable error.
python3 -m harness.soak --simulate --simulate-fail-after-calls 7 \
  --simulate-mode error --interval 0.05 --reopen-interval 0.05 \
  --duration 0.5 --csv logs/recovery.csv

# Exercise timeout and silent-drop handling.
python3 -m harness.soak --simulate --simulate-fail-after-calls 7 \
  --simulate-mode hang --call-timeout 0.1 --duration 0.5 \
  --csv logs/hang.csv
python3 -m harness.soak --simulate --simulate-fail-after-calls 7 \
  --simulate-mode silent --duration 0.5 --csv logs/silent.csv
```

Injected failures require `--duration`; a trigger limit cannot bound a failure before the first trigger.

## Hardware tests

Run a baseline before changing anything:

```bash
python3 -m harness.soak --csv logs/baseline.csv --text-log logs/baseline.log
```

Then test one condition at a time:

```bash
python3 -m harness.provoke --kvm --duration 120 --csv logs/kvm.csv
python3 -m harness.provoke --power --duration 120 --csv logs/power.csv
python3 -m harness.provoke --bandwidth \
  --bandwidth-source /Volumes/USB/large-file.bin \
  --duration 120 --csv logs/bandwidth.csv
python3 -m harness.provoke --stale-handle --duration 30 --csv logs/stale.csv
python3 -m harness.provoke --no-flush --max-triggers 1 --csv logs/no-flush.csv
python3 -m harness.provoke --wiggle --duration 180 --csv logs/wiggle.csv
```

- `--power`: connect one documented bus-powered device; do not power-cycle DATAPixx.
- `--bandwidth`: reads an existing file without writing to the volume.
- `--stale-handle`: exits a helper without `DPxClose()`, then tests reopen.
- `--no-flush`: omits `DPxWriteRegCache()` deliberately.
- `--wiggle`: prompts for the Mac connector, DATAPixx connector, and MSR feedthrough in order.

A missed call deadline terminates the worker and starts the reopen loop. The parent continues writing markers and handling signals.

## Topology and logs

```bash
python3 -m harness.usb_topology --json-output logs/usb-topology.json
log stream --style compact \
  --predicate 'subsystem == "com.apple.iokit.IOUSBHostFamily"'
```

The topology parser reports device hierarchy and stated current limits. It does not measure aggregate current, bandwidth, cable quality, or electrical health.

## Visual checks

These commands open Metal and real DATAPixx hardware:

```bash
python3 -m harness.minimal_experiment --image testimage.jpg
python3 -m harness.things_task --image-dir /path/to/things/images
```

The minimal experiment sends condition 17 before displaying one image for 500 ms, then condition 18. Condition 17 is a pre-image marker, not a measured onset. `things_task` loads 200 images, displays each for 0.5 seconds, and sends a one-based trial code.

## Reading results

Each CSV row is flushed and `fsync`ed. Use `sequence_number` and nearby monotonic timestamps to align trigger rows, recovery markers, text logs, topology snapshots, and macOS logs.

Important outcomes:

- `sent`: simulated emission was committed.
- `no_emission`: simulation observed no committed emission.
- `flushed_unverified`: real hardware accepted a flushed request; no electrical pulse was measured.
- `uncommitted`: real `--no-flush` control.
- `worker_timeout`, `worker_protocol_error`, `worker_exception`: software-side failures.

Ready/error checks prove only library and register communication. Verify output pulses at the MEG input, loopback, logic analyzer, or oscilloscope. Verify visual timing with a display-appropriate measurement.

Record the command, Git SHA, software/firmware versions, USB topology, changed variable, logs, failure/recovery, and external pulse measurement for every run.

## Limits

Simulation cannot verify macOS/Metal timing, electrical pulses, DATAPixx firmware, physical USB recovery, KVM behavior, or cable/feedthrough faults. PGL also does not emit a distinct experiment-onset trigger automatically; add and verify one in the lab task when required.

All commands document their options with `--help`:

```bash
python3 -m harness.soak --help
python3 -m harness.provoke --help
python3 -m harness.usb_topology --help
```
