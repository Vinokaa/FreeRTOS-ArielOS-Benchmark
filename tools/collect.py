#!/usr/bin/env python3
"""Coleta e agrega as medições impressas pelas aplicações (log serial -> CSV + estatísticas).

Funciona com os logs dos DOIS lados (Ariel OS e FreeRTOS): o sistema e a unidade são
detectados pela linha BENCH_BEGIN,<app>,<os>,... (o sistema pode ser forçado com --os-label).
Protocolo 0.2.0: valores em ciclos de CPU (unit=cycles); logs antigos (clock_res_us) em µs.

Uso:
    python3 tools/collect.py runs/ariel/*.log runs/freertos/*.log -o out/

Saídas em out/:
    results.csv    uma linha por execução e métrica (calculada no host a partir das linhas
                   SAMPLE quando o log não traz RESULT)
    samples.csv    amostras brutas (linhas SAMPLE dos dois sistemas)
    summary.csv    agregado ENTRE execuções por (os, app, metric, cfg): média/mediana/desvio-padrão
                   das médias, pior máximo, média dos desvios, P50 e pior dos P99
    compare.csv    Ariel OS × FreeRTOS lado a lado (média das médias, P99, razões)
    footprint.csv  linhas FOOTPRINT (saída de footprint.py), se presentes nos arquivos
    value_counts_<app>_<cfg>.csv
                   frequência de cada valor (value_counts) das amostras de jitter, somando
                   todas as execuções de cada sistema: um arquivo por configuração
                   (blinky 1ms/10ms/100ms e iot sensor_10ms)
    jitter_iot_sensor_10ms.png
                   gráfico de barras (matplotlib) da distribuição do jitter do IoT (% das
                   amostras por faixa de valor), FreeRTOS em azul e Ariel OS em laranja
"""
import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlog import normalize, parse_begin  # noqa: E402

FIELDS = ["n", "min", "max", "mean", "std", "p50", "p90", "p99"]


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def parse(path, run, os_override):
    """Linhas de um log: (results, samples, footprints, infos).

    samples: [run, os, app, metric, cfg, idx, valor, unidade]
    results: [run, os, app, metric, cfg, n, min, max, mean, std, p50, p90, p99, unidade]
    """
    lines = [normalize(l) for l in Path(path).read_text(errors="replace").splitlines()]
    begin = next((b for b in map(parse_begin, lines) if b), None)
    os_label = os_override or (begin["os"] if begin else "unknown")
    unit = begin["unit"] if begin else "us"

    results, samples, footprints, infos = [], [], [], []
    for line in lines:
        kind, _, rest = line.partition(",")
        rest = rest.split(",")
        try:
            if kind == "RESULT" and len(rest) >= 12:
                results.append([run, os_label, rest[0], rest[1], rest[2]]
                               + [int(x) for x in rest[3:11]] + [rest[11]])
            elif kind == "SAMPLE" and len(rest) >= 5:
                samples.append([run, os_label, rest[0], rest[1], rest[2],
                                int(rest[3]), int(rest[4]), unit])
            elif kind == "FOOTPRINT":
                footprints.append(rest)
            elif kind == "INFO" and len(rest) >= 3:
                infos.append([run, os_label, rest[0], rest[1], rest[2]])
        except ValueError:
            continue  # linha truncada/corrompida no serial
    return results, samples, footprints, infos


