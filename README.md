# AURA — WhatsApp AI receptionist

AURA answers a small business's WhatsApp in Roman Urdu and English: it books, reschedules and cancels
appointments in Google Calendar, quotes prices, understands voice notes, sends reminders and hands
customers over to the owner when needed. Owners manage everything in a web dashboard.

| Part | What it does |
|---|---|
| WhatsApp server (`main.py`) | Receives WhatsApp messages (WaAPI webhook) and replies |
| Dashboard (`Dashboard.py`) | Bookings, conversations, escalations, services, settings |
| Intent model | Fine-tuned XLM-RoBERTa: what the customer wants |
| Entity model | Fine-tuned XLM-RoBERTa: service, date, time, name |
| Gemini / Groq Whisper | Backup understanding / voice notes |
| Redis + SQLite | Conversation memory / businesses, bookings, chats, settings |

## Run with Docker (recommended)

You need **Docker Desktop** (Windows/Mac) or Docker Engine (Linux), and about 8 GB of RAM.
You don't need Python or the source code.

1. Put these files in one folder:
   - `docker-compose.yml`
   - `.env`, a copy of `.env.example` with your own `ADMIN_PASSWORD`
2. Start it:
   ```
   docker compose up -d
   ```
   - The first start downloads the AURA image from GitHub (`ghcr.io/jazib-umer/aura`, about 2–2.5 GB).
     Later starts download nothing.
   - Then wait about 1–2 minutes while the models load.
   - To use an image you built yourself instead (`docker build -t aura:latest .`), set
     `AURA_IMAGE=aura:latest` in `.env`.
3. Open the dashboard at **http://localhost:8501** and log in as `admin` with your `ADMIN_PASSWORD`.
4. In **Admin → Platform settings**, set up the shared accounts. Each section has a check button.
   - WaAPI token
   - Gemini key
   - Groq key
   - Google service-account key (.json)
   - Email server
   - Your own admin password
5. In **Admin → Businesses**, add a business with its WaAPI instance ID, Google Calendar and owner contacts.
   - Share that calendar with the service-account email shown in Platform settings.

WhatsApp works on its own after that. AURA starts a free Cloudflare tunnel for a public address and
registers it with every business's WaAPI instance. You can see the address under
**Platform settings → WhatsApp**.

Free tunnels are temporary: Cloudflare deletes one after a long network drop, for example when the laptop
sleeps or changes Wi-Fi. AURA checks the tunnel every 30 seconds. After 90 seconds without a connection, it
starts a new tunnel and re-registers every webhook. That takes no action from you, but messages sent during
those ~2 minutes may be lost.

### Everyday commands

| Task | Command |
|---|---|
| Start / stop | `docker compose up -d` / `docker compose down` |
| Live logs | `docker compose logs -f aura` |
| Status | `docker compose ps` |
| Chat with AURA in the terminal | `docker compose exec aura python chat_cli.py` |
| Fake a WhatsApp message | `docker compose exec aura python tests/send_test_webhook.py "kal 3 baje haircut"` |
| Forgot the admin password | `docker compose exec aura python -m app.services.config reset-admin` |
Data lives in the Docker volume `aura_aura-data`. It survives `docker compose down` and restarts.
`docker compose down -v` **deletes it**.

### Running AURA on another computer with your own accounts

Your accounts never go into the image or onto GitHub. Keep a private "AURA keys" copy, for example on
your Google Drive, with:
- `.env`, your settings including the account keys (see `.env.example`)
- `secrets/credentials.json`, your Google service-account key

On the new computer:

1. Run `git clone https://github.com/JAZIB-UMER/AURA-WhatsApp-Receptionist.git` and
   `cd AURA-WhatsApp-Receptionist`.
2. Copy your private `.env` and `secrets/credentials.json` into this folder.
3. If you have `aura.tar` (the images as one file, e.g. from Google Drive), put it in this folder and
   load it. Then nothing needs to be downloaded, not even Redis:
   ```
   docker load -i aura.tar
   ```
4. Run `docker compose up -d`.

On the first start, AURA imports the keys from `.env` and `secrets/credentials.json` into Platform
settings, so you don't need to type anything. The database starts empty: add the businesses in the
dashboard.

### Sharing the image

```
docker build -t ghcr.io/jazib-umer/aura:latest .
docker save ghcr.io/jazib-umer/aura:latest redis:7.4-alpine -o aura.tar   # one file for Google Drive / USB
docker push ghcr.io/jazib-umer/aura:latest                                # or publish on GitHub
```

The image contains the code and the trained models, and **no secrets or data**.
- Keys come from your private `.env` / `secrets/`, or are entered in the dashboard.
- The database is created fresh on each computer.

Never share your `.env`, `secrets/`, `credentials.json` or `aura.db`: they contain your keys.

## Run without Docker (development)

```
python -m venv aura_env && aura_env\Scripts\activate
pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
copy .env.example .env               # then set ADMIN_PASSWORD
```

Start Redis (for example `docker run -d -p 6379:6379 redis:7.4-alpine`), then run:

```
uvicorn main:app --port 8000         # WhatsApp server
streamlit run Dashboard.py           # dashboard, http://localhost:8501
```

To connect WhatsApp without Docker:
1. Install cloudflared: `winget install --id Cloudflare.cloudflared`
2. Add `PUBLIC_TUNNEL=cloudflare` to `.env`.

AURA then runs and repairs the tunnel itself, as in Docker.

The trained models from Colab go in `models/intent_model_v2/` and `models/entity_model/`. They are too
large for git. AURA runs half-precision copies made from them. They use half the disk and give the same
predictions on every test set:

```
python training/convert_fp16.py models/intent_model_v2 models/intent_model_v2_fp16
python training/convert_fp16.py models/entity_model   models/entity_model_fp16
```

Set `MODEL_DTYPE=float32` in `.env` to run in the original precision.

## Tests

```
python tests/simulate_conversations.py   # full conversations (booking, reschedule, cancel, escalation, ...)
python tests/test_dashboard.py           # dashboard pages (Streamlit AppTest)
python tests/test_validation.py          # dashboard input checks
python tests/test_voice.py               # voice notes
python tests/test_tunnel.py              # automatic WhatsApp webhook
python tests/test_datetime_parser.py     # Roman Urdu / English dates and times
python training/evaluate_model.py models/intent_model_v2_fp16
python training/evaluate_ner.py models/entity_model_fp16
```

## Configuration at a glance

| Where | What |
|---|---|
| `.env` | First admin password, ports, image name (and paths when running without Docker) |
| Dashboard → Platform settings (admin) | WaAPI token, Gemini, Groq, Google service account, email server, admin password |
| Dashboard → each business | WhatsApp instance, calendar, owner phone and email, hours, services, questions |
