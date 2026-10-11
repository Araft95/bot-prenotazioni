"""
Bot Telegram per prenotare appuntamenti (barbiere, estetista, studio, ecc.).

Cosa sa fare:
- menu a pulsanti
- prenotazione guidata: servizio -> giorno -> orario -> conferma
- elenco e cancellazione dei propri appuntamenti
- avvisa il titolare a ogni nuova prenotazione o cancellazione
- comando /oggi per il titolare, con gli appuntamenti del giorno
"""

import logging
import os
import sqlite3
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# CONFIGURAZIONE: cambia questi valori per adattare il bot a un'attività reale
# ---------------------------------------------------------------------------

NOME_ATTIVITA = "Barberia Da Mario"
INDIRIZZO = "Via Roma 1, Milano"
TELEFONO = "+39 02 1234567"

# chiave interna: (nome mostrato, prezzo in euro)
SERVIZI = {
    "taglio": ("✂️ Taglio", 20),
    "barba": ("🧔 Barba", 12),
    "combo": ("💈 Taglio + Barba", 28),
    "bimbo": ("👦 Taglio bimbo", 15),
}

# Orari di apertura per giorno della settimana (0 = lunedì ... 6 = domenica).
# I giorni che non compaiono sono di chiusura.
ORARI = {
    1: [("09:00", "13:00"), ("15:00", "19:00")],  # martedì
    2: [("09:00", "13:00"), ("15:00", "19:00")],  # mercoledì
    3: [("09:00", "13:00"), ("15:00", "19:00")],  # giovedì
    4: [("09:00", "13:00"), ("15:00", "19:00")],  # venerdì
    5: [("09:00", "18:00")],                      # sabato, orario continuato
}

DURATA_SLOT = 30         # minuti per ogni appuntamento
GIORNI_PRENOTABILI = 14  # quanti giorni in avanti si può prenotare
FUSO = ZoneInfo("Europe/Rome")

GIORNI_SETTIMANA = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]

# ---------------------------------------------------------------------------
# Avvio: token, titolare e database
# ---------------------------------------------------------------------------

load_dotenv()  # legge il file .env
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID") or 0)  # 0 = nessun titolare configurato

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # nasconde i log di ogni richiesta
log = logging.getLogger(__name__)

conn = sqlite3.connect(os.getenv("DB_PATH", "prenotazioni.db"))
conn.row_factory = sqlite3.Row  # permette di leggere le colonne per nome
conn.execute("""
    CREATE TABLE IF NOT EXISTS appuntamenti (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        servizio TEXT NOT NULL,
        giorno TEXT NOT NULL,  -- AAAA-MM-GG
        ora TEXT NOT NULL,     -- HH:MM
        UNIQUE (giorno, ora)   -- impedisce due prenotazioni sullo stesso orario
    )
""")
conn.commit()

# ---------------------------------------------------------------------------
# Funzioni sul database e sugli orari
# ---------------------------------------------------------------------------


def adesso():
    return datetime.now(FUSO)


def slot_liberi(giorno):
    """Restituisce gli orari ancora prenotabili in un giorno, es. ["09:00", "09:30"]."""
    occupati = {r["ora"] for r in conn.execute(
        "SELECT ora FROM appuntamenti WHERE giorno = ?", (giorno.isoformat(),))}
    liberi = []
    for inizio, fine in ORARI.get(giorno.weekday(), []):
        t = datetime.combine(giorno, time.fromisoformat(inizio), tzinfo=FUSO)
        t_fine = datetime.combine(giorno, time.fromisoformat(fine), tzinfo=FUSO)
        while t + timedelta(minutes=DURATA_SLOT) <= t_fine:
            ora = t.strftime("%H:%M")
            if t > adesso() and ora not in occupati:
                liberi.append(ora)
            t += timedelta(minutes=DURATA_SLOT)
    return liberi


def giorni_disponibili():
    """I prossimi giorni di apertura che hanno almeno un orario libero."""
    oggi = adesso().date()
    giorni = [oggi + timedelta(days=i) for i in range(GIORNI_PRENOTABILI)]
    return [g for g in giorni if slot_liberi(g)]


