#!/usr/bin/env python3
"""Capture complete benchmark runs from either RTOS; reset the board between runs.

Flash the firmware first (one at a time). Requires pyserial. Each run is validated
(protocol 0.2.0, cycle units, clock sanity, 1000 samples per metric, no lost events)
before the next starts. Run collect.py on the resulting logs to compute statistics.
"""
import argparse
from collections import defaultdict
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlog import CPU_HZ, normalize, parse_begin  # noqa: E402

BLINKY = {("jitter", "1ms"), ("jitter", "10ms"), ("jitter", "100ms")}
UART = {("task_switch", "uart_to_led"), ("irq_latency", "swi_uart")}
IOT = {("jitter", "sensor_10ms"), ("irq_latency", "swi_drdy"),
       ("task_switch", "isr_to_thread"), ("proc_exec", "avg8_alarm")}
EXPECTED = {
    ("freertos", "blinky"): BLINKY,
    ("freertos", "uart-to-led"): UART,
    ("freertos", "iot"): IOT,
    ("ariel-os", "blinky"): BLINKY,
    ("ariel-os", "uart-to-led"): UART,
    ("ariel-os", "iot"): IOT,
}
APPS = sorted({app for _, app in EXPECTED})
# CCOUNT cycles in 100 ms of the independent 1 us timer; reject a clock off by >1%.
CCOUNT_100MS = CPU_HZ // 10
CCOUNT_TOLERANCE = CCOUNT_100MS // 100


def validate(lines, app, os_label):
    """Reject incomplete, duplicate, lossy or mis-clocked captures before analysis."""
    lines = [normalize(line) for line in lines]
    begins = [b for b in map(parse_begin, lines) if b and b["app"] == app]
    if not begins:
        raise ValueError("BENCH_BEGIN missing")
    if len(begins) > 1:
        raise ValueError("BENCH_BEGIN repeated (board reset during the run?)")
    begin = begins[0]
    if begin["os"] != os_label:
        raise ValueError(f"log is from {begin['os']}, expected {os_label}")
    if begin["unit"] != "cycles":
        raise ValueError(f"unit {begin['unit']!r}: expected cycles (protocol 0.2.0)")
    if f"BENCH_END,{app}" not in lines:
        raise ValueError("BENCH_END missing")

    groups = defaultdict(list)
    ccount = None
    for line in lines:
        fields = line.split(",")
        if fields[:3] == ["INFO", app, "events_lost"] and int(fields[3]):
            raise ValueError(f"lost events: {fields[3]}")
        if fields[:3] == ["INFO", app, "ccount_per_100ms"]:
            ccount = int(fields[3])
        if fields[:2] == ["SAMPLE", app]:
            if len(fields) != 6:
                raise ValueError(f"malformed sample: {line}")
            int(fields[5])
            groups[tuple(fields[2:4])].append(int(fields[4]))
    if ccount is None:
        raise ValueError("INFO ccount_per_100ms missing")
    if abs(ccount - CCOUNT_100MS) > CCOUNT_TOLERANCE:
        raise ValueError(f"ccount_per_100ms={ccount}: CPU not at {CPU_HZ} Hz?")
    if set(groups) != EXPECTED[(os_label, app)]:
        raise ValueError(f"unexpected metric groups: {set(groups)}")
    for key, indices in groups.items():
        if indices != list(range(1000)):
            raise ValueError(f"{key}: expected indices 0..999 exactly once")


def capture_run(ser, output, app, os_label, interval_ms, timeout):
    lines = []
    deadline = time.monotonic() + timeout

    def until(predicate):
        while time.monotonic() < deadline:
            data = ser.readline()
            if not data:
                continue
            output.write(data)
            line = normalize(data.decode("utf-8", errors="replace"))
            lines.append(line)
            if predicate(line):
                return line
        raise TimeoutError("benchmark timeout; partial log preserved")

    # Standard ESP32 USB-UART auto-reset sequence; boot with GPIO0 high.
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.reset_input_buffer()
    ser.rts = False

    if app == "uart-to-led":
        until(lambda line: line == "READY")
        for sequence in range(1005):
            command = "10t"[sequence % 3]
            ser.write(command.encode("ascii"))
            reply = until(lambda line: line.startswith("R,"))
            if not re.fullmatch(rf"R,{sequence},{command},\d+", reply):
                raise ValueError(f"unexpected command response: {reply}")
            time.sleep(interval_ms / 1000)
        ser.write(b"b")
    until(lambda line: line == f"BENCH_END,{app}")
    output.flush()
    validate(lines, app, os_label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--os", dest="os_label", choices=["freertos", "ariel-os"], required=True)
    parser.add_argument("--app", choices=APPS, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--runs", type=int, default=30, help="30 pilot runs in the group's paper")
    parser.add_argument("--interval-ms", type=float, default=500, help="UART command pacing")
    parser.add_argument("--timeout", type=float, default=900, help="seconds per complete run")
    args = parser.parse_args()
    if args.runs < 1 or args.interval_ms < 0 or args.timeout <= 0:
        parser.error("runs/timeout must be positive and interval must be nonnegative")
    import serial

    args.out.mkdir(parents=True, exist_ok=True)
    with serial.Serial(args.port, 115200, timeout=0.2) as ser:
        for run in range(1, args.runs + 1):
            path = args.out / f"{args.os_label}_{args.app}_{run:02d}.log"
            # Do not silently replace experimental evidence from a previous batch.
            with path.open("xb") as output:
                capture_run(ser, output, args.app, args.os_label, args.interval_ms, args.timeout)
            print(f"[{run}/{args.runs}] {path}: complete")


if __name__ == "__main__":
    main()
