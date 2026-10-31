# UART-to-LED — FreeRTOS

Implementação ESP-IDF do mesmo benchmark de [Ariel OS](../../Ariel%20OS/uart-to-led/). Ambas usam ESP32-WROOM-32, UART0 a 115200 baud (8N1, sem controle de fluxo), GPIO 2 para o LED e o mesmo protocolo serial.

No boot, a aplicação envia `READY`. Cada comando válido recebido pelo terminal gera uma linha `R,<seq>,<cmd>,<ciclos>`, em que `ciclos` mede o intervalo entre o envio do comando pela tarefa UART e o início da tarefa LED. As duas tarefas ficam fixadas no mesmo núcleo; a tarefa LED tem prioridade maior que a tarefa UART e é acordada imediatamente pelo envio à fila.

| Comando | Efeito |
| --- | --- |
| `1` | Liga o LED (GPIO 2 em nível alto). |
| `0` | Desliga o LED (GPIO 2 em nível baixo). |
| `t` | Alterna o estado do LED. |

Linhas terminadas por `CR` ou `LF` são ignoradas. Outros caracteres recebem a resposta `?`.

A instrumentação armazena 1000 latências de task-switch e 1000 de interrupção,
após descartar 5 comandos de aquecimento por métrica. Cada comando válido gera
uma SWI2; a tarefa aguarda sua conclusão antes de iniciar a medição da fila.
Depois de pelo menos 1005 comandos válidos, envie `b` para exportar uma única vez
os dois grupos `SAMPLE`, com índices de 0 a 999, seguidos de
`BENCH_END,uart-to-led`. Antes de completar os dois buffers, `b` responde `?`.
A aplicação continua aceitando `0`, `1` e `t` após a exportação. `READY` e as
respostas `R` permanecem compatíveis com o script original. O boot acrescenta
`BENCH_BEGIN`, `META` e `INFO`; `R` mantém ciclos, `SAMPLE` usa microssegundos
inteiros (`ciclos / 240`).

| Métrica / configuração CSV | Intervalo medido |
| --- | --- |
| `task_switch / uart_to_led` | CCOUNT antes do envio à fila até a leitura após o recebimento pela tarefa LED. |
| `irq_latency / swi_uart` | Relógio de hardware de 1 µs antes de disparar SWI2 até a primeira leitura no handler em IRAM, nível 1. |

A medição de interrupção usa o mesmo método de disparo por software do IoT e
do experimento de Wang. Não mede a chegada elétrica do byte nem a interrupção
de recepção do driver UART. A notificação confirma a conclusão da ISR e fica
fora da janela medida de interrupção; a SWI termina antes da janela de
task-switch. Não há exportação durante a coleta, apenas as respostas `R`.
Para comparar interrupções com o UART Ariel, ainda é preciso implementar nele
o mesmo experimento; a saída `R` existente permite comparar task-switch.

Os defaults fixam CPU em 240 MHz, tick em 1 ms, otimização de desempenho e
execução em um núcleo. Confira a configuração gerada se já houver `sdkconfig`.
O firmware rejeita compilações com frequência diferente ou gerenciamento
dinâmico de frequência, pois isso invalidaria a conversão de ciclos.
O build também exige um núcleo para liberar a SWI2 reservada ao IPC no ESP-IDF.

## Compilar e gravar

Ative o ESP-IDF e, a partir da raiz do repositório, execute:

```sh
cd FreeRTOS/uart-to-led
idf.py set-target esp32
idf.py build
idf.py -p PORTA flash monitor
```

Substitua `PORTA` pela porta serial da placa, por exemplo `/dev/ttyUSB0` ou `/dev/cu.usbserial-...`. Para sair do monitor, pressione `Ctrl-]`.

## Benchmark comparável ao Ariel OS

Use o mesmo script que acompanha a implementação Ariel. Para mudar a entrada do terminal a cada 500 ms, defina `--interval 500`; o intervalo pertence ao gerador de carga, não ao firmware, para que a medição de troca de tarefa não inclua uma espera artificial.

```sh
python3 "Ariel OS/uart-to-led/tools/bech_uart_to_led.py" run \
  --rtos freertos --port PORTA --interval 500 --runs 1 --samples 30 \
  --out freertos.csv

python3 "Ariel OS/uart-to-led/tools/bech_uart_to_led.py" run \
  --rtos ariel --port PORTA --interval 500 --runs 1 --samples 30 \
  --out ariel.csv

python3 "Ariel OS/uart-to-led/tools/bech_uart_to_led.py" summary freertos.csv ariel.csv
```

The script requires `pyserial` (`python3 -m pip install pyserial`). Flash only one firmware at a time and set `PORTA` to that board's serial port.

Para seguir a amostragem de Wang e do `bench-common` com o script original,
use `--warmup 5 --samples 1000`. Para 30 execuções piloto com exportação
`SAMPLE`, execute da raiz do repositório, com o firmware UART já gravado:

```sh
python3 FreeRTOS/tools/capture.py --app uart-to-led --port PORTA \
  --runs 30 --interval-ms 500 --out runs/freertos-uart-piloto
python3 "Ariel OS/scripts/collect.py" runs/freertos-uart-piloto/*.log \
  -o out/uart
python3 FreeRTOS/tools/footprint.py FreeRTOS/uart-to-led/build/uart_to_led.elf \
  --app uart-to-led --os freertos
```

Cada execução reinicia a placa por DTR/RTS. A 500 ms, a coleta leva cerca de
8,4 minutos por execução. O footprint usa o ELF e pode ser obtido sem placa;
o valor de RAM estática não inclui stacks e TCBs alocados no heap em execução.
