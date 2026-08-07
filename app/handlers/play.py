from pyrogram.types import Message
async def play_handler(client, message: Message, controller):
    if len(message.command) < 2:
        return await message.reply_text('❌ **Usage:** `/play <song name or URL>`', quote=True)
    query = message.text.split(maxsplit=1)[1]
    status_msg = await message.reply_text('🔎 **Searching & Resolving Stream...**', quote=True)
    try:
        response = await controller.play(message.chat.id, query, message.from_user.first_name if message.from_user else 'User')
        await status_msg.edit_text(response, disable_web_page_preview=True)
    except Exception as e:
        await status_msg.edit_text(f'❌ **Playback Failed:** `{str(e)}`')
