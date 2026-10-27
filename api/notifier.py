"""Send alert text to Telegram and/or WhatsApp (Twilio).  Falls back to a console dry-run when nothing is configured.

Credentials come from environment variables (or a project-root .env, which is git-ignored):

  Telegram (recommended - free, 2 minutes):
      TELEGRAM_BOT_TOKEN   token from @BotFather  (/newbot)
      TELEGRAM_CHAT_ID     your chat id (message your bot once, then open
                           https://api.telegram.org/bot<TOKEN>/getUpdates and read message.chat.id)

  WhatsApp via Twilio (needs a Twilio account + WhatsApp sandbox/sender):
      TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
      TWILIO_WHATSAPP_FROM   e.g. whatsapp:+14155238886  (the Twilio sandbox number)
      WHATSAPP_TO            e.g. whatsapp:+9199XXXXXXXX (you must have joined the sandbox)
"""
import os
import sys

import requests

try:                                            # python-dotenv is already in requirements.txt
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:                               # noqa: BLE001 - optional
    pass

TG_LIMIT = 4000          # Telegram max is 4096 chars per message
WA_LIMIT = 1500          # Twilio WhatsApp body max is 1600 chars
DEFAULT_TELEGRAM_CHAT_ID = "776942278"   # owner's chat with @Algox_tradebot; TELEGRAM_CHAT_ID overrides
# Built-in default at the owner's request. NOTE: the repo is public, so this token is visible to anyone -
# if it is abused, revoke it in @BotFather (/revoke) and set the new one as TELEGRAM_BOT_TOKEN in .env (overrides this).
DEFAULT_TELEGRAM_BOT_TOKEN = "9070029080:AAFxM2AxPjwc6gm3xqmZOXNW9aRilgESqnA"


def _tg_chat():
    return os.environ.get("TELEGRAM_CHAT_ID") or DEFAULT_TELEGRAM_CHAT_ID


def _tg_token():
    return os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_TELEGRAM_BOT_TOKEN


def telegram_configured():
    return bool(_tg_token() and _tg_chat())


def whatsapp_configured():
    return all(os.environ.get(k) for k in
               ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "WHATSAPP_TO"))


def _chunks(text, limit):
    """Split on line boundaries so a message never exceeds `limit` characters."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                       # pathological single line
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        out.append(cur)
    return out or [""]


def send_telegram(text):
    token, chat = _tg_token(), _tg_chat()
    for part in _chunks(text, TG_LIMIT):
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                          json={"chat_id": chat, "text": part, "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError("telegram %s: %s" % (r.status_code, r.text[:200]))


def send_whatsapp(text):
    sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    for part in _chunks(text, WA_LIMIT):
        r = requests.post("https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid, auth=(sid, tok),
                          data={"From": os.environ["TWILIO_WHATSAPP_FROM"], "To": os.environ["WHATSAPP_TO"], "Body": part},
                          timeout=20)
        if r.status_code >= 300:
            raise RuntimeError("twilio %s: %s" % (r.status_code, r.text[:200]))


def notify(text, send=True):
    """Deliver `text` to every configured channel.  Returns the list of channels used ([] = dry-run).

    send=False forces a console dry-run even if credentials exist.
    """
    used, tried = [], []
    if send:
        for name, ok, fn in (("telegram", telegram_configured(), send_telegram),
                             ("whatsapp", whatsapp_configured(), send_whatsapp)):
            if not ok:
                continue
            tried.append(name)
            try:
                fn(text)
                used.append(name)
            except Exception as e:                       # noqa: BLE001
                print("!! %s send failed: %s" % (name, e), file=sys.stderr)
    if not used:
        why = (" - send FAILED on %s" % ",".join(tried) if tried else
               " - no TELEGRAM_*/TWILIO_* credentials set") if send else ""
        print("----- [DRY-RUN: not sent%s] -----\n%s\n-----" % (why, text))
    return used
