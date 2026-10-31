# IoT — FreeRTOS

Port de `Ariel OS/iot` para ESP32-WROOM-32 / ESP-IDF.

| Tarefa | Prioridade | Ativação | Trabalho |
| --- | --- | --- | --- |
| Sensor | 3 | 10 ms, deadline absoluto | Triângulo + ruído LCG determinístico, 12 bits; gera SWI2. |
| Processamento | 4 | Notificação da ISR | Média móvel de 8 leituras, conversão `avg * 3300 / 4095`, histerese 1800/1700 mV; publica struct protegido. |
| Atuador | 2 | 20 ms, deadline absoluto | Lê o alarme e atualiza GPIO2. |
| Log | 1 | 1 s | Lê o struct e imprime `LOG,seq=...,raw=...,mv=...,alarm=...`. |

Aquisição, processamento e atuação continuam após o benchmark. A ISR apenas
registra timestamps, limpa a interrupção e notifica o processamento, sem logs
ou cálculo de estatísticas. Eventos pendentes não são sobrescritos; perdas
durante a janela de coleta aparecem em `INFO,iot,events_lost,N`.

São exportadas 1000 amostras válidas de jitter, latência de interrupção,
task-switch e execução do processamento, com 5 descartes iniciais por métrica.
O logger exporta somente após o sensor e o processamento completarem a coleta.

## Compilar, coletar e analisar

Com ESP-IDF ativado, execute da raiz do repositório:

```sh
idf.py -C FreeRTOS/iot set-target esp32
idf.py -C FreeRTOS/iot build size
idf.py -C FreeRTOS/iot -p PORTA flash
python3 FreeRTOS/tools/capture.py --app iot --port PORTA \
  --runs 30 --out runs/freertos-iot-piloto
python3 "Ariel OS/scripts/collect.py" runs/freertos-iot-piloto/*.log \
  -o out/iot
python3 FreeRTOS/tools/footprint.py FreeRTOS/iot/build/iot.elf \
  --app iot --os freertos
```

Substitua `PORTA` pela porta serial. A captura requer `pyserial`, reinicia a placa
por DTR/RTS e valida 4000 amostras por execução, sem eventos perdidos. Aguarde
`BENCH_END,iot`; não mantenha outro monitor serial aberto durante a captura.
`set-target` regenera a configuração, então preserve ajustes locais antes.

Os defaults usam CPU fixa em 240 MHz, tick de 1 ms e um núcleo, liberando a SWI2
que o ESP-IDF reserva para IPC em builds multicore. `build size` e a análise do
ELF funcionam sem placa. O footprint inclui buffers de medição e RAM estática;
stacks e TCBs alocados no heap em execução precisam de avaliação separada.
