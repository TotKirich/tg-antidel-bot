
import json
import telebot

CONFIG_FILE = "config.json"

def save_chat_id(chat_id):
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config["admin_chat_id"] = chat_id
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def clear_chat_id():
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config.pop("admin_chat_id", None)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

config = json.load(open(CONFIG_FILE))
bot = telebot.TeleBot(config["bot_token"])
admin_username = config["admin_username"]

@bot.message_handler(commands=["start"])
def start(msg):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✅ Включить уведомления", "🚫 Выключить уведомления")
    bot.send_message(msg.chat.id, "Выберите действие:", reply_markup=markup)

@bot.message_handler(func=lambda m: True)
def handle(msg):
    if msg.text.startswith("✅"):
        if msg.from_user.username == admin_username:
            save_chat_id(msg.chat.id)
            bot.send_message(msg.chat.id, "✅ Уведомления включены.")
        else:
            bot.send_message(msg.chat.id, "⛔ У вас нет доступа.")
    elif msg.text.startswith("🚫"):
        if msg.from_user.username == admin_username:
            clear_chat_id()
            bot.send_message(msg.chat.id, "🚫 Уведомления отключены.")
        else:
            bot.send_message(msg.chat.id, "⛔ У вас нет доступа.")

bot.infinity_polling()
