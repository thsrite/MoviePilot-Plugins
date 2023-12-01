import re
import time
import warnings
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.core.event import eventmanager
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.plugins.sitestatistic.siteuserinfo import ISiteUserInfo
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils

warnings.filterwarnings("ignore", category=FutureWarning)

lock = Lock()


class SiteUnreadMsg(_PluginBase):
    # 插件名称
    plugin_name = "站点未读消息"
    # 插件描述
    plugin_desc = "发送站点未读消息。"
    # 插件图标
    plugin_icon = "Synomail_A.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "siteunreadmsg_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites = None
    siteoper = None
    _scheduler: Optional[BackgroundScheduler] = None
    _history = []
    _exits_key = []
    _site_schema: List[ISiteUserInfo] = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _queue_cnt: int = 5
    _unread_sites: list = []

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._queue_cnt = config.get("queue_cnt")
            self._unread_sites = config.get("unread_sites") or []

            # 过滤掉已删除的站点
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            self._unread_sites = [site.get("id") for site in all_sites if
                                  not site.get("public") and site.get("id") in self._unread_sites]
            self.__update_config()

        if self._enabled or self._onlyonce:
            # 加载模块
            self._site_schema = ModuleHelper.load('app.plugins.siteunreadmsg.siteuserinfo',
                                                  filter_func=lambda _, obj: hasattr(obj, 'schema'))

            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            self._site_schema.sort(key=lambda x: x.order)

            # 立即运行一次
            if self._onlyonce:
                logger.info(f"站点未读消息服务启动，立即运行一次")
                self._scheduler.add_job(self.refresh_all_site_unread_msg, 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点未读消息")
                # 关闭一次性开关
                self._onlyonce = False

                # 保存配置
                self.__update_config()

            # 周期运行
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.refresh_all_site_unread_msg,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="站点未读消息")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        pass

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

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                         for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])

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
                                        }
                                    }
                                ]
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
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'queue_cnt',
                                            'label': '队列数量'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'unread_sites',
                                            'label': '未读消息站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "cron": "5 1 * * *",
            "queue_cnt": 5,
            "unread_sites": []
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        unread_data = self.get_data("history")
        if not unread_data:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        # 数据按时间降序排序
        unread_data = sorted(unread_data,
                             key=lambda item: item.get('time') or 0,
                             reverse=True)

        # 站点数据明细
        unread_msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': data.get("site")
                    },
                    {
                        'component': 'td',
                        'text': data.get("head")
                    },
                    {
                        'component': 'td',
                        'text': data.get("content")
                    },
                    {
                        'component': 'td',
                        'text': data.get("time")
                    }
                ]
            } for data in unread_data
        ]

        # 拼装页面
        return [
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
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '站点'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '标题'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '内容'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '时间'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': unread_msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def __build_class(self, html_text: str) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(html_text):
                    return site_schema
            except Exception as e:
                logger.error(f"站点匹配失败 {e}")
        return None

    def build(self, site_info: CommentedMap) -> Optional[ISiteUserInfo]:
        """
        构建站点信息
        """
        site_cookie = site_info.get("cookie")
        if not site_cookie:
            return None
        site_name = site_info.get("name")
        url = site_info.get("url")
        proxy = site_info.get("proxy")
        ua = site_info.get("ua")
        # 会话管理
        with requests.Session() as session:
            proxies = settings.PROXY if proxy else None
            proxy_server = settings.PROXY_SERVER if proxy else None
            render = site_info.get("render")

            logger.debug(f"站点 {site_name} url={url} site_cookie={site_cookie} ua={ua}")
            if render:
                # 演染模式
                html_text = PlaywrightHelper().get_page_source(url=url,
                                                               cookies=site_cookie,
                                                               ua=ua,
                                                               proxies=proxy_server)
            else:
                # 普通模式
                res = RequestUtils(cookies=site_cookie,
                                   session=session,
                                   ua=ua,
                                   proxies=proxies
                                   ).get_res(url=url)
                if res and res.status_code == 200:
                    if re.search(r"charset=\"?utf-8\"?", res.text, re.IGNORECASE):
                        res.encoding = "utf-8"
                    else:
                        res.encoding = res.apparent_encoding
                    html_text = res.text
                    # 第一次登录反爬
                    if html_text.find("title") == -1:
                        i = html_text.find("window.location")
                        if i == -1:
                            return None
                        tmp_url = url + html_text[i:html_text.find(";")] \
                            .replace("\"", "") \
                            .replace("+", "") \
                            .replace(" ", "") \
                            .replace("window.location=", "")
                        res = RequestUtils(cookies=site_cookie,
                                           session=session,
                                           ua=ua,
                                           proxies=proxies
                                           ).get_res(url=tmp_url)
                        if res and res.status_code == 200:
                            if "charset=utf-8" in res.text or "charset=UTF-8" in res.text:
                                res.encoding = "UTF-8"
                            else:
                                res.encoding = res.apparent_encoding
                            html_text = res.text
                            if not html_text:
                                return None
                        else:
                            logger.error("站点 %s 被反爬限制：%s, 状态码：%s" % (site_name, url, res.status_code))
                            return None

                    # 兼容假首页情况，假首页通常没有 <link rel="search" 属性
                    if '"search"' not in html_text and '"csrf-token"' not in html_text:
                        res = RequestUtils(cookies=site_cookie,
                                           session=session,
                                           ua=ua,
                                           proxies=proxies
                                           ).get_res(url=url + "/index.php")
                        if res and res.status_code == 200:
                            if re.search(r"charset=\"?utf-8\"?", res.text, re.IGNORECASE):
                                res.encoding = "utf-8"
                            else:
                                res.encoding = res.apparent_encoding
                            html_text = res.text
                            if not html_text:
                                return None
                elif res is not None:
                    logger.error(f"站点 {site_name} 连接失败，状态码：{res.status_code}")
                    return None
                else:
                    logger.error(f"站点 {site_name} 无法访问：{url}")
                    return None
            # 解析站点类型
            if html_text:
                site_schema = self.__build_class(html_text)
                if not site_schema:
                    logger.error("站点 %s 无法识别站点类型" % site_name)
                    return None
                return site_schema(site_name, url, site_cookie, html_text, session=session, ua=ua, proxy=proxy)
            return None

    def __refresh_site_data(self, site_info: CommentedMap):
        """
        更新单个site 数据信息
        :param site_info:
        :return:
        """
        site_name = site_info.get('name')
        site_url = site_info.get('url')
        if not site_url:
            return None
        unread_msg_notify = True
        try:
            site_user_info: ISiteUserInfo = self.build(site_info=site_info)
            if site_user_info:
                logger.debug(f"站点 {site_name} 开始以 {site_user_info.site_schema()} 模型解析")
                # 开始解析
                site_user_info.parse()
                logger.debug(f"站点 {site_name} 解析完成")

                # 获取不到数据时，仅返回错误信息，不做历史数据更新
                if site_user_info.err_msg:
                    return None

                # 发送通知，存在未读消息
                self.__notify_unread_msg(site_name, site_user_info, unread_msg_notify)
        except Exception as e:
            logger.error(f"站点 {site_name} 获取流量数据失败：{str(e)}")

    def __notify_unread_msg(self, site_name: str, site_user_info: ISiteUserInfo, unread_msg_notify: bool):
        if site_user_info.message_unread <= 0:
            return
        if not unread_msg_notify:
            return

        # 解析出内容，则发送内容
        if len(site_user_info.message_unread_contents) > 0:
            for head, date, content in site_user_info.message_unread_contents:
                msg_title = f"【站点 {site_user_info.site_name} 消息】"
                msg_text = f"时间：{date}\n标题：{head}\n内容：\n{content}"
                # 防止同一消息重复发送
                key = site_user_info.site_name + "_" + date + "_" + head + "_" + content
                if key not in self._exits_key:
                    self._exits_key.append(key)
                    self.post_message(mtype=NotificationType.SiteMessage, title=msg_title, text=msg_text)
                    self._history.append({
                        "site": site_name,
                        "head": head,
                        "content": content,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                        "date": date,
                    })
        else:
            self.post_message(mtype=NotificationType.SiteMessage,
                              title=f"站点 {site_user_info.site_name} 收到 "
                                    f"{site_user_info.message_unread} 条新消息，请登陆查看")

    def refresh_all_site_unread_msg(self):
        """
        多线程刷新站点未读消息
        """
        if not self.sites.get_indexers():
            return

        logger.info("开始刷新站点未读消息 ...")

        with lock:
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            # 没有指定站点，默认使用全部站点
            if not self._unread_sites:
                refresh_sites = all_sites
            else:
                refresh_sites = [site for site in all_sites if
                                 site.get("id") in self._unread_sites]
            if not refresh_sites:
                return

            self._history = self.get_data("history") or []
            # 并发刷新
            with ThreadPool(min(len(refresh_sites), int(self._queue_cnt or 5))) as p:
                p.map(self.__refresh_site_data, refresh_sites)

            if self._history:
                # 保存数据
                self.save_data("history", self._history)

            logger.info("站点未读消息刷新完成")

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "notify": self._notify,
            "queue_cnt": self._queue_cnt,
            "unread_sites": self._unread_sites,
        })

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            unread_sites = config.get("unread_sites")
            if unread_sites:
                if isinstance(unread_sites, str):
                    unread_sites = [unread_sites]

                # 删除对应站点
                if site_id:
                    unread_sites = [site for site in unread_sites if int(site) != int(site_id)]
                else:
                    # 清空
                    unread_sites = []

                # 若无站点，则停止
                if len(unread_sites) == 0:
                    self._enabled = False

                self._unread_sites = unread_sites
                # 保存配置
                self.__update_config()
