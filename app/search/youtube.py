import asyncio, yt_dlp
class YouTubeSearch:
    def __init__(self):
        self._ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'default_search': 'ytsearch'}
    async def search_and_resolve(self, query: str) -> dict:
        loop = asyncio.get_event_loop()
        def _extract():
            with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                if 'entries' in info: info = info['entries'][0]
                return {'title': info.get('title', 'Unknown'), 'duration': info.get('duration', 0), 'stream_url': info.get('url'), 'webpage_url': info.get('webpage_url')}
        return await loop.run_in_executor(None, _extract)
