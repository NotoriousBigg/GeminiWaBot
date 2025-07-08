import json
from datetime import datetime, time
import pytz
import asyncio
from google.generativeai import ChatSession
from neonize.aioze.client import NewAClient
from neonize.utils import build_jid, log
from neonize.events import event, ConnectedEv, MessageEv, PairStatusEv
from dotenv import load_dotenv
import google.generativeai as genai
from redis import Redis
import logging
import sys
import os

from utils import my_collections

tz = pytz.timezone("Africa/Nairobi")


def is_night_time():
    """Check if the current time is between 8 PM and 6 AM"""
    now = datetime.now(tz).time()
    night_start = time(20, 0)  # 8 PM
    night_end = time(6, 0)  # 6 AM
    return now >= night_start or now <= night_end

sys.path.insert(0, os.getcwd())

load_dotenv()

DB_PATH = os.getenv("DATABASE_PATH", "/var/lib/mybot/db.sqlite3")
REDIS_URI = os.getenv("REDIS_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUDO = [num.strip() for num in os.getenv("SUDO", "").split(",") if num.strip()]
PREFIX = os.getenv("PREFIX")
MODE = os.getenv("MODE")

genai.configure(api_key=GEMINI_API_KEY)

gemini_model = None

# change this to anything you want.
SYSTEM_PROMPT = """
You are Kresswell's personal AI assistant. Your job is to act on Kresswell’s behalf when he’s not available. You're friendly, approachable, and helpful — like a trusted digital version of him.

You don’t pretend to be Kresswell, but you represent him. Speak casually and naturally, like someone who’s cool, thoughtful, and respectful. You can respond to general questions, hold casual conversations, and help out where you can — even if it’s just chatting.

If you don’t know something or it’s too personal, simply say “Kresswell will get back to you on that.” You’re here to keep conversations flowing, assist when possible, and make sure people feel acknowledged, not ignored.

You don’t need to stick to one topic — just keep things easy-going, clear, and human. You can joke, give light advice, or just vibe if that’s what the conversation calls for.

Your tone: relaxed, polite, a bit playful if the moment allows. Always real, never robotic. Keep your messages brief and short
Also, Include necessary emojies to your messages to make them lively. You can mix swahili and english in your messages.
"""


Prime = NewAClient(DB_PATH)
redisc = Redis.from_url(REDIS_URI)


log.setLevel(logging.INFO)

def interrupted(*_):
    event.set()

async def init_gemini():
    global gemini_model
    gemini_model = genai.GenerativeModel(
        model_name="gemini-1.5-flash-002",
        system_instruction=SYSTEM_PROMPT
    )

# pm chatbot states on redis
CHATBOT_KEY = "chatbot:active"
CHATBOT_OVERRIDE_KEY = "chatbot:override"

if redisc.get(CHATBOT_KEY) is None:
    redisc.set(CHATBOT_KEY, "1")

def get_override() -> str | None:
    val = redisc.get(CHATBOT_OVERRIDE_KEY)
    return val.decode() if val else None          # "on" / "off" / None

def set_override(state: str | None) -> None:
    """state = 'on', 'off', or None to clear"""
    if state is None:
        redisc.delete(CHATBOT_OVERRIDE_KEY)
    else:
        redisc.set(CHATBOT_OVERRIDE_KEY, state)

def chatbot_is_active() -> bool:
    override = get_override()
    if override == "on":
        return True
    if override == "off":
        return False
    return is_night_time()

# returns if current user is sudo or not
def is_sudo(number: str) -> bool:
    return number in SUDO


@Prime.event(ConnectedEv)
async def on_connected(_: NewAClient, __: ConnectedEv):
    log.info("Connected successfully...")

async def get_user_chat(user_id) -> ChatSession:
    """Get or create chat session for user"""
    chat_data = redisc.get(f"chat:{user_id}")

    if chat_data:
        history = json.loads(chat_data)
        return gemini_model.start_chat(history=history)

    # Create a new chat
    return gemini_model.start_chat()

async def update_user_history(user_id, query: str, response_text: str):
    """Save user interaction to Redis"""
    try:
        history = [
            {"role": "user", "parts": [query]},
            {"role": "model", "parts": [response_text]}
        ]
        current_data = redisc.get(f"chat:{user_id}")
        current_history = json.loads(current_data) if current_data else []

        updated_history = current_history + history
        if len(updated_history) > 100:
            updated_history = updated_history[-100:]

        redisc.set(f"chat:{user_id}", json.dumps(updated_history))
        redisc.expire(f"chat:{user_id}", 86400 * 7)
    except Exception as e:
        log.error(f"Failed to save chat history: {e}")

async def optimus_reply(user_id, message):
    chat = await get_user_chat(user_id)
    try:
        response = chat.send_message(message)
        return response.text.strip()
    except Exception as e:
        log.error(f"Gemini error for user {user_id}: {e}")
        return "❌ I ran into an error processing that."

@Prime.event(MessageEv)
async def on_message(cl: NewAClient, message: MessageEv):
    text = message.Message.conversation or message.Message.extendedTextMessage.text
    chat = message.Info.MessageSource.Chat
    message_id = message.Info.ID
    is_group = message.Info.MessageSource.IsGroup
    gc = message.Info.MessageSource.Chat.User
    mentioned = message.Message.extendedTextMessage.contextInfo.mentionedJID
    user_id = message.Info.MessageSource.Sender.User
    pushname = getattr(message.Info, "Pushname", "Bot User")

    if is_group:
        return None

    if not text:
        return None

    command = text.split(" ")[0].strip().lower()
    if command.startswith(PREFIX):
        command_name = command[1:]
        if (MODE == 'PUBLIC' or MODE == 'PRIVATE') and is_sudo(user_id):
            match command_name:
                case "chatbot":
                    if not is_sudo(str(user_id)):
                        return await cl.reply_message(
                            "🚫 You’re not allowed to change the bot state.",
                            quoted=message
                        )

                    parts = text.split()
                    if len(parts) < 2 or parts[1].lower() not in ("on", "off", "auto"):
                        return await cl.reply_message(
                            f"Usage: {PREFIX}chatbot on|off|auto",
                            quoted=message
                        )

                    action = parts[1].lower()
                    match action:
                        case "on":
                            set_override("on")
                            msg = "✅ Chatbot forced *ON* — it will answer even during the day."
                        case "off":
                            set_override("off")
                            msg = "✅ Chatbot forced *OFF* — it will stay silent until you enable it."
                        case "auto":
                            set_override(None)
                            msg = "🔄 Chatbot returned to *auto mode* (night‑only)."

                    await cl.reply_message(msg, quoted=message)
                    return None

    # user dialogue when chatbot is active, returns None when chatbot is off
    if (chatbot_is_active() and MODE=="PUBLIC") or is_sudo(user_id):
        chat = await get_user_chat(user_id)
        response = chat.send_message(text)
        reply_text = response.text.strip()
        await cl.reply_message(
            reply_text,
            quoted=message
        )

        asyncio.create_task(update_user_history(user_id, text, reply_text))


    return None


@Prime.event(PairStatusEv)
async def PairStatusMessage(_: NewAClient, message: PairStatusEv):
    log.info(f"logged as {message.ID.User}")

async def pair_phone():
    if await Prime.is_connected:
        return
    phone = int(input("Enter your phone number without the + sign: "))

    await Prime.PairPhone(str(phone), show_push_notification=True)

async def start_bot():
    asyncio.create_task(init_gemini())
    await pair_phone()
    await Prime.connect()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_bot())
    finally:
        loop.close()