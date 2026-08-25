# tvbot-audio-client

Cliente de áudio da TV Box. Roda em Python direto no Armbian, **fora do
Docker**, e não carrega nenhum modelo.

A TV Box tem 2 GB de RAM e faz exatamente duas coisas:

1. **Capta** áudio do Anker PowerConf S3 e manda o PCM para o server por TCP.
2. **Toca** no Anker o que o server manda de volta — o beep de confirmação e o
   WAV da resposta.

Wake word, STT, chamada à API e TTS ficam **todos** no outro repositório
(`cerebro-cafe`), que roda no PC com GPU. Nada disso passa por aqui.

---

## Arquivos

```
tvbot-audio-client/
├── run.sh                ponto de entrada: gera o .env e sobe o client
├── client_armbian.py     o programa inteiro, num arquivo só
├── scripts/
│   ├── generate_env.sh   detecta IP do PC e índices do Anker
│   └── find_mic_index.py abre o stream de verdade para achar o índice PyAudio
├── requirements.txt      pyaudio, numpy, python-dotenv — só isso
├── .env.example          referência; o .env real é gerado pelo run.sh
└── media/                beep.wav é gerado aqui no primeiro start
```

---

## Instalação

```bash
sudo apt install -y portaudio19-dev python3-dev alsa-utils
sudo apt install -y nmap        # opcional: deixa a busca pelo PC bem mais rápida

git clone <este-repo> ~/tvbot-audio-client
cd ~/tvbot-audio-client
pip3 install -r requirements.txt

./run.sh
```

É isso. O `run.sh` gera o `.env` sozinho antes de subir o cliente.

## O `run.sh`

Mesma ideia do `run.sh` do projeto Go2: nada de editar índice na mão, porque
eles mudam a cada boot e a cada reconexão do USB.

**IP do PC** — tenta, nesta ordem: override manual → cache confirmado pela
porta 9876 → cache confirmado por ping → scan da sub-rede → cache sem
confirmação. O tier do ping importa mais aqui do que no Go2: o container
carrega o Whisper e o Piper *antes* de abrir o socket, então existe uma janela
de dezenas de segundos em que o PC responde a ping mas a 9876 ainda está
fechada. Sem esse tier, ligar a TV Box antes do PC faria um scan inútil da
rede inteira.

**Anker** — o `ANKER_CARD_INDEX` sai do `aplay -l` (playback), não do
`arecord -l`. Neste projeto o Anker também toca a resposta do TTS, não só o
beep, então é o card de saída que importa. Se o Anker não aparecer no
`aplay -l`, cai para o `arecord -l` com um aviso.

**`MIC_DEVICE_INDEX`** — o `find_mic_index.py` abre um stream de verdade (1 e
depois 2 canais) em vez de confiar no `maxInputChannels`, que reporta 0 de
forma espúria logo após o boot.

Ambas as detecções têm retry (`MAX_RETRIES`, padrão 5).

### Overrides

```bash
PC_SERVER_IP=192.168.0.111 ./run.sh    # pula a detecção de IP
NETWORK_IFACE=wlan0 ./run.sh           # força a interface (ignora docker0/veth)
MAX_RETRIES=10 RETRY_DELAY_S=5 ./run.sh
SKIP_ENV=1 ./run.sh                    # usa o .env atual, sem regerar
```

### Depurando na mão

```bash
aplay -l                          # card de playback do Anker
arecord -l                        # card de captura
python3 scripts/find_mic_index.py # índice PyAudio
speaker-test -D plughw:1,0 -c 1 -t sine -l 1
```

---

## O protocolo

**Ida (TV Box → server):** fluxo cru de PCM, sem framing.

```
int16 little-endian · mono · 16000 Hz · chunks de 30 ms (960 bytes)
```

O Anker captura em 48 kHz (taxa nativa); o cliente decima ÷3 antes de enviar.

**Volta (server → TV Box):** frames com header de tamanho fixo.

```
┌────────┬──────────────┬──────────────────┐
│ opcode │  tamanho     │     payload      │
│ 1 byte │ 4 bytes (BE) │  `tamanho` bytes │
└────────┴──────────────┴──────────────────┘

0x01  BEEP    tamanho = 0        → toca ./media/beep.wav (wake word reconhecida)
0x02  AUDIO   payload = WAV      → toca a resposta pelo Anker
```

> Esse framing é a única diferença em relação ao `client_armbian.py` do
> projeto Go2, onde o canal reverso era um único byte. Aqui ele precisa
> carregar áudio, e TCP não tem fronteira de mensagem — daí o header.
>
> As constantes do protocolo estão duplicadas aqui e em `server/protocol.py`
> do outro repositório. São 5 linhas; se você mudar de um lado, mude do outro.

O cliente reconecta sozinho se a rede cair ou se o server reiniciar.

---

## Configuração

O `.env` é **gerado**, não editado — o `run.sh` reescreve o arquivo a cada
execução. O `.env.example` está no repo só como referência do formato. Para
fixar um valor, passe como variável de ambiente para o `run.sh`.

| Variável | O que é |
|---|---|
| `PC_SERVER_IP` | IP do PC onde o `cerebro-cafe` está rodando |
| `TCP_PORT` | 9876 — precisa bater com o server |
| `MIC_DEVICE_INDEX` | índice do Anker no PyAudio |
| `ANKER_CARD_INDEX` | card do Anker no ALSA (`aplay -l`) |
| `AUDIO_SAMPLE_RATE` | 16000 — precisa bater com o server |
| `AUDIO_CHUNK_MS` | 30 — precisa bater com o server |
| `BEEP_FILE` | caminho absoluto do beep (gerado se não existir) |

As três últimas de "precisa bater" não são negociáveis: o `webrtcvad` e o
`openWakeWord` no server esperam frames de exatamente 30 ms a 16 kHz.
