# YTDownloader Telegram Bot

Questo bot scarica da YouTube e invia in chat:
- thumbnail (foto)
- video
- audio

Usa cache su SQLite tramite `file_id` Telegram.

## Limite upload (importante)

Con i server ufficiali `https://api.telegram.org` l’upload via Bot API è limitato (documentato) a ~50MB.
Per inviare file grandi (fino a ~2GB) devi usare un **Local Bot API Server** e puntare il bot su quello.

Se vedi errori tipo “Download incompleto (rimasti file .part)”, significa che `yt-dlp` non ha completato
il download (spesso perché il file supera il limite effettivo quando usi i server ufficiali).

## Avvio rapido con Docker Compose (consigliato, sblocca fino a ~2GB)

1. Copia `.env.example` in `.env` e compila:
   - `BOT_TOKEN`
   - `ADMIN_USER_ID`
   - `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` (da https://my.telegram.org)
2. Avvia:
   - `docker compose up -d --build`

Il bot userà automaticamente `API_BASE_URL=http://telegram-bot-api:8081` (Local Bot API Server).

## Avvio senza Docker (limite ~50MB)

1. Esporta variabili:
   - `export BOT_TOKEN=...`
   - `export ADMIN_USER_ID=...`
2. Avvia:
   - `python3 main.py`

## Config

- `BOT_TOKEN`: token BotFather
- `ADMIN_USER_ID`: il tuo user id Telegram
- `API_BASE_URL`: default `https://api.telegram.org` (per 2GB usa il local server)
- `DB_PATH`: default `bot_cache.sqlite3`
- `MAX_UPLOAD_BYTES`: limite massimo desiderato (2GB default). Su server ufficiali viene ridotto automaticamente.
- `ADMIN_CONTACT`: username (senza `@`) mostrato agli sponsor per chiedere il rinnovo. Vuoto = riga omessa.
- `BITCOIN_CHANNEL`: canale delle proposte di pubblicazione (default `@BitcoinPodcastTelegram`).

## Sponsor ("Consigliati")

Ogni messaggio del bot chiude con il blocco `Consigliati:` e una riga per sponsor attivo.
Senza sponsor attivi il blocco non compare.

Gestione dal pulsante **💼 Sponsor**, visibile e utilizzabile solo da `ADMIN_USER_ID`:
il pannello elenca gli sponsor, ognuno apre il proprio menu con ✏️ testo mostrato,
👤 proprietario, 🔄 rinnovo e 🗑️ rimozione. Il testo mostrato è libero: ci si mette un
nome, un link o un @handle (es. `@IlBarattoloBot`) e compare esattamente così.

Restano i comandi equivalenti:

- `/sponsor` — apre il pannello
- `/sponsor_add Nome Sponsor [@username_proprietario]`
- `/sponsor_remove Nome Sponsor`, `/sponsor_clear`

Uno sponsor dura **30 giorni**. Una settimana prima della scadenza il bot avvisa l'admin (con il
pulsante di rinnovo) e il proprietario, se ha già scritto al bot almeno una volta — è il motivo per
cui in fase di inserimento viene chiesto il suo username. Alla scadenza sparisce dai Consigliati e
parte un secondo avviso. Il rinnovo aggiunge 30 giorni alla scadenza se è ancora valida, altrimenti
riparte da oggi.

## Link accettati

Basta che nel messaggio ci sia un link supportato: funziona anche se è in mezzo ad altro
testo, se il messaggio è inoltrato, se sta nella didascalia di una foto o di un video, o se
è nascosto dietro un testo formattato. Il bot isola il link e ignora il resto.

## Formato dei file inviati

- **Video**: `sendVideo` con miniatura esplicita (jpeg 320px generato con ffmpeg dalla copertina
  di YouTube) più durata, larghezza e altezza. Senza questi Telegram deve indovinare un frame di
  copertina da solo e spesso mostra un riquadro nero.
- **Audio**: ricodificato in **mp3 192k** da ffmpeg, con tag e copertina incorporati, inviato con
  mime `audio/mpeg` e il titolo del video come nome file. Il flusso nativo di YouTube è opus in
  webm: Telegram lo accetta ma i telefoni non lo salvano come brano.

La cache dei `file_id` è versionata (`CACHE_FORMAT` in `storage.py`): alzando quel numero le voci
prodotte da versioni precedenti vengono ignorate e rigenerate al primo download successivo.

## Pubblicazione sul canale bitcoin

Le **iscrizioni dell'admin** sono il canale automatico: a ogni nuovo video di un canale seguito da
`ADMIN_USER_ID`, il bot scarica video e audio e li pubblica su `BITCOIN_CHANNEL` senza chiedere
niente — la scelta di cosa seguire è già il filtro.

Per tutto il resto serve l'approvazione: quando titolo, descrizione, tag o canale di un video
contengono termini esplicitamente bitcoin (`bitcoin`, `btc`, `sats`, `satoshi`, `nakamoto`,
`halving`, `hodl`, `lightning network`, `taproot`, `₿`…), il bot manda all'admin una proposta con i
termini trovati e i pulsanti ✅ Pubblica / ❌ Scarta.
