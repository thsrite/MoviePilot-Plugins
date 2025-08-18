import base64
import re
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.plugin import PluginManager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.utils.http import RequestUtils

lock = threading.Lock()
scheduler_lock = threading.Lock()

SCREENSHOT_DEVICES = {
    "default": {
        "mobile": {
            "device": "iPhone 13 Pro Max",
            "size": {}
        },
        "desktop": {
            "device": "iPad Pro 11",
            "size": {'width': 740, 'height': 1024}
        }
    },
    "border": {
        "mobile": {
            "device": "iPhone 13 Pro Max",
            "size": {}
        },
        "desktop": {
            "device": "iPad Pro 11",
            "size": {}
        }
    }
}

IMAGES_PATH = settings.CONFIG_PATH / "temp" / "WeatherWidget" / "images"
IMAGES_PATH.mkdir(parents=True, exist_ok=True)
WEATHER_API_KEY = "bdd98ec1d87747f3a2e8b1741a5af796"


class WeatherWidget(_PluginBase):
    # region 全局定义

    # 插件名称
    plugin_name = "天气"
    # 插件描述
    plugin_desc = "定时推送天气，并在仪表盘中显示实时天气。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/InfinityPacer/MoviePilot-Plugins/main/icons/weatherwidget.png"
    # 插件版本
    plugin_version = "1.8.1"
    # 插件作者
    plugin_author = "InfinityPacer"
    # 作者主页
    author_url = "https://github.com/InfinityPacer"
    # 插件配置项ID前缀
    plugin_config_prefix = "weatherwidget_"
    # 加载顺序
    plugin_order = 80
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    # enable
    _enabled = None
    # border
    _border = None
    # clear_cache
    _clear_cache = None
    # location
    _location = None
    # weather_url
    _weather_url = None
    # 启用自动主题
    _auto_theme_enabled = None
    # 自动高度
    _auto_height = None
    # use_dark_mode
    _use_dark_mode = None
    # adapt_mode
    _adapt_mode = None
    # component_size
    _component_size = None
    # weather_background
    _weather_background = None
    # weather_current_time
    _weather_current_time = None
    # weather_air_tag
    _weather_air_tag = None
    # weather_air_tag_background
    _weather_air_tag_background = None
    # location_url
    _location_url = None
    # 开启天气通知
    _weather_notify = None
    # 天气通知周期
    _weather_notify_cron = None
    # 消息类型
    _weather_notify_type = None
    # last_screenshot_time
    _last_screenshot_time = None
    # min_screenshot_span
    _min_screenshot_span = 5 * 60
    # 截图超时时间
    _screenshot_timeout = 2 * 60
    # 截图类型
    _screenshot_type = None
    # 天气刷新间隔
    _refresh_interval = 1
    # 定时器
    _scheduler = None
    # 退出事件
    _event = threading.Event()
    _ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

    # endregion

    def init_plugin(self, config: dict = None):
        if not config:
            return

        self.stop_service()

        self._enabled = config.get("enabled", False)
        self._border = config.get("border", False)
        self._clear_cache = config.get("clear_cache", False)
        self._location = config.get("location", "")
        self._location_url = config.get("location_url", "")
        self._weather_url = self.__get_weather_url()
        self._auto_theme_enabled = config.get("auto_theme_enabled", True)
        self._auto_height = config.get("auto_height", False)
        self._last_screenshot_time = None
        self._use_dark_mode = self.__should_use_dark_mode()
        self._adapt_mode = config.get("adapt_mode", "compatibility")
        self._component_size = config.get("component_size", "mini")
        self._weather_notify = config.get("weather_notify", True)
        self._weather_notify_cron = config.get("weather_notify_cron")
        self._weather_notify_type = config.get("weather_notify_type", "Plugin")
        self._screenshot_type = self.__get_screenshot_type()
        self._weather_background = config.get("weather_background",
                                              "linear-gradient(225deg, #fee5ca, #e9f0ff 55%, #dce3fb)")
        self._weather_current_time = config.get("weather_current_time",
                                                datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M'))
        self._weather_air_tag = config.get("weather_air_tag", " AQI 优 ")
        self._weather_air_tag_background = config.get("weather_air_tag_background", "#95B359")

        if self._clear_cache:
            self.__save_data({})
            self.__clear_image()

        self.__update_config()

        if not self._enabled:
            return

        if not self._location:
            logger.error("城市不能为空")
            return

        if not self.__check_image():
            logger.info("没有找到截图，立即运行一次截图任务")
            self.__add_screenshot_task()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
            定义远程控制命令
            :return: 命令关键字、事件、描述、附带数据
            """
        return [
            {
                "cmd": "/weather_notify",
                "event": EventType.PluginAction,
                "desc": "实时天气",
                "category": "工具",
                "data": {"action": "weather_notify"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        pass

    def __get_total_elements(self, image: str, key: str = "mobile") -> List[dict]:
        """
        组装汇总元素
        """
        if self._border:
            return [
                {
                    'component': 'VCardItem',
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'text': f"{self._location}"
                        }
                    ]
                },
                {
                    'component': 'VCardItem',
                    'props': {
                        'class': 'w-full',
                        'style': {
                            'position': 'relative',
                            'height': 'auto',
                            'background': f'{self._weather_background}',
                            'padding': "0"
                        }
                    },
                    'content': [
                        {
                            'component': 'a',
                            'props': {
                                'href': f'{self._weather_url}',
                                "target": "_blank"
                            },
                            'content': [
                                {
                                    'component': 'VImg',
                                    'props': {
                                        'src': f'{image}',
                                        'height': 'auto' if key == 'mobile' else (
                                            '264px' if self._adapt_mode == 'compatibility' else 'auto'),
                                        'max-width': '100%',
                                        'width': '100%'
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        else:
            return [
                {
                    'component': 'VCardItem',
                    'props': {
                        'class': 'w-full',
                        'style': {
                            'position': 'relative',
                            'height': 'auto',
                            'background': f'{self._weather_background}',
                            'padding': 0 if self._adapt_mode == 'compatibility'
                                            or key == 'mobile' or self._auto_height else '0 0 1.5rem',
                        }
                    },
                    'content': [
                        {
                            'component': 'a',
                            'props': {
                                'href': f'{self._weather_url}',
                                "target": "_blank"
                            },
                            'content': [
                                {
                                    'component': 'VImg',
                                    'props': {
                                        'src': f'{image}',
                                        'height': 'auto' if self._auto_height or key == 'mobile' else (
                                            '336px' if self._adapt_mode == 'compatibility' else '310px'),
                                        'max-width': '100%',
                                        'width': '100%',
                                        'cover': False if self._adapt_mode == 'compatibility' else True,
                                    }
                                },
                                {
                                    'component': 'VCardText',
                                    'props': {
                                        'class': 'v-card-text w-full flex flex-row justify-start items-start absolute '
                                                 'top-0 left-0 cursor-pointer'
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardTitle',
                                            'props': {
                                                'class': 'mb-1 line-clamp-2 overflow-hidden text-ellipsis ...',
                                                'style': {
                                                    'color': 'rgb(231 227 252)'
                                                    if self._use_dark_mode else 'rgb(58 53 65 / 87%)',
                                                }
                                            },
                                            'text': self._location
                                        }
                                    ]
                                },
                                {
                                    'component': 'VCardText',
                                    'props': {
                                        'class': 'w-full flex flex-row justify-end items-start absolute '
                                                 'left-0 cursor-pointer',
                                        'style': {
                                            'top': '1rem',
                                            'font-size': '12px',
                                            'line-height': '12px',
                                            'color': 'rgb(231 227 252)'
                                            if self._use_dark_mode else 'rgb(58 53 65 / 87%)',
                                            'text-align': 'right'
                                        }
                                    },
                                    'text': self._weather_current_time,
                                },
                                {
                                    'component': 'p',
                                    'props': {
                                        'class': 'w-full flex flex-row justify-end items-start absolute '
                                                 'right-0 cursor-pointer',
                                        'style': {
                                            'top': '2.4rem',
                                            'display': 'inline-block',
                                            'width': '76px',
                                            'padding-top': '4px',
                                            'padding-bottom': '4px',
                                            'margin-right': '20px',
                                            'font-size': '15px',
                                            'line-height': '16px',
                                            'text-align': 'center',
                                            'white-space': 'nowrap',
                                            'border-radius': '14px',
                                            'color': 'white',
                                            "background-color": self._weather_air_tag_background
                                        }
                                    },
                                    'text': self._weather_air_tag,
                                }
                            ]
                        }
                    ]
                }
            ]

    def get_dashboard(self, key: str = None, user_agent: str = None) \
            -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        """
        获取插件仪表盘页面，需要返回：1、仪表板cols配置字典；2、全局配置（自动刷新等）；2、仪表板页面元素配置json（含数据）
        1、col配置参考：
        {
            "cols": 12, "md": 6
        }
        2、全局配置参考：
        {
            "refresh": 10, // 自动刷新时间，单位秒
            "border": True, // 是否显示边框，默认True，为False时取消组件边框和边距，由插件自行控制
        }
        3、页面配置使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        """
        # 根据UA获取设备类型
        key = self.__detect_device_type(user_agent=user_agent).lower()
        # 获取图片资源
        image = self.__get_weather_base64_image(location=self._location, key=key)

        # 列配置
        size_to_cols_map = {
            "mini": {"cols": 12, "md": 4},
            "small": {"cols": 12, "md": 6},
            "medium": {"cols": 12, "md": 8},
            "large": {"cols": 12, "md": 12}
        }
        cols = size_to_cols_map.get(self._component_size, size_to_cols_map.get("mini"))

        # 全局配置
        attrs = {
            "border": not image
        }

        # 拼装页面元素
        if not image:
            elements = [
                {
                    'component': 'VCardItem',
                    'content': [
                        {
                            'component': 'div',
                            'text': '暂无数据',
                            'props': {
                                'class': 'text-center'
                            }
                        }
                    ]
                }
            ]
        else:
            elements = self.__get_total_elements(image=image, key=key)
        return cols, attrs, elements

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {

                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'border',
                                            'label': '显示边框',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_cache',
                                            'label': '清理缓存',
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_theme_enabled',
                                            'label': '自动主题',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_height',
                                            'label': '自动高度',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'weather_notify',
                                            'label': '开启天气通知',
                                        },
                                    }
                                ],
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'weather_notify_cron',
                                            'label': '天气通知周期',
                                            'placeholder': '5位cron表达式',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'weather_notify_type',
                                            'label': '消息类型',
                                            'items': [{"title": item.value, "value": item.name}
                                                      for item in NotificationType]
                                        }
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'component_size',
                                            'label': '组件规格',
                                            'items': [
                                                {"title": "迷你", "value": "mini"},
                                                {"title": "小型", "value": "small"},
                                                {"title": "中型", "value": "medium"},
                                                {"title": "大型", "value": "large"}
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'location',
                                            'label': '城市',
                                            'placeholder': '城市地点',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'location_url',
                                            'label': '城市链接',
                                            'placeholder': '和风天气的城市天气链接',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'adapt_mode',
                                            'label': '适配方案',
                                            'items': [
                                                {'title': '兼容模式', 'value': 'compatibility'},
                                                {'title': '画质模式', 'value': 'quality'}
                                            ]
                                        }
                                    }
                                ],
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：通过在和风天气官网获取对应链接精确定位城市，如「秦淮区」的链接为'
                                                    'https://www.qweather.com/weather/qinhuai-101190109.html，'
                                                    '则在城市链接填写 qinhuai-101190109'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：数据异常时，可通过填写城市链接精确定位城市'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '天气数据来源于 '
                                        },
                                        'content': [
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://www.qweather.com',
                                                    'target': '_blank'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'u',
                                                        'text': '和风天气'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'span',
                                                'text': '，再次感谢和风天气（https://www.qweather.com）提供的服务'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "border": False,
            "auto_theme_enabled": True,
            "adapt_mode": "compatibility",
            "weather_notify": True,
            "weather_notify_cron": "0 8 * * *",
            "auto_height": False,
            "component_size": "mini"
        }

    def get_page(self) -> List[dict]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """

        services = []

        if self._enabled:
            services.append({
                "id": "RefreshWeather",
                "name": "定时获取天气信息",
                "trigger": "interval",
                "func": self.__take_screenshots,
                "kwargs": {"hours": self._refresh_interval}
            })

        if self._weather_notify and self._weather_notify_cron:
            services.append({
                "id": "NotifyWeather",
                "name": "定时推送天气通知",
                "trigger": CronTrigger.from_crontab(self._weather_notify_cron),
                "func": self.notify_weather,
                "kwargs": {}
            })

        return services

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown(wait=False)
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.info(str(e))

    def __update_config(self):
        """保存插件配置"""
        self.update_config(
            {
                "enabled": self._enabled,
                "border": self._border,
                "location": self._location,
                "location_url": self._location_url,
                "weather_background": self._weather_background,
                "weather_current_time": self._weather_current_time,
                "weather_air_tag": self._weather_air_tag,
                "weather_air_tag_background": self._weather_air_tag_background,
                "auto_theme_enabled": self._auto_theme_enabled,
                "auto_height": self._auto_height,
                'adapt_mode': self._adapt_mode,
                "component_size": self._component_size,
                "weather_notify": self._weather_notify,
                "weather_notify_cron": self._weather_notify_cron,
                "weather_notify_type": self._weather_notify_type
            })

    def invoke_service(self, request: Request, location: str, apikey: str) -> Any:
        """invokeService"""
        return PluginManager().run_plugin_method(self.__class__.__name__, "get_weather_image", **{
            "request": request,
            "location": location,
            "apikey": apikey
        })

    def get_weather_image(self, request: Request, location: str, apikey: str) -> Any:
        """读取图片"""

        if apikey != settings.API_TOKEN:
            return None
        if not location:
            logger.error("没有地址信息，获取天气图片失败")
            return None
        # 每次请求时，获取一次最新的图片信息
        self.__add_screenshot_task()
        # 获取UA
        self._ua = request.headers.get('user-agent', 'Unknown User-Agent') or self._ua
        key = self.__detect_device_type(user_agent=self._ua).lower()
        # 这里实际上返回的是上一次的图片信息
        image = self.__get_latest_image(key=key)
        if not image:
            return None
        return Response(content=image.read_bytes(), media_type="image/jpeg")

    def __get_weather_base64_image(self, location: str, key: str = "mobile") -> Optional[str]:
        """获取base64图片"""
        if not location:
            logger.error("没有地址信息，获取天气图片失败")
            return None
        # 每次请求时，获取一次最新的图片信息
        self.__add_screenshot_task()
        # 这里实际上返回的是上一次的图片信息
        image = self.__get_latest_image(key=key)
        if not image:
            return None
        # 读取图片文件并编码为 base64
        with open(image, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return f"data:image/{image.suffix.replace('.', '')};base64,{encoded_string}"

    def __add_screenshot_task(self):
        """添加截图任务"""
        if not self._enabled:
            return

        if not self._scheduler:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if len(self._scheduler.get_jobs()):
            logger.info("已经存在待执行的截图任务，清空任务并继续添加")
            self._scheduler.remove_all_jobs()

        self._scheduler.add_job(
            func=self.__take_screenshots,
            trigger="date",
            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
            name="获取一次天气信息",
        )
        logger.info("已添加截图任务，等待执行")
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
        if not self._scheduler.running:
            self._scheduler.start()

    def __update_with_log_screenshot_time(self, current_time: Optional[datetime]):
        """更新截图时间"""
        self._last_screenshot_time = current_time
        if current_time:
            logger.info(
                f"截图记录更新，最后一次截图时间重置为 {self._last_screenshot_time.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            logger.info(f"截图记录更新，发生错误，最后一次截图时间重置为None")

    def __take_screenshots(self):
        """管理多设备截图任务"""
        IMAGES_PATH.mkdir(parents=True, exist_ok=True)
        current_time = datetime.now(tz=pytz.timezone(settings.TZ))
        if self._last_screenshot_time and self.__check_image():
            time_since_last = (current_time - self._last_screenshot_time).total_seconds()
            time_to_wait = self._min_screenshot_span - time_since_last
            if time_since_last < self._min_screenshot_span:
                logger.info(f"截图过快，最小截图间隔为 {self._min_screenshot_span} 秒。请在 {time_to_wait:.2f} 秒后重试。")
                return

        self.__update_with_log_screenshot_time(current_time=current_time)

        if not self._weather_url:
            logger.error("无法获取天气请求地址，请检查配置")
            self.__update_with_log_screenshot_time(current_time=None)
            return

        try:
            with sync_playwright() as playwright:
                start_time = datetime.now()
                logger.info("正在准备截图服务，playwright服务启动中")
                self.__update_with_log_screenshot_time(current_time=current_time)
                screenshot_devices = self.__get_screenshot_device()
                if not screenshot_devices:
                    logger.error("获取截图设备失败，请检查")
                self._use_dark_mode = self.__should_use_dark_mode()
                color_scheme = "dark" if self._use_dark_mode else "light"
                with playwright.chromium.launch(headless=True, proxy=settings.PROXY_SERVER) as browser:
                    for key, device in screenshot_devices.items():
                        try:
                            logger.info(f'{key} 正在启动 screenshot ...')
                            self.__screenshot_element(playwright=playwright, browser=browser, key=key, device=device,
                                                      color_scheme=color_scheme)
                            elapsed_time = datetime.now() - start_time
                            logger.info(f'运行完毕，用时 {elapsed_time.total_seconds()} 秒')
                        except Exception as e:
                            logger.error(f"screenshot_element failed: {str(e)}")
        except Exception as e:
            logger.error(f"take_screenshots failed: {str(e)}")

    def __screenshot_element(self, playwright, browser, key: str, device: dict, color_scheme: str = 'light'):
        """执行单个截图任务"""
        current_time = datetime.now(tz=pytz.timezone(settings.TZ))
        timestamp = current_time.strftime("%Y%m%d%H%M%S")
        selector = ".c-city-weather-current"
        image_path = IMAGES_PATH / f"{self.__get_screenshot_image_pre_path(key=key)}_{timestamp}.png"

        logger.info(f"开始加载 {key} 页面: {self._weather_url}")
        self.__update_with_log_screenshot_time(current_time=current_time)
        with browser.new_context(color_scheme=color_scheme, **playwright.devices[device.get("device")]) as context:
            with context.new_page() as page:
                try:
                    size = device.get("size")
                    if size:
                        page.set_viewport_size(device.get("size"))
                    page.goto(self._weather_url)
                    page.wait_for_selector(selector, timeout=self._screenshot_timeout * 1000)
                    self.__reset_weather_style(page=page)
                    self.__reset_page_style(page=page, key=key)
                    logger.info(f"{key} 页面加载成功，标题: {page.title()}")
                    self.__update_with_log_screenshot_time(current_time=datetime.now(tz=pytz.timezone(settings.TZ)))
                    element = page.query_selector(selector)
                    if element:
                        # 获取元素的位置和尺寸
                        box = element.bounding_box()
                        if box:
                            # 计算新的裁剪区域
                            clip = {
                                "x": box["x"] + 2,
                                "y": box["y"] + 2,
                                "width": box["width"] - 4,
                                "height": box["height"] - 4
                            }
                            # 截图并保存
                            # element.screenshot(path=image_path)
                            page.screenshot(path=image_path, clip=clip)
                            logger.info(f"{key} 截图成功，截图路径: {image_path}")
                        else:
                            element.screenshot(path=image_path)
                            logger.info(f"{key} 截图成功，截图路径: {image_path}")
                        self.__manage_images(key=key)
                        self.__update_with_log_screenshot_time(current_time=datetime.now(tz=pytz.timezone(settings.TZ)))
                    else:
                        logger.warning(f"{key} 未找到指定的选择器: {selector}")
                        self.__update_with_log_screenshot_time(current_time=None)
                except Exception as e:
                    logger.error(f"{key} 截图失败，URL: {self._weather_url}, 错误：{e}")
                    self.__update_with_log_screenshot_time(current_time=None)

    def __reset_page_style(self, page: Any, key: str = 'mobile'):
        """重置页面样式"""

        # 无边框模式时，调整右上角的样式显示效果
        if not self._border:
            # 重置时间为空
            page.evaluate("""() => {
                        const element = document.querySelector('.current-time');
                        if (element) {
                            element.style.display = 'block';
                            element.style.height = '16px';
                            element.textContent  = '';
                        }
                    }""")

            # 移除空气质量
            page.evaluate("""() => {
                    const element = document.querySelector('.current-live .current-live__item > .air-tag');
                    if (element) {
                        element.remove();
                    }
                }""")

        # 修改天气边框圆角为直角
        page.evaluate("""() => {
                    const element = document.querySelector('.c-city-weather-current');
                    if (element) {
                        element.style.borderRadius = '0';
                    }
                }""")

        # 显示边框时，不需要重置其他样式
        if self._border:
            return

        # # 修改天气背景Padding、Position
        # page.evaluate("""() => {
        #         const element = document.querySelector('.current-weather__bg');
        #         if (element) {
        #             element.style.paddingTop = '40px';
        #             element.style.position = 'relative';
        #         }
        #     }""")

        # 修改时间位置
        # page.evaluate("""() => {
        #         const element = document.querySelector('.current-time');
        #         if (element) {
        #             element.style.position = 'absolute';
        #             element.style.right = '28px';
        #             element.style.top = '10px';
        #         }
        #     }""")

        # 修改空气质量位置

        # page.evaluate("""(key) => {
        #         const element = document.querySelector('.current-live .current-live__item > .air-tag');
        #         if (element) {
        #             element.style.top = key === 'mobile' ? '-36px' : '-41px';
        #             //element.style.right = '28px';
        #         }
        #     }""", key)

    def __reset_weather_style(self, page: Any):
        """重置天气样式"""
        # 获取指定元素
        weather_element = page.query_selector('.c-city-weather-current')

        # 获取元素的 CSS 样式
        # css_background = weather_element.evaluate("el => getComputedStyle(el).background")
        css_background_image = weather_element.evaluate("el => getComputedStyle(el).backgroundImage")

        # if css_background:
        #     self._weather_background = css_background
        if css_background_image:
            self._weather_background = css_background_image
        else:
            self._weather_background = "linear-gradient(225deg, #fee5ca, #e9f0ff 55%, #dce3fb)"

        # 尝试获取页面中的时间，如果不存在，则使用当前时间
        current_time_from_page = page.evaluate(
            "() => document.querySelector('.current-time') ? document.querySelector('.current-time').textContent : ''")

        if current_time_from_page:
            self._weather_current_time = current_time_from_page
        else:
            # 获取当前时间并格式化为 'YYYY-MM-DD HH:MM' 格式
            self._weather_current_time = datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M')

        # 尝试获取页面中的空气质量标签
        air_tag_from_page = page.evaluate("""() => {
                                        const element = document.querySelector('.current-live .current-live__item > .air-tag');
                                        if (element) {
                                            return element ? element.textContent : '';
                                        }
                                    }""")

        # 尝试获取页面中的空气质量标签和背景色
        air_tag_details = page.evaluate("""
                () => {
                    const element = document.querySelector('.current-live .current-live__item > .air-tag');
                    if (element) {
                        return {
                            text: element.textContent.trim(),
                            backgroundColor: window.getComputedStyle(element, null).getPropertyValue('background-color')
                        };
                    } else {
                        return { text: '', backgroundColor: '' };
                    }
                }
            """)

        if air_tag_details:
            self._weather_air_tag = f" {air_tag_details.get('text', 'AQI 优')} "
            self._weather_air_tag_background = air_tag_details.get("backgroundColor", "#95B359")
        else:
            # 如果没有找到空气质量标签或背景色，使用默认值
            self._weather_air_tag = " AQI 优 "
            self._weather_air_tag_background = "#95B359"  # 默认背景颜色

        self.__update_config()
        logger.info(f"更新天气背景样式为：{self._weather_background}")
        logger.info(f"更新当前时间为：{self._weather_current_time}")
        logger.info(f"更新空气质量为：{self._weather_air_tag}")
        logger.info(f"更新空气质量背景色为：{self._weather_air_tag_background}")

    def __manage_images(self, key: str, max_files: int = 5):
        """管理图片文件，确保每种类型最多保留 max_files 张"""
        files = sorted(IMAGES_PATH.glob(f"{self.__get_screenshot_image_pre_path(key=key)}_*.png"),
                       key=lambda x: x.stat().st_mtime)
        if len(files) > max_files:
            for file in files[:-max_files]:
                file.unlink()
                logger.info(f"删除旧图片: {file}")

    def __check_image(self) -> bool:
        """判断是否存在图片"""
        files_exist = any(IMAGES_PATH.glob(f"{self.__get_screenshot_image_pre_path()}_*.png"))
        if not files_exist:
            logger.error("没有找到图片信息")
        return files_exist

    def __get_latest_image(self, key: str) -> Optional[Path]:
        """获取指定key的最新图片路径"""
        # 搜索所有匹配的图片文件，并按修改时间排序
        try:
            image = max(IMAGES_PATH.glob(f"{self.__get_screenshot_image_pre_path(key=key)}_*.png"),
                        key=lambda x: x.stat().st_mtime)
            if not image:
                return None
            if not image.exists():
                return None
            if not image.is_file():
                return None
            # 判断是否图片文件
            if image.suffix.lower() not in [".jpg", ".png", ".gif", ".bmp", ".jpeg", ".webp"]:
                return None
            return image
        except ValueError:
            logger.error(f"{key}: 没有找到图片信息")
            return None

    @staticmethod
    def __get_weather_api_key() -> str:
        """获取天气api密钥"""
        return WEATHER_API_KEY

    def __get_weather_url(self) -> Optional[str]:
        """获取天气Url"""
        if not self._location:
            logger.error("没有配置城市，无法获取对应的城市天气链接")
            return None

        if self._location_url:
            return f"https://www.qweather.com/weather/{self._location_url}.html"

        location_map = self.get_data("location")
        if location_map:
            weather_url = location_map.get(self._location, {}).get("fxLink")
            if weather_url:
                return weather_url
        else:
            location_map = {}

        url = (f"https://geoapi.qweather.com/v2/city/lookup?"
               f"key={self.__get_weather_api_key()}&location={self._location}&lang=zh")

        response = RequestUtils(ua=self._ua).get_res(url)
        logger.info(f"请求和风天气获取详情信息：{url}")
        logger.info(f"响应信息: {response.text}")

        if response.status_code != 200:
            logger.error(f"连接和风天气失败, 状态码: {response.status_code}")
            return None
        else:
            data = response.json()
            if data.get('code') == "200":
                remote_locations = data.get('location', [])
                if remote_locations:
                    first = remote_locations[0]
                    weather_url = first.get("fxLink")
                    logger.info(f"城市: {self._location} 获取到对应的城市天气链接为: {weather_url}")
                    if weather_url:
                        location_map[self._location] = first
                        self.__save_data(location_map)
                        return weather_url

        logger.error(f"连接和风天气成功, 但获取详情信息失败")
        return None

    def __save_data(self, data: dict):
        """保存插件数据"""
        self.save_data("location", data)

    @staticmethod
    def __clear_image():
        """清理缓存图片"""
        # 检查目录是否存在
        if IMAGES_PATH.exists():
            # 删除目录及其所有内容
            shutil.rmtree(IMAGES_PATH)
            logger.info(f"目录 {IMAGES_PATH} 已清理")
        else:
            logger.info(f"目录 {IMAGES_PATH} 不存在")

    @staticmethod
    def __detect_device_type(user_agent: Optional[str]) -> str:
        """根据UA获取设备类型"""
        if not user_agent:
            logger.info(f"无法获取UA，当前访问设备类型默认为 desktop")
            return "desktop"

        logger.info(f"detect_device_type user_agent: {user_agent}")
        # 定义移动设备的关键字列表
        mobile_keywords = ['Mobile', 'Android', 'iPhone', 'iPad']

        # 检查UA中是否包含移动设备的关键字
        for keyword in mobile_keywords:
            if keyword in user_agent:
                logger.info(f"当前访问设备类型为 mobile")
                return 'mobile'

        logger.info(f"当前访问设备类型为 desktop")
        return 'desktop'

    def __get_screenshot_type(self):
        """获取截图类型"""
        return "border" if self._border else "default"

    def __get_screenshot_device(self) -> Optional[dict]:
        """获取截图设备信息"""
        screenshot_type = self._screenshot_type
        screenshot_devices = SCREENSHOT_DEVICES.get(screenshot_type)
        return screenshot_devices

    def __get_screenshot_image_pre_path(self, key: str = None) -> str:
        """获取截图前置路径"""
        image_path = f"weather_{self._location}_{self._screenshot_type}"
        return f"{image_path}_{key}" if key else image_path

    def __should_use_dark_mode(self):
        """是否启用暗黑模式"""
        if not self._auto_theme_enabled:
            return False

        try:
            response = RequestUtils(timeout=10, ua=self._ua).get_res(self._weather_url)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"请求天气信息失败: {e}")
            return None

        try:
            # 正则表达式用于找到日出和日落时间
            sunrise_pattern = r'"rise":"(\d{2}:\d{2})"'
            sunset_pattern = r'"set":"(\d{2}:\d{2})"'

            sunrise_match = re.search(sunrise_pattern, response.text)
            sunset_match = re.search(sunset_pattern, response.text)

            if sunrise_match and sunset_match:
                sunrise_time_str = sunrise_match.group(1)
                sunset_time_str = sunset_match.group(1)

                # 转换时间字符串为datetime.time对象
                sunrise_time = datetime.strptime(sunrise_time_str, '%H:%M').time()
                sunset_time = datetime.strptime(sunset_time_str, '%H:%M').time()

                # 获取当前时间（只关心时间，不关心日期）
                current_time = datetime.now(tz=pytz.timezone(settings.TZ)).time()

                if sunrise_time < sunset_time:
                    # 日出和日落在同一天
                    return not (sunrise_time <= current_time <= sunset_time)
                else:
                    # 跨天情况：日落发生在日出前（例如在极地地区）
                    return not (sunset_time <= current_time <= sunrise_time)

            return False
        except re.error as e:
            logger.error(f"Regex error: {e}")
            return False
        except ValueError as e:
            logger.error(f"Date conversion error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return False

    @eventmanager.register(EventType.PluginAction)
    def notify_weather(self, event: Event = None):
        """推送天气通知"""
        if not self._weather_notify:
            return

        if event:
            logger.info(f"event： {event}")
            event_data = event.event_data
            if not event_data or event_data.get("action") != "weather_notify":
                return

        weather_report = self.__resolve_weather()
        self.post_message(mtype=NotificationType[self._weather_notify_type], title="【实时天气】", text=weather_report)
        return

    def __resolve_weather(self) -> Optional[str]:
        """解析当前天气"""
        try:
            response = RequestUtils(timeout=10, ua=self._ua).get_res(self._weather_url)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"请求天气信息失败: {e}")
            return None

        try:
            # 使用 BeautifulSoup 解析 HTML
            soup = BeautifulSoup(response.text, 'html.parser')

            weather_report_parts = []

            # 提取当前时间
            current_time = soup.find("p", class_="current-time")
            if current_time:
                weather_report_parts.append(f"当前时间: {current_time.text.strip()}")

            # 提取温度
            temperature = soup.find("div", class_="current-live__item")
            if temperature:
                temperature = temperature.find_next_sibling().p
                if temperature:
                    weather_report_parts.append(f"温度: {temperature.text.strip()}")

            # 提取天气状况
            weather_condition = soup.find("div", class_="current-live__item")
            if weather_condition:
                weather_condition = weather_condition.find_next_sibling().p.find_next_sibling()
                if weather_condition:
                    weather_report_parts.append(f"天气状况: {weather_condition.text.strip()}")

            # 提取空气质量
            air_quality = soup.find("a", class_="air-tag")
            if air_quality:
                weather_report_parts.append(f"空气质量: {air_quality.text.strip()}")

            # 提取基本天气数据
            basic_items = soup.find_all("div", class_="current-basic___item")
            for item in basic_items:
                # 提取每个项目的内容(p0)和标题(p1)
                p0 = item.find("p")
                p1 = p0.find_next_sibling("p") if p0 else None

                if p0 and p1:
                    content = p0.text.strip()
                    label = p1.text.strip()
                    weather_report_parts.append(f"{label}: {content}")

            # 提取天气汇总
            weather_abstract = soup.find("div", class_="current-abstract")
            if weather_abstract:
                weather_report_parts.append(f"\n{weather_abstract.text.strip()}")

            # 构建和返回最终的天气报告
            if not weather_report_parts:
                return "未能获取天气信息"
            return "\n".join(weather_report_parts)
        except Exception as e:
            logger.error(f"解析天气信息时出错: {e}")
            return None