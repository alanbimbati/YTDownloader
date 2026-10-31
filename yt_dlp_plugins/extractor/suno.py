# yt-dlp has no Suno extractor: found as a plugin because /app, which holds this package, is on sys.path.
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, float_or_none, traverse_obj

_SONG_PATH = r"/song/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"


class SunoIE(InfoExtractor):
    IE_NAME = "suno"
    _VALID_URL = r"https?://(?:www\.|app\.)?suno\.(?:com|ai)" + _SONG_PATH

    def _real_extract(self, url):
        song_id = self._match_id(url)
        clip = self._download_json(f"https://studio-api.prod.suno.com/api/clip/{song_id}", song_id)
        if clip.get("status") != "complete":
            raise ExtractorError("This song is not finished yet", expected=True)

        # The player's own audio (media_urls) is scrambled and audio_url points at a refusal: the video,
        # the cover art over the song, is the one plain file, and the audio is taken out of it.
        if not clip.get("video_url"):
            # Shown to the user as is.
            raise ExtractorError("Questa canzone Suno non è pubblica: Suno non ne dà un file scaricabile.", expected=True)
        formats = [{"url": clip["video_url"], "format_id": "mp4", "ext": "mp4", "vcodec": "h264", "acodec": "aac"}]

        return {
            "id": song_id,
            "title": clip.get("title") or song_id,
            "uploader": clip.get("display_name"),
            "uploader_id": clip.get("handle"),
            "artist": clip.get("display_name"),
            "genre": clip.get("display_tags"),
            "thumbnail": clip.get("image_large_url") or clip.get("image_url"),
            "duration": float_or_none(traverse_obj(clip, ("metadata", "duration"))),
            "webpage_url": f"https://suno.com/song/{song_id}",
            "formats": formats,
        }


class SunoShortIE(InfoExtractor):
    IE_NAME = "suno:short"
    _VALID_URL = r"https?://(?:www\.)?suno\.com/s/(?P<id>[\w-]+)"

    def _real_extract(self, url):
        short_id = self._match_id(url)
        final = self._request_webpage(url, short_id).url
        if not SunoIE.suitable(final):
            raise ExtractorError("This Suno link does not lead to a song", expected=True)
        return self.url_result(final, SunoIE)
