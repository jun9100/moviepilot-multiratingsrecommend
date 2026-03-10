import asyncio
from typing import Any, Dict, List, Optional, Tuple

from app.chain.recommend import RecommendChain
from app.core.context import MediaInfo
from app.log import logger
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.utils.http import AsyncRequestUtils


class MultiRatingsRecommend(_PluginBase):
    plugin_name = "TMDB低分保护"
    plugin_desc = "接管现有TMDB推荐评分，按TMDB/IMDb/豆瓣中的最低分显示，降低TMDB评分误导。"
    plugin_icon = "mdi-shield-half-full"
    plugin_version = "0.2.0"
    plugin_author = "jun9100"
    author_url = "https://github.com/jun9100"
    plugin_config_prefix = "multiratingsrecommend_"
    plugin_order = 70

    _OVERVIEW_PREFIXES = (
        "当前分：",
        "全部评分：",
        "评分：",
    )

    def __init__(self):
        super().__init__()
        self._enabled = True
        self._enable_imdb = True
        self._enable_douban = True
        self._omdb_api_key = ""
        self._max_items = 20
        self._tmdb_api = TmdbApi()
        self._tmdb_detail_cache: Dict[Tuple[str, int], Optional[dict]] = {}
        self._imdb_rating_cache: Dict[str, Optional[float]] = {}
        self._douban_info_cache: Dict[str, Optional[dict]] = {}

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        return {
            "enable": True,
            "enable_imdb": True,
            "enable_douban": True,
            "omdb_api_key": "",
            "max_items": 20,
        }

    def init_plugin(self, config: dict = None):
        conf = self._default_config()
        conf.update(config or {})
        self._enabled = bool(conf.get("enable"))
        self._enable_imdb = bool(conf.get("enable_imdb"))
        self._enable_douban = bool(conf.get("enable_douban"))
        self._omdb_api_key = (conf.get("omdb_api_key") or "").strip()
        try:
            self._max_items = max(1, min(int(conf.get("max_items") or 20), 30))
        except (TypeError, ValueError):
            self._max_items = 20
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
        return {
            "async_tmdb_trending": self.async_tmdb_trending,
            "async_tmdb_discover": self.async_tmdb_discover,
        }

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
                    "插件会直接接管现有TMDB推荐行：右上角显示 TMDB / IMDb / 豆瓣 中的最低分。"
                    "卡片简介首行会写明当前分数来自哪个站点，并列出全部评分。"
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
                    "label": "参与IMDb评分（需OMDb API Key）",
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
                    "label": "每页最多补分条数",
                    "type": "number",
                    "min": 1,
                    "max": 30,
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
                    f"豆瓣：{'参与最低分计算' if self._enable_douban else '不参与'}；"
                    f"IMDb：{'参与最低分计算' if self._enable_imdb else '不参与'}；"
                    f"每页补分条数：{self._max_items}"
                ),
            }
        ]

    def stop_service(self):
        self._tmdb_detail_cache.clear()
        self._imdb_rating_cache.clear()
        self._douban_info_cache.clear()
        self._tmdb_api.close()
        self._trigger_recommend_cache_clear()

    async def async_tmdb_trending(self, page: int = 1):
        module = self._get_system_tmdb_module("async_tmdb_trending")
        if not module:
            return ()
        medias = await module.async_tmdb_trending(page=page)
        return await self._build_result(medias)

    async def async_tmdb_discover(
        self,
        mtype: MediaType,
        sort_by: str,
        with_genres: str,
        with_original_language: str,
        with_keywords: str,
        with_watch_providers: str,
        vote_average: float,
        vote_count: int,
        release_date: str,
        page: Optional[int] = 1,
    ):
        module = self._get_system_tmdb_module("async_tmdb_discover")
        if not module:
            return ()
        medias = await module.async_tmdb_discover(
            mtype=mtype,
            sort_by=sort_by,
            with_genres=with_genres,
            with_original_language=with_original_language,
            with_keywords=with_keywords,
            with_watch_providers=with_watch_providers,
            vote_average=vote_average,
            vote_count=vote_count,
            release_date=release_date,
            page=page,
        )
        return await self._build_result(medias)

    async def _build_result(self, medias: Optional[List[MediaInfo]]) -> Tuple[MediaInfo, ...]:
        items = [self._clone_media(media) for media in (medias or [])]
        if not items:
            return ()
        target_count = min(self._max_items, len(items))
        if target_count:
            enriched_items = await asyncio.gather(
                *(self._enrich_media(items[index]) for index in range(target_count))
            )
            items[:target_count] = enriched_items
        return tuple(items)

    def _get_system_tmdb_module(self, method: str):
        modules = self.chain.modulemanager.get_running_modules(method)
        for module in modules:
            if module.__class__.__module__.startswith("app.modules."):
                return module
        logger.warn(f"未找到系统TMDB模块：{method}")
        return None

    @staticmethod
    def _clone_media(media: MediaInfo) -> MediaInfo:
        cloned = MediaInfo()
        cloned.from_dict(media.to_dict())
        return cloned

    async def _enrich_media(self, media: MediaInfo) -> MediaInfo:
        imdb_id = media.imdb_id
        if media.tmdb_id and not imdb_id and (self._enable_imdb or self._enable_douban):
            imdb_id = self._extract_imdb_id(await self._get_tmdb_detail(media))
            if imdb_id:
                media.imdb_id = imdb_id

        ratings: List[Tuple[str, float]] = []
        tmdb_rating = self._normalize_rating(media.vote_average)
        if tmdb_rating is not None:
            ratings.append(("TMDB", tmdb_rating))

        if self._enable_imdb and imdb_id:
            imdb_rating = await self._get_imdb_rating(imdb_id)
            if imdb_rating is not None:
                ratings.append(("IMDb", imdb_rating))

        if self._enable_douban:
            douban_info = await self._get_douban_info(media, imdb_id)
            if douban_info:
                if douban_info.get("id") and not media.douban_id:
                    media.douban_id = str(douban_info.get("id"))
                douban_rating = self._extract_douban_rating(douban_info)
                if douban_rating is not None:
                    ratings.append(("豆瓣", douban_rating))

        if not ratings:
            return media

        ordered_ratings = sorted(ratings, key=lambda item: (item[1], self._label_priority(item[0])))
        primary_label, primary_value = ordered_ratings[0]
        media.vote_average = primary_value
        media.overview = self._merge_rating_overview(
            ratings=ordered_ratings,
            primary_label=primary_label,
            primary_value=primary_value,
            overview=media.overview,
        )
        return media

    async def _get_tmdb_detail(self, media: MediaInfo) -> Optional[dict]:
        if not media.tmdb_id or not media.type:
            return None
        media_type = media.type.value if isinstance(media.type, MediaType) else str(media.type)
        cache_key = (media_type, media.tmdb_id)
        if cache_key in self._tmdb_detail_cache:
            return self._tmdb_detail_cache[cache_key]
        tmdb_mtype = MediaType.MOVIE if media_type == MediaType.MOVIE.value else MediaType.TV
        detail = await self._tmdb_api.async_get_info(mtype=tmdb_mtype, tmdbid=media.tmdb_id)
        self._tmdb_detail_cache[cache_key] = detail
        return detail

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
            raw_rating = data.get("imdbRating")
            rating = self._normalize_rating(raw_rating)
        self._imdb_rating_cache[imdb_id] = rating
        return rating

    async def _get_douban_info(self, media: MediaInfo, imdb_id: Optional[str]) -> Optional[dict]:
        cache_key = imdb_id or f"{media.title}:{media.year}:{media.season}:{media.type}"
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        douban_info = None
        try:
            matched_info = await self.chain.async_match_doubaninfo(
                name=media.title or "",
                imdbid=imdb_id,
                mtype=media.type,
                year=media.year,
                season=media.season,
                raise_exception=False,
            )
            douban_id = matched_info.get("id") if matched_info else None
            if douban_id:
                douban_info = await self.chain.async_douban_info(
                    doubanid=str(douban_id),
                    mtype=media.type,
                    raise_exception=False,
                )
                if douban_info and not douban_info.get("id"):
                    douban_info["id"] = str(douban_id)
            if not douban_info:
                douban_info = matched_info
        except Exception as err:
            logger.warn(f"豆瓣评分补充失败：{media.title} - {err}")
            douban_info = None
        self._douban_info_cache[cache_key] = douban_info
        return douban_info

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

    @staticmethod
    def _label_priority(label: str) -> int:
        order = {
            "豆瓣": 0,
            "IMDb": 1,
            "TMDB": 2,
        }
        return order.get(label, 99)

    @classmethod
    def _merge_rating_overview(
        cls,
        ratings: List[Tuple[str, float]],
        primary_label: str,
        primary_value: float,
        overview: Optional[str],
    ) -> str:
        parts = [f"{label} {value:.1f}" for label, value in ratings]
        clean_overview = cls._strip_rating_overview(overview)
        prefix_lines = [
            f"当前分：{primary_label} {primary_value:.1f}（取最低分）",
            f"全部评分：{' / '.join(parts)}",
        ]
        if clean_overview:
            prefix_lines.append(clean_overview)
        return "\n".join(prefix_lines)

    @classmethod
    def _strip_rating_overview(cls, overview: Optional[str]) -> str:
        lines = [line.strip() for line in str(overview or "").splitlines() if line.strip()]
        while lines and lines[0].startswith(cls._OVERVIEW_PREFIXES):
            lines.pop(0)
        return "\n".join(lines).strip()

    def _trigger_recommend_cache_clear(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._clear_recommend_cache())
            return
        loop.create_task(self._clear_recommend_cache())

    async def _clear_recommend_cache(self):
        try:
            await RecommendChain.async_tmdb_movies.cache_clear()
            await RecommendChain.async_tmdb_tvs.cache_clear()
            await RecommendChain.async_tmdb_trending.cache_clear()
        except Exception as err:
            logger.warn(f"清理推荐缓存失败：{err}")
