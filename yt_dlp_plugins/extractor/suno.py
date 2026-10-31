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

        formats = [
            # The page's own audio_url points at a refusal; media_urls is what the player streams.
            {"url": m["url"], "format_id": m.get("content_type") or "audio", "ext": "m4a",
             "vcodec": "none", "acodec": "opus" if "opus" in (m.get("content_type") or "") else None}
            for m in clip.get("media_urls") or [] if m.get("url")
        ]
        if clip.get("video_url"):
            # The video is the cover art over the song: worth it when the video and audio are both asked for.
            formats.append({"url": clip["video_url"], "format_id": "video", "ext": "mp4",
                            "vcodec": "h264", "acodec": "aac"})
        if not formats:
            raise ExtractorError("No audio is available for this song", expected=True)

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