def salva_appuntamento(utente, servizio, giorno, ora):
    """Salva la prenotazione. Restituisce None se l'orario è già stato preso."""
    try:
        cur = conn.execute(
            "INSERT INTO appuntamenti (user_id, nome, servizio, giorno, ora) VALUES (?, ?, ?, ?, ?)",
            (utente.id, utente.full_name, servizio, giorno, ora))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def appuntamenti_futuri(user_id):
    now = adesso()
    oggi, ora = now.date().isoformat(), now.strftime("%H:%M")
    return conn.execute(
        """SELECT * FROM appuntamenti
           WHERE user_id = ? AND (giorno > ? OR (giorno = ? AND ora > ?))
           ORDER BY giorno, ora""",
        (user_id, oggi, oggi, ora)).fetchall()


def cancella_appuntamento(app_id, user_id):
    """Cancella solo se l'appuntamento appartiene a chi lo chiede."""
    riga = conn.execute(
        "SELECT * FROM appuntamenti WHERE id = ? AND user_id = ?", (app_id, user_id)).fetchone()
    if riga:
        conn.execute("DELETE FROM appuntamenti WHERE id = ?", (app_id,))
        conn.commit()
    return riga


def formatta_giorno(giorno):
    if isinstance(giorno, str):
        giorno = date.fromisoformat(giorno)
    return f"{GIORNI_SETTIMANA[giorno.weekday()]} {giorno.day:02d}/{giorno.month:02d}"


def descrivi(riga):
    """Una riga leggibile per un appuntamento, es. "Mar 14/10 alle 10:30 · ✂️ Taglio"."""
    return f"{formatta_giorno(riga['giorno'])} alle {riga['ora']} · {SERVIZI[riga['servizio']][0]}"


async def avvisa_titolare(context, testo):
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(ADMIN_ID, testo, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        # succede se il titolare non ha mai scritto /start al bot
        log.warning("Impossibile avvisare il titolare: %s", e)


# ---------------------------------------------------------------------------
# Tastiere (i pulsanti sotto i messaggi)
# ---------------------------------------------------------------------------

PULSANTE_MENU = InlineKeyboardButton("⬅️ Menu", callback_data="menu")


def tastiera_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Prenota un appuntamento", callback_data="prenota")],
        [InlineKeyboardButton("📋 I miei appuntamenti", callback_data="miei")],
        [InlineKeyboardButton("💈 Servizi e prezzi", callback_data="servizi"),
         InlineKeyboardButton("📍 Dove siamo", callback_data="info")],
    ])


def in_righe(pulsanti, per_riga):
    """Divide una lista di pulsanti in righe da `per_riga` elementi."""
    return [pulsanti[i:i + per_riga] for i in range(0, len(pulsanti), per_riga)]


# ---------------------------------------------------------------------------
# Comandi
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome = escape(update.effective_user.first_name)
    await update.message.reply_text(
        f"Ciao {nome}! 👋\nBenvenuto da <b>{escape(NOME_ATTIVITA)}</b>.\nCosa vuoi fare?",
        reply_markup=tastiera_menu(), parse_mode=ParseMode.HTML)


