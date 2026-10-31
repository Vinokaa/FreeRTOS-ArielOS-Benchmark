#!/usr/bin/env python3
"""Capture complete FreeRTOS benchmark runs; reset the board between runs.

Requires pyserial. Run collect.py on the resulting logs to compute statistics.
"""
import argparse
from collections import defaultdict
from pathlib import Path
import re
import time

EXPECTED = {
    "iot": {("jitter", "sensor_10ms"), ("irq_latency", "swi_drdy"),
            ("task_switch", "isr_to_thread"), ("proc_exec", "avg8_alarm")},
    "uart-to-led": {("task_switch", "uart_to_led"), ("irq_latency", "swi_uart")},
}


def validate(lines, app):
    """Reject incomplete, duplicate or lossy captures before statistical analysis."""
    groups = defaultdict(list)
    if not any(line.startswith(f"BENCH_BEGIN,{app},freertos,") for line in lines):
        raise ValueError("BENCH_BEGIN missing")
    if f"BENCH_END,{app}" not in lines:
        raise ValueError("BENCH_END missing")
    for line in lines:
        fields = line.split(",")
        if fields[:3] == ["INFO", app, "events_lost"] and int(fields[3]):
            raise ValueError(f"lost events: {fields[3]}")
        if fields[:2] == ["SAMPLE", app]:
            if len(fields) != 6:
                raise ValueError(f"malformed sample: {line}")
            int(fields[5])
            groups[tuple(fields[2:4])].append(int(fields[4]))
    if set(groups) != EXPECTED[app]:
        raise ValueError(f"unexpected metric groups: {set(groups)}")
    for key, indices in groups.items():
        if indices != list(range(1000)):
            raise ValueError(f"{key}: expected indices 0..999 exactly once")


def capture_run(ser, output, app, interval_ms, timeout):
    lines = []
    deadline = time.monotonic() + timeout

    def until(predicate):
        while time.monotonic() < deadline:
            data = ser.readline()
            if not data:
                continue
            output.write(data)
            line = data.decode("utf-8", errors="replace").strip()
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
    validate(lines, app)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=EXPECTED, required=True)
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
            path = args.out / f"freertos_{args.app}_{run:02d}.log"
            # Do not silently replace experimental evidence from a previous batch.
            with path.open("xb") as output:
                capture_run(ser, output, args.app, args.interval_ms, args.timeout)
            print(f"[{run}/{args.runs}] {path}: complete")


if __name__ == "__main__":
    main()
