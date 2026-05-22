# YouTube to Telegram Uploader Bot

Questo bot permette di scaricare video e audio da YouTube per caricarli su canali Telegram privati in modo completamente formattato. È pensato per arricchire i contenuti scaricati con una dicitura sponsorizzata e caricare file di grandi dimensioni sfruttando un Local Bot API Server.

## Caratteristiche
- **Download da YouTube**: Scarica sia formati video che solo audio.
- **Formattazione dei contenuti**: Formatta i contenuti testuali in modo pulito per Telegram.
- **Dicitura Sponsorizzata**: Permette l'inserimento di messaggi promozionali e sponsor.
- **Supporto per file di grandi dimensioni**: Utilizza le credenziali API di Telegram per un Local Bot API Server, gestendo file fino a 2GB.

## Configurazione

1. Clona il repository.
2. Rinomina `.env.example` in `.env` e inserisci i tuoi dati reali:
   - `BOT_TOKEN`: Il token del tuo bot Telegram.
   - `ADMIN_USER_ID`: Il tuo ID utente Telegram per l'accesso admin.
   - `TELEGRAM_API_ID` e `TELEGRAM_API_HASH`: Le credenziali da [my.telegram.org](https://my.telegram.org) necessarie per caricare file di grandi dimensioni.

## Avvio
Puoi usare Docker per avviare l'intero ambiente facilmente:
```bash
docker-compose up -d
```
