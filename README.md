# 📲 TransferSending

Sposta foto, video e documenti da **iPhone** e **Android** al computer (**Mac** o **Windows**), e dal computer agli altri telefoni.
Funziona sulla Wi-Fi di casa: niente cloud, niente account e nessuna app da installare sui telefoni.

- **Tanti file insieme**, anche migliaia e anche video da molti GB
- **Foto ordinate per data di scatto**: `Anno / Mese`, così passato e presente non si mischiano
- **Ogni file viene verificato** con la sua impronta (SHA-256) e nessun file viene mai sovrascritto
- **Niente doppioni**: se mandi due volte la stessa foto, non viene copiata di nuovo
- **Se la Wi-Fi cade, l'invio riprende** da dove si era fermato
- **Protetto**: connessione cifrata, codice segreto nel QR, solo rete locale, spegnimento automatico

## Scarica

Vai su **[Releases](../../releases/latest)** e scarica il file per il tuo computer:

| Computer | File |
|---|---|
| Windows 10/11 | `TransferSending-Windows.zip` |
| Mac con chip Apple (M1, M2, M3, M4…) | `TransferSending-Mac-AppleSilicon.zip` |
| Mac con processore Intel | `TransferSending-Mac-Intel.zip` |

> Non sai che Mac hai? Menu  → **Informazioni su questo Mac**: se c'è scritto "Chip Apple M…" prendi *AppleSilicon*, se c'è scritto "Intel" prendi *Intel*.

## Primo avvio

### Windows
1. Estrai lo zip e fai doppio clic su `TransferSending.exe`.
2. Se compare "Windows ha protetto il PC": **Ulteriori informazioni → Esegui comunque**.
3. Se Windows chiede il permesso di rete, scegli **Reti private** e consenti.

### Mac
1. Apri lo zip e trascina **TransferSending** nella cartella **Applicazioni**.
2. Aprilo. La prima volta il Mac lo blocca perché l'app non è firmata da Apple:
   - vai in **Impostazioni di Sistema → Privacy e sicurezza**, scorri in basso e premi **Apri comunque**;
   - (su macOS più vecchi basta **tasto destro → Apri**).
3. Se il Mac chiede di "accettare connessioni in entrata" o di "trovare dispositivi sulla rete locale", rispondi **Consenti**.

## Come si usa

1. Avvia TransferSending sul computer: si apre una pagina con un **codice QR**.
2. Sul telefono, collegato alla **stessa Wi-Fi**, inquadra il QR con la fotocamera.
3. La prima volta il telefono dice che la connessione "non è privata". È normale, perché il certificato è creato dal tuo computer, e il collegamento è comunque cifrato:
   - **iPhone**: *Mostra dettagli → visita questo sito web → Visita il sito web*
   - **Android (Chrome)**: *Avanzate → Procedi*
4. **Invia al computer**: scegli foto, video o file e premi **Invia**. Tieni lo schermo acceso fino al ✅.
5. **Ricevi dal computer**: da un altro telefono (per esempio l'Android) apri la scheda *Ricevi*, scegli un mese e premi **Scarica tutto (.zip)** oppure tocca un singolo file.

Quando hai finito premi **Spegni**. Comunque si spegne da solo dopo 30 minuti senza attività.

## Dove finiscono i file

Nella cartella **Documenti / Trasferimenti** del computer:

```
Trasferimenti/
└─ iPhone/
   ├─ 2019/
   │  └─ 05 - Maggio/
   │     ├─ 2019-05-12_14-32-05_IMG_0001.HEIC
   │     └─ 2019-05-12_14-32-05_IMG_0001.MOV   ← Live Photo, resta accanto alla sua foto
   ├─ 2026/
   │  └─ 11 - Novembre/
   ├─ Screenshot/
   ├─ Documenti/       ← PDF, Word, Excel…
   ├─ Senza data/      ← foto senza data di scatto
   └─ Altri file/
```

La data viene presa, in ordine:
1. dai dati interni della foto (EXIF) o del video, cioè la data reale dello scatto;
2. dal nome del file (es. `IMG-20190512-WA0003.jpg` di WhatsApp);
3. se non si trova una data, il file va in **Senza data**, invece di finire nel mese sbagliato.

## Consigli

- **iPhone**: per mantenere l'originale HEIC con tutti i dati, vai in *Impostazioni → Fotocamera → Formati* e scegli *Alta efficienza*. Se Safari converte in JPG, la data di scatto viene comunque mantenuta.
- Cancella le foto dal telefono **solo dopo** aver visto il messaggio ✅ *"Tutti i file sono arrivati e sono stati verificati"*.
- Usalo sulla **Wi-Fi di casa**, non su reti pubbliche (bar, hotel).

## Opzioni avanzate

Dal terminale (o da un collegamento) si possono passare queste opzioni:

```
TransferSending --cartella "D:\Foto"   # cartella di destinazione diversa
TransferSending --spegni-dopo 60       # minuti di inattività prima dello spegnimento (0 = mai)
TransferSending --porta 9000           # porta di rete (predefinita 8765)
```

## Per sviluppatori

```bash
pip install -r requirements.txt
python -m transfersending            # avvia
pip install pytest pillow && python -m pytest tests
pyinstaller TransferSending.spec     # crea l'app (.app su Mac, .exe su Windows)
```

Le app vengono create automaticamente da GitHub Actions. Per pubblicare una nuova versione: `git tag v0.1.1 && git push --tags`.

### Come funziona la sicurezza
- **HTTPS** con certificato generato sul tuo computer (`cert.pem` nella cartella dati dell'app).
- **Codice segreto** casuale, nuovo a ogni avvio, contenuto nel QR. Senza il codice le API rispondono `401`.
- Il server accetta solo **indirizzi della rete locale**, mentre la pagina col QR è raggiungibile solo dal computer stesso.
- Ogni pezzo da 8 MB viene verificato con **SHA-256**. Il file completo viene verificato e salvato su disco prima di essere spostato nella cartella finale.
- I file vengono **solo copiati**: nulla viene cancellato dal telefono e nulla viene sovrascritto sul computer.
