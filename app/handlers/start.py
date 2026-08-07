from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
async def start_handler(client, message: Message):
    text = '✨ **Shade Music Bot** `v0.6.0-Phase1`\n\n👋 Welcome! Main High-Performance Voice Chat Audio Bot hoon.\n⚡ Status: **Operational & Ready**'
    await message.reply_text(text, quote=True)
