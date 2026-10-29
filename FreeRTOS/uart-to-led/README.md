# UART-to-LED — FreeRTOS

Implementação ESP-IDF do mesmo benchmark de [Ariel OS](../../Ariel%20OS/uart-to-led/). Ambas usam ESP32-WROOM-32, UART0 a 115200 baud (8N1, sem controle de fluxo), GPIO 2 para o LED e o mesmo protocolo serial.

No boot, a aplicação envia `READY`. Cada comando válido recebido pelo terminal gera uma linha `R,<seq>,<cmd>,<ciclos>`, em que `ciclos` mede o intervalo entre o envio do comando pela tarefa UART e o início da tarefa LED. As duas tarefas ficam fixadas no mesmo núcleo; a tarefa LED tem prioridade maior que a tarefa UART e é acordada imediatamente pelo envio à fila.

| Comando | Efeito |
| --- | --- |
| `1` | Liga o LED (GPIO 2 em nível alto). |
| `0` | Desliga o LED (GPIO 2 em nível baixo). |
| `t` | Alterna o estado do LED. |

Linhas terminadas por `CR` ou `LF` são ignoradas. Outros caracteres recebem a resposta `?`.

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
