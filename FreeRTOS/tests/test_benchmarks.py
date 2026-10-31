"""Host regression checks for sampling, serial capture and cross-RTOS analysis."""
import importlib.util
import io
from collections import Counter
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_module("capture", "FreeRTOS/tools/capture.py")
footprint = load_module("footprint", "FreeRTOS/tools/footprint.py")
collect = load_module("collect", "Ariel OS/scripts/collect.py")


def sample_log(app):
    lines = [f"BENCH_BEGIN,{app},freertos,0.1.0,clock_res_us=1,n=1000,warmup=5"]
    for metric, cfg in sorted(capture.EXPECTED[app]):
        lines += [f"SAMPLE,{app},{metric},{cfg},{i},{i - 500}" for i in range(1000)]
    return lines + [f"INFO,{app},events_lost,0", f"BENCH_END,{app}"]


class SamplingTests(unittest.TestCase):
    def test_uart_irq_handshake_warmup_wrap_and_frozen_buffer(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        source = (ROOT / "FreeRTOS/uart-to-led/main/uart_to_led_main.c").read_text()
        functions = source[source.index("static void IRAM_ATTR benchmark_swi("):
                           source.index("static void write_response(")]
        code = r'''
#include <assert.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include "bench_common.h"
typedef int BaseType_t;
typedef void *TaskHandle_t;
#define IRAM_ATTR
#define pdFALSE 0
#define pdTRUE 1
#define pdMS_TO_TICKS(ms) (ms)
#define DPORT_CPU_INTR_FROM_CPU_2_REG 0
#define DPORT_CPU_INTR_FROM_CPU_2 1
static bench_collector_t irq_col;
static TaskHandle_t benchmark_uart_task;
static _Atomic uint32_t irq_entered_us;
static uint32_t fake_time = UINT32_MAX - 3;
static unsigned pending, generated, cleared, yielded;
static int64_t esp_timer_get_time(void) {
    uint32_t t = fake_time;
    fake_time += 7;
    return t;
}
static void fake_write(unsigned reg, unsigned value);
#define DPORT_REG_WRITE(reg, value) fake_write(reg, value)
static void vTaskNotifyGiveFromISR(TaskHandle_t task, BaseType_t *woken) {
    assert(task == benchmark_uart_task);
    ++pending;
    *woken = pdTRUE;
}
#define portYIELD_FROM_ISR() (++yielded)
static uint32_t ulTaskNotifyTake(BaseType_t clear, unsigned timeout) {
    assert(clear == pdTRUE && timeout == 1000);
    uint32_t n = pending;
    pending = 0;
    return n;
}
''' + functions + r'''
static void fake_write(unsigned reg, unsigned value) {
    assert(reg == DPORT_CPU_INTR_FROM_CPU_2_REG);
    if (value) {
        ++generated;
        benchmark_swi(NULL); /* Synchronous fake IRQ, before notification wait. */
    } else {
        ++cleared;
    }
}
int main(void) {
    for (unsigned i = 0; i < BENCH_WARMUP; ++i) measure_interrupt_latency();
    assert(irq_col.len == 0);
    for (unsigned i = 0; i < BENCH_N; ++i) measure_interrupt_latency();
    assert(bench_full(&irq_col));
    for (unsigned i = 0; i < BENCH_N; ++i) assert(irq_col.samples[i] == 7);
    unsigned count = generated;
    measure_interrupt_latency();
    assert(generated == count && count == BENCH_N + BENCH_WARMUP);
    assert(cleared == count && yielded == count && pending == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uart_irq.c"
            binary = Path(directory) / "uart_irq"
            path.write_text(code)
            subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "FreeRTOS/components/bench_common/include"),
                            str(path), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)

    def test_processing_filter_startup_ring_and_alarm_hysteresis(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        # Exercise the actual arithmetic block without simulating an RTOS.
        source = (ROOT / "FreeRTOS/iot/main/iot_main.c").read_text()
        block = source[source.index("        window[index] = raw;"):
                       source.index("        portENTER_CRITICAL(&shared_lock);",
                                    source.index("static void processing_task("))]
        constants = "\n".join(line for line in source.splitlines()
                              if line.startswith(("#define FILTER_WINDOW ",
                                                  "#define ALARM_ON_MV ",
                                                  "#define ALARM_OFF_MV ")))
        code = "#include <stdint.h>\n#include <stddef.h>\n#include <stdbool.h>\n#include <assert.h>\n"
        code += constants + r'''
static uint16_t window[FILTER_WINDOW];
static size_t index, filled;
static uint32_t seq;
static bool alarm;
static uint32_t step(uint16_t raw) {
''' + block + r'''
    return mv;
}
int main(void) {
    /* Startup averages only available readings, rather than zero-padding. */
    assert(step(2300) == 1853 && alarm);
    for (unsigned i = 0; i < FILTER_WINDOW; ++i) window[i] = 0;
    index = filled = seq = 0;
    alarm = false;
    for (unsigned i = 0; i < 8; ++i) step(2000);
    assert(step(2000) == 1611 && !alarm);
    for (unsigned i = 0; i < 8; ++i) step(2300);
    assert(step(2300) == 1853 && alarm);
    for (unsigned i = 0; i < 8; ++i) step(2150);
    assert(step(2150) == 1732 && alarm); /* Inside band: keep ON. */
    for (unsigned i = 0; i < 8; ++i) step(2100);
    assert(step(2100) == 1692 && !alarm);
    for (unsigned i = 0; i < 8; ++i) step(2150);
    assert(step(2150) == 1732 && !alarm); /* Inside band: keep OFF. */
    assert(filled == FILTER_WINDOW && seq == 45);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "processing.c"
            binary = Path(directory) / "processing"
            path.write_text(code)
            subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                            str(path), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)

    def test_sensor_matches_ariel_logged_reference_vectors(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        # Compile the actual acquisition function; reference values come from
        # the first ten normal LOG lines in Ariel's existing hardware capture.
        source = (ROOT / "FreeRTOS/iot/main/iot_main.c").read_text()
        synth = source[source.index("static uint16_t synth("):
                       source.index("\nstatic void IRAM_ATTR")]
        code = "#include <stdint.h>\n#include <assert.h>\n" + synth + r'''
int main(void) {
    const uint16_t expected[] = {1985, 2495, 2015, 1493, 1992,
                                 2502, 1990, 1500, 2000, 2510};
    for (unsigned i = 0; i < 10; ++i)
        assert(synth(100 * (i + 1) - 1) == expected[i]);
    /* Explicit unsigned wrap also covers the LCG at sequence rollover. */
    assert(synth(UINT32_MAX) == 1987);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensor.c"
            binary = Path(directory) / "sensor"
            path.write_text(code)
            subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                            str(path), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)

    def test_actual_c_collector_warmup_freeze_and_wrap(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        code = r'''
#include <assert.h>
#include "bench_common.h"
int main(void) {
    bench_collector_t col = {0};
    for (int i = 0; i < BENCH_WARMUP; ++i) bench_push(&col, 999999);
    assert(col.len == 0 && !bench_full(&col));
    for (int i = 0; i < BENCH_N; ++i) bench_push(&col, i - 500);
    assert(bench_full(&col));
    for (int i = 0; i < 100; ++i) bench_push(&col, 999999);
    assert(col.len == BENCH_N);
    for (int i = 0; i < BENCH_N; ++i) assert(col.samples[i] == i - 500);
    assert(bench_delta(UINT32_MAX - 4, 5) == 10);
    assert(bench_delta(10000, 19999) - 10000 == -1);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "collector.c"
            binary = Path(directory) / "collector"
            source.write_text(code)
            subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "FreeRTOS/components/bench_common/include"),
                            str(source), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)


class CaptureTests(unittest.TestCase):
    def test_uart_requires_irq_samples_as_well_as_task_switch(self):
        lines = sample_log("uart-to-led")
        capture.validate(lines, "uart-to-led")
        old_protocol = [line for line in lines if not line.startswith(
            "SAMPLE,uart-to-led,irq_latency,")]
        with self.assertRaisesRegex(ValueError, "unexpected metric groups"):
            capture.validate(old_protocol, "uart-to-led")

    def test_complete_iot(self):
        capture.validate(sample_log("iot"), "iot")

    def test_timeout_preserves_partial_serial_log(self):
        class Serial:
            dtr = rts = False

            def reset_input_buffer(self):
                pass

            def readline(self):
                return (sample_log("iot")[0] + "\n").encode()

        output = io.BytesIO()
        with patch.object(capture.time, "sleep"), \
                patch.object(capture.time, "monotonic", side_effect=[0, 1, 3]):
            with self.assertRaises(TimeoutError):
                capture.capture_run(Serial(), output, "iot", 0, 2)
        self.assertIn(b"BENCH_BEGIN,iot,freertos", output.getvalue())
        self.assertNotIn(b"BENCH_END", output.getvalue())

    def test_reject_malformed_value_wrong_group_and_missing_begin(self):
        lines = sample_log("iot")
        for bad in (lines[1:],
                    [line.replace(",sensor_10ms,", ",wrong_config,") for line in lines],
                    lines[:1] + [lines[1].rsplit(",", 1)[0] + ",invalid"] + lines[2:]):
            with self.subTest():
                with self.assertRaises(ValueError):
                    capture.validate(bad, "iot")

    def test_uart_rejects_response_for_wrong_sequence(self):
        class Serial:
            dtr = rts = False

            def __init__(self):
                self.pending = [sample_log("uart-to-led")[0], "READY", "R,99,1,480"]

            def reset_input_buffer(self):
                pass

            def readline(self):
                return (self.pending.pop(0) + "\n").encode()

            def write(self, data):
                pass

        with patch.object(capture.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "unexpected command response"):
                capture.capture_run(Serial(), io.BytesIO(), "uart-to-led", 0, 30)

    def test_reject_missing_duplicate_and_lost_samples(self):
        lines = sample_log("iot")
        for bad in (lines[:-1], lines[:1] + lines[2:],
                    lines[:2] + lines[1:],
                    [line.replace("events_lost,0", "events_lost,1") for line in lines]):
            with self.subTest():
                with self.assertRaises(ValueError):
                    capture.validate(bad, "iot")

    def test_uart_drives_1005_commands_then_exports(self):
        class Serial:
            dtr = rts = False

            def __init__(self):
                self.pending = [sample_log("uart-to-led")[0], "READY"]
                self.commands = []

            def reset_input_buffer(self):
                pass

            def readline(self):
                return (self.pending.pop(0) + "\n").encode() if self.pending else b""

            def write(self, data):
                if data == b"b":
                    self.pending += sample_log("uart-to-led")[1:]
                else:
                    seq = len(self.commands)
                    self.commands.append(data)
                    self.pending.append(f"R,{seq},{data.decode()},480")

        serial = Serial()
        output = io.BytesIO()
        with patch.object(capture.time, "sleep"):
            capture.capture_run(serial, output, "uart-to-led", 500, 30)
        self.assertEqual(len(serial.commands), 1005)
        self.assertEqual(serial.commands[:3], [b"1", b"0", b"t"])
        self.assertIn(b"BENCH_END,uart-to-led", output.getvalue())
        self.assertEqual(output.getvalue().count(b"SAMPLE,uart-to-led,task_switch,"), 1000)
        self.assertEqual(output.getvalue().count(b"SAMPLE,uart-to-led,irq_latency,"), 1000)


class AnalysisTests(unittest.TestCase):
    def test_comparison_cli_with_explicitly_artificial_inputs(self):
        import csv

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            paths = []
            for os_label in ("freertos", "ariel-os"):
                path = work / f"{os_label}.log"
                lines = [line.replace(",freertos,", f",{os_label},")
                         for line in sample_log("iot")]
                path.write_text("\n".join(lines))
                paths.append(str(path))
            out = work / "out"
            subprocess.run([sys.executable, str(ROOT / "Ariel OS/scripts/collect.py"),
                            *paths, "-o", str(out)], check=True, capture_output=True)
            for name, count in (("samples.csv", 8000), ("results.csv", 8),
                                ("summary.csv", 8), ("compare.csv", 4)):
                with (out / name).open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), count, name)
            self.assertTrue(all(row["ariel/freertos_p99"] == "1.000" for row in rows))

    def test_rust_log_suffix_and_negative_jitter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ariel.log"
            path.write_text(
                "[INFO ] BENCH_BEGIN,iot,ariel-os,0.1.0 (bench_common report.rs:20)\n"
                "\x1b[32m[INFO ] SAMPLE,iot,jitter,sensor_10ms,0,-3 "
                "(bench_common bench-common/src/report.rs:51)\x1b[0m\n")
            _, samples, _, _ = collect.parse(path, 1, None)
        self.assertEqual(samples, [[1, "ariel-os", "iot", "jitter", "sensor_10ms", 0, -3]])

    def test_existing_ariel_capture_exposes_missing_processing_samples(self):
        path = ROOT / "Ariel OS/runs/ariel_iot_01.log"
        _, samples, _, _ = collect.parse(path, 1, None)
        self.assertEqual(Counter(sample[3] for sample in samples),
                         {"jitter": 1000, "irq_latency": 1000,
                          "task_switch": 1000, "proc_exec": 992})
        self.assertEqual(len(collect.results_from_samples(samples, [])), 4)

    def test_freertos_capture_feeds_existing_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "freertos.log"
            path.write_text("\n".join(sample_log("iot")))
            _, samples, _, _ = collect.parse(path, 1, None)
        results = collect.results_from_samples(samples, [])
        self.assertEqual(len(results), 4)
        for result in results:
            self.assertEqual(result[1], "freertos")
            self.assertEqual(result[5:9], [1000, -500, 499, 0])


