#!/usr/bin/env python3
"""ESP32 ELF section footprint, for either RTOS (Python standard library only).

Reports application sections; excludes bootloader, partitions, debug data and
file padding. Static RAM includes IRAM code and RTC sections. Heap-allocated
task stacks/TCBs are not static RAM. Keep sdkconfig and the map beside the ELF.
"""
import argparse
import csv
from pathlib import Path
import struct
import sys


def sections(path):
    data = Path(path).read_bytes()
    if data[:6] != b"\x7fELF\x01\x01":
        raise ValueError("expected a 32-bit little-endian ESP32 ELF")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    if header[2] != 94:  # EM_XTENSA
        raise ValueError("expected Xtensa architecture (ESP32)")
    offset, entry_size, count = header[6], header[11], header[12]
    if entry_size != 40 or count == 0:
        raise ValueError("unsupported ELF section table")
    return [struct.unpack_from("<IIIIIIIIII", data, offset + i * entry_size)
            for i in range(count)]


def mapped_flash(address):
    return (0x3F400000 <= address < 0x3F800000 or
            0x400D0000 <= address < 0x40400000)


def measure(path):
    sizes = dict(code_B=0, rodata_B=0, rwdata_B=0, zidata_B=0,
                 flash_total_B=0, ram_static_B=0, flash_code_B=0, iram_code_B=0)
    for _, kind, flags, address, _, size, *_ in sections(path):
        if not flags & 2 or not size:  # SHF_ALLOC
            continue
        flash = mapped_flash(address)
        if kind == 8 and flash:  # NOBITS flash alignment/dummy sections
            continue
        if kind == 8:
            sizes["zidata_B"] += size
        elif flags & 4:  # SHF_EXECINSTR
            sizes["code_B"] += size
            sizes["flash_code_B" if flash else "iram_code_B"] += size
        elif flags & 1 and not flash:  # ESP-IDF may mark flash rodata SHF_WRITE
            sizes["rwdata_B"] += size
        else:
            sizes["rodata_B"] += size
        if kind != 8:
            sizes["flash_total_B"] += size
        if not flash:
            sizes["ram_static_B"] += size
    return sizes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--app", required=True)
    parser.add_argument("--os", choices=["freertos", "ariel-os"], required=True)
    args = parser.parse_args()
    try:
        sizes = measure(args.elf)
    except (ValueError, OSError, struct.error) as error:
        parser.exit(1, f"footprint: {error}\n")
    csv.writer(sys.stdout, lineterminator="\n").writerow(
        ["FOOTPRINT", args.os, args.app, *sizes.values(), str(args.elf)])


if __name__ == "__main__":
    main()
