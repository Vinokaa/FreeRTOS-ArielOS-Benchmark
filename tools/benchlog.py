"""Shared parsing of benchmark log lines printed by both RTOS (Python standard library only).

FreeRTOS prints records as bare lines (``SAMPLE,...``). Ariel OS prefixes them with the log
level (``[INFO] SAMPLE,...``) and older defmt captures also carry ANSI colors and a
``(file.rs:N)`` suffix. ``normalize`` reduces all of them to the bare record.
"""
import re

CPU_HZ = 240_000_000
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RUST_LOCATION = re.compile(r"\s+\([^()\n]*\.rs:\d+(?::\d+)?\)\s*$")
RECORD = re.compile(r"\b(?:BENCH_BEGIN|BENCH_END|SAMPLE_HEADER|SAMPLE|RESULT|FOOTPRINT|INFO|META|LOG),")


def normalize(line):
    """Bare record text: no colors, level prefix, Rust location suffix or line ending."""
    line = RUST_LOCATION.sub("", ANSI.sub("", line)).strip()
    match = RECORD.search(line)
    return line[match.start():] if match else line


def norm_os(label):
    label = label.lower()
    if label.startswith("freertos"):
        return "freertos"
    if label.startswith("ariel"):
        return "ariel-os"
    return label


def parse_begin(line):
    """``BENCH_BEGIN`` fields as a dict (app, os, version, unit, ...), or None.

    Protocol 0.2.0 declares ``unit=cycles``; older logs (``clock_res_us=1``) are microseconds.
    """
    fields = normalize(line).split(",")
    if fields[0] != "BENCH_BEGIN" or len(fields) < 3:
        return None
    begin = {"app": fields[1], "os": norm_os(fields[2]),
             "version": fields[3] if len(fields) > 3 else ""}
    for field in fields[4:]:
        key, sep, value = field.partition("=")
        if sep:
            begin[key] = value
    begin.setdefault("unit", "us")
    return begin