def results_from_samples(samples, results):
    """Linhas RESULT para grupos que só têm SAMPLE."""
    have = {tuple(r[:5]) for r in results}
    groups = defaultdict(list)
    units = {}
    for s in samples:
        groups[tuple(s[:5])].append(s[6])
        units[tuple(s[:5])] = s[7]
    out = []
    for key, vals in groups.items():
        if key in have:
            continue
        v = sorted(vals)
        n = len(v)
        mean = int(sum(v) / n)  # trunca em direção a zero, como a divisão inteira do firmware
        std = math.isqrt(sum((x - mean) ** 2 for x in v) // n)
        pct = lambda p: v[(p * (n - 1) + 50) // 100]
        out.append(list(key) + [n, v[0], v[-1], mean, std, pct(50), pct(90), pct(99), units[key]])
    return out


def write_value_counts(samples, out, metric="jitter"):
    """Um CSV de value_counts por (app, cfg) da métrica, com os dois sistemas lado a lado.

    Colunas: os, value, count, pct (percentual dentro do sistema), runs, unit.
    Devolve os nomes dos arquivos gerados.
    """
    counts = defaultdict(lambda: defaultdict(Counter))  # (app, cfg) -> os -> Counter
    runs = defaultdict(set)
    units = defaultdict(set)
    for run, os_, app, met, cfg, _, value, unit in samples:
        if met != metric:
            continue
        counts[(app, cfg)][os_][value] += 1
        runs[(app, cfg, os_)].add(run)
        units[(app, cfg, os_)].add(unit)

    written = []
    for (app, cfg), by_os in sorted(counts.items()):
        name = f"value_counts_{app}_{cfg}.csv"
        with open(out / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["os", "value", "count", "pct", "runs", "unit"])
            for os_, c in sorted(by_os.items()):
                total = sum(c.values())
                unit = "/".join(sorted(units[(app, cfg, os_)]))
                for value, n in sorted(c.items()):
                    w.writerow([os_, value, n, f"{100 * n / total:.2f}",
                                len(runs[(app, cfg, os_)]), unit])
        written.append(name)
    return written


OS_STYLE = {  # (rótulo, cor): FreeRTOS azul, Ariel OS laranja
    "freertos": ("FreeRTOS", "#2a78d6"),
    "ariel-os": ("Ariel OS", "#eb6834"),
}


def write_jitter_bar_png(samples, out, app="iot", cfg="sensor_10ms", bin_cycles=48):
    """Gráfico de barras (matplotlib, PNG) do jitter de (app, cfg): % das amostras de cada
    sistema por faixa de `bin_cycles` ciclos (48 ciclos = 0,2 µs a 240 MHz), lado a lado.

    Devolve o nome do arquivo, ou None se não houver amostras.
    """
    import matplotlib
    matplotlib.use("Agg")  # sem janela: só grava o arquivo
    import matplotlib.pyplot as plt

    counts = {os_: Counter() for os_ in OS_STYLE}
    for _, os_, a_, met, c_, _, value, _ in samples:
        if a_ == app and met == "jitter" and c_ == cfg and os_ in counts:
            counts[os_][math.floor(value / bin_cycles + 0.5)] += 1  # índice da faixa
    oses = [o for o in OS_STYLE if counts[o]]
    if not oses:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=200)
    width = bin_cycles * 0.9 / len(oses)
    for k, o in enumerate(oses):
        label, color = OS_STYLE[o]
        total = sum(counts[o].values())
        bins = sorted(counts[o])
        x = [b * bin_cycles + (k - (len(oses) - 1) / 2) * width for b in bins]
        y = [100 * counts[o][b] / total for b in bins]
        ax.bar(x, y, width=width, color=color, label=label, zorder=2)

    span = max(abs(b) * bin_cycles for o in oses for b in counts[o]) + bin_cycles
    x_lim = 250 * math.ceil(span / 250)
    ax.set_xlim(-x_lim, x_lim)
    ax.set_xticks(range(-x_lim, x_lim + 1, 250))
    ax.axvline(0, color="#6b6a66", linewidth=0.8, linestyle="--", zorder=1)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=0))
    ax.grid(axis="y", color="#e6e5e1", linewidth=0.8, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel("Jitter (ciclos de CPU; 240 ciclos = 1 µs)")
    ax.set_ylabel("Amostras (%)")
    ax.set_title(f"Jitter da tarefa periódica ({app}, {cfg}) — faixas de {bin_cycles} ciclos",
                 loc="left", fontsize=11)
    ax.legend(frameon=False)
    fig.tight_layout()

    name = f"jitter_{app}_{cfg}.png"
    fig.savefig(out / name)
    plt.close(fig)
    return name


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
            w.writerow(["run", "os", "app", "metric", "cfg", "idx", "value", "unit"])
            w.writerows(smp)
        write_value_counts(smp, out)
        write_jitter_bar_png(smp, out)

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
        w.writerow(["os", "app", "metric", "cfg", "unit", "runs", "mean_of_means", "median_of_means",
                    "std_of_means", "worst_max", "mean_of_std", "p50_of_p99", "worst_p99"])
        for key, rows in sorted(groups.items()):
            units = {r[-1] for r in rows}
            if len(units) > 1:
                sys.exit(f"erro: {key} mistura unidades {sorted(units)}; não combine logs "
                         "de protocolos diferentes (µs × ciclos)")
            unit = units.pop()
            means = [r[idx["mean"]] for r in rows]
            maxs = [r[idx["max"]] for r in rows]
            stds = [r[idx["std"]] for r in rows]
            p99s = sorted(r[idx["p99"]] for r in rows)
            agg[key] = dict(runs=len(rows), unit=unit, mom=statistics.fmean(means),
                            p99=percentile(p99s, 50), worst=max(maxs))
            w.writerow(list(key) + [unit, len(rows), f"{statistics.fmean(means):.2f}",
                                    f"{statistics.median(means):.2f}",
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
        w.writerow(["app", "metric", "cfg", "unit", "ariel_mean", "freertos_mean", "ariel/freertos_mean",
                    "ariel_p99", "freertos_p99", "ariel/freertos_p99", "ariel_worst", "freertos_worst"])
        for (app, metric, cfg), d in sorted(pairs.items()):
            if "ariel-os" in d and "freertos" in d:
                A, F = d["ariel-os"], d["freertos"]
                if A["unit"] != F["unit"]:
                    print(f"ATENÇÃO: {app}/{metric}/{cfg} não comparado: unidades "
                          f"{A['unit']} × {F['unit']}")
                    continue
                w.writerow([app, metric, cfg, A["unit"], f"{A['mom']:.2f}", f"{F['mom']:.2f}",
                            ratio(A["mom"], F["mom"]), f"{A['p99']:.2f}", f"{F['p99']:.2f}",
                            ratio(A["p99"], F["p99"]), A["worst"], F["worst"]])
                n_cmp += 1

    print(f"{len(res)} resultados de {len(a.logs)} log(s); {len(groups)} grupos; {n_cmp} comparações -> {out}/")
    lost = sum(int(i[4]) for i in infos if i[3] == "events_lost")
    if lost:
        print(f"ATENÇÃO: {lost} evento(s) perdido(s) (INFO events_lost) — investigue antes de usar os dados.")


if __name__ == "__main__":
    main()
