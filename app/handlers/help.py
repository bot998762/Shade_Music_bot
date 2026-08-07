from pyrogram.types import Message
from app.handlers.registry import registry
async def help_handler(client, message: Message):
    commands = registry.get_commands()
    text = '📖 **Available Commands:**\n\n'
    for name, info in commands.items(): text += f'• `/{name}` — {info["description"]}\n'
    await message.reply_text(text, quote=True)