async def mio_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Serve al titolare per scoprire il proprio ID da mettere nel file .env."""
    await update.message.reply_text(f"Il tuo ID Telegram è: {update.effective_user.id}")


async def oggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Solo per il titolare: elenco degli appuntamenti di oggi."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Questo comando è riservato al titolare.")
        return
    righe = conn.execute(
        "SELECT * FROM appuntamenti WHERE giorno = ? ORDER BY ora",
        (adesso().date().isoformat(),)).fetchall()
    if not righe:
        await update.message.reply_text("Nessun appuntamento oggi. ☕")
        return
    elenco = "\n".join(
        f"<b>{r['ora']}</b> · {SERVIZI[r['servizio']][0]} · "
        f"<a href=\"tg://user?id={r['user_id']}\">{escape(r['nome'])}</a>"
        for r in righe)
    await update.message.reply_text(
        f"📅 <b>Appuntamenti di oggi</b>\n\n{elenco}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Pulsanti: ogni pulsante invia un "callback_data" che decide cosa succede.
# I dati della prenotazione viaggiano dentro il callback_data, separati da "|",
# ad esempio "ora|taglio|2026-10-14|10:30".
# ---------------------------------------------------------------------------


async def mostra_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Cosa vuoi fare?", reply_markup=tastiera_menu())


async def mostra_servizi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    elenco = "\n".join(f"{nome} — {prezzo} €" for nome, prezzo in SERVIZI.values())
    await update.callback_query.edit_message_text(
        f"💈 <b>Servizi e prezzi</b>\n\n{elenco}",
        reply_markup=InlineKeyboardMarkup([[PULSANTE_MENU]]), parse_mode=ParseMode.HTML)


async def mostra_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    orari = "\n".join(
        f"{GIORNI_SETTIMANA[g]}: " + (", ".join(f"{a}–{b}" for a, b in ORARI[g]) if g in ORARI else "chiuso")
        for g in range(7))
    await update.callback_query.edit_message_text(
        f"📍 <b>{escape(NOME_ATTIVITA)}</b>\n{escape(INDIRIZZO)}\n📞 {escape(TELEFONO)}\n\n"
        f"🕒 <b>Orari</b>\n{orari}",
        reply_markup=InlineKeyboardMarkup([[PULSANTE_MENU]]), parse_mode=ParseMode.HTML)


async def scegli_servizio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    pulsanti = [[InlineKeyboardButton(f"{nome} — {prezzo} €", callback_data=f"srv|{chiave}")]
                for chiave, (nome, prezzo) in SERVIZI.items()]
    pulsanti.append([PULSANTE_MENU])
    await update.callback_query.edit_message_text(
        "Quale servizio desideri?", reply_markup=InlineKeyboardMarkup(pulsanti))


async def scegli_giorno(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, servizio = query.data.split("|")
    giorni = giorni_disponibili()
    if not giorni:
        await query.edit_message_text(
            "😔 Non ci sono orari liberi nei prossimi giorni. Riprova più avanti!",
            reply_markup=InlineKeyboardMarkup([[PULSANTE_MENU]]))
        return
    pulsanti = [InlineKeyboardButton(formatta_giorno(g), callback_data=f"day|{servizio}|{g.isoformat()}")
                for g in giorni]
    righe = in_righe(pulsanti, 3)
    righe.append([InlineKeyboardButton("⬅️ Indietro", callback_data="prenota")])
    await query.edit_message_text(
        f"{SERVIZI[servizio][0]}\n\nScegli il giorno:", reply_markup=InlineKeyboardMarkup(righe))


async def scegli_ora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, servizio, giorno = query.data.split("|")
    liberi = slot_liberi(date.fromisoformat(giorno))
    pulsanti = [InlineKeyboardButton(ora, callback_data=f"ora|{servizio}|{giorno}|{ora}") for ora in liberi]
    righe = in_righe(pulsanti, 4)
    righe.append([InlineKeyboardButton("⬅️ Indietro", callback_data=f"srv|{servizio}")])
    testo = f"{SERVIZI[servizio][0]} · {formatta_giorno(giorno)}\n\n"
    testo += "Scegli l'orario:" if liberi else "Questo giorno si è appena riempito, scegline un altro."
    await query.edit_message_text(testo, reply_markup=InlineKeyboardMarkup(righe))


async def chiedi_conferma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, servizio, giorno, ora = query.data.split("|", 3)
    nome, prezzo = SERVIZI[servizio]
    await query.edit_message_text(
        f"Riepilogo:\n\n{nome}\n📅 {formatta_giorno(giorno)} alle {ora}\n💶 {prezzo} €\n\nConfermi?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Conferma", callback_data=f"ok|{servizio}|{giorno}|{ora}")],
            [InlineKeyboardButton("⬅️ Cambia orario", callback_data=f"day|{servizio}|{giorno}"),
             InlineKeyboardButton("❌ Annulla", callback_data="menu")],
        ]))


async def conferma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, servizio, giorno, ora = query.data.split("|", 3)

    # Ricontrolla: il messaggio potrebbe essere vecchio o l'orario già preso da altri.
    if servizio not in SERVIZI or ora not in slot_liberi(date.fromisoformat(giorno)) \
            or salva_appuntamento(update.effective_user, servizio, giorno, ora) is None:
        await query.answer("Questo orario non è più disponibile 😕", show_alert=True)
        await query.edit_message_text(
            "Scegli un altro orario:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Vedi altri orari", callback_data=f"srv|{servizio}")],
                [PULSANTE_MENU]]))
        return

    await query.answer("Prenotato! ✅")
    riepilogo = f"{SERVIZI[servizio][0]} · {formatta_giorno(giorno)} alle {ora}"
    await query.edit_message_text(
        f"✅ <b>Appuntamento confermato!</b>\n\n{riepilogo}\n\n"
        f"Ti aspettiamo in {escape(INDIRIZZO)}.\nPuoi cancellarlo da «I miei appuntamenti».",
        reply_markup=InlineKeyboardMarkup([[PULSANTE_MENU]]), parse_mode=ParseMode.HTML)

    utente = update.effective_user
    await avvisa_titolare(
        context,
        f"🆕 <b>Nuova prenotazione</b>\n{riepilogo}\n"
        f"Cliente: <a href=\"tg://user?id={utente.id}\">{escape(utente.full_name)}</a>")


async def miei_appuntamenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await mostra_lista_miei(update.callback_query, update.effective_user.id)


async def mostra_lista_miei(query, user_id):
    righe = appuntamenti_futuri(user_id)
    if not righe:
        await query.edit_message_text(
            "Non hai appuntamenti in programma.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Prenota ora", callback_data="prenota")], [PULSANTE_MENU]]))
        return
    elenco = "\n".join(f"• {descrivi(r)}" for r in righe)
    pulsanti = [[InlineKeyboardButton(f"❌ Cancella {formatta_giorno(r['giorno'])} {r['ora']}",
                                      callback_data=f"del|{r['id']}")] for r in righe]
    pulsanti.append([PULSANTE_MENU])
    await query.edit_message_text(
        f"📋 <b>I tuoi appuntamenti</b>\n\n{elenco}",
        reply_markup=InlineKeyboardMarkup(pulsanti), parse_mode=ParseMode.HTML)


async def cancella(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, app_id = query.data.split("|")
    riga = cancella_appuntamento(int(app_id), update.effective_user.id)
    if not riga:
        await query.answer("Appuntamento già cancellato.")
    else:
        await query.answer("Appuntamento cancellato.")
        utente = update.effective_user
        await avvisa_titolare(
            context,
            f"🗑 <b>Prenotazione cancellata</b>\n{descrivi(riga)}\n"
            f"Cliente: <a href=\"tg://user?id={utente.id}\">{escape(utente.full_name)}</a>")
    await mostra_lista_miei(query, update.effective_user.id)  # mostra la lista aggiornata


# ---------------------------------------------------------------------------
# Avvio del bot
# ---------------------------------------------------------------------------


def crea_app(token):
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mioid", mio_id))
    app.add_handler(CommandHandler("oggi", oggi))
    app.add_handler(CallbackQueryHandler(mostra_menu, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(mostra_servizi, pattern=r"^servizi$"))
    app.add_handler(CallbackQueryHandler(mostra_info, pattern=r"^info$"))
    app.add_handler(CallbackQueryHandler(scegli_servizio, pattern=r"^prenota$"))
    app.add_handler(CallbackQueryHandler(scegli_giorno, pattern=r"^srv\|"))
    app.add_handler(CallbackQueryHandler(scegli_ora, pattern=r"^day\|"))
    app.add_handler(CallbackQueryHandler(chiedi_conferma, pattern=r"^ora\|"))
    app.add_handler(CallbackQueryHandler(conferma, pattern=r"^ok\|"))
    app.add_handler(CallbackQueryHandler(miei_appuntamenti, pattern=r"^miei$"))
    app.add_handler(CallbackQueryHandler(cancella, pattern=r"^del\|"))
    return app


def main():
    if not TOKEN:
        raise SystemExit("Manca TELEGRAM_TOKEN: copia .env.example in .env e inserisci il token di BotFather.")
    log.info("Bot avviato. Premi Ctrl+C per fermarlo.")
    crea_app(TOKEN).run_polling()


if __name__ == "__main__":
    main()
