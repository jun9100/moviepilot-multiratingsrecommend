import asyncio
import gzip
import inspect
import json
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
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
    plugin_desc = "统一接管推荐、搜索、识别结果评分，主评分取 TMDB / 豆瓣 的低分，缺失时回退 IMDb。"
    plugin_icon = "mdi-shield-half-full"
    plugin_version = "0.4.1"
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
        "TMDB": 0,
        "豆瓣": 1,
        "IMDb": 2,
        "Bangumi": 3,
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
        self._imdb_source = "auto"
        self._imdb_ratings_path = ""
        self._omdb_api_key = ""
        self._max_items = 30
        self._tmdb_api = TmdbApi()
        self._media_chain = MediaChain()
        self._tmdb_detail_cache: Dict[Tuple[str, int], Optional[dict]] = {}
        self._tmdb_match_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}
        self._imdb_rating_cache: Dict[str, Optional[float]] = {}
        self._douban_info_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}
        self._imdb_blocked_until: float = 0
        self._imdb_block_reason: str = ""
        self._imdb_dataset_status: str = "未启用"
        self._imdb_dataset_meta: Dict[str, Any] = {}
        self._imdb_dataset_lock = Lock()

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        return {
            "enable": True,
            "enable_imdb": True,
            "enable_douban": True,
            "imdb_source": "auto",
            "imdb_ratings_path": "",
            "omdb_api_key": "",
            "max_items": 30,
        }

    def init_plugin(self, config: dict = None):
        conf = self._default_config()
        conf.update(config or {})
        if conf.get("imdb_source") not in {"auto", "dataset", "omdb"}:
            conf["imdb_source"] = "auto"
        if config != conf:
            self.update_config(conf)
        self._enabled = bool(conf.get("enable"))
        self._enable_imdb = bool(conf.get("enable_imdb"))
        self._enable_douban = bool(conf.get("enable_douban"))
        self._imdb_source = str(conf.get("imdb_source") or "auto").strip().lower()
        self._imdb_ratings_path = str(conf.get("imdb_ratings_path") or "").strip()
        self._omdb_api_key = (conf.get("omdb_api_key") or "").strip()
        try:
            self._max_items = max(1, min(int(conf.get("max_items") or 30), 50))
        except (TypeError, ValueError):
            self._max_items = 30
        self._reset_runtime_cache()
        self._refresh_imdb_dataset_status()
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
                    "卡片右上角优先显示 TMDB / 豆瓣 的低分，两者都缺失时回退 IMDb；"
                    "详情页会在简介上方显示各平台评分串。IMDb 支持本地数据集优先模式。"
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
                    "label": "参与 IMDb 评分",
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
            {
                "component": "VSelect",
                "props": {
                    "model": "imdb_source",
                    "label": "IMDb 数据来源",
                    "items": [
                        {"title": "本地数据集优先，OMDb 回退（推荐）", "value": "auto"},
                        {"title": "仅本地数据集", "value": "dataset"},
                        {"title": "仅 OMDb", "value": "omdb"},
                    ],
                    "item-title": "title",
                    "item-value": "value",
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_imdb }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "imdb_ratings_path",
                    "label": "IMDb title.ratings.tsv(.gz) 路径",
                    "placeholder": "例如：/config/imdb/title.ratings.tsv.gz",
                    "clearable": True,
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_imdb || imdb_source === 'omdb' }}",
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
                    "disabled": "{{ !enable || !enable_imdb || imdb_source === 'dataset' }}",
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
                    f"IMDb 来源：{self._imdb_source}；"
                    f"最大补分条数：{self._max_items}；"
                    f"主评分策略：TMDB / 豆瓣 取低分，缺失时回退 IMDb"
                    + (f"；IMDb 数据集：{self._imdb_dataset_status}" if self._enable_imdb else "")
                    + (f"；OMDb 状态：{self._imdb_block_reason}" if self._imdb_blocked_until > time.time() else "")
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
        self._imdb_blocked_until = 0
        self._imdb_block_reason = ""
        self._imdb_dataset_meta = {}
        self._imdb_dataset_status = "未启用"

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
        if self._is_missing_media(media):
            media = self._run_async(self._fallback_media_item(media, **kwargs))
        if self._is_missing_media(media):
            return media
        return self._run_async(self._enrich_media(self._clone_media(media)))

    async def _handle_async_media_item(self, method: str, *args, **kwargs):
        media = await self._async_call_system_method(method, *args, **kwargs)
        if self._is_missing_media(media):
            media = await self._fallback_media_item(media, **kwargs)
        if self._is_missing_media(media):
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

        primary_rating = self._select_primary_rating(ratings)
        display_ratings = self._display_ratings(ratings)
        if primary_rating is not None:
            media.vote_average = primary_rating
        media.tagline = self._merge_rating_tagline(display_ratings, media.tagline)
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
            matched_tmdb = await self._match_tmdbinfo_by_title(media)

        if not matched_tmdb:
            return None

        tmdb_id = matched_tmdb.get("id")
        if tmdb_id:
            media.tmdb_id = int(tmdb_id)
            return await self._get_tmdb_detail(media.tmdb_id, media_type)
        return matched_tmdb

    async def _fallback_media_item(self, media: Optional[MediaInfo], **kwargs) -> Optional[MediaInfo]:
        current = self._clone_media(media) if isinstance(media, MediaInfo) else MediaInfo()
        media_type = self._get_media_type(kwargs.get("mtype") or current.type)
        douban_id = kwargs.get("doubanid") or getattr(current, "douban_id", None)
        bangumi_id = kwargs.get("bangumiid") or getattr(current, "bangumi_id", None)

        if douban_id:
            douban_info = await self._get_douban_info_by_id(str(douban_id), media_type)
            if douban_info:
                current.set_douban_info(douban_info)
                if media_type and not current.type:
                    current.type = media_type
                return current
            matched_tmdb = await self._get_tmdbinfo_by_doubanid(str(douban_id), media_type)
            if matched_tmdb:
                tmdb_id = matched_tmdb.get("id")
                matched_type = media_type or self._get_media_type(matched_tmdb.get("media_type"))
                detail = await self._get_tmdb_detail(int(tmdb_id), matched_type) if tmdb_id else matched_tmdb
                if detail:
                    fallback = MediaInfo(tmdb_info=detail)
                    fallback.douban_id = str(douban_id)
                    return fallback

        if bangumi_id:
            bangumi_info = await self.chain.async_bangumi_info(bangumiid=int(bangumi_id))
            if bangumi_info:
                current.set_bangumi_info(bangumi_info)
                return current

        return media

    async def _resolve_douban_info(self, media: MediaInfo) -> Optional[dict]:
        media_type = self._get_media_type(media.type)
        if media.douban_id:
            return await self._get_douban_info_by_id(media.douban_id, media_type)

        douban_info = None
        if media.tmdb_id:
            douban_info = await self._get_doubaninfo_by_tmdbid(media.tmdb_id, media_type)
        elif media.bangumi_id:
            douban_info = await self._get_doubaninfo_by_bangumiid(media.bangumi_id)
        if not douban_info and (media.imdb_id or media.title):
            douban_info = await self._match_douban_info(media, media.imdb_id)
        douban_id = douban_info.get("id") if douban_info else None
        if douban_id and self._extract_douban_rating(douban_info) is None:
            detail = await self._get_douban_info_by_id(str(douban_id), media_type)
            if detail:
                douban_info = detail

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

    async def _match_tmdbinfo_by_title(self, media: MediaInfo) -> Optional[dict]:
        media_type = self._get_media_type(media.type)
        if not media_type:
            return None
        names = []
        for value in [media.original_title, media.title, media.en_title, *(media.names or [])]:
            if value and value not in names:
                names.append(value)
        if not names:
            return None
        cache_key = ("title", "|".join(names), str(media.year or ""), media_type.value)
        if cache_key in self._tmdb_match_cache:
            return self._tmdb_match_cache[cache_key]
        info = None
        for name in names:
            info = await self.chain.async_match_tmdbinfo(
                name=name,
                mtype=media_type,
                year=media.year,
                season=media.season,
            )
            if info:
                break
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
        if self._imdb_source in {"auto", "dataset"}:
            dataset_rating = await self._get_imdb_rating_from_dataset(imdb_id)
            if dataset_rating is not None:
                return dataset_rating
            if self._imdb_source == "dataset":
                return None
        if not self._omdb_api_key:
            return None
        if self._imdb_blocked_until > time.time():
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
        elif data and data.get("Error"):
            error_message = str(data.get("Error"))
            if "limit" in error_message.lower():
                self._imdb_blocked_until = time.time() + 12 * 3600
                self._imdb_block_reason = error_message
                logger.warn(f"IMDb 评分接口已触发限额熔断：{error_message}")
        self._imdb_rating_cache[imdb_id] = rating
        return rating

    async def _get_imdb_rating_from_dataset(self, imdb_id: str) -> Optional[float]:
        if not imdb_id:
            return None
        if not self._imdb_ratings_path:
            self._imdb_dataset_status = "未配置数据集路径"
            return None
        return await run_in_threadpool(self._lookup_imdb_rating_from_dataset, imdb_id)

    def _lookup_imdb_rating_from_dataset(self, imdb_id: str) -> Optional[float]:
        dataset_path = self._get_imdb_dataset_source_path()
        if not dataset_path:
            self._imdb_dataset_status = "数据集路径不存在"
            return None
        try:
            db_path = self._ensure_imdb_dataset_index(dataset_path)
            if not db_path or not db_path.exists():
                self._imdb_dataset_status = "数据集索引不可用"
                return None
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT average_rating FROM ratings WHERE imdb_id = ?",
                    (imdb_id,),
                ).fetchone()
            if row:
                return self._normalize_rating(row[0])
            return None
        except Exception as err:
            self._imdb_dataset_status = f"索引失败：{err}"
            logger.error(f"IMDb 数据集查询失败：{err}")
            return None

    def _refresh_imdb_dataset_status(self):
        if not self._enable_imdb:
            self._imdb_dataset_status = "未启用"
            return
        if self._imdb_source == "omdb":
            self._imdb_dataset_status = "未使用"
            return
        source_path = self._get_imdb_dataset_source_path()
        if not source_path:
            self._imdb_dataset_status = "未配置数据集路径"
            return
        meta = self._load_imdb_dataset_meta()
        if meta and self._is_imdb_dataset_meta_current(meta, source_path):
            self._imdb_dataset_meta = meta
            self._imdb_dataset_status = f"已索引 {meta.get('record_count', 0)} 条"
        else:
            self._imdb_dataset_status = "待建立索引"

    def _get_imdb_dataset_source_path(self) -> Optional[Path]:
        if not self._imdb_ratings_path:
            return None
        dataset_path = Path(self._imdb_ratings_path).expanduser()
        if dataset_path.exists() and dataset_path.is_file():
            return dataset_path
        return None

    def _get_imdb_dataset_db_path(self) -> Path:
        return self.get_data_path() / "imdb_ratings.sqlite3"

    def _get_imdb_dataset_meta_path(self) -> Path:
        return self.get_data_path() / "imdb_ratings_meta.json"

    def _load_imdb_dataset_meta(self) -> Dict[str, Any]:
        meta_path = self._get_imdb_dataset_meta_path()
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_imdb_dataset_meta(self, meta: Dict[str, Any]):
        self._get_imdb_dataset_meta_path().write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _is_imdb_dataset_meta_current(meta: Dict[str, Any], source_path: Path) -> bool:
        try:
            stat = source_path.stat()
        except FileNotFoundError:
            return False
        return (
            meta.get("source_path") == str(source_path)
            and meta.get("source_size") == stat.st_size
            and meta.get("source_mtime_ns") == stat.st_mtime_ns
        )

    def _ensure_imdb_dataset_index(self, source_path: Path) -> Path:
        with self._imdb_dataset_lock:
            db_path = self._get_imdb_dataset_db_path()
            meta = self._load_imdb_dataset_meta()
            if db_path.exists() and meta and self._is_imdb_dataset_meta_current(meta, source_path):
                self._imdb_dataset_meta = meta
                self._imdb_dataset_status = f"已索引 {meta.get('record_count', 0)} 条"
                return db_path
            self._build_imdb_dataset_index(source_path, db_path)
            meta = self._load_imdb_dataset_meta()
            self._imdb_dataset_meta = meta
            self._imdb_dataset_status = f"已索引 {meta.get('record_count', 0)} 条"
            return db_path

    def _build_imdb_dataset_index(self, source_path: Path, db_path: Path):
        self._imdb_dataset_status = "正在建立索引"
        tmp_path = db_path.with_suffix(".tmp.sqlite3")
        if tmp_path.exists():
            tmp_path.unlink()

        record_count = 0
        with sqlite3.connect(tmp_path) as conn:
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute(
                "CREATE TABLE ratings (imdb_id TEXT PRIMARY KEY, average_rating REAL NOT NULL, num_votes INTEGER NOT NULL)"
            )
            batch: List[Tuple[str, float, int]] = []
            with self._open_imdb_dataset(source_path) as handle:
                next(handle, None)
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    imdb_id = parts[0].strip()
                    rating = self._normalize_rating(parts[1].strip())
                    if not imdb_id or rating is None:
                        continue
                    try:
                        num_votes = int(parts[2].strip() or 0)
                    except ValueError:
                        num_votes = 0
                    batch.append((imdb_id, rating, num_votes))
                    if len(batch) >= 20000:
                        conn.executemany(
                            "INSERT OR REPLACE INTO ratings (imdb_id, average_rating, num_votes) VALUES (?, ?, ?)",
                            batch,
                        )
                        record_count += len(batch)
                        batch.clear()
                if batch:
                    conn.executemany(
                        "INSERT OR REPLACE INTO ratings (imdb_id, average_rating, num_votes) VALUES (?, ?, ?)",
                        batch,
                    )
                    record_count += len(batch)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_votes ON ratings (num_votes DESC)")
            conn.commit()

        tmp_path.replace(db_path)
        stat = source_path.stat()
        self._save_imdb_dataset_meta(
            {
                "source_path": str(source_path),
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "record_count": record_count,
                "built_at": int(time.time()),
            }
        )

    @staticmethod
    def _open_imdb_dataset(source_path: Path):
        if source_path.suffix.lower() == ".gz":
            return gzip.open(source_path, "rt", encoding="utf-8", newline="")
        return source_path.open("r", encoding="utf-8", newline="")

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
    def _is_missing_media(media: Any) -> bool:
        if media is None:
            return True
        if not isinstance(media, MediaInfo):
            return False
        return not any([
            getattr(media, "title", None),
            getattr(media, "tmdb_id", None),
            getattr(media, "douban_id", None),
            getattr(media, "bangumi_id", None),
        ])

    @classmethod
    def _select_primary_rating(cls, ratings: Dict[str, float]) -> Optional[float]:
        tmdb_rating = ratings.get("TMDB")
        douban_rating = ratings.get("豆瓣")
        if tmdb_rating is not None and douban_rating is not None:
            return min(tmdb_rating, douban_rating)
        if tmdb_rating is not None:
            return tmdb_rating
        if douban_rating is not None:
            return douban_rating
        imdb_rating = ratings.get("IMDb")
        if imdb_rating is not None:
            return imdb_rating
        bangumi_rating = ratings.get("Bangumi")
        if bangumi_rating is not None:
            return bangumi_rating
        return None

    @classmethod
    def _display_ratings(cls, ratings: Dict[str, float]) -> List[Tuple[str, float]]:
        return [
            (label, ratings[label])
            for label in ("TMDB", "豆瓣", "IMDb", "Bangumi")
            if ratings.get(label) is not None
        ]

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
