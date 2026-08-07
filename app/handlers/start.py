from pyrogram.types import Message
async def start_handler(client, message: Message):
    await message.reply_text('✨ **Shade Music Bot** `v0.6.0-Phase1`\n\n👋 Status: **Operational & Ready**', quote=True)