class FootprintTests(unittest.TestCase):
    def test_elf_allocations_iram_and_flash_dummy(self):
        entries = [(0,) * 10]
        for kind, flags, address, size in (
                (1, 6, 0x400D0010, 100), (1, 6, 0x40080000, 20),
                (1, 3, 0x3F400000, 40), (1, 3, 0x3FFB0000, 12),
                (8, 3, 0x3FFB0010, 200), (8, 3, 0x3F400100, 4096),
                (1, 0, 0, 900)):
            entries.append((0, kind, flags, address, 0, size, 0, 0, 4, 0))
        header = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01" + b"\0" * 10,
                             2, 94, 1, 0, 0, 52, 0, 52, 0, 0, 40, len(entries), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.elf"
            path.write_bytes(header + b"".join(struct.pack("<IIIIIIIIII", *s) for s in entries))
            sizes = footprint.measure(path)
        self.assertEqual(sizes, dict(code_B=120, rodata_B=40, rwdata_B=12, zidata_B=200,
                                     flash_total_B=172, ram_static_B=232,
                                     flash_code_B=100, iram_code_B=20))

    def test_reject_non_elf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.elf"
            path.write_bytes(b"not an ELF")
            with self.assertRaises(ValueError):
                footprint.measure(path)


if __name__ == "__main__":
    unittest.main()
