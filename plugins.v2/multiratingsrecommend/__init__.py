import asyncio
from typing import Any, Dict, List, Optional, Tuple

from app import schemas
from app.chain.tmdb import TmdbChain
from app.core.context import MediaInfo
from app.core.event import Event, eventmanager
from app.log import logger
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, MediaType
from app.utils.http import AsyncRequestUtils


class MultiRatingsRecommend(_PluginBase):
    plugin_name = "TMDB多评分推荐"
    plugin_desc = "基于TMDB推荐补充IMDb和豆瓣评分，并把多评分写入卡片简介。"
    plugin_icon = "mdi-star-box-multiple-outline"
    plugin_version = "0.1.1"
    plugin_author = "jun9100"
    author_url = "https://github.com/jun9100"
    plugin_config_prefix = "multiratingsrecommend_"
    plugin_order = 70

    _SOURCE_CONFIG = {
        "tmdb_trending": {
            "name": "TMDB流行趋势+IMDb/豆瓣",
            "type": "榜单",
        },
        "tmdb_movies": {
            "name": "TMDB热门电影+IMDb/豆瓣",
            "type": "电影",
        },
        "tmdb_tvs": {
            "name": "TMDB热门电视剧+IMDb/豆瓣",
            "type": "电视剧",
        },
    }

    def __init__(self):
        super().__init__()
        self._enabled = True
        self._enable_imdb = True
        self._enable_douban = True
        self._omdb_api_key = ""
        self._max_items = 20
        self._tmdb_api = TmdbApi()
        self._tmdb_detail_cache: Dict[Tuple[str, int], dict] = {}
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

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/recommend/tmdb_trending",
                "endpoint": self.recommend_tmdb_trending,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "TMDB流行趋势（多评分）",
                "description": "基于TMDB流行趋势补充IMDb和豆瓣评分。",
                "response_model": List[schemas.MediaInfo],
            },
            {
                "path": "/recommend/tmdb_movies",
                "endpoint": self.recommend_tmdb_movies,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "TMDB热门电影（多评分）",
                "description": "基于TMDB热门电影补充IMDb和豆瓣评分。",
                "response_model": List[schemas.MediaInfo],
            },
            {
                "path": "/recommend/tmdb_tvs",
                "endpoint": self.recommend_tmdb_tvs,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "TMDB热门电视剧（多评分）",
                "description": "基于TMDB热门电视剧补充IMDb和豆瓣评分。",
                "response_model": List[schemas.MediaInfo],
            },
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "class": "mb-4",
                },
                "text": "右上角仍显示TMDB评分。IMDb和豆瓣评分会追加到卡片简介前缀；IMDb需要配置OMDb API Key。",
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
                    "label": "补充豆瓣评分",
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable_imdb",
                    "label": "补充IMDb评分（需OMDb API Key）",
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
                    "label": "每页补充分数的条数",
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
                    f"豆瓣评分：{'开启' if self._enable_douban else '关闭'}；"
                    f"IMDb评分：{'开启' if self._enable_imdb else '关闭'}；"
                    f"每页条数：{self._max_items}"
                ),
            }
        ]

    def stop_service(self):
        self._tmdb_detail_cache.clear()
        self._imdb_rating_cache.clear()
        self._douban_info_cache.clear()
        self._tmdb_api.close()

    @eventmanager.register(ChainEventType.RecommendSource)
    def on_recommend_source(self, event: Event):
        if not self.get_state() or not event.event_data:
            return
        event_data = event.event_data
        existing_paths = {source.api_path for source in event_data.extra_sources}
        for source_name, source_conf in self._SOURCE_CONFIG.items():
            api_path = f"plugin/{self.__class__.__name__}/recommend/{source_name}"
            if api_path in existing_paths:
                continue
            event_data.extra_sources.append(
                schemas.RecommendMediaSource(
                    name=source_conf["name"],
                    api_path=api_path,
                    type=source_conf["type"],
                )
            )

    async def recommend_tmdb_trending(self, page: int = 1) -> List[dict]:
        return await self._build_source("tmdb_trending", page)

    async def recommend_tmdb_movies(self, page: int = 1) -> List[dict]:
        return await self._build_source("tmdb_movies", page)

    async def recommend_tmdb_tvs(self, page: int = 1) -> List[dict]:
        return await self._build_source("tmdb_tvs", page)

    async def _build_source(self, source_name: str, page: int) -> List[dict]:
        page = max(1, int(page or 1))
        medias = await self._fetch_source_medias(source_name, page)
        if not medias:
            return []
        medias = medias[: self._max_items]
        cloned = [self._clone_media(media) for media in medias]
        enriched = await asyncio.gather(*(self._enrich_media(media) for media in cloned))
        return [media.to_dict() for media in enriched if media]

    async def _fetch_source_medias(self, source_name: str, page: int) -> List[MediaInfo]:
        tmdb_chain = TmdbChain()
        if source_name == "tmdb_trending":
            medias = await tmdb_chain.async_tmdb_trending(page=page)
        elif source_name == "tmdb_movies":
            medias = await tmdb_chain.async_tmdb_discover(
                mtype=MediaType.MOVIE,
                sort_by="popularity.desc",
                with_genres="",
                with_original_language="",
                with_keywords="",
                with_watch_providers="",
                vote_average=0.0,
                vote_count=0,
                release_date="",
                page=page,
            )
        elif source_name == "tmdb_tvs":
            medias = await tmdb_chain.async_tmdb_discover(
                mtype=MediaType.TV,
                sort_by="popularity.desc",
                with_genres="",
                with_original_language="zh|en|ja|ko",
                with_keywords="",
                with_watch_providers="",
                vote_average=0.0,
                vote_count=0,
                release_date="",
                page=page,
            )
        else:
            medias = []
        return medias or []

    @staticmethod
    def _clone_media(media: MediaInfo) -> MediaInfo:
        cloned = MediaInfo()
        cloned.from_dict(media.to_dict())
        return cloned

    async def _enrich_media(self, media: MediaInfo) -> MediaInfo:
        imdb_id = media.imdb_id
        tmdb_detail = None
        if media.tmdb_id and not imdb_id:
            tmdb_detail = await self._get_tmdb_detail(media)
            imdb_id = self._extract_imdb_id(tmdb_detail)
            if imdb_id:
                media.imdb_id = imdb_id
        ratings: List[Tuple[str, Optional[float]]] = [("TMDB", media.vote_average)]
        if self._enable_imdb and imdb_id:
            ratings.append(("IMDb", await self._get_imdb_rating(imdb_id)))
        if self._enable_douban:
            douban_info = await self._get_douban_info(media, imdb_id)
            if douban_info:
                if douban_info.get("id") and not media.douban_id:
                    media.douban_id = str(douban_info.get("id"))
                ratings.append(("豆瓣", self._extract_douban_rating(douban_info)))
        media.overview = self._merge_rating_overview(ratings, media.overview)
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
            if raw_rating and raw_rating != "N/A":
                try:
                    rating = round(float(raw_rating), 1)
                except (TypeError, ValueError):
                    rating = None
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
        raw_value = rating.get("value")
        if raw_value in (None, "", "N/A"):
            return None
        try:
            return round(float(raw_value), 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _merge_rating_overview(ratings: List[Tuple[str, Optional[float]]], overview: Optional[str]) -> str:
        parts = [f"{label} {value:.1f}" for label, value in ratings if value not in (None, 0)]
        if not parts:
            return overview or ""
        rating_line = f"评分：{' / '.join(parts)}"
        overview = (overview or "").strip()
        if not overview:
            return rating_line
        return f"{rating_line}\n{overview}"
