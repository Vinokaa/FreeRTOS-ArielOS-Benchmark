#!/usr/bin/env python3
"""Coleta e agrega as medições impressas pelas aplicações (log serial -> CSV + estatísticas).

Funciona com os logs dos DOIS lados (Ariel OS e FreeRTOS): o sistema é detectado pela linha
BENCH_BEGIN,<app>,<os>,... (ou forçado com --os-label).

Uso:
    # captura (uma ou várias execuções; ex.: 30 pilotos)
    python3 scripts/collect.py ariel/*.log freertos/*.log -o out/

Saídas em out/:
    results.csv    uma linha RESULT por execução e métrica (calculada no host a partir das linhas
                   SAMPLE quando o log não traz RESULT — caso do Ariel OS)
    samples.csv    amostras brutas (linhas SAMPLE; no FreeRTOS, se compilado com CONFIG_BENCH_DUMP_RAW)
    summary.csv    agregado ENTRE execuções por (os, app, metric, cfg): média/mediana/desvio-padrão
                   das médias, pior máximo, média dos desvios, P50 e pior dos P99
    compare.csv    Ariel OS × FreeRTOS lado a lado (média das médias, P99, razões)
    footprint.csv  linhas FOOTPRINT (saída de footprint.py), se presentes nos arquivos
"""
import argparse
import csv
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FIELDS = ["n", "min", "max", "mean", "std", "p50", "p90", "p99"]
BEGIN = re.compile(r"\bBENCH_BEGIN,([^,\s]+),([^,\s]+)")


def norm_os(label):
    l = label.lower()
    if l.startswith("freertos"):
        return "freertos"
    if l.startswith("ariel"):
        return "ariel-os"
    return label


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def parse(path, run, os_override):
    lines = [ANSI.sub("", l) for l in Path(path).read_text(errors="replace").splitlines()]
    os_label = os_override
    if not os_label:
        for l in lines:
            m = BEGIN.search(l)
            if m:
                os_label = norm_os(m.group(2))
                break
    os_label = os_label or "unknown"

    results, samples, footprints, infos = [], [], [], []
    for line in lines:
        m = re.search(r"\b(RESULT|SAMPLE|FOOTPRINT|INFO),(.*)$", line)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2).strip().split(",")
        try:
            if kind == "RESULT" and len(rest) >= 12:
                results.append([run, os_label, rest[0], rest[1], rest[2]] + [int(x) for x in rest[3:11]] + [rest[11]])
            elif kind == "SAMPLE" and len(rest) >= 5:
                samples.append([run, os_label, rest[0], rest[1], rest[2], int(rest[3]), int(rest[4])])
            elif kind == "FOOTPRINT":
                footprints.append(rest)
            elif kind == "INFO" and len(rest) >= 3:
                infos.append([run, os_label, rest[0], rest[1], rest[2]])
        except ValueError:
            continue  # linha truncada/corrompida no serial
    return results, samples, footprints, infos


def results_from_samples(samples, results):
    """Linhas RESULT (mesmo formato do firmware) para grupos que só têm SAMPLE."""
    have = {tuple(r[:5]) for r in results}
    groups = defaultdict(list)
    for s in samples:
        groups[tuple(s[:5])].append(s[6])
    out = []
    for key, vals in groups.items():
        if key in have:
            continue
        v = sorted(vals)
        n = len(v)
        mean = int(sum(v) / n)  # trunca em direção a zero, como a divisão i64 do firmware
        std = math.isqrt(sum((x - mean) ** 2 for x in v) // n)
        pct = lambda p: v[(p * (n - 1) + 50) // 100]
        out.append(list(key) + [n, v[0], v[-1], mean, std, pct(50), pct(90), pct(99), "us"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--os-label", help="força o rótulo do sistema para todos os logs (ariel-os | freertos)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    res, smp, fps, infos = [], [], [], []
    for i, p in enumerate(sorted(a.logs), 1):
        r, s, f, n = parse(p, i, a.os_label)
        res += r; smp += s; fps += f; infos += n
    res += results_from_samples(smp, res)

    with open(out / "results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run", "os", "app", "metric", "cfg"] + FIELDS + ["unit"])
        w.writerows(res)

    if smp:
        with open(out / "samples.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run", "os", "app", "metric", "cfg", "idx", "value_us"])
            w.writerows(smp)

    if fps:
        with open(out / "footprint.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["os", "app", "code_B", "rodata_B", "rwdata_B", "zidata_B", "flash_total_B",
                        "ram_static_B", "flash_code_B", "iram_code_B", "elf"])
            w.writerows(fps)

    groups = defaultdict(list)
    for r in res:
        groups[(r[1], r[2], r[3], r[4])].append(r)
    idx = {k: 5 + i for i, k in enumerate(FIELDS)}

    agg = {}
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["os", "app", "metric", "cfg", "runs", "mean_of_means_us", "median_of_means_us",
                    "std_of_means_us", "worst_max_us", "mean_of_std_us", "p50_of_p99_us", "worst_p99_us"])
        for key, rows in sorted(groups.items()):
            means = [r[idx["mean"]] for r in rows]
            maxs = [r[idx["max"]] for r in rows]
            stds = [r[idx["std"]] for r in rows]
            p99s = sorted(r[idx["p99"]] for r in rows)
            agg[key] = dict(runs=len(rows), mom=statistics.fmean(means), p99=percentile(p99s, 50), worst=max(maxs))
            w.writerow(list(key) + [len(rows), f"{statistics.fmean(means):.2f}", f"{statistics.median(means):.2f}",
                                    f"{statistics.stdev(means):.2f}" if len(means) > 1 else "0.00",
                                    max(maxs), f"{statistics.fmean(stds):.2f}",
                                    f"{percentile(p99s, 50):.2f}", max(p99s)])

    pairs = defaultdict(dict)
    for (os_, app, metric, cfg), v in agg.items():
        pairs[(app, metric, cfg)][os_] = v

    def ratio(a_, b_):
        return f"{a_ / b_:.3f}" if b_ not in (0, 0.0) else "n/a"

    n_cmp = 0
    with open(out / "compare.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["app", "metric", "cfg", "ariel_mean_us", "freertos_mean_us", "ariel/freertos_mean",
                    "ariel_p99_us", "freertos_p99_us", "ariel/freertos_p99", "ariel_worst_us", "freertos_worst_us"])
        for (app, metric, cfg), d in sorted(pairs.items()):
            if "ariel-os" in d and "freertos" in d:
                A, F = d["ariel-os"], d["freertos"]
                w.writerow([app, metric, cfg, f"{A['mom']:.2f}", f"{F['mom']:.2f}", ratio(A["mom"], F["mom"]),
                            f"{A['p99']:.2f}", f"{F['p99']:.2f}", ratio(A["p99"], F["p99"]), A["worst"], F["worst"]])
                n_cmp += 1

    print(f"{len(res)} resultados de {len(a.logs)} log(s); {len(groups)} grupos; {n_cmp} comparações -> {out}/")
    lost = sum(int(i[4]) for i in infos if i[3] == "events_lost")
    if lost:
        print(f"ATENÇÃO: {lost} evento(s) perdido(s) (INFO events_lost) — investigue antes de usar os dados.")


if __name__ == "__main__":
    main()