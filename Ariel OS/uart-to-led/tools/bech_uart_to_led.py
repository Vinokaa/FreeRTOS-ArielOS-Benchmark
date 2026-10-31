#!/usr/bin/env python3
"""Bateria de testes do UART-to-LED (FreeRTOS x Ariel OS).

Protocolo esperado do firmware (igual nos dois RTOS):
  - no boot, imprime uma linha "READY";
  - a cada comando recebido ('1', '0' ou 't'), responde "R,<seq>,<cmd>,<ciclos>",
    onde <ciclos> é a latência de task-switch medida com o contador de ciclos da CPU.

Uso:
  # piloto (30 amostras, como no artigo)
  python3 bench_uart_to_led.py run --rtos ariel --port /dev/ttyUSB0 --runs 1 --samples 30 --out piloto_ariel.csv

  # coleta definitiva: 10 execuções (com reset da placa entre elas) x 200 amostras = 2000
  python3 bench_uart_to_led.py run --rtos ariel --port /dev/ttyUSB0 --runs 10 --samples 200 --out ariel.csv

  # resumo estatístico de um ou mais CSVs
  python3 bench_uart_to_led.py summary ariel.csv freertos.csv

Dependência: pip install pyserial
"""

import argparse
import csv
import datetime as dt
import math
import re
import statistics
import sys
import time

RESULT_RE = re.compile(rb"R,(\d+),([10t]),(\d+)")
COMMANDS = [b"1", b"0", b"t"]
FIELDS = ["rtos", "run", "sample", "seq", "cmd", "cycles", "latency_ns", "warmup", "timestamp"]


def reset_board(ser):
    """Reinicia o ESP32 pelas linhas DTR/RTS do conversor USB-serial (boot normal)."""
    ser.dtr = False  # IO0 em nível alto -> boot normal, não modo de gravação
    ser.rts = True   # EN em nível baixo -> reset
    time.sleep(0.1)
    ser.rts = False  # solta o reset
    time.sleep(0.05)


def wait_for(ser, pattern, timeout):
    """Lê até encontrar `pattern` (regex em bytes). Ignora logs e bytes binários no meio."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if not chunk:
            continue
        buf += chunk
        m = pattern.search(buf)
        if m and buf[m.end():m.end() + 1] in (b"\r", b"\n"):
            return m
        buf = buf[-256:]  # evita crescer sem limite com lixo de log
    return None


def run(args):
    import serial  # pyserial

    clock_hz = args.cpu_mhz * 1_000_000
    ns_per_cycle = 1e9 / clock_hz
    total_timeouts = 0

    with serial.Serial(args.port, args.baud, timeout=0.05) as ser, \
            open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()

        for run_id in range(1, args.runs + 1):
            if not args.no_reset:
                reset_board(ser)
                if not wait_for(ser, re.compile(rb"READY"), args.boot_timeout):
                    sys.exit(f"[run {run_id}] a placa não imprimiu READY em {args.boot_timeout}s")
            time.sleep(args.settle)
            ser.reset_input_buffer()

            timeouts = 0
            for i in range(args.samples + args.warmup):
                cmd = COMMANDS[i % len(COMMANDS)]
                ser.write(cmd)
                m = wait_for(ser, RESULT_RE, args.timeout)
                if m is None:
                    timeouts += 1
                    if timeouts > args.max_timeouts:
                        sys.exit(f"[run {run_id}] muitas respostas perdidas ({timeouts}); abortando")
                    continue
                cycles = int(m.group(3))
                writer.writerow({
                    "rtos": args.rtos,
                    "run": run_id,
                    "sample": i,
                    "seq": int(m.group(1)),
                    "cmd": m.group(2).decode(),
                    "cycles": cycles,
                    "latency_ns": round(cycles * ns_per_cycle, 1),
                    "warmup": int(i < args.warmup),
                    "timestamp": dt.datetime.now().isoformat(timespec="milliseconds"),
                })
                time.sleep(args.interval / 1000)
            f.flush()
            total_timeouts += timeouts
            print(f"[run {run_id}/{args.runs}] ok ({args.samples} amostras, {timeouts} perdidas)")

    print(f"Dados salvos em {args.out} (respostas perdidas no total: {total_timeouts})")
    summarize([args.out])


def percentile(sorted_vals, p):
    """Percentil com interpolação linear (mesmo método do numpy padrão)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize(paths):
    groups = {}
    for path in paths:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row["warmup"] == "1":
                    continue
                groups.setdefault(row["rtos"], []).append(float(row["latency_ns"]) / 1000)

    header = f"{'RTOS':<10}{'n':>7}{'média':>10}{'mediana':>10}{'desvio':>10}{'p95':>10}{'p99':>10}{'mín':>10}{'máx':>10}"
    print("\nLatência de task-switch (µs)")
    print(header)
    print("-" * len(header))
    for rtos, vals in groups.items():
        s = sorted(vals)
        std = statistics.stdev(s) if len(s) > 1 else 0.0
        print(f"{rtos:<10}{len(s):>7}{statistics.mean(s):>10.3f}{statistics.median(s):>10.3f}"
              f"{std:>10.3f}{percentile(s, 95):>10.3f}{percentile(s, 99):>10.3f}{s[0]:>10.3f}{s[-1]:>10.3f}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    r = sub.add_parser("run", help="executa a bateria e salva CSV")
    r.add_argument("--rtos", required=True, help="rótulo gravado no CSV (ex.: ariel, freertos)")
    r.add_argument("--port", default="/dev/ttyUSB0")
    r.add_argument("--baud", type=int, default=115200)
    r.add_argument("--runs", type=int, default=10, help="execuções, com reset da placa entre elas")
    r.add_argument("--samples", type=int, default=200, help="amostras válidas por execução")
    r.add_argument("--warmup", type=int, default=10, help="amostras iniciais marcadas como aquecimento")
    r.add_argument("--interval", type=float, default=20, help="pausa entre comandos, em ms")
    r.add_argument("--timeout", type=float, default=1.0, help="espera máxima por resposta, em s")
    r.add_argument("--max-timeouts", type=int, default=20)
    r.add_argument("--boot-timeout", type=float, default=10.0)
    r.add_argument("--settle", type=float, default=0.5, help="espera após o boot, em s")
    r.add_argument("--cpu-mhz", type=int, default=240, help="clock da CPU para converter ciclos")
    r.add_argument("--no-reset", action="store_true", help="não reinicia a placa (uma execução contínua)")
    r.add_argument("--out", required=True)

    s = sub.add_parser("summary", help="estatísticas de um ou mais CSVs")
    s.add_argument("csv", nargs="+")

    args = p.parse_args()
    if args.mode == "run":
        run(args)
    else:
        summarize(args.csv)


if __name__ == "__main__":
    main()