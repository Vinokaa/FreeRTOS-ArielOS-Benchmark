# UART-to-LED — Ariel OS × FreeRTOS

Este benchmark usa o mesmo coletor e protocolo do Blinky e do IoT:
1005 medições por métrica, descarte das cinco primeiras, 1000 amostras em ciclos
de CPU e análise no host. O alvo é a ESP32-WROOM-32, com um núcleo a 240 MHz,
UART0 a 115200 baud, 8N1, TX GPIO1, RX GPIO3 e LED GPIO2.

## Métricas e relação com Wang

Base: Wang et al., *Concurrency Frameworks for Embedded Systems: A Comparative
Evaluation of FreeRTOS, RTIC, and Embassy*, IET Computers & Digital Techniques
[DOI 10.1049/cdt2/7881841](https://doi.org/10.1049/cdt2/7881841),
especialmente seções 6.4, 6.5 e 6.7 e tabela 10.

| Métrica | Referência no artigo | Implementação neste repositório |
| --- | --- | --- |
| `task_switch / uart_to_led` | Timestamp antes de sinalizar o LED até o início da tarefa receptora (§6.4). Inclui comunicação e despacho. | CCOUNT antes de `Channel::send` até imediatamente após `recv`, antes de atualizar GPIO2. FreeRTOS usa `xQueueSend`/`xQueueReceive`. |
| `irq_latency / swi_uart` | Timestamp antes do disparo EXTI por software até a entrada da ISR (§6.5). | SWI2 (`FROM_CPU_INTR2`), prioridade 1, handler em IRAM. CCOUNT antes de `raise` até a primeira leitura no handler. |
| Flash e RAM estática | Análise do binário: código, RO, RW e ZI. | `tools/footprint.py` sobre os ELFs dos dois sistemas, incluindo código em IRAM na RAM estática. |
| Jitter periódico | Diferença entre ativações periódicas e o período esperado (§6.3). | Não se aplica ao UART-to-LED: os comandos vêm do host. O intervalo serial de 500 ms não é uma tarefa periódica do firmware. |

O artigo usa STM32F303VC/Cortex-M4 a **72 MHz** e DWT; aqui usamos ESP32/Xtensa
a **240 MHz** e CCOUNT. A comparação direta de desempenho é entre FreeRTOS e
Ariel na mesma ESP32, com as mesmas condições. Os resultados de Wang são uma
referência de método, não um valor esperado nem um limite de aprovação para
esta plataforma. Ariel utiliza threads preemptivas neste benchmark; a presença
de primitivas Embassy no código não o torna o experimento Embassy do artigo.

As estatísticas seguem o padrão existente do projeto: mínimo, máximo, média,
desvio, P50, P90 e P99; a amplitude pode ser calculada por `max - min`. A tabela
10 de Wang também apresenta P95 e P99.9, que não integram o CSV padrão atual.
As médias e os desvios por execução do coletor existente são inteiros em ciclos;
preserve `samples.csv` se precisar de outra precisão ou de percentis adicionais.

## Protocolo e implementação

- `BENCH_BEGIN` declara versão `0.2.0`, unidade, frequência e amostragem.
  `INFO,...,ccount_per_100ms` verifica o relógio antes de imprimir `READY`.
- `1`, `0` e `t` ligam, desligam ou alternam o LED; cada comando válido responde
  `R,<seq>,<cmd>,<ciclos>`. A sequência começa em zero após o reset.
- Cada um dos primeiros 1005 comandos dispara uma SWI2. A thread UART aguarda
  sua conclusão antes de medir o envio ao LED. A ISR apenas registra o tempo,
  limpa a interrupção e sinaliza a thread, sem imprimir ou atualizar coletores.
- A thread LED tem prioridade 3 e a UART, 2. Ambas usam o mesmo núcleo. A UART
  espera a inicialização do LED antes de anunciar `READY`.
- O flag de confirmação da SWI usa o bit 1: o bit 0 pertence ao `block_on` no
  Ariel OS v0.6.0. Assim, um wake do driver UART não confirma uma interrupção.
- A thread UART é a única dona dos dois coletores estáticos. Eles congelam após
  1000 amostras válidas; nenhuma cópia de 4 KB é necessária na exportação.
- `b` exporta uma única vez os dois grupos `SAMPLE`, índices 0–999, seguidos de
  `BENCH_END,uart-to-led`. Antes de completar os buffers, ou após exportar,
  responde `?`. Os comandos do LED continuam funcionando após a exportação.
- CR/LF são ignorados; outros caracteres respondem `?` e não geram amostras.

Toda a saída da aplicação, inclusive `bench-common`, usa o mesmo driver UART.
As respostas `R` são transmitidas depois das janelas medidas; a exportação em
bloco ocorre somente pelo comando `b`, depois de terminar a coleta.

A latência de interrupção não mede a chegada elétrica do byte nem a ISR de RX
da UART. A latência de task-switch não inclui USB, espera no host ou transmissão
serial. A diferença de CCOUNT usa subtração com wrap de 32 bits. Os dois buffers
de amostras ocupam 8000 bytes, além de seus metadados, e integram o footprint
instrumentado. RAM estática não representa uso máximo de RAM: FreeRTOS aloca
pilhas/TCBs no heap, enquanto o ELF Ariel inclui reservas estáticas.

## Compilar sem placa

Com as ferramentas e dependências instaladas conforme o README principal:

```sh
# Na raiz do repositório, com ESP-IDF ativado:
idf.py -C FreeRTOS/uart-to-led build

# Com o ambiente Rust/Xtensa ativado, na pasta Ariel OS/uart-to-led:
laze build -b espressif-esp32-devkitc
```

Depois da compilação, na raiz, o footprint pode ser obtido sem ESP:

```sh
python3 tools/footprint.py FreeRTOS/uart-to-led/build/uart_to_led.elf \
  --app uart-to-led --os freertos
python3 tools/footprint.py \
  "Ariel OS/build/bin/espressif-esp32-devkitc/cargo/xtensa-esp32-none-elf/release/uart-to-led" \
  --app uart-to-led --os ariel-os
```

## Capturar e comparar quando a placa estiver disponível

Grave um firmware por vez na mesma placa e feche os monitores seriais. Na raiz
do repositório, substitua `PORTA` pelo dispositivo serial:

```sh
# Com FreeRTOS UART gravado:
python3 tools/capture.py --os freertos --app uart-to-led --port PORTA \
  --runs 5 --interval-ms 500 --out runs/uart/freertos

# Depois de gravar Ariel OS UART:
python3 tools/capture.py --os ariel-os --app uart-to-led --port PORTA \
  --runs 5 --interval-ms 500 --out runs/uart/ariel

python3 tools/collect.py runs/uart/freertos/*.log runs/uart/ariel/*.log -o out/uart
```

O script reinicia a placa a cada execução, envia 1005 comandos alternando
`1`, `0`, `t`, verifica cada resposta e envia `b`. Cada execução leva cerca de
8,4 minutos, mais boot e exportação. Os cinco runs são uma escolha de piloto
do projeto; não devem ser atribuídos à amostragem de Wang. `compare.csv` reúne
as duas métricas para os dois sistemas; `samples.csv`, `results.csv` e
`summary.csv` preservam amostras e estatísticas por execução e entre execuções.
Os logs UART antigos do Ariel, contendo somente `R`, precisam ser recoletados
para essa comparação. O script legado `tools/bech_uart_to_led.py` continua útil
para uma inspeção rápida de task-switch, mas não coleta a interrupção.

## Validação

**Resultado: 25 testes de host aprovados**, sem ESP conectada. Também passaram
a verificação de sintaxe Python e a conferência das dependências UART entre
`Cargo.toml` e `Cargo.lock`. Para repetir a suíte:

```sh
python3 -m unittest discover -s tools/tests -v
```

Ela verifica captura simulada dos dois sistemas, 1005 comandos e um único
pedido de exportação, respostas fora de sequência, timeout com preservação do
log parcial, grupos incompletos, amostras duplicadas, perdas declaradas,
comparação CSV das duas métricas e regressões dos demais benchmarks. Os testes
C existentes também exercitam descarte, congelamento, wrap e confirmação da SWI
FreeRTOS. Dados artificiais dos testes não são medições de desempenho.

Neste ambiente, ESP-IDF, `cargo`/`rustc`, `laze` e o compilador Xtensa não estão
instalados; portanto, o build dos firmwares e o footprint dos novos ELFs ainda
não foram validados. Permanecem pendentes na placa: boot/READY, comandos e
GPIO2, confirmação de SWI2, CCOUNT próximo de 24 milhões por 100 ms, exportação
completa e repetível, `b` antecipado/repetido e funcionamento após a exportação.
