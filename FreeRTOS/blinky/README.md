# Blinky — FreeRTOS

Port de `Ariel OS/blinky2` para ESP32-WROOM-32 / ESP-IDF.

Uma única tarefa (prioridade 2, fixada no núcleo 0) alterna o LED da GPIO 2 com
deadline absoluto (`xTaskDelayUntil`). Para cada período de 1, 10 e 100 ms são
coletadas 1005 ativações; as 5 primeiras são descartadas e as 1000 restantes
são exportadas como `SAMPLE,blinky,jitter,<1ms|10ms|100ms>,<idx>,<ciclos>`.

O jitter é `(C_i − C_{i−1}) − período × 240000`, com `C_i` lido do contador de
ciclos da CPU (CCOUNT, 240 MHz) logo após a tarefa acordar. As amostras só são
impressas depois de cada janela de medição. Após `BENCH_END,blinky` o LED
continua piscando a cada 500 ms.

## Compilar, coletar e analisar

Com ESP-IDF ativado, da raiz do repositório:

```sh
idf.py -C FreeRTOS/blinky set-target esp32
idf.py -C FreeRTOS/blinky build size
idf.py -C FreeRTOS/blinky -p PORTA flash
python3 tools/capture.py --os freertos --app blinky --port PORTA \
  --runs 30 --out runs/freertos-blinky-piloto
python3 tools/collect.py runs/freertos-blinky-piloto/*.log -o out/blinky
python3 tools/footprint.py FreeRTOS/blinky/build/blinky.elf \
  --app blinky --os freertos
```

Uma execução completa leva cerca de 2 minutos (1 + 10 + 100 s de coleta).
Os defaults são os mesmos do IoT: CPU fixa em 240 MHz, tick de 1 ms, um núcleo
e otimização de desempenho.
