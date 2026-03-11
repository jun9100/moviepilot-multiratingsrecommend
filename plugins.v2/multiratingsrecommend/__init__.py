import asyncio
import gzip
import inspect
import json
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus

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
    plugin_desc = "统一接管推荐、搜索、识别结果评分，主评分取 豆瓣 / TMDB 的低分，缺失时依次回退 IMDb、Bangumi。"
    plugin_icon = "mdi-shield-half-full"
    plugin_version = "0.6.17"
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
    _LIST_ENRICH_CONCURRENCY = 6
    _DOUBAN_ENRICH_TIMEOUT_ITEM = 4.0
    _DOUBAN_ENRICH_TIMEOUT_LIST = 2.5
    _OMDB_BLOCK_STATE_KEY = "omdb_block_state"
    _DOUBAN_BLOCK_STATE_KEY = "douban_block_state"
    _DOUBAN_RATING_STORE_KEY = "douban_rating_store"
    _DOUBAN_WEB_QUOTA_KEY = "douban_web_quota"
    _DOUBAN_WEB_MISS_STATE_KEY = "douban_web_miss_state"
    _DOUBAN_BLOCK_HOURS = 6
    _DOUBAN_MIN_INTERVAL_SECONDS = 1.2
    _DOUBAN_RATING_STORE_LIMIT = 5000
    _DOUBAN_WEB_MISS_STORE_LIMIT = 5000
    _DOUBAN_WEB_MISS_COOLDOWN_SECONDS = 24 * 3600
    _LIST_RESULT_CACHE_MAX_ENTRIES = 120
    _PREWARM_LIST_METHODS = (
        "tmdb_trending",
        "movie_showing",
        "douban_discover",
        "movie_top250",
        "tv_weekly_chinese",
        "tv_weekly_global",
        "tv_animation",
        "movie_hot",
        "tv_hot",
        "bangumi_calendar",
        "bangumi_discover",
        "bangumi_recommend",
    )
    _DOUBAN_BLOCK_MARKERS = (
        "error code: 004",
        "sec.douban.com",
        "please login and retry",
        "有异常请求从你的ip发出",
        "有异常请求从你的 ip 发出",
    )
    _DIAGNOSTIC_KEEP = 30
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
        self._enable_external_douban = False
        self._external_douban_url_template = ""
        self._douban_web_cookie = ""
        self._enable_douban_web_fallback = True
        self._douban_web_fallback_detail_only = True
        self._douban_web_daily_limit = 100
        self._enable_diagnostics = False
        self._imdb_source = "auto"
        self._imdb_ratings_path = ""
        self._omdb_api_key = ""
        self._max_items = 30
        self._list_enrich_timeout = 6.0
        self._item_enrich_timeout = 8.0
        self._enable_list_result_cache = True
        self._list_cache_ttl_seconds = 900
        self._prewarm_list_cache_on_startup = True
        self._prewarm_list_methods_limit = 12
        self._tmdb_api = TmdbApi()
        self._media_chain = MediaChain()
        self._tmdb_detail_cache: Dict[Tuple[str, int], Optional[dict]] = {}
        self._tmdb_match_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}
        self._imdb_rating_cache: Dict[str, Optional[float]] = {}
        self._douban_info_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}
        self._douban_web_rating_cache: Dict[str, Optional[float]] = {}
        self._douban_blocked_until: float = 0
        self._douban_block_reason: str = ""
        self._douban_rating_store: Dict[str, Dict[str, Any]] = {}
        self._douban_rate_lock = Lock()
        self._douban_next_request_at: float = 0
        self._douban_web_quota_lock = Lock()
        self._douban_web_quota_date: str = ""
        self._douban_web_quota_count: int = 0
        self._douban_web_miss_state: Dict[str, int] = {}
        self._list_cache_lock = Lock()
        self._list_result_cache: Dict[str, Dict[str, Any]] = {}
        self._list_prewarm_thread: Optional[Thread] = None
        self._list_prewarm_running = False
        self._imdb_blocked_until: float = 0
        self._imdb_block_reason: str = ""
        self._imdb_dataset_status: str = "未启用"
        self._imdb_dataset_meta: Dict[str, Any] = {}
        self._imdb_dataset_lock = Lock()
        self._imdb_dataset_building = False
        self._imdb_dataset_build_thread: Optional[Thread] = None
        self._imdb_dataset_build_error = ""
        self._external_douban_status = "未启用"
        self._diagnostic_records: List[Dict[str, Any]] = []

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        return {
            "enable": True,
            "enable_imdb": True,
            "enable_douban": True,
            "enable_external_douban": False,
            "external_douban_url_template": "",
            "douban_web_cookie": "",
            "enable_douban_web_fallback": True,
            "douban_web_fallback_detail_only": True,
            "douban_web_daily_limit": 100,
            "enable_diagnostics": False,
            "imdb_source": "auto",
            "imdb_ratings_path": "",
            "omdb_api_key": "",
            "max_items": 30,
            "list_enrich_timeout": 6,
            "item_enrich_timeout": 8,
            "enable_list_result_cache": True,
            "list_cache_ttl_seconds": 900,
            "prewarm_list_cache_on_startup": True,
            "prewarm_list_methods_limit": 12,
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
        self._enable_external_douban = bool(conf.get("enable_external_douban"))
        self._external_douban_url_template = str(conf.get("external_douban_url_template") or "").strip()
        self._douban_web_cookie = str(conf.get("douban_web_cookie") or "").strip()
        self._enable_douban_web_fallback = bool(conf.get("enable_douban_web_fallback", True))
        self._douban_web_fallback_detail_only = bool(conf.get("douban_web_fallback_detail_only", True))
        try:
            self._douban_web_daily_limit = max(0, int(conf.get("douban_web_daily_limit", 100) or 0))
        except (TypeError, ValueError):
            self._douban_web_daily_limit = 100
        self._enable_diagnostics = bool(conf.get("enable_diagnostics"))
        self._imdb_source = str(conf.get("imdb_source") or "auto").strip().lower()
        self._imdb_ratings_path = str(conf.get("imdb_ratings_path") or "").strip()
        self._omdb_api_key = (conf.get("omdb_api_key") or "").strip()
        try:
            self._max_items = max(1, min(int(conf.get("max_items") or 30), 50))
        except (TypeError, ValueError):
            self._max_items = 30
        try:
            self._list_enrich_timeout = max(1.0, min(float(conf.get("list_enrich_timeout") or 6), 20.0))
        except (TypeError, ValueError):
            self._list_enrich_timeout = 6.0
        try:
            self._item_enrich_timeout = max(1.0, min(float(conf.get("item_enrich_timeout") or 8), 30.0))
        except (TypeError, ValueError):
            self._item_enrich_timeout = 8.0
        self._enable_list_result_cache = bool(conf.get("enable_list_result_cache", True))
        try:
            self._list_cache_ttl_seconds = max(60, min(int(conf.get("list_cache_ttl_seconds") or 900), 24 * 3600))
        except (TypeError, ValueError):
            self._list_cache_ttl_seconds = 900
        self._prewarm_list_cache_on_startup = bool(conf.get("prewarm_list_cache_on_startup", True))
        try:
            self._prewarm_list_methods_limit = max(1, min(int(conf.get("prewarm_list_methods_limit") or 12), len(self._PREWARM_LIST_METHODS)))
        except (TypeError, ValueError):
            self._prewarm_list_methods_limit = 12
        self._reset_runtime_cache()
        self._load_douban_block_state()
        self._load_douban_rating_store()
        self._load_douban_web_quota_state()
        self._load_douban_web_miss_state()
        self._load_omdb_block_state()
        self._refresh_imdb_dataset_status()
        self._schedule_imdb_dataset_index_build()
        self._trigger_recommend_cache_clear()
        self._schedule_list_cache_prewarm()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/imdb/status",
                "endpoint": self.api_imdb_status,
                "methods": ["GET"],
                "summary": "获取 IMDb 数据集状态",
                "description": "返回 IMDb 数据集索引和 OMDb 熔断状态",
            },
            {
                "path": "/imdb/rebuild",
                "endpoint": self.api_imdb_rebuild,
                "methods": ["POST"],
                "summary": "重建 IMDb 数据集索引",
                "description": "后台重建 IMDb title.ratings 数据集索引",
            },
            {
                "path": "/imdb/unblock",
                "endpoint": self.api_imdb_unblock,
                "methods": ["POST"],
                "summary": "清除 IMDb 熔断状态",
                "description": "手动清除 OMDb 限流熔断状态，并清空 IMDb 评分内存缓存",
            },
        ]

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

    def get_actions(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        return [
            {
                "id": "filter_medias_keywords",
                "action_id": "filter_medias_keywords",
                "name": "过滤媒体关键词",
                "func": MultiRatingsRecommend.action_filter_medias_keywords,
            }
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
                "text": (
                    "插件会统一改写推荐页、搜索结果、媒体详情和工作流中的评分。"
                    "卡片右上角优先显示 豆瓣 / TMDB 的低分，两者都缺失时回退 IMDb，再回退 Bangumi；"
                    "详情页会在简介上方显示各平台评分串。IMDb 支持本地数据集优先模式；"
                    "豆瓣支持额外配置外部详情 API 兜底。"
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
                "component": "VSwitch",
                "props": {
                    "model": "enable_external_douban",
                    "label": "启用外部豆瓣详情 API 兜底",
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_douban }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "external_douban_url_template",
                    "label": "外部豆瓣详情 URL 模板",
                    "placeholder": "例如：http://127.0.0.1:8080/douban/subject/{douban_id}",
                    "clearable": True,
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_douban || !enable_external_douban }}",
                    "hint": "支持变量：{douban_id} {media_type} {title} {year}",
                    "persistent-hint": True,
                },
            },
            {
                "component": "VTextarea",
                "props": {
                    "model": "douban_web_cookie",
                    "label": "豆瓣网页 Cookie（可选）",
                    "placeholder": "例如：bid=...; dbcl2=...; ck=...",
                    "rows": 2,
                    "auto-grow": True,
                    "clearable": True,
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_douban }}",
                    "hint": "用于豆瓣网页评分兜底（error code:004 时需要登录态 Cookie）",
                    "persistent-hint": True,
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable_douban_web_fallback",
                    "label": "启用豆瓣网页评分兜底",
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_douban }}",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "douban_web_fallback_detail_only",
                    "label": "仅详情页触发豆瓣网页兜底",
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_douban || !enable_douban_web_fallback }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "douban_web_daily_limit",
                    "label": "豆瓣网页兜底每日请求上限（0=不限）",
                    "type": "number",
                    "min": 0,
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_douban || !enable_douban_web_fallback }}",
                    "hint": "建议 50~200，降低风控风险",
                    "persistent-hint": True,
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
            {
                "component": "VTextField",
                "props": {
                    "model": "list_enrich_timeout",
                    "label": "列表补分单条超时秒数",
                    "type": "number",
                    "min": 1,
                    "max": 20,
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                    "hint": "超时自动降级为原始评分，避免整行卡住",
                    "persistent-hint": True,
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "item_enrich_timeout",
                    "label": "详情补分超时秒数",
                    "type": "number",
                    "min": 1,
                    "max": 30,
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                    "hint": "超时自动降级为原始详情，避免详情页空白",
                    "persistent-hint": True,
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable_list_result_cache",
                    "label": "启用推荐分类服务端缓存",
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "list_cache_ttl_seconds",
                    "label": "推荐分类缓存 TTL 秒数",
                    "type": "number",
                    "min": 60,
                    "max": 86400,
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_list_result_cache }}",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "prewarm_list_cache_on_startup",
                    "label": "启动时预热推荐分类缓存",
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_list_result_cache }}",
                },
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "prewarm_list_methods_limit",
                    "label": "启动预热分类数量",
                    "type": "number",
                    "min": 1,
                    "max": len(self._PREWARM_LIST_METHODS),
                    "class": "mb-2",
                    "disabled": "{{ !enable || !enable_list_result_cache || !prewarm_list_cache_on_startup }}",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "enable_diagnostics",
                    "label": "启用补分诊断记录",
                    "class": "mb-2",
                    "disabled": "{{ !enable }}",
                },
            },
        ], self._default_config()

    def get_page(self) -> Optional[List[dict]]:
        self._sync_douban_web_quota_date()
        page = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                },
                "text": (
                    f"当前状态：{'已启用' if self._enabled else '未启用'}；"
                    f"豆瓣：{'参与计算' if self._enable_douban else '不参与'}；"
                    f"外部豆瓣：{self._external_douban_status}；"
                    f"IMDb：{'参与计算' if self._enable_imdb else '不参与'}；"
                    f"IMDb 来源：{self._imdb_source}；"
                    f"最大补分条数：{self._max_items}；"
                    f"单条超时：{self._list_enrich_timeout:.1f}s；"
                    f"详情超时：{self._item_enrich_timeout:.1f}s；"
                    f"列表并发：{self._LIST_ENRICH_CONCURRENCY}；"
                    f"主评分策略：豆瓣 / TMDB 取低分，缺失时依次回退 IMDb、Bangumi"
                    + (
                        f"；分类缓存：{len(self._list_result_cache)} 条，TTL {self._list_cache_ttl_seconds}s"
                        if self._enable_list_result_cache
                        else "；分类缓存：关闭"
                    )
                    + (f"；豆瓣状态：{self._douban_block_reason}" if self._douban_blocked_until > time.time() else "")
                    + (
                        "；豆瓣网页兜底：关闭"
                        if not self._enable_douban_web_fallback
                        else (
                            f"；豆瓣网页兜底：{'仅详情页' if self._douban_web_fallback_detail_only else '详情+列表'}，"
                            f"今日 {self._douban_web_quota_count}/{self._douban_web_daily_limit if self._douban_web_daily_limit else '∞'}"
                        )
                    )
                    + (f"；IMDb 数据集：{self._imdb_dataset_status}" if self._enable_imdb else "")
                    + (f"；OMDb 状态：{self._imdb_block_reason}" if self._imdb_blocked_until > time.time() else "")
                ),
            }
        ]
        if self._enable_diagnostics:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "class": "mt-3",
                    },
                    "text": self._diagnostic_summary_text(),
                }
            )
        return page

    @staticmethod
    def action_filter_medias_keywords(
        context: Any,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
    ) -> Tuple[bool, Any]:
        """
        插件工作流动作：按关键词过滤 context.medias。
        - include: 命中才保留
        - exclude: 命中就剔除
        """
        medias = list(getattr(context, "medias", []) or [])
        if not medias:
            return True, context

        include_re = None
        exclude_re = None
        include_pattern = str(include or "").strip()
        exclude_pattern = str(exclude or "").strip()
        if include_pattern:
            try:
                include_re = re.compile(include_pattern, re.I)
            except re.error:
                include_re = re.compile(re.escape(include_pattern), re.I)
                logger.warn(f"媒体关键词过滤 include 非法正则，已降级字面匹配：{include_pattern}")
        if exclude_pattern:
            try:
                exclude_re = re.compile(exclude_pattern, re.I)
            except re.error:
                exclude_re = re.compile(re.escape(exclude_pattern), re.I)
                logger.warn(f"媒体关键词过滤 exclude 非法正则，已降级字面匹配：{exclude_pattern}")

        kept: List[Any] = []
        for media in medias:
            searchable = MultiRatingsRecommend._build_media_keyword_text(media)
            if include_re and not include_re.search(searchable):
                continue
            if exclude_re and exclude_re.search(searchable):
                continue
            kept.append(media)

        context.medias = kept
        logger.info(f"媒体关键词过滤后剩余 {len(kept)} 条（原始 {len(medias)} 条）")
        return True, context

    def api_imdb_status(self) -> Dict[str, Any]:
        return self._get_imdb_status_payload()

    def api_imdb_rebuild(self) -> Dict[str, Any]:
        started = self._schedule_imdb_dataset_index_build(force=True)
        return {
            "success": started or self._imdb_dataset_building,
            "started": started,
            "status": self._get_imdb_status_payload(),
        }

    def api_imdb_unblock(self) -> Dict[str, Any]:
        self._clear_omdb_block_state()
        self._imdb_rating_cache.clear()
        return {
            "success": True,
            "status": self._get_imdb_status_payload(),
        }

    def stop_service(self):
        self._reset_runtime_cache()
        self._tmdb_api.close()
        self._trigger_recommend_cache_clear()

    def _reset_runtime_cache(self):
        self._tmdb_detail_cache.clear()
        self._tmdb_match_cache.clear()
        self._imdb_rating_cache.clear()
        self._douban_info_cache.clear()
        self._douban_web_rating_cache.clear()
        self._douban_blocked_until = 0
        self._douban_block_reason = ""
        self._douban_next_request_at = 0
        self._douban_web_quota_date = ""
        self._douban_web_quota_count = 0
        self._clear_list_result_cache()
        self._list_prewarm_running = False
        self._list_prewarm_thread = None
        self._imdb_blocked_until = 0
        self._imdb_block_reason = ""
        self._imdb_dataset_meta = {}
        self._imdb_dataset_status = "未启用"
        self._imdb_dataset_building = False
        self._imdb_dataset_build_thread = None
        self._imdb_dataset_build_error = ""
        self._external_douban_status = "已配置" if self._enable_external_douban and self._external_douban_url_template else (
            "未配置" if self._enable_external_douban else "未启用"
        )
        self._diagnostic_records.clear()

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

    @staticmethod
    def _normalize_cache_scalar(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, MediaType):
            return value.value
        if isinstance(value, (list, tuple)):
            return tuple(MultiRatingsRecommend._normalize_cache_scalar(item) for item in value)
        if isinstance(value, dict):
            return {
                str(key): MultiRatingsRecommend._normalize_cache_scalar(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        return str(value)

    def _build_list_cache_key(self, method: str, args: Sequence[Any], kwargs: Dict[str, Any]) -> str:
        normalized_kwargs = dict(kwargs or {})
        if "page" not in normalized_kwargs:
            normalized_kwargs["page"] = 1
        payload = {
            "method": method,
            "args": [self._normalize_cache_scalar(item) for item in (args or [])],
            "kwargs": {
                str(key): self._normalize_cache_scalar(value)
                for key, value in sorted(normalized_kwargs.items(), key=lambda pair: str(pair[0]))
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _serialize_media_items(items: Sequence[MediaInfo]) -> List[dict]:
        result: List[dict] = []
        for media in items or []:
            if isinstance(media, MediaInfo):
                result.append(media.to_dict())
        return result

    @staticmethod
    def _deserialize_media_items(payload: Sequence[dict]) -> Tuple[MediaInfo, ...]:
        items: List[MediaInfo] = []
        for data in payload or []:
            if not isinstance(data, dict):
                continue
            media = MediaInfo()
            media.from_dict(data)
            items.append(media)
        return tuple(items)

    def _clear_list_result_cache(self):
        with self._list_cache_lock:
            self._list_result_cache.clear()

    def _get_list_result_cache(self, cache_key: str) -> Optional[Tuple[MediaInfo, ...]]:
        if not self._enable_list_result_cache:
            return None
        now = time.time()
        with self._list_cache_lock:
            entry = self._list_result_cache.get(cache_key)
            if not entry:
                return None
            ts = float(entry.get("ts") or 0)
            if now - ts > self._list_cache_ttl_seconds:
                self._list_result_cache.pop(cache_key, None)
                return None
            payload = entry.get("items") or []
        return self._deserialize_media_items(payload)

    def _set_list_result_cache(self, cache_key: str, items: Sequence[MediaInfo]):
        if not self._enable_list_result_cache:
            return
        payload = self._serialize_media_items(items)
        with self._list_cache_lock:
            self._list_result_cache[cache_key] = {
                "ts": time.time(),
                "items": payload,
            }
            if len(self._list_result_cache) > self._LIST_RESULT_CACHE_MAX_ENTRIES:
                oldest = min(self._list_result_cache.items(), key=lambda item: float(item[1].get("ts") or 0))[0]
                self._list_result_cache.pop(oldest, None)

    def _schedule_list_cache_prewarm(self):
        if not self._enabled or not self._enable_list_result_cache or not self._prewarm_list_cache_on_startup:
            return
        if self._list_prewarm_thread and self._list_prewarm_thread.is_alive():
            return
        self._list_prewarm_running = True
        thread = Thread(
            target=self._run_list_cache_prewarm,
            daemon=True,
            name="mp-list-cache-prewarm",
        )
        self._list_prewarm_thread = thread
        thread.start()

    def _run_list_cache_prewarm(self):
        try:
            asyncio.run(self._prewarm_list_cache())
        except Exception as err:
            logger.warn(f"推荐分类缓存预热失败：{err}")
        finally:
            self._list_prewarm_running = False

    async def _prewarm_list_cache(self):
        selected_methods = self._PREWARM_LIST_METHODS[: self._prewarm_list_methods_limit]
        for method in selected_methods:
            try:
                medias = await self._async_call_system_method_excluding_self(method)
                result = await self._build_result(medias)
                cache_key = self._build_list_cache_key(method, (), {})
                self._set_list_result_cache(cache_key, result)
            except Exception as err:
                logger.warn(f"预热分类 {method} 失败：{err}")

    def _handle_sync_media_item(self, method: str, *args, **kwargs):
        media = self._call_system_method(method, *args, **kwargs)
        if self._is_missing_media(media):
            media = self._run_async(self._fallback_media_item(media, **kwargs))
        if self._is_missing_media(media):
            return media
        original = self._clone_media(media)
        try:
            return self._run_async(
                asyncio.wait_for(
                    self._enrich_media(self._clone_media(media), enrich_context="item"),
                    timeout=self._item_enrich_timeout,
                )
            )
        except asyncio.TimeoutError:
            logger.warn(f"详情补分超时，降级原始详情：{original.title or original.tmdb_id or method}")
        except Exception as err:
            logger.warn(f"详情补分失败，降级原始详情：{original.title or original.tmdb_id or method} - {err}")
        original.overview = self._strip_rating_overview(original.overview)
        original.tagline = self._fallback_rating_tagline(original)
        return original

    async def _handle_async_media_item(self, method: str, *args, **kwargs):
        media = await self._async_call_system_method(method, *args, **kwargs)
        if self._is_missing_media(media):
            media = await self._fallback_media_item(media, **kwargs)
        if self._is_missing_media(media):
            return media
        original = self._clone_media(media)
        try:
            return await asyncio.wait_for(
                self._enrich_media(self._clone_media(media), enrich_context="item"),
                timeout=self._item_enrich_timeout,
            )
        except asyncio.TimeoutError:
            logger.warn(f"详情补分超时，降级原始详情：{original.title or original.tmdb_id or method}")
        except Exception as err:
            logger.warn(f"详情补分失败，降级原始详情：{original.title or original.tmdb_id or method} - {err}")
        original.overview = self._strip_rating_overview(original.overview)
        original.tagline = self._fallback_rating_tagline(original)
        return original

    def _handle_sync_media_list(self, method: str, *args, **kwargs):
        cache_key = self._build_list_cache_key(method, args, kwargs)
        cached_result = self._get_list_result_cache(cache_key)
        if cached_result is not None:
            return cached_result
        medias = self._call_system_method(method, *args, **kwargs)
        result = self._run_async(self._build_result(medias))
        self._set_list_result_cache(cache_key, result)
        return result

    async def _handle_async_media_list(self, method: str, *args, **kwargs):
        cache_key = self._build_list_cache_key(method, args, kwargs)
        cached_result = self._get_list_result_cache(cache_key)
        if cached_result is not None:
            return cached_result
        medias = await self._async_call_system_method(method, *args, **kwargs)
        result = await self._build_result(medias)
        self._set_list_result_cache(cache_key, result)
        return result

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

    async def _async_call_system_method_excluding_self(self, method: str, *args, **kwargs):
        result = None
        modules = sorted(self.chain.modulemanager.get_running_modules(method), key=lambda module: module.get_priority())
        for module in modules:
            if module is self:
                continue
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
            semaphore = asyncio.Semaphore(min(self._LIST_ENRICH_CONCURRENCY, target_count))

            async def _enrich_with_limit(index: int) -> MediaInfo:
                async with semaphore:
                    original = items[index]
                    try:
                        return await asyncio.wait_for(
                            self._enrich_media(original, enrich_context="list"),
                            timeout=self._list_enrich_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warn(f"列表补分超时，降级原始评分：{original.title or original.tmdb_id or index}")
                    except Exception as err:
                        logger.warn(f"列表补分失败，降级原始评分：{original.title or original.tmdb_id or index} - {err}")
                    original.overview = self._strip_rating_overview(original.overview)
                    original.tagline = self._fallback_rating_tagline(original)
                    return original

            enriched_items = await asyncio.gather(*(_enrich_with_limit(index) for index in range(target_count)))
            items[:target_count] = enriched_items
        for index in range(target_count, len(items)):
            items[index].overview = self._strip_rating_overview(items[index].overview)
        return tuple(items)

    async def _enrich_media(self, media: MediaInfo, enrich_context: str = "item") -> MediaInfo:
        media.overview = self._strip_rating_overview(media.overview)
        media.tagline = ""

        ratings: Dict[str, float] = {}
        diagnostic_notes: List[str] = []
        source_label = self._source_label(media)
        current_rating = self._normalize_rating(media.vote_average)
        if source_label and current_rating is not None:
            ratings[source_label] = current_rating
            diagnostic_notes.append(f"初始评分：{source_label} {current_rating:.1f}")
            if source_label == "豆瓣" and media.douban_id:
                self._remember_douban_rating(str(media.douban_id), current_rating, "初始评分")

        tmdb_detail = await self._resolve_tmdb_detail(media)
        if tmdb_detail:
            tmdb_rating = self._normalize_rating(tmdb_detail.get("vote_average"))
            if tmdb_rating is not None:
                ratings["TMDB"] = tmdb_rating
                diagnostic_notes.append(f"TMDB：{tmdb_rating:.1f}")
            imdb_id = self._extract_imdb_id(tmdb_detail)
            if imdb_id:
                media.imdb_id = imdb_id
        else:
            diagnostic_notes.append("TMDB：未补到详情")

        douban_info = None
        if self._enable_douban:
            douban_timeout = (
                self._DOUBAN_ENRICH_TIMEOUT_ITEM
                if str(enrich_context or "").strip().lower() == "item"
                else self._DOUBAN_ENRICH_TIMEOUT_LIST
            )
            try:
                douban_info = await asyncio.wait_for(
                    self._resolve_douban_info(media, enrich_context=enrich_context),
                    timeout=douban_timeout,
                )
            except asyncio.TimeoutError:
                diagnostic_notes.append(f"豆瓣：解析超时（>{douban_timeout:.1f}s），跳过")
                douban_info = None
            if douban_info:
                douban_id = douban_info.get("id")
                if douban_id:
                    media.douban_id = str(douban_id)
                imdb_id_from_douban = self._extract_douban_imdb_id(douban_info)
                if imdb_id_from_douban and not media.imdb_id:
                    media.imdb_id = imdb_id_from_douban
                    diagnostic_notes.append(f"IMDb ID：来自豆瓣 {imdb_id_from_douban}")
                douban_rating = self._extract_douban_rating(douban_info)
                if douban_rating is not None:
                    ratings["豆瓣"] = douban_rating
                    diagnostic_notes.append(
                        f"豆瓣：{douban_rating:.1f}（{douban_info.get('__mr_source') or '内置'}）"
                    )
                else:
                    diagnostic_notes.append(
                        f"豆瓣：已命中 {douban_info.get('__mr_source') or '内置'}，但无评分"
                    )
            else:
                diagnostic_notes.append("豆瓣：未匹配到")

        if self._enable_imdb and media.imdb_id:
            imdb_rating = await self._get_imdb_rating(media.imdb_id)
            if imdb_rating is not None:
                ratings["IMDb"] = imdb_rating
                diagnostic_notes.append(f"IMDb：{imdb_rating:.1f}")
            elif self._imdb_blocked_until > time.time():
                diagnostic_notes.append("IMDb：OMDb 限额熔断中")
            elif self._imdb_source in {"auto", "dataset"} and self._imdb_dataset_status in {"未配置数据集路径", "数据集路径不存在"}:
                diagnostic_notes.append(f"IMDb：数据集不可用（{self._imdb_dataset_status}）")
            else:
                diagnostic_notes.append("IMDb：未命中")
        elif self._enable_imdb:
            diagnostic_notes.append("IMDb：无 imdb_id")

        bangumi_rating = self._extract_bangumi_media_rating(
            media,
            fallback_rating=current_rating if source_label == "Bangumi" else None,
        )
        if bangumi_rating is not None and ratings.get("Bangumi") is None:
            ratings["Bangumi"] = bangumi_rating
            diagnostic_notes.append(f"Bangumi：{bangumi_rating:.1f}")

        if not ratings:
            self._record_diagnostic(media, ratings, None, diagnostic_notes)
            return media

        primary_rating = self._select_primary_rating(ratings)
        display_ratings = self._display_ratings(ratings)
        if primary_rating is not None:
            media.vote_average = primary_rating
        media.tagline = self._merge_rating_tagline(display_ratings)
        if primary_rating is not None:
            diagnostic_notes.append(f"主评分：{primary_rating:.1f}")
        self._record_diagnostic(media, ratings, primary_rating, diagnostic_notes)
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
            douban_info = await self._get_douban_info_by_id(str(douban_id), media_type, current)
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

    async def _resolve_douban_info(self, media: MediaInfo, enrich_context: str = "item") -> Optional[dict]:
        media_type = self._get_media_type(media.type)
        allow_web_fallback = self._should_use_douban_web_fallback(enrich_context=enrich_context)
        douban_info = None
        if media.douban_id:
            douban_info = await self._get_douban_info_by_id(
                media.douban_id,
                media_type,
                media,
                allow_web_fallback=allow_web_fallback,
                enrich_context=enrich_context,
            )
            if douban_info and self._extract_douban_rating(douban_info) is not None:
                return douban_info

        if media.tmdb_id:
            douban_info = self._prefer_douban_info(
                douban_info,
                await self._get_doubaninfo_by_tmdbid(media.tmdb_id, media_type),
            )
        elif media.bangumi_id:
            douban_info = self._prefer_douban_info(
                douban_info,
                await self._get_doubaninfo_by_bangumiid(media.bangumi_id),
            )
        if (not douban_info or self._extract_douban_rating(douban_info) is None) and (
            media.imdb_id or self._candidate_titles(media)
        ):
            douban_info = self._prefer_douban_info(
                douban_info,
                await self._match_douban_info(
                    media,
                    media.imdb_id,
                    allow_web_fallback=allow_web_fallback,
                    enrich_context=enrich_context,
                ),
            )
        douban_id = douban_info.get("id") if douban_info else None
        if douban_id and self._extract_douban_rating(douban_info) is None:
            detail = await self._get_douban_info_by_id(
                str(douban_id),
                media_type,
                media,
                allow_web_fallback=allow_web_fallback,
                enrich_context=enrich_context,
            )
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
        names = self._candidate_titles(media)
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
        if self._is_douban_blocked():
            self._douban_info_cache[cache_key] = None
            return None
        info = self._annotate_douban_info(
            await self._call_douban_service(
                f"豆瓣TMDB映射:{tmdb_id}",
                lambda: self._media_chain.async_get_doubaninfo_by_tmdbid(tmdbid=int(tmdb_id), mtype=media_type),
            ),
            "TMDB映射",
        )
        info = self._enrich_with_cached_douban_rating(info)
        self._remember_douban_rating_from_info(info)
        self._douban_info_cache[cache_key] = info
        return info

    async def _get_doubaninfo_by_bangumiid(self, bangumi_id: int) -> Optional[dict]:
        cache_key = ("bangumi", str(bangumi_id), "")
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        if self._is_douban_blocked():
            self._douban_info_cache[cache_key] = None
            return None
        info = self._annotate_douban_info(
            await self._call_douban_service(
                f"豆瓣Bangumi映射:{bangumi_id}",
                lambda: self._media_chain.async_get_doubaninfo_by_bangumiid(bangumiid=int(bangumi_id)),
            ),
            "Bangumi映射",
        )
        info = self._enrich_with_cached_douban_rating(info)
        self._remember_douban_rating_from_info(info)
        self._douban_info_cache[cache_key] = info
        return info

    async def _get_douban_info_by_id(
        self,
        douban_id: str,
        media_type: Optional[MediaType],
        media: Optional[MediaInfo] = None,
        allow_web_fallback: bool = True,
        enrich_context: str = "item",
    ) -> Optional[dict]:
        cache_key = ("id", str(douban_id), media_type.value if media_type else "")
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        cached_info = self._build_cached_douban_info(str(douban_id))
        info = cached_info

        if not self._is_douban_blocked():
            detail_info = self._annotate_douban_info(
                await self._call_douban_service(
                    f"豆瓣详情:{douban_id}",
                    lambda: self.chain.async_douban_info(doubanid=str(douban_id), mtype=media_type, raise_exception=False),
                ),
                "内置详情",
            )
            info = self._prefer_douban_info(info, detail_info)
            if not detail_info and media_type:
                fallback_key = ("id", str(douban_id), "")
                if fallback_key in self._douban_info_cache:
                    info = self._prefer_douban_info(info, self._douban_info_cache[fallback_key])
                else:
                    fallback_info = self._annotate_douban_info(
                        await self._call_douban_service(
                            f"豆瓣详情(无类型):{douban_id}",
                            lambda: self.chain.async_douban_info(doubanid=str(douban_id), mtype=None, raise_exception=False),
                        ),
                        "内置详情",
                    )
                    self._douban_info_cache[fallback_key] = fallback_info
                    info = self._prefer_douban_info(info, fallback_info)

            if not info or self._extract_douban_rating(info) is None:
                recognized = await self._async_call_system_method_excluding_self(
                    "async_recognize_media",
                    doubanid=str(douban_id),
                    mtype=media_type,
                    cache=False,
                )
                recognized_info = self._annotate_douban_info(
                    getattr(recognized, "douban_info", None) if recognized else None,
                    "豆瓣识别",
                )
                info = self._prefer_douban_info(info, recognized_info)

            if (not info or self._extract_douban_rating(info) is None) and allow_web_fallback:
                web_rating = await self._get_douban_web_rating_by_id(
                    str(douban_id),
                    enrich_context=enrich_context,
                )
                if web_rating is not None:
                    base = dict(info or {})
                    base["id"] = str(base.get("id") or douban_id)
                    rating = base.get("rating") if isinstance(base.get("rating"), dict) else {}
                    rating["value"] = web_rating
                    base["rating"] = rating
                    info = self._annotate_douban_info(base, "豆瓣网页")
        if (not info or self._extract_douban_rating(info) is None) and cached_info:
            info = self._prefer_douban_info(info, cached_info)
        if (not info or self._extract_douban_rating(info) is None) and self._enable_external_douban:
            external_info = await self._get_external_douban_info_by_id(str(douban_id), media_type, media)
            info = self._prefer_douban_info(info, external_info)
        if info and not info.get("id"):
            info["id"] = str(douban_id)
        self._remember_douban_rating_from_info(info)
        self._douban_info_cache[cache_key] = info
        return info

    async def _get_douban_web_rating_by_id(self, douban_id: str, enrich_context: str = "item") -> Optional[float]:
        douban_id = str(douban_id or "").strip()
        if not douban_id:
            return None
        if douban_id in self._douban_web_rating_cache:
            return self._douban_web_rating_cache[douban_id]
        cached_info = self._build_cached_douban_info(douban_id)
        cached_rating = self._extract_douban_rating(cached_info)
        if cached_rating is not None:
            self._douban_web_rating_cache[douban_id] = cached_rating
            return cached_rating
        if self._is_recent_douban_web_miss(douban_id):
            self._douban_web_rating_cache[douban_id] = None
            return None
        if not self._should_use_douban_web_fallback(enrich_context=enrich_context):
            self._douban_web_rating_cache[douban_id] = cached_rating
            return cached_rating
        if self._is_douban_blocked():
            self._douban_web_rating_cache[douban_id] = cached_rating
            return cached_rating
        if not self._try_consume_douban_web_quota():
            self._douban_web_rating_cache[douban_id] = cached_rating
            return cached_rating
        await self._wait_douban_slot()
        url = f"https://movie.douban.com/subject/{douban_id}/"
        try:
            html = await AsyncRequestUtils(
                timeout=8,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://movie.douban.com/",
                },
                cookies=self._douban_web_cookie or None,
            ).get(url)
            if not html:
                if cached_rating is not None:
                    self._douban_web_rating_cache[douban_id] = cached_rating
                    return cached_rating
                self._mark_douban_web_miss(douban_id)
                self._douban_web_rating_cache[douban_id] = None
                return None
            if self._is_douban_block_message(html):
                self._trip_douban_block(f"豆瓣网页:{douban_id}")
                self._douban_web_rating_cache[douban_id] = cached_rating
                return cached_rating
            rating = self._extract_douban_rating_from_html(html)
            if rating is not None:
                self._remember_douban_rating(douban_id, rating, "豆瓣网页")
                self._clear_douban_web_miss(douban_id)
            else:
                self._mark_douban_web_miss(douban_id)
            self._douban_web_rating_cache[douban_id] = rating
            return rating
        except Exception as err:
            if self._is_douban_block_message(str(err)):
                self._trip_douban_block(f"豆瓣网页:{douban_id}：{err}")
            else:
                logger.warn(f"豆瓣网页评分获取失败：{douban_id} - {err}")
                self._mark_douban_web_miss(douban_id)
            self._douban_web_rating_cache[douban_id] = cached_rating
            return cached_rating

    @classmethod
    def _extract_douban_rating_from_html(cls, html: str) -> Optional[float]:
        text = str(html or "")
        patterns = (
            r'property="v:average">\\s*([0-9]+(?:\\.[0-9]+)?)\\s*<',
            r'"ratingValue"\\s*:\\s*"([0-9]+(?:\\.[0-9]+)?)"',
            r'class="rating_num"[^>]*>\\s*([0-9]+(?:\\.[0-9]+)?)\\s*<',
        )
        for pattern in patterns:
            matched = re.search(pattern, text, flags=re.IGNORECASE)
            if matched:
                return cls._normalize_rating(matched.group(1))
        return None

    async def _match_douban_info(
        self,
        media: MediaInfo,
        imdb_id: Optional[str],
        allow_web_fallback: bool = True,
        enrich_context: str = "item",
    ) -> Optional[dict]:
        titles = self._candidate_titles(media)
        cache_key = (
            "match",
            imdb_id or "",
            "|".join(titles),
            str(media.year or ""),
            str(media.season or ""),
            self._get_media_type(media.type).value if self._get_media_type(media.type) else "",
        )
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        if self._is_douban_blocked():
            self._douban_info_cache[cache_key] = None
            return None
        info = None
        media_type = self._get_media_type(media.type)
        for title in titles or [media.title or ""]:
            attempts = self._build_douban_match_attempts(
                title=title or media.title or "",
                media_type=media_type,
                year=media.year,
                season=media.season,
                imdb_id=imdb_id,
            )
            for attempt in attempts:
                matched = await self._call_douban_service(
                    f"豆瓣匹配:{attempt['title']}",
                    lambda attempt=attempt: self.chain.async_match_doubaninfo(
                        name=attempt["title"],
                        imdbid=attempt["imdbid"],
                        mtype=attempt["media_type"],
                        year=attempt["year"],
                        season=attempt["season"],
                        raise_exception=False,
                    ),
                )
                matched = self._annotate_douban_info(matched, attempt["source"])
                matched = self._enrich_with_cached_douban_rating(matched)
                douban_id = matched.get("id") if matched else None
                if douban_id:
                    detail = await self._get_douban_info_by_id(
                        str(douban_id),
                        media_type,
                        media,
                        allow_web_fallback=allow_web_fallback,
                        enrich_context=enrich_context,
                    )
                    if detail:
                        if not detail.get("id"):
                            detail["id"] = str(douban_id)
                        matched = self._prefer_douban_info(matched, detail)
                info = self._prefer_douban_info(info, matched)
                self._remember_douban_rating_from_info(info)
                if info and self._extract_douban_rating(info) is not None:
                    self._douban_info_cache[cache_key] = info
                    return info
        self._douban_info_cache[cache_key] = info
        return info

    @staticmethod
    def _build_douban_match_attempts(
        title: str,
        media_type: Optional[MediaType],
        year: Optional[str],
        season: Optional[int],
        imdb_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        attempts: List[Dict[str, Any]] = []
        normalized_title = str(title or "").strip()
        if not normalized_title:
            return attempts

        def add_attempt(
            source: str,
            imdb_value: Optional[str],
            year_value: Optional[str],
            season_value: Optional[int],
        ) -> None:
            key = (normalized_title, imdb_value or "", year_value or "", season_value or 0, media_type.value if media_type else "")
            if any(item.get("_key") == key for item in attempts):
                return
            attempts.append(
                {
                    "_key": key,
                    "title": normalized_title,
                    "imdbid": imdb_value,
                    "media_type": media_type,
                    "year": year_value,
                    "season": season_value,
                    "source": source,
                }
            )

        add_attempt(f"标题匹配:{normalized_title}", imdb_id, year, season)
        add_attempt(f"标题匹配(无IMDb):{normalized_title}", None, year, season)
        add_attempt(f"标题宽松:{normalized_title}", None, year, None)
        add_attempt(f"标题宽松(无年份):{normalized_title}", None, None, None)
        for attempt in attempts:
            attempt.pop("_key", None)
        return attempts

    async def _get_external_douban_info_by_id(
        self,
        douban_id: str,
        media_type: Optional[MediaType],
        media: Optional[MediaInfo] = None,
    ) -> Optional[dict]:
        cache_key = ("external", str(douban_id), media_type.value if media_type else "")
        if cache_key in self._douban_info_cache:
            return self._douban_info_cache[cache_key]
        if not self._enable_external_douban or not self._external_douban_url_template:
            self._external_douban_status = "未配置"
            self._douban_info_cache[cache_key] = None
            return None
        media_type_value = media_type.value if media_type else ""
        try:
            title = media.title if media else ""
            year = media.year if media else ""
            url = self._external_douban_url_template.format(
                douban_id=str(douban_id),
                media_type=media_type_value,
                title=quote_plus(str(title or "")),
                year=quote_plus(str(year or "")),
            )
        except Exception as err:
            self._external_douban_status = f"模板错误：{err}"
            logger.warn(f"外部豆瓣 URL 模板错误：{err}")
            self._douban_info_cache[cache_key] = None
            return None
        try:
            payload = await AsyncRequestUtils(timeout=8).get_json(url)
            info = self._normalize_external_douban_info(payload, douban_id)
            info = self._enrich_with_cached_douban_rating(info)
            if info:
                self._external_douban_status = "已配置且可用"
                self._remember_douban_rating_from_info(info)
                self._douban_info_cache[cache_key] = info
                return info
            self._external_douban_status = "已配置但未返回有效详情"
        except Exception as err:
            self._external_douban_status = f"最近错误：{err}"
            logger.warn(f"外部豆瓣详情请求失败：{douban_id} - {err}")
        self._douban_info_cache[cache_key] = None
        return None

    def _normalize_external_douban_info(self, payload: Any, douban_id: str) -> Optional[dict]:
        data = payload
        visited = set()
        while isinstance(data, dict):
            next_data = None
            for key in ("data", "result", "subject", "item"):
                value = data.get(key)
                if value and id(value) not in visited:
                    next_data = value
                    visited.add(id(value))
                    break
            if next_data is None:
                break
            data = next_data
        if isinstance(data, list):
            if not data:
                return None
            rated_item = None
            for item in data:
                normalized = self._normalize_external_douban_info(item, douban_id)
                if normalized and self._extract_douban_rating(normalized) is not None:
                    rated_item = normalized
                    break
            if rated_item:
                return rated_item
            data = data[0]
        if not isinstance(data, dict):
            return None
        info = dict(data)
        if not info.get("id"):
            info["id"] = str(info.get("douban_id") or douban_id)
        rating = info.get("rating")
        if isinstance(rating, (int, float, str)):
            normalized = self._normalize_rating(rating)
            info["rating"] = {"value": normalized} if normalized is not None else {}
        elif isinstance(rating, dict):
            if "value" not in rating:
                for key in ("average", "score", "rating"):
                    normalized = self._normalize_rating(rating.get(key))
                    if normalized is not None:
                        info["rating"] = {"value": normalized}
                        break
        else:
            for key in ("score", "average_rating", "average"):
                normalized = self._normalize_rating(info.get(key))
                if normalized is not None:
                    info["rating"] = {"value": normalized}
                    break
        if not info.get("title") and info.get("name"):
            info["title"] = info.get("name")
        if not info.get("original_title") and info.get("original_name"):
            info["original_title"] = info.get("original_name")
        if not info.get("intro") and info.get("summary"):
            info["intro"] = info.get("summary")
        if not info.get("pubdate") and info.get("release_date"):
            info["pubdate"] = [info.get("release_date")]
        if not info.get("countries") and info.get("country"):
            country = info.get("country")
            info["countries"] = country if isinstance(country, list) else [country]
        if not info.get("genres") and info.get("genre"):
            genre = info.get("genre")
            info["genres"] = genre if isinstance(genre, list) else [genre]
        if not info.get("durations") and info.get("duration"):
            duration = info.get("duration")
            info["durations"] = duration if isinstance(duration, list) else [duration]
        return self._annotate_douban_info(info, "外部API")

    async def _get_imdb_rating(self, imdb_id: str) -> Optional[float]:
        if not imdb_id:
            return None
        if self._imdb_blocked_until and self._imdb_blocked_until <= time.time():
            self._clear_omdb_block_state()
        if imdb_id in self._imdb_rating_cache:
            return self._imdb_rating_cache[imdb_id]
        if self._imdb_source in {"auto", "dataset"}:
            dataset_rating = await self._get_imdb_rating_from_dataset(imdb_id)
            if dataset_rating is not None:
                self._imdb_rating_cache[imdb_id] = dataset_rating
                return dataset_rating
            if self._imdb_source == "dataset":
                return None
        if not self._omdb_api_key:
            return None
        if self._imdb_blocked_until > time.time():
            return None
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
            if rating is not None:
                self._imdb_rating_cache[imdb_id] = rating
                return rating
        elif data and data.get("Error"):
            error_message = str(data.get("Error"))
            if "limit" in error_message.lower():
                self._set_omdb_block_state(time.time() + 12 * 3600, error_message)
                logger.warn(f"IMDb 评分接口已触发限额熔断：{error_message}")
        # Do not cache OMDb misses/limit errors as None.
        # This avoids stale miss cache keeping IMDb score empty after quota recovers.
        return rating

    async def _get_imdb_rating_from_dataset(self, imdb_id: str) -> Optional[float]:
        if not imdb_id:
            return None
        if imdb_id in self._imdb_rating_cache:
            return self._imdb_rating_cache[imdb_id]
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
            db_path = self._get_imdb_dataset_db_path()
            meta = self._load_imdb_dataset_meta()
            if db_path.exists() and meta and self._is_imdb_dataset_meta_current(meta, dataset_path):
                self._imdb_dataset_meta = meta
                self._imdb_dataset_status = f"已索引 {meta.get('record_count', 0)} 条"
            else:
                if self._imdb_dataset_building:
                    self._imdb_dataset_status = "正在建立索引"
                    return None
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
                rating = self._normalize_rating(row[0])
                self._imdb_rating_cache[imdb_id] = rating
                return rating
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

    def _schedule_imdb_dataset_index_build(self, force: bool = False) -> bool:
        if not self._enable_imdb or self._imdb_source == "omdb":
            return False
        source_path = self._get_imdb_dataset_source_path()
        if not source_path:
            return False
        meta = self._load_imdb_dataset_meta()
        if not force and meta and self._is_imdb_dataset_meta_current(meta, source_path):
            self._imdb_dataset_meta = meta
            self._imdb_dataset_status = f"已索引 {meta.get('record_count', 0)} 条"
            return False
        if self._imdb_dataset_building and self._imdb_dataset_build_thread and self._imdb_dataset_build_thread.is_alive():
            return False
        self._imdb_dataset_building = True
        self._imdb_dataset_build_error = ""
        self._imdb_dataset_status = "正在建立索引"
        thread = Thread(
            target=self._run_imdb_dataset_index_build,
            args=(source_path, force),
            daemon=True,
            name="mp-imdb-index",
        )
        self._imdb_dataset_build_thread = thread
        thread.start()
        return True

    def _run_imdb_dataset_index_build(self, source_path: Path, force: bool):
        try:
            self._ensure_imdb_dataset_index(source_path, force=force)
        except Exception as err:
            self._imdb_dataset_build_error = str(err)
            self._imdb_dataset_status = f"索引失败：{err}"
            logger.error(f"IMDb 数据集后台建索引失败：{err}")
        finally:
            self._imdb_dataset_building = False
            if not self._imdb_dataset_build_error:
                self._refresh_imdb_dataset_status()

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

    def _load_douban_block_state(self):
        data = self.get_data(self._DOUBAN_BLOCK_STATE_KEY) or {}
        blocked_until = float(data.get("blocked_until") or 0)
        if blocked_until > time.time():
            self._douban_blocked_until = blocked_until
            self._douban_block_reason = str(data.get("reason") or "命中豆瓣风控")
            return
        self._clear_douban_block_state()

    def _set_douban_block_state(self, blocked_until: float, reason: str):
        reason_text = str(reason or "").strip() or "命中豆瓣风控"
        self._douban_blocked_until = blocked_until
        self._douban_block_reason = reason_text
        self.save_data(
            self._DOUBAN_BLOCK_STATE_KEY,
            {
                "blocked_until": blocked_until,
                "reason": reason_text,
            },
        )

    def _clear_douban_block_state(self):
        self._douban_blocked_until = 0
        self._douban_block_reason = ""
        self.del_data(self._DOUBAN_BLOCK_STATE_KEY)

    def _is_douban_blocked(self) -> bool:
        if self._douban_blocked_until and self._douban_blocked_until <= time.time():
            self._clear_douban_block_state()
            return False
        return self._douban_blocked_until > time.time()

    @classmethod
    def _is_douban_block_message(cls, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        compact = lowered.replace(" ", "")
        for marker in cls._DOUBAN_BLOCK_MARKERS:
            marker_lower = marker.lower()
            marker_compact = marker_lower.replace(" ", "")
            if marker_lower in lowered or marker_compact in compact:
                return True
        return False

    @classmethod
    def _payload_contains_douban_block(cls, payload: Any) -> bool:
        if payload in (None, ""):
            return False
        if isinstance(payload, (dict, list, tuple)):
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except Exception:
                text = str(payload)
        else:
            text = str(payload)
        return cls._is_douban_block_message(text)

    def _trip_douban_block(self, reason: str):
        blocked_until = time.time() + self._DOUBAN_BLOCK_HOURS * 3600
        self._set_douban_block_state(blocked_until, reason)
        logger.warn(f"检测到豆瓣风控，熔断 {self._DOUBAN_BLOCK_HOURS} 小时：{reason}")

    async def _wait_douban_slot(self):
        interval = max(float(self._DOUBAN_MIN_INTERVAL_SECONDS), 0)
        if interval <= 0:
            return
        while True:
            now = time.monotonic()
            with self._douban_rate_lock:
                if now >= self._douban_next_request_at:
                    self._douban_next_request_at = now + interval
                    return
                wait_seconds = self._douban_next_request_at - now
            await asyncio.sleep(min(max(wait_seconds, 0), interval))

    async def _call_douban_service(self, context: str, request_fn):
        if self._is_douban_blocked():
            return None
        await self._wait_douban_slot()
        try:
            payload = await request_fn()
        except Exception as err:
            if self._is_douban_block_message(str(err)):
                self._trip_douban_block(f"{context}：{err}")
            else:
                logger.warn(f"{context} 失败：{err}")
            return None
        if self._payload_contains_douban_block(payload):
            self._trip_douban_block(f"{context}：返回风控页面")
            return None
        return payload

    def _load_douban_rating_store(self):
        data = self.get_data(self._DOUBAN_RATING_STORE_KEY) or {}
        store: Dict[str, Dict[str, Any]] = {}
        if isinstance(data, dict):
            for raw_id, raw_value in data.items():
                douban_id = str(raw_id or "").strip()
                if not douban_id:
                    continue
                rating = None
                updated_at = 0
                source = ""
                if isinstance(raw_value, dict):
                    rating = self._normalize_rating(raw_value.get("rating"))
                    try:
                        updated_at = int(raw_value.get("updated_at") or 0)
                    except (TypeError, ValueError):
                        updated_at = 0
                    source = str(raw_value.get("source") or "")
                else:
                    rating = self._normalize_rating(raw_value)
                if rating is None:
                    continue
                store[douban_id] = {
                    "rating": rating,
                    "updated_at": updated_at,
                    "source": source,
                }
        self._douban_rating_store = store
        self._trim_douban_rating_store()

    def _load_douban_web_quota_state(self):
        data = self.get_data(self._DOUBAN_WEB_QUOTA_KEY) or {}
        if isinstance(data, dict):
            self._douban_web_quota_date = str(data.get("date") or "")
            try:
                self._douban_web_quota_count = max(0, int(data.get("count") or 0))
            except (TypeError, ValueError):
                self._douban_web_quota_count = 0
        else:
            self._douban_web_quota_date = ""
            self._douban_web_quota_count = 0
        self._sync_douban_web_quota_date()

    def _load_douban_web_miss_state(self):
        data = self.get_data(self._DOUBAN_WEB_MISS_STATE_KEY) or {}
        state: Dict[str, int] = {}
        if isinstance(data, dict):
            for raw_id, raw_ts in data.items():
                douban_id = str(raw_id or "").strip()
                if not douban_id:
                    continue
                try:
                    ts = int(raw_ts or 0)
                except (TypeError, ValueError):
                    ts = 0
                if ts > 0:
                    state[douban_id] = ts
        self._douban_web_miss_state = state
        self._trim_douban_web_miss_state()

    def _save_douban_web_quota_state(self):
        self.save_data(
            self._DOUBAN_WEB_QUOTA_KEY,
            {
                "date": self._douban_web_quota_date,
                "count": self._douban_web_quota_count,
            },
        )

    def _save_douban_web_miss_state(self):
        if not self._douban_web_miss_state:
            self.del_data(self._DOUBAN_WEB_MISS_STATE_KEY)
            return
        self.save_data(self._DOUBAN_WEB_MISS_STATE_KEY, self._douban_web_miss_state)

    @staticmethod
    def _today_str() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _sync_douban_web_quota_date(self):
        today = self._today_str()
        if self._douban_web_quota_date != today:
            self._douban_web_quota_date = today
            self._douban_web_quota_count = 0
            self._save_douban_web_quota_state()

    def _try_consume_douban_web_quota(self) -> bool:
        with self._douban_web_quota_lock:
            self._sync_douban_web_quota_date()
            if self._douban_web_daily_limit > 0 and self._douban_web_quota_count >= self._douban_web_daily_limit:
                return False
            self._douban_web_quota_count += 1
            self._save_douban_web_quota_state()
            return True

    def _trim_douban_rating_store(self):
        if len(self._douban_rating_store) <= self._DOUBAN_RATING_STORE_LIMIT:
            return
        ordered_items = sorted(
            self._douban_rating_store.items(),
            key=lambda item: int(item[1].get("updated_at") or 0),
            reverse=True,
        )
        self._douban_rating_store = dict(ordered_items[: self._DOUBAN_RATING_STORE_LIMIT])

    def _trim_douban_web_miss_state(self):
        if not self._douban_web_miss_state:
            return
        now = int(time.time())
        threshold = max(now - self._DOUBAN_WEB_MISS_COOLDOWN_SECONDS, 0)
        active = {
            key: ts for key, ts in self._douban_web_miss_state.items()
            if int(ts or 0) >= threshold
        }
        if len(active) > self._DOUBAN_WEB_MISS_STORE_LIMIT:
            ordered_items = sorted(active.items(), key=lambda item: int(item[1] or 0), reverse=True)
            active = dict(ordered_items[: self._DOUBAN_WEB_MISS_STORE_LIMIT])
        self._douban_web_miss_state = active

    def _is_recent_douban_web_miss(self, douban_id: str) -> bool:
        ts = int(self._douban_web_miss_state.get(str(douban_id or "").strip()) or 0)
        if ts <= 0:
            return False
        return (int(time.time()) - ts) < self._DOUBAN_WEB_MISS_COOLDOWN_SECONDS

    def _mark_douban_web_miss(self, douban_id: str):
        key = str(douban_id or "").strip()
        if not key:
            return
        self._douban_web_miss_state[key] = int(time.time())
        self._trim_douban_web_miss_state()
        self._save_douban_web_miss_state()

    def _clear_douban_web_miss(self, douban_id: str):
        key = str(douban_id or "").strip()
        if not key:
            return
        if key in self._douban_web_miss_state:
            self._douban_web_miss_state.pop(key, None)
            self._save_douban_web_miss_state()

    def _save_douban_rating_store(self):
        if not self._douban_rating_store:
            self.del_data(self._DOUBAN_RATING_STORE_KEY)
            return
        self.save_data(self._DOUBAN_RATING_STORE_KEY, self._douban_rating_store)

    def _remember_douban_rating(self, douban_id: str, rating: Any, source: str = ""):
        normalized_rating = self._normalize_rating(rating)
        douban_id = str(douban_id or "").strip()
        if not douban_id or normalized_rating is None:
            return
        old_value = self._douban_rating_store.get(douban_id)
        source_text = str(source or "").strip()
        if old_value and old_value.get("rating") == normalized_rating and old_value.get("source") == source_text:
            return
        self._douban_rating_store[douban_id] = {
            "rating": normalized_rating,
            "updated_at": int(time.time()),
            "source": source_text,
        }
        self._trim_douban_rating_store()
        self._save_douban_rating_store()

    def _remember_douban_rating_from_info(self, info: Optional[dict]):
        if not info:
            return
        douban_id = str(info.get("id") or "").strip()
        rating = self._extract_douban_rating(info)
        if douban_id and rating is not None:
            self._remember_douban_rating(douban_id, rating, str(info.get("__mr_source") or "未知来源"))

    def _enrich_with_cached_douban_rating(self, info: Optional[dict]) -> Optional[dict]:
        if not info:
            return info
        if self._extract_douban_rating(info) is not None:
            return info
        douban_id = str(info.get("id") or "").strip()
        if not douban_id:
            return info
        cached_info = self._build_cached_douban_info(douban_id)
        cached_rating = self._extract_douban_rating(cached_info)
        if cached_rating is None:
            return info
        merged = dict(info)
        merged["id"] = douban_id
        merged["rating"] = {"value": cached_rating}
        source = str(info.get("__mr_source") or "未知来源")
        return self._annotate_douban_info(merged, f"{source}+缓存")

    def _should_use_douban_web_fallback(self, enrich_context: str = "item") -> bool:
        if not self._enable_douban or not self._enable_douban_web_fallback:
            return False
        if self._douban_web_fallback_detail_only and str(enrich_context or "").strip().lower() != "item":
            return False
        return True

    def _build_cached_douban_info(self, douban_id: str) -> Optional[dict]:
        douban_id = str(douban_id or "").strip()
        if not douban_id:
            return None
        record = self._douban_rating_store.get(douban_id)
        if not isinstance(record, dict):
            return None
        rating = self._normalize_rating(record.get("rating"))
        if rating is None:
            return None
        return self._annotate_douban_info(
            {
                "id": douban_id,
                "rating": {"value": rating},
            },
            "本地缓存",
        )

    def _load_omdb_block_state(self):
        data = self.get_data(self._OMDB_BLOCK_STATE_KEY) or {}
        blocked_until = float(data.get("blocked_until") or 0)
        if blocked_until > time.time():
            self._imdb_blocked_until = blocked_until
            self._imdb_block_reason = str(data.get("reason") or "")
        else:
            self._clear_omdb_block_state()

    def _set_omdb_block_state(self, blocked_until: float, reason: str):
        self._imdb_blocked_until = blocked_until
        self._imdb_block_reason = str(reason or "")
        self.save_data(
            self._OMDB_BLOCK_STATE_KEY,
            {
                "blocked_until": blocked_until,
                "reason": self._imdb_block_reason,
            },
        )

    def _clear_omdb_block_state(self):
        self._imdb_blocked_until = 0
        self._imdb_block_reason = ""
        self.del_data(self._OMDB_BLOCK_STATE_KEY)

    def _get_imdb_status_payload(self) -> Dict[str, Any]:
        self._sync_douban_web_quota_date()
        built_at = self._imdb_dataset_meta.get("built_at")
        return {
            "enabled": self._enabled,
            "imdb_enabled": self._enable_imdb,
            "imdb_source": self._imdb_source,
            "external_douban_enabled": self._enable_external_douban,
            "external_douban_status": self._external_douban_status,
            "douban_blocked": self._douban_blocked_until > time.time(),
            "douban_blocked_until": int(self._douban_blocked_until) if self._douban_blocked_until else 0,
            "douban_block_reason": self._douban_block_reason,
            "douban_rating_store_size": len(self._douban_rating_store),
            "douban_web_fallback_enabled": self._enable_douban_web_fallback,
            "douban_web_fallback_detail_only": self._douban_web_fallback_detail_only,
            "douban_web_daily_limit": self._douban_web_daily_limit,
            "douban_web_quota_date": self._douban_web_quota_date,
            "douban_web_quota_used": self._douban_web_quota_count,
            "list_cache_enabled": self._enable_list_result_cache,
            "list_cache_entries": len(self._list_result_cache),
            "list_cache_ttl_seconds": self._list_cache_ttl_seconds,
            "list_cache_prewarm_running": self._list_prewarm_running,
            "dataset_path": self._imdb_ratings_path,
            "dataset_status": self._imdb_dataset_status,
            "dataset_building": self._imdb_dataset_building,
            "dataset_error": self._imdb_dataset_build_error,
            "dataset_record_count": self._imdb_dataset_meta.get("record_count", 0),
            "dataset_built_at": built_at,
            "omdb_blocked": self._imdb_blocked_until > time.time(),
            "omdb_blocked_until": int(self._imdb_blocked_until) if self._imdb_blocked_until else 0,
            "omdb_block_reason": self._imdb_block_reason,
        }

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

    def _ensure_imdb_dataset_index(self, source_path: Path, force: bool = False) -> Path:
        with self._imdb_dataset_lock:
            db_path = self._get_imdb_dataset_db_path()
            meta = self._load_imdb_dataset_meta()
            if not force and db_path.exists() and meta and self._is_imdb_dataset_meta_current(meta, source_path):
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

    @staticmethod
    def _annotate_douban_info(info: Optional[dict], source: str) -> Optional[dict]:
        if not info:
            return None
        annotated = dict(info)
        annotated["__mr_source"] = source
        return annotated

    @staticmethod
    def _candidate_titles(media: MediaInfo) -> List[str]:
        names: List[str] = []
        for value in [media.title, media.original_title, media.en_title, *(media.names or [])]:
            title = str(value or "").strip()
            if not title:
                continue
            if title not in names:
                names.append(title)
            title_no_season = re.sub(r"(?:第\s*[一二三四五六七八九十0-9]+\s*季|season\s*\d+)$", "", title, flags=re.I).strip()
            if title_no_season and title_no_season not in names:
                names.append(title_no_season)
        return names

    def _record_diagnostic(
        self,
        media: MediaInfo,
        ratings: Dict[str, float],
        primary_rating: Optional[float],
        notes: List[str],
    ):
        if not self._enable_diagnostics:
            return
        record = {
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime()),
            "title": media.title or media.original_title or media.en_title or "未知条目",
            "ids": f"tmdb:{media.tmdb_id or '-'} douban:{media.douban_id or '-'} imdb:{media.imdb_id or '-'} bgm:{media.bangumi_id or '-'}",
            "ratings": self._merge_rating_tagline(self._display_ratings(ratings)) if ratings else "无评分",
            "primary": f"{primary_rating:.1f}" if primary_rating is not None else "-",
            "notes": "；".join(note for note in notes if note),
        }
        self._diagnostic_records.insert(0, record)
        del self._diagnostic_records[self._DIAGNOSTIC_KEEP:]

    def _diagnostic_summary_text(self) -> str:
        if not self._diagnostic_records:
            return "诊断记录：暂无数据。"
        lines = [
            f"{item['time']} {item['title']} | 主评分 {item['primary']} | {item['ratings']} | {item['notes']}"
            for item in self._diagnostic_records[:8]
        ]
        return "最近诊断： " + " || ".join(lines)

    @classmethod
    def _prefer_douban_info(cls, current: Optional[dict], candidate: Optional[dict]) -> Optional[dict]:
        if not candidate:
            return current
        if not current:
            return candidate
        current_has_rating = cls._extract_douban_rating(current) is not None
        candidate_has_rating = cls._extract_douban_rating(candidate) is not None
        if candidate_has_rating and not current_has_rating:
            return candidate
        if current_has_rating and not candidate_has_rating:
            return current
        current_size = sum(1 for value in current.values() if value)
        candidate_size = sum(1 for value in candidate.values() if value)
        if candidate_size > current_size:
            return candidate
        return current

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

    @staticmethod
    def _build_media_keyword_text(media: Any) -> str:
        if not media:
            return ""
        keys = (
            "title",
            "original_title",
            "title_year",
            "year",
            "overview",
            "tagline",
            "source",
            "type",
        )
        parts: List[str] = []
        for key in keys:
            value = getattr(media, key, None)
            if value:
                parts.append(str(value))
        return " ".join(parts)

    @classmethod
    def _select_primary_rating(cls, ratings: Dict[str, float]) -> Optional[float]:
        douban_rating = ratings.get("豆瓣")
        tmdb_rating = ratings.get("TMDB")
        if douban_rating is not None and tmdb_rating is not None:
            return min(douban_rating, tmdb_rating)
        if douban_rating is not None:
            return douban_rating
        if tmdb_rating is not None:
            return tmdb_rating
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
    def _extract_douban_imdb_id(cls, douban_info: Optional[dict]) -> Optional[str]:
        if not isinstance(douban_info, dict):
            return None
        for key in ("imdb", "imdb_id", "imdbid", "imdbId"):
            imdb_id = cls._normalize_imdb_id(douban_info.get(key))
            if imdb_id:
                return imdb_id
        return None

    @staticmethod
    def _normalize_imdb_id(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        matched = re.search(r"(tt\d{5,})", text, flags=re.IGNORECASE)
        if matched:
            return matched.group(1).lower()
        return None

    @classmethod
    def _extract_bangumi_media_rating(
        cls,
        media: Optional[MediaInfo],
        fallback_rating: Optional[float] = None,
    ) -> Optional[float]:
        if not media:
            return None
        bangumi_info = getattr(media, "bangumi_info", None)
        if isinstance(bangumi_info, dict):
            rating = bangumi_info.get("rating")
            if isinstance(rating, dict):
                for key in ("score", "value", "average"):
                    normalized = cls._normalize_rating(rating.get(key))
                    if normalized is not None:
                        return normalized
            else:
                normalized = cls._normalize_rating(rating)
                if normalized is not None:
                    return normalized
            for key in ("score", "average_score", "average", "vote_average"):
                normalized = cls._normalize_rating(bangumi_info.get(key))
                if normalized is not None:
                    return normalized
        fallback = cls._normalize_rating(fallback_rating)
        if fallback is not None:
            return fallback
        return None

    @classmethod
    def _strip_rating_overview(cls, overview: Optional[str]) -> str:
        lines = [line.strip() for line in str(overview or "").splitlines() if line.strip()]
        while lines and lines[0].startswith(cls._OVERVIEW_PREFIXES):
            lines.pop(0)
        return "\n".join(lines).strip()

    @classmethod
    def _merge_rating_tagline(cls, ratings: List[Tuple[str, float]]) -> str:
        rating_line = " / ".join(f"{label} {value:.1f}" for label, value in ratings)
        return rating_line

    @classmethod
    def _fallback_rating_tagline(cls, media: Optional[MediaInfo]) -> str:
        if not media:
            return ""
        ratings: Dict[str, float] = {}
        source_label = cls._source_label(media)
        current_rating = cls._normalize_rating(getattr(media, "vote_average", None))
        if source_label and current_rating is not None:
            ratings[source_label] = current_rating
        bangumi_rating = cls._extract_bangumi_media_rating(
            media,
            fallback_rating=current_rating if source_label == "Bangumi" else None,
        )
        if bangumi_rating is not None and ratings.get("Bangumi") is None:
            ratings["Bangumi"] = bangumi_rating
        if not ratings:
            return ""
        return cls._merge_rating_tagline(cls._display_ratings(ratings))

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
        self._clear_list_result_cache()
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
