import asyncio
import inspect
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi.concurrency import run_in_threadpool

from app.chain.media import MediaChain
from app.chain.recommend import RecommendChain
from app.core.context import MediaInfo
from app.log import logger
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils


class MultiRatingsRecommend(_PluginBase):
    plugin_name = "全平台低分保护"
    plugin_desc = "统一接管推荐、搜索、识别结果的评分，显示 TMDB / IMDb / 豆瓣 / Bangumi 中的最低分。"
    plugin_icon = "mdi-shield-half-full"
    plugin_version = "0.3.0"
    plugin_author = "jun9100"
    author_url = "https://github.com/jun9100"
    plugin_config_prefix = "multiratingsrecommend_"
    plugin_order = 70

    _SYNC_MEDIA_ITEM_METHODS = (
        "recognize_media",
    )
    _ASYNC_MEDIA_ITEM_METHODS = (
        "async_recognize_media",
    )
    _SYNC_MEDIA_LIST_METHODS = (
        "search_medias",
        "tmdb_trending",
        "tmdb_discover",
        "tmdb_collection",
        "tmdb_movie_similar",
        "tmdb_tv_similar",
        "tmdb_movie_recommend",
        "tmdb_tv_recommend",
        "tmdb_person_credits",
        "movie_showing",
        "douban_discover",
        "movie_top250",
        "tv_weekly_chinese",
        "tv_weekly_global",
        "tv_animation",
        "movie_hot",
        "tv_hot",
        "douban_movie_recommend",
        "douban_tv_recommend",
        "douban_person_credits",
        "bangumi_calendar",
        "bangumi_discover",
        "bangumi_recommend",
        "bangumi_person_credits",
    )
    _ASYNC_MEDIA_LIST_METHODS = (
        "async_search_medias",
        "async_tmdb_trending",
        "async_tmdb_discover",
        "async_tmdb_collection",
        "async_tmdb_movie_similar",
        "async_tmdb_tv_similar",
        "async_tmdb_movie_recommend",
        "async_tmdb_tv_recommend",
        "async_tmdb_person_credits",
        "async_movie_showing",
        "async_douban_discover",
        "async_movie_top250",
        "async_tv_weekly_chinese",
        "async_tv_weekly_global",
        "async_tv_animation",
        "async_movie_hot",
        "async_tv_hot",
        "async_douban_movie_recommend",
        "async_douban_tv_recommend",
        "async_douban_person_credits",
        "async_bangumi_calendar",
        "async_bangumi_discover",
        "async_bangumi_recommend",
        "async_bangumi_person_credits",
    )
    _RATING_ORDER = {
        "豆瓣": 0,
        "IMDb": 1,
        "Bangumi": 2,
        "TMDB": 3,
    }
    _OVERVIEW_PREFIXES = (
        "当前分：",
        "全部评分：",
        "评分：",
    )
    _RATING_TAGLINE_PATTERN = re.compile(
        r"^(?:(?:TMDB|IMDb|豆瓣|Bangumi)\s\d+(?:\.\d)?(?:\s/\s(?:TMDB|IMDb|豆瓣|Bangumi)\s\d+(?:\.\d)?)*)"
        r"(?:\s·\s)?"
    )

    def __init__(self):
        super().__init__()
        self._enabled = True
        self._enable_imdb = True
        self._enable_douban = True
        self._omdb_api_key = ""
        self._max_items = 30
        self._tmdb_api = TmdbApi()
        self._media_chain = MediaChain()
        self._tmdb_detail_cache: Dict[Tuple[str, int], Optional[dict]] = {}
        self._tmdb_match_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}
        self._imdb_rating_cache: Dict[str, Optional[float]] = {}
        self._douban_info_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        return {
            "enable": True,
            "enable_imdb": True,
            "enable_douban": True,
            "omdb_api_key": "",
            "max_items": 30,
        }

    def init_plugin(self, config: dict = None):
        conf = self._default_config()
        conf.update(config or {})
        self._enabled = bool(conf.get("enable"))
        self._enable_imdb = bool(conf.get("enable_imdb"))
        self._enable_douban = bool(conf.get("enable_douban"))
        self._omdb_api_key = (conf.get("omdb_api_key") or "").strip()
        try:
            self._max_items = max(1, min(int(conf.get("max_items") or 30), 50))
        except (TypeError, ValueError):
            self._max_items = 30
        self._reset_runtime_cache()
        self._trigger_recommend_cache_clear()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_module(self) -> Dict[str, Any]:
        if not self.get_state():
            return {}
        modules: Dict[str, Any] = {}
        for method in self._SYNC_MEDIA_ITEM_METHODS:
            modules[method] = self._make_sync_item_handler(method)
        for method in self._ASYNC_MEDIA_ITEM_METHODS:
            modules[method] = self._make_async_item_handler(method)
        for method in self._SYNC_MEDIA_LIST_METHODS:
            modules[method] = self._make_sync_list_handler(method)
        for method in self._ASYNC_MEDIA_LIST_METHODS:
            modules[method] = self._make_async_list_handler(method)
        return modules

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "class": "mb-4",
                },
                "text": (
                    "插件会统一改写推荐页、搜索结果、媒体详情和工作流中的评分。"
                    "卡片右上角显示最低分；详情页会在简介上方显示各平台评分串。"
                ),
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable",
                    "label": "启用插件",
                    "class": "mb-2",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable_douban",
                    "label": "参与豆瓣评分",
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable_imdb",
                    "label": "参与 IMDb 评分（需 OMDb API Key）",
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "omdb_api_key",
                    "label": "OMDb API Key",
                    "placeholder": "例如：your-omdb-key",
                    "clearable": True,
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_imdb }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "max_items",
                    "label": "每次列表接口最大补分条数",
                    "type": "number",
                    "min": 1,
                    "max": 50,
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
        ], self._default_config()

    def get_page(self) -> Optional[List[dict]]:
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                },
                "text": (
                    f"当前状态：{'已启用' if self._enabled else '未启用'}；"
                    f"豆瓣：{'参与计算' if self._enable_douban else '不参与'}；"
                    f"IMDb：{'参与计算' if self._enable_imdb else '不参与'}；"
                    f"最大补分条数：{self._max_items}"
                ),
            }
        ]

    def stop_service(self):
        self._reset_runtime_cache()
        self._tmdb_api.close()
        self._trigger_recommend_cache_clear()

    def _reset_runtime_cache(self):
        self._tmdb_detail_cache.clear()
        self._tmdb_match_cache.clear()
        self._imdb_rating_cache.clear()
        self._douban_info_cache.clear()

    def _make_sync_item_handler(self, method: str):
        def handler(*args, **kwargs):
            return self._handle_sync_media_item(method, *args, **kwargs)
        return handler

    def _make_async_item_handler(self, method: str):
        async def handler(*args, **kwargs):
            return await self._handle_async_media_item(method, *args, **kwargs)
        return handler

    def _make_sync_list_handler(self, method: str):
        def handler(*args, **kwargs):
            return self._handle_sync_media_list(method, *args, **kwargs)
        return handler

    def _make_async_list_handler(self, method: str):
        async def handler(*args, **kwargs):
            return await self._handle_async_media_list(method, *args, **kwargs)
        return handler

    def _handle_sync_media_item(self, method: str, *args, **kwargs):
        media = self._call_system_method(method, *args, **kwargs)
        if not media:
            return media
        return self._run_async(self._enrich_media(self._clone_media(media)))

    async def _handle_async_media_item(self, method: str, *args, **kwargs):
        media = await self._async_call_system_method(method, *args, **kwargs)
        if not media:
            return media
        return await self._enrich_media(self._clone_media(media))

    def _handle_sync_media_list(self, method: str, *args, **kwargs):
        medias = self._call_system_method(method, *args, **kwargs)
        return self._run_async(self._build_result(medias))

    async def _handle_async_media_list(self, method: str, *args, **kwargs):
        medias = await self._async_call_system_method(method, *args, **kwargs)
        return await self._build_result(medias)

    def _call_system_method(self, method: str, *args, **kwargs):
        result = None
        modules = sorted(self.chain.modulemanager.get_running_modules(method), key=lambda module: module.get_priority())
        for module in modules:
            func = getattr(module, method, None)
            if not func:
                continue
            if self._is_empty(result):
                result = func(*args, **kwargs)
            elif isinstance(result, list):
                temp = func(*args, **kwargs)
                if isinstance(temp, list):
                    result.extend(temp)
            else:
                break
        return result

    async def _async_call_system_method(self, method: str, *args, **kwargs):
        result = None
        modules = sorted(self.chain.modulemanager.get_running_modules(method), key=lambda module: module.get_priority())
        for module in modules:
            func = getattr(module, method, None)
            if not func:
                continue
            if self._is_empty(result):
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = await run_in_threadpool(func, *args, **kwargs)
            elif isinstance(result, list):
                if inspect.iscoroutinefunction(func):
                    temp = await func(*args, **kwargs)
                else:
                    temp = await run_in_threadpool(func, *args, **kwargs)
                if isinstance(temp, list):
                    result.extend(temp)
            else:
                break
        return result

    async def _build_result(self, medias: Optional[Sequence[MediaInfo]]) -> Tuple[MediaInfo, ...]:
        items = [self._clone_media(media) for media in (medias or [])]
        if not items:
            return ()
        target_count = min(self._max_items, len(items))
        if target_count:
            enriched_items = await asyncio.gather(*(self._enrich_media(items[index]) for index in range(target_count)))
            items[:target_count] = enriched_items
        for index in range(target_count, len(items)):
            items[index].overview = self._strip_rating_overview(items[index].overview)
        return tuple(items)

    async def _enrich_media(self, media: MediaInfo) -> MediaInfo:
        media.overview = self._strip_rating_overview(media.overview)

        ratings: Dict[str, float] = {}
        source_label = self._source_label(media)
        current_rating = self._normalize_rating(media.vote_average)
        if source_label and current_rating is not None:
            ratings[source_label] = current_rating

        tmdb_detail = await self._resolve_tmdb_detail(media)
        if tmdb_detail:
            tmdb_rating = self._normalize_rating(tmdb_detail.get("vote_average"))
            if tmdb_rating is not None:
                ratings["TMDB"] = tmdb_rating
            imdb_id = self._extract_imdb_id(tmdb_detail)
            if imdb_id:
                media.imdb_id = imdb_id

        if self._enable_douban:
            douban_info = await self._resolve_douban_info(media)
            if douban_info:
                douban_id = douban_info.get("id")
                if douban_id:
                    media.douban_id = str(douban_id)
                douban_rating = self._extract_douban_rating(douban_info)
                if douban_rating is not None:
                    ratings["豆瓣"] = douban_rating

        if self._enable_imdb and media.imdb_id:
            imdb_rating = await self._get_imdb_rating(media.imdb_id)
            if imdb_rating is not None:
                ratings["IMDb"] = imdb_rating

        if not ratings:
            return media

        ordered_ratings = sorted(ratings.items(), key=lambda item: (item[1], self._label_priority(item[0])))
        media.vote_average = ordered_ratings[0][1]
        media.tagline = self._merge_rating_tagline(ordered_ratings, media.tagline)
        return media

    async def _resolve_tmdb_detail(self, media: MediaInfo) -> Optional[dict]:
        media_type = self._get_media_type(media.type)
        if media.tmdb_id:
            return await self._get_tmdb_detail(media.tmdb_id, media_type)

        matched_tmdb = None
        if media.douban_id:
            matched_tmdb = await self._get_tmdbinfo_by_doubanid(media.douban_id, media_type)
        elif media.bangumi_id:
            matched_tmdb = await self._get_tmdbinfo_by_bangumiid(media.bangumi_id)

        if not matched_tmdb:
            return None

        tmdb_id = matched_tmdb.get("id")
        if tmdb_id:
            media.tmdb_id = int(tmdb_id)
            return await self._get_tmdb_detail(media.tmdb_id, media_type)
        return matched_tmdb

    async def _resolve_douban_info(self, media: MediaInfo) -> Optional[dict]:
        media_type = self._get_media_type(media.type)
        if media.douban_id:
            return await self._get_douban_info_by_id(media.douban_id, media_type)

        douban_info = None
        if media.tmdb_id:
            douban_info = await self._get_doubaninfo_by_tmdbid(media.tmdb_id, media_type)
        elif media.bangumi_id:
            douban_info = await self._get_doubaninfo_by_bangumiid(media.bangumi_id)
        elif media.imdb_id or media.title:
            douban_info = await self._match_douban_info(media, media.imdb_id)

        return douban_info

    async def _get_tmdb_detail(self, tmdb_id: int, media_type: Optional[MediaType]) -> Optional[dict]:
        if not tmdb_id or not media_type:
            return None
        cache_key = (media_type.value, int(tmdb_id))
        if cache_key in self._tmdb_detail_cache:
            return self._tmdb_detail_cache[cache_key]
        detail = await self._tmdb_api.async_get_info(mtype=media_type, tmdbid=int(tmdb_id))
        self._tmdb_detail_cache[cache_key] = detail
        return detail

    async def _get_tmdbinfo_by_doubanid(self, douban_id: str, media_type: Optional[MediaType]) -> Optional[dict]:
        cache_key = ("douban", str(douban_id), media_type.value if media_type else "")
        if cache_key in self._tmdb_match_cache:
            return self._tmdb_match_cache[cache_key]
        info = await self._media_chain.async_get_tmdbinfo_by_doubanid(doubanid=str(douban_id), mtype=media_type)
        self._tmdb_match_cache[cache_key] = info
        return info

    async def _get_tmdbinfo_by_bangumiid(self, bangumi_id: int) -> Optional[dict]:
        cache_key = ("bangumi", str(bangumi_id), "")
        if cache_key in self._tmdb_match_cache:
            return self._tmdb_match_cache[cache_key]
        info = await self._media_chain.async_get_tmdbinfo_by_bangumiid(bangumiid=int(bangumi_id))
        self._tmdb_match_cache[cache_key] = info
        return info

    async def _get_doubaninfo_by_tmdbid(self, tmdb_id: int, media_type: Optional[MediaType]) -> Optional[dict]:
        cache_key = ("tmdb", str(tmdb_id), media_type.value if media_type else "")
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        info = await self._media_chain.async_get_doubaninfo_by_tmdbid(tmdbid=int(tmdb_id), mtype=media_type)
        self._douban_info_cache[cache_key] = info
        return info

    async def _get_doubaninfo_by_bangumiid(self, bangumi_id: int) -> Optional[dict]:
        cache_key = ("bangumi", str(bangumi_id), "")
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        info = await self._media_chain.async_get_doubaninfo_by_bangumiid(bangumiid=int(bangumi_id))
        self._douban_info_cache[cache_key] = info
        return info

    async def _get_douban_info_by_id(self, douban_id: str, media_type: Optional[MediaType]) -> Optional[dict]:
        cache_key = ("id", str(douban_id), media_type.value if media_type else "")
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        info = await self.chain.async_douban_info(doubanid=str(douban_id), mtype=media_type, raise_exception=False)
        self._douban_info_cache[cache_key] = info
        return info

    async def _match_douban_info(self, media: MediaInfo, imdb_id: Optional[str]) -> Optional[dict]:
        cache_key = ("match", imdb_id or media.title or "", str(media.year or ""))
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        info = None
        try:
            info = await self.chain.async_match_doubaninfo(
                name=media.title or "",
                imdbid=imdb_id,
                mtype=self._get_media_type(media.type),
                year=media.year,
                season=media.season,
                raise_exception=False,
            )
            douban_id = info.get("id") if info else None
            if douban_id:
                detail = await self._get_douban_info_by_id(str(douban_id), self._get_media_type(media.type))
                if detail:
                    if not detail.get("id"):
                        detail["id"] = str(douban_id)
                    info = detail
        except Exception as err:
            logger.warn(f"豆瓣评分补充失败：{media.title} - {err}")
            info = None
        self._douban_info_cache[cache_key] = info
        return info

    async def _get_imdb_rating(self, imdb_id: str) -> Optional[float]:
        if not self._omdb_api_key:
            return None
        if imdb_id in self._imdb_rating_cache:
            return self._imdb_rating_cache[imdb_id]
        data = await AsyncRequestUtils(timeout=10).get_json(
            "https://www.omdbapi.com/",
            params={
                "i": imdb_id,
                "apikey": self._omdb_api_key,
            },
        )
        rating = None
        if data and data.get("Response") == "True":
            rating = self._normalize_rating(data.get("imdbRating"))
        self._imdb_rating_cache[imdb_id] = rating
        return rating

    @staticmethod
    def _clone_media(media: MediaInfo) -> MediaInfo:
        cloned = MediaInfo()
        cloned.from_dict(media.to_dict())
        return cloned

    @staticmethod
    def _is_empty(result: Any) -> bool:
        if isinstance(result, tuple):
            return all(value is None for value in result)
        return result is None

    @staticmethod
    def _normalize_rating(value: Any) -> Optional[float]:
        if value in (None, "", "N/A"):
            return None
        try:
            normalized = round(float(value), 1)
        except (TypeError, ValueError):
            return None
        if normalized <= 0:
            return None
        return normalized

    @classmethod
    def _label_priority(cls, label: str) -> int:
        return cls._RATING_ORDER.get(label, 99)

    @staticmethod
    def _source_label(media: MediaInfo) -> Optional[str]:
        source = (media.source or "").lower()
        if source == "themoviedb":
            return "TMDB"
        if source == "douban":
            return "豆瓣"
        if source == "bangumi":
            return "Bangumi"
        if media.tmdb_id:
            return "TMDB"
        if media.douban_id:
            return "豆瓣"
        if media.bangumi_id:
            return "Bangumi"
        return None

    @staticmethod
    def _get_media_type(media_type: Any) -> Optional[MediaType]:
        if isinstance(media_type, MediaType):
            return media_type
        value = str(media_type or "").strip().lower()
        if value in {"电影", "movie"}:
            return MediaType.MOVIE
        if value in {"电视剧", "tv", "show"}:
            return MediaType.TV
        return None

    @staticmethod
    def _extract_imdb_id(detail: Optional[dict]) -> Optional[str]:
        if not detail:
            return None
        return detail.get("external_ids", {}).get("imdb_id") or detail.get("imdb_id")

    @staticmethod
    def _extract_douban_rating(douban_info: Optional[dict]) -> Optional[float]:
        if not douban_info:
            return None
        rating = douban_info.get("rating")
        if not isinstance(rating, dict):
            return None
        return MultiRatingsRecommend._normalize_rating(rating.get("value"))

    @classmethod
    def _strip_rating_overview(cls, overview: Optional[str]) -> str:
        lines = [line.strip() for line in str(overview or "").splitlines() if line.strip()]
        while lines and lines[0].startswith(cls._OVERVIEW_PREFIXES):
            lines.pop(0)
        return "\n".join(lines).strip()

    @classmethod
    def _merge_rating_tagline(cls, ratings: List[Tuple[str, float]], tagline: Optional[str]) -> str:
        rating_line = " / ".join(f"{label} {value:.1f}" for label, value in ratings)
        clean_tagline = cls._strip_rating_tagline(tagline)
        if clean_tagline:
            return f"{rating_line} · {clean_tagline}"
        return rating_line

    @classmethod
    def _strip_rating_tagline(cls, tagline: Optional[str]) -> str:
        text = str(tagline or "").strip()
        if not text:
            return ""
        return cls._RATING_TAGLINE_PATTERN.sub("", text).strip(" ·")

    @staticmethod
    def _run_async(awaitable):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        result: List[Any] = []
        errors: List[BaseException] = []

        def _runner():
            try:
                result.append(asyncio.run(awaitable))
            except BaseException as err:  # noqa: BLE001
                errors.append(err)

        import threading

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()
        if errors:
            raise errors[0]
        return result[0] if result else None

    def _trigger_recommend_cache_clear(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._clear_recommend_cache())
            return
        loop.create_task(self._clear_recommend_cache())

    async def _clear_recommend_cache(self):
        method_names = (
            "tmdb_movies",
            "tmdb_tvs",
            "tmdb_trending",
            "douban_movie_showing",
            "douban_movies",
            "douban_tvs",
            "douban_movie_top250",
            "douban_tv_weekly_chinese",
            "douban_tv_weekly_global",
            "douban_tv_animation",
            "douban_movie_hot",
            "douban_tv_hot",
            "bangumi_calendar",
            "async_tmdb_movies",
            "async_tmdb_tvs",
            "async_tmdb_trending",
            "async_douban_movie_showing",
            "async_douban_movies",
            "async_douban_tvs",
            "async_douban_movie_top250",
            "async_douban_tv_weekly_chinese",
            "async_douban_tv_weekly_global",
            "async_douban_tv_animation",
            "async_douban_movie_hot",
            "async_douban_tv_hot",
            "async_bangumi_calendar",
        )
        try:
            for method_name in method_names:
                method = getattr(RecommendChain, method_name, None)
                clear_fn = getattr(method, "cache_clear", None)
                if not clear_fn:
                    continue
                if inspect.iscoroutinefunction(clear_fn):
                    await clear_fn()
                else:
                    clear_fn()
        except Exception as err:
            logger.warn(f"清理推荐缓存失败：{err}")
