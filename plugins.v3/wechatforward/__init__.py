import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationChannel
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils, RequestUtils


class WeChatForward(_PluginBase):
    # 插件名称
    plugin_name = "微信消息转发"
    # 插件描述
    plugin_desc = "根据正则转发通知到其他WeChat应用。"
    # 插件图标
    plugin_icon = "Wechat_A.png"
    # 插件版本
    plugin_version = "3.0.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "wechatforward_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _rebuild: bool = False
    _wechat_confs: Any = None
    _specify_confs: Optional[str] = None
    _ignore_userid: Optional[str] = None
    _wechat_token_pattern_confs: Dict[Any, Dict[str, Any]] = {}
    _extra_msg_history: Dict[str, str] = {}
    _history_days: int = 7
    _wechat_proxy: Optional[str] = None
    _subscribe_oper: Optional[SubscribeOper] = None
    _subscribe_history_oper: Optional[SubscribeHistoryOper] = None

    # 企业微信发送消息URL
    _send_msg_url = "%s/cgi-bin/message/send?access_token=%s"
    # 企业微信获取TokenURL
    _token_url = "%s/cgi-bin/gettoken?corpid=%s&corpsecret=%s"

    example = [
        {
            "remark": "入库消息",
            "appid": 1000001,
            "corpid": "",
            "appsecret": "",
            "pattern": "已入库",
            "extra_confs": [

            ],
        },
        {
            "remark": "站点签到数据统计",
            "appid": 1000002,
            "corpid": "",
            "appsecret": "",
            "pattern": "自动签到|自动登录|数据统计|刷流任务",
            "extra_confs": []
        }
    ]

    def __init__(self):
        """初始化插件实例状态，避免热重载实例共享令牌或去重历史。"""
        super().__init__()
        self._enabled = False
        self._rebuild = False
        self._wechat_confs = []
        self._specify_confs = ""
        self._ignore_userid = ""
        self._wechat_token_pattern_confs = {}
        self._extra_msg_history = {}
        self._history_days = 7
        self._wechat_proxy = "https://qyapi.weixin.qq.com"
        self._subscribe_oper = SubscribeOper()
        self._subscribe_history_oper = SubscribeHistoryOper()

    def init_plugin(self, config: dict = None):
        """读取配置并在启用时重建企业微信访问令牌缓存。"""
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._rebuild = bool(config.get("rebuild"))
        self._ignore_userid = config.get("ignore_userid") or ""
        self._specify_confs = config.get("specify_confs") or ""
        self._wechat_proxy = (config.get("wechat_proxy") or "https://qyapi.weixin.qq.com").rstrip("/")
        try:
            self._history_days = max(1, int(config.get("history_days") or 7))
        except (TypeError, ValueError):
            self._history_days = 7

        if self.__sync_old_config(config):
            self.update_config({
                "enabled": self._enabled,
                "rebuild": self._rebuild,
                "wechat_confs": config["wechat_confs"],
                "ignore_userid": self._ignore_userid,
                "specify_confs": self._specify_confs,
                "history_days": self._history_days,
                "wechat_proxy": self._wechat_proxy,
            })

        self._wechat_confs = config.get("wechat_confs") or []
        self._wechat_token_pattern_confs = {}
        self._extra_msg_history = {}

        if self._enabled and self._wechat_confs:
            self.__save_wechat_token()

    @staticmethod
    def __sync_old_config(config: dict) -> bool:
        """把旧版的逐行微信配置转换成当前 JSON 配置结构。"""
        if config.get("wechat_confs") or not config.get("wechat") or not config.get("pattern"):
            return False

        extra_confs_by_appid: Dict[str, List[dict]] = {}
        for extra_conf in str(config.get("extra_confs") or "").splitlines():
            extra_conf = extra_conf.strip()
            if not extra_conf:
                continue
            if extra_conf.startswith("#"):
                extra_conf = extra_conf[1:].strip()
            extras = extra_conf.split(" > ")
            if len(extras) != 4:
                continue
            extra_confs_by_appid.setdefault(extras[3], []).append({
                "pattern": extras[0],
                "userid": extras[1],
                "msg": extras[2],
            })

        patterns = str(config.get("pattern") or "").splitlines()
        wechat_confs = []
        for index, raw_wechat in enumerate(str(config.get("wechat") or "").splitlines()):
            if not raw_wechat:
                continue
            remark = ""
            if raw_wechat.count("#") == 1:
                raw_wechat, remark = raw_wechat.split("#", 1)
                remark = remark.strip()
            wechat_config = raw_wechat.split(":", 2)
            if len(wechat_config) != 3:
                continue
            appid, corpid, appsecret = (item.strip() for item in wechat_config)
            if not appid or not corpid or not appsecret:
                continue
            wechat_confs.append({
                "remark": remark or f"{appid}配置",
                "appid": appid,
                "corpid": corpid,
                "appsecret": appsecret,
                "pattern": patterns[index] if index < len(patterns) else "",
                "extra_confs": extra_confs_by_appid.get(appid, []),
            })

        if not wechat_confs:
            return False
        config["wechat_confs"] = json.dumps(wechat_confs, indent=4, ensure_ascii=False)
        logger.info("旧版本配置已转为新版本配置")
        return True

    def __save_wechat_token(self):
        """读取可复用的令牌缓存，否则按当前配置重新获取令牌。"""
        # 如果重建则重新解析存库
        if self._rebuild:
            self.__parse_token()
        else:
            try:
                # 从数据库获取token
                wechat_confs = self.get_data('wechat_confs')
                if not self._wechat_token_pattern_confs and wechat_confs:
                    self._wechat_token_pattern_confs = wechat_confs
                    logger.info(f"WeChat配置 从数据库获取成功：{len(self._wechat_token_pattern_confs.keys())}条配置")
                else:
                    self.__parse_token()
            except Exception:
                self.__parse_token()

    @staticmethod
    def __parse_wechat_confs(value: Any) -> List[dict]:
        """把配置编辑器的 JSON 文本或已解析列表归一为配置字典列表。"""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                logger.error("微信配置不是有效的 JSON，跳过令牌解析")
                return []
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def __parse_token(self):
        """
        解析token存库
        """
        # 解析配置
        for wechat in self.__parse_wechat_confs(self._wechat_confs):
            remark = wechat.get("remark")
            appid = wechat.get("appid")
            corpid = wechat.get("corpid")
            appsecret = wechat.get("appsecret")
            pattern = wechat.get("pattern")
            extra_confs = wechat.get("extra_confs")
            if not appid or not corpid or not appsecret:
                logger.error(f"{remark} 应用配置不正确, 跳过处理")
                continue

            # 获取token
            access_token, expires_in, access_token_time = self.__get_access_token(corpid=corpid,
                                                                                  appsecret=appsecret)
            if not access_token:
                # 没有token，获取token
                logger.error(f"WeChat配置 {remark} 获取token失败，请检查配置")
                continue

            self._wechat_token_pattern_confs[appid] = {
                "remark": remark,
                "corpid": corpid,
                "appsecret": appsecret,
                "access_token": access_token,
                "expires_in": expires_in,
                "access_token_time": access_token_time,
                "pattern": pattern,
                "extra_confs": extra_confs,
            }
            logger.info("WeChat配置 %s 配置成功：appid=%s", remark, appid)

        if self._rebuild:
            self._rebuild = False
            self.__update_config()

        # token存库
        if len(self._wechat_token_pattern_confs.keys()) > 0:
            self.__save_wechat_confs()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "rebuild": self._rebuild,
            "wechat_confs": self._wechat_confs,
            "ignore_userid": self._ignore_userid,
            "specify_confs": self._specify_confs,
            "history_days": self._history_days,
            "wechat_proxy": self._wechat_proxy
        })

    def __save_wechat_confs(self):
        """通过插件数据持久化令牌缓存，不把敏感字段写入日志。"""
        self.save_data(key="wechat_confs",
                       value=self._wechat_token_pattern_confs)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

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
                                            'label': '开启转发'
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
                                            'model': 'rebuild',
                                            'label': '重建缓存'
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 4
                                },
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dialog_closed",
                                            "label": "设置微信配置"
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '保留历史天数'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'wechat_proxy',
                                            'label': '微信代理'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'ignore_userid',
                                            'rows': '1',
                                            'label': '忽略userid',
                                            'placeholder': '开始下载|添加下载任务失败'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'specify_confs',
                                            'rows': '2',
                                            'label': '特定消息指定用户',
                                            'placeholder': 'title > text > userid'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {
                            'style': {
                                'margin-top': '12px'
                            },
                        },
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
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': '配置教程请参考：'
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/WeChatForward.md',
                                                    'target': '_blank'
                                                },
                                                'text': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/WeChatForward.md'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VDialog",
                        "props": {
                            "model": "dialog_closed",
                            "max-width": "65rem",
                            "overlay-class": "v-dialog--scrollable v-overlay--scroll-blocked",
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t"
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "设置微信配置"
                                },
                                "content": [
                                    {
                                        "component": "VDialogCloseBtn",
                                        "props": {
                                            "model": "dialog_closed"
                                        }
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {},
                                        "content": [
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
                                                                'component': 'VAceEditor',
                                                                'props': {
                                                                    'modelvalue': 'wechat_confs',
                                                                    'lang': 'json',
                                                                    'theme': 'monokai',
                                                                    'style': 'height: 30rem',
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
                                                                    'variant': 'tonal'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'span',
                                                                        'text': '注意：只有正确配置微信配置时，该配置项才会生效，详细配置参考。'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
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
            "rebuild": False,
            "wechat_proxy": "https://qyapi.weixin.qq.com",
            "ignore_userid": "",
            "specify_confs": "",
            "history_days": 7,
            "wechat_confs": json.dumps(WeChatForward.example, indent=4, ensure_ascii=False)
        }

    def get_page(self) -> List[dict]:
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        # 按照时间倒序
        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        msgs = [
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
                        'text': history.get("time")
                    },
                    {
                        'component': 'td',
                        'text': f"{history.get('appid')}{history.get('remark') if history.get('remark') else ''}"
                    },
                    {
                        'component': 'td',
                        'text': history.get("userid")
                    },
                    {
                        'component': 'td',
                        'text': history.get("title")
                    },
                    {
                        'component': 'td',
                        'text': history.get("text")
                    }
                ]
            } for history in historys
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
                                                'text': 'time'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': 'appid'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': 'userid'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': 'title'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': 'text'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    @staticmethod
    def __is_wechat_channel(channel: Any) -> bool:
        """判断通知渠道是否为企业微信，兼容事件快照的枚举值和字符串值。"""
        return channel in (NotificationChannel.Wechat, NotificationChannel.Wechat.value)

    @eventmanager.register(EventType.NoticeMessage)
    async def send(self, event: Event):
        """按通知标题和配置规则转发消息。"""
        if not self._enabled or not self._wechat_token_pattern_confs:
            logger.error("插件未启用或未配置微信配置")
            return

        snapshot = event.snapshot()
        data = snapshot.payload
        if data is None:
            logger.warning("通知事件 payload 无法按 V3 契约解析：%s", snapshot.errors)
            return

        channel = data.channel
        if channel and not self.__is_wechat_channel(channel):
            return

        title = data.title or ""
        text = data.text or ""
        # image 是历史通知载荷的扩展字段，V3 契约允许但不要求该字段。
        image = (data.model_extra or {}).get("image")
        base_userid = data.userid

        # 遍历配置匹配正则发送消息；复制项目避免异步令牌刷新时修改字典影响迭代。
        for wechat_appid, wechat_conf in list(self._wechat_token_pattern_confs.items()):
            if not wechat_conf:
                continue

            access_token = None
            pattern = wechat_conf.get("pattern")
            if pattern and self.__regex_search(pattern, title):
                userid = base_userid
                if self._ignore_userid and self.__regex_search(self._ignore_userid, title):
                    userid = None
                else:
                    userid = self.__specify_userid(title=title, text=text, userid=userid)

                access_token = await self.__flush_access_token(appid=wechat_appid)
                if not access_token:
                    logger.error("未获取到有效token，请检查配置")
                    continue

                if image:
                    await self.__send_image_message(
                        title=title,
                        text=text,
                        image_url=image,
                        userid=userid,
                        access_token=access_token,
                        appid=wechat_appid,
                    )
                else:
                    await self.__send_message(
                        title=title,
                        text=text,
                        userid=userid,
                        access_token=access_token,
                        appid=wechat_appid,
                    )

            # 开始下载 > userid > {name} 后台下载任务已提交，请耐心等候入库通知。 > appid
            # 已添加订阅 > userid > {name} 电视剧正在更新，已添加订阅，待更新后自动下载。 > appid
            extra_confs = wechat_conf.get("extra_confs")
            if extra_confs:
                if not access_token:
                    access_token = await self.__flush_access_token(appid=wechat_appid)
                if access_token:
                    await self.__send_extra_msg(
                        wechat_appid=wechat_appid,
                        extra_confs=extra_confs,
                        access_token=access_token,
                        title=title,
                        text=text,
                    )

    @staticmethod
    def __regex_search(pattern: Any, value: str) -> bool:
        """执行用户配置的正则；配置错误时跳过当前规则而不中断事件分发。"""
        try:
            return bool(re.search(str(pattern), value))
        except (re.error, TypeError):
            logger.error("无效的微信转发正则：%s", pattern)
            return False

    def __specify_userid(self, title, text, userid):
        """按标题和正文规则覆盖消息接收用户。"""
        if self._specify_confs:
            for specify_conf in self._specify_confs.split("\n"):
                if not specify_conf:
                    continue
                # 跳过注释
                if str(specify_conf).startswith("#"):
                    continue
                specify = specify_conf.split(" > ")
                if len(specify) != 3:
                    continue
                if self.__regex_search(specify[0], title) and (
                        self.__regex_search(specify[1], text)
                        or self.__regex_search(specify[1], title)
                ):
                    userid = specify[2]
                    logger.info(f"消息 {title} {text} 指定用户 {userid}")
                    break

        return userid

    async def __send_extra_msg(self, wechat_appid, extra_confs, access_token, title, text):
        """按自定义规则向指定用户发送额外消息。"""
        self._extra_msg_history = await self.async_get_data(key="extra_msg") or {}
        if not isinstance(self._extra_msg_history, dict):
            self._extra_msg_history = {}

        is_save_history = False
        for extra_conf in extra_confs:
            if not isinstance(extra_conf, dict):
                continue

            extra_pattern = extra_conf.get("pattern")
            extra_userid = extra_conf.get("userid")
            extra_msg = str(extra_conf.get("msg") or "")
            if not extra_pattern or not extra_userid or not self.__regex_search(extra_pattern, title):
                continue

            logger.info(f"{title} 正则匹配到额外消息 {extra_pattern}")
            if "{name}" in extra_msg:
                extra_msg = extra_msg.replace('{name}', self.__parse_tv_title(title))
            target_userids = {
                str(user).strip() for user in str(extra_userid).split(",") if str(user).strip()
            }

            if "已完成订阅" in title:
                for subscribe in await self.__list_subscribe_history():
                    if not self.__is_completed_subscribe(title, subscribe):
                        continue
                    user_id = subscribe.username
                    logger.info(f"{title} 获取到订阅用户 {user_id}")
                    if user_id and str(user_id) in target_userids:
                        logger.info(f"{title} 消息用户 {user_id} 匹配到目标用户 {extra_userid}")
                        sent = await self.__send_image_message(
                            title=title,
                            text=extra_msg,
                            userid=str(user_id),
                            access_token=access_token,
                            appid=wechat_appid,
                            image_url=subscribe.backdrop,
                        )
                        if sent:
                            logger.info(f"{wechat_appid} 发送额外消息 {extra_msg} 成功")
                    break
                continue

            user_id = self.__extract_userid(text)
            if not user_id:
                logger.error(f"{title} 未获取到用户，跳过处理")
                continue
            logger.info(f"{title} 获取到消息用户 {user_id}")
            if user_id not in target_userids:
                continue

            if "开始下载" in title:
                history_key = f"{user_id}-{self.__parse_tv_title(title)}"
                extra_history_time = self._extra_msg_history.get(history_key)
                if extra_history_time:
                    try:
                        sent_at = datetime.strptime(extra_history_time, '%Y-%m-%d %H:%M:%S')
                    except (TypeError, ValueError):
                        sent_at = None
                    if sent_at and (datetime.now() - sent_at).total_seconds() < 600:
                        logger.warning(f"{title} 额外消息 {self.__parse_tv_title(title)} 十分钟内重复发送，跳过。")
                        continue

                subscribes = await self._subscribe_oper.async_list_by_username(
                    username=str(user_id),
                    state="R",
                    mtype=MediaType.TV.value,
                )
                if any(f"{subscribe.name} ({subscribe.year})" in title for subscribe in subscribes):
                    logger.warning(f"{title} 额外消息 {self.__parse_tv_title(title)} 用户 {user_id} 已订阅，不再发送额外消息。")
                    continue

            logger.info(f"{title} 消息用户 {user_id} 匹配到目标用户 {extra_userid}")
            sent = await self.__send_message(
                title=extra_msg,
                userid=user_id,
                access_token=access_token,
                appid=wechat_appid,
            )
            if sent:
                logger.info(f"{title} {wechat_appid} 发送额外消息 {extra_msg} 成功")
                if "开始下载" in title:
                    self._extra_msg_history[history_key] = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(time.time())
                    )
                    is_save_history = True

        if is_save_history:
            await self.async_save_data(key="extra_msg", value=self._extra_msg_history)

    async def __list_subscribe_history(self) -> List[Any]:
        """分页读取影视订阅历史，避免直接依赖宿主 ORM 模型。"""
        subscribes = []
        page_size = 100
        for media_type in (MediaType.MOVIE.value, MediaType.TV.value):
            page = 1
            while True:
                page_items = await self._subscribe_history_oper.async_list_by_type(
                    mtype=media_type,
                    page=page,
                    count=page_size,
                )
                if not page_items:
                    break
                subscribes.extend(page_items)
                if len(page_items) < page_size:
                    break
                page += 1
        return sorted(subscribes, key=lambda item: item.id, reverse=True)

    @staticmethod
    def __is_completed_subscribe(title: str, subscribe: Any) -> bool:
        """判断订阅历史是否对应通知中的完成标题。"""
        base_title = f"{subscribe.name} ({subscribe.year})"
        if f"{base_title} 已完成订阅" == title:
            return True
        if subscribe.season is None:
            return False
        return f"{base_title} S{str(subscribe.season).rjust(2, '0')} 已完成订阅" == title

    @staticmethod
    def __extract_userid(text: str) -> Optional[str]:
        """从通知正文兼容提取用户标识。"""
        for pattern in (r"用户：(.*?)\n", r"\*用户\*：(.*?)\n", r"来自用户：(.*?)$"):
            result = re.search(pattern, text)
            if result:
                return result.group(1).strip()
        return None

    def __parse_tv_title(self, title):
        """
        解析title标题
        """
        titles = title.split(" ")
        _title = ""
        for sub_title_str in titles:
            # 电影 功夫熊猫 (2008) 开始下载
            # 电影 功夫熊猫 (2008) 已添加订阅
            # 电视剧 追风者 (2024) S01 E01-E04 开始下载
            # 电视剧 追风者 (2024) S01 已添加订阅
            # 电视剧 追风者 (2024) S01 已完成订阅
            if '开始下载' in sub_title_str:
                continue
            if '已添加订阅' in sub_title_str:
                continue
            if '已完成订阅' in sub_title_str:
                continue
            _title += f"{sub_title_str} "
        return self.__convert_season_episode(str(_title.rstrip()))

    @staticmethod
    def __convert_season_episode(text):
        season_pattern = re.compile(r'S(\d+)')
        episode_pattern = re.compile(r'E(\d+)')

        def replace_season(match):
            return f'第{int(match.group(1)):,}季'

        def replace_episode(match):
            return f'第{int(match.group(1)):,}集'

        def convert_episode_range(text):
            pattern = re.compile(r'E(\d+)-E(\d+)')
            result = pattern.sub(lambda x: f'第{int(x.group(1)):02d}-{int(x.group(2)):02d}集', text)
            return result

        text = re.sub(season_pattern, replace_season, text)

        if text.count("-") == 1:
            text = convert_episode_range(text)
        else:
            text = re.sub(episode_pattern, replace_episode, text)

        return text

    async def __flush_access_token(self, appid: Any, force: bool = False):
        """获取指定应用的有效令牌，并在刷新后异步持久化。"""
        wechat_confs = self._wechat_token_pattern_confs.get(appid)
        if not wechat_confs:
            logger.error(f"未获取到 {appid} 配置信息，请检查配置")
            return None

        access_token = wechat_confs.get("access_token")
        expires_in = wechat_confs.get("expires_in")
        access_token_time = wechat_confs.get("access_token_time")
        refresh_token = force or not access_token
        if not refresh_token:
            try:
                refresh_token = (
                    datetime.now() - datetime.strptime(access_token_time, '%Y-%m-%d %H:%M:%S')
                ).total_seconds() >= int(expires_in)
            except (TypeError, ValueError):
                refresh_token = True

        if refresh_token:
            access_token, expires_in, access_token_time = await self.__get_access_token_async(
                corpid=wechat_confs.get("corpid"),
                appsecret=wechat_confs.get("appsecret"),
            )
            if not access_token:
                logger.error(f"WeChat配置 {appid} 获取token失败，请检查配置")
                return None

            wechat_confs.update({
                "access_token": access_token,
                "expires_in": expires_in,
                "access_token_time": access_token_time,
            })
            self._wechat_token_pattern_confs[appid] = wechat_confs
            await self.async_save_data(key="wechat_confs", value=self._wechat_token_pattern_confs)

        return access_token

    async def __send_message(self, title: str, text: str = None, userid: str = None,
                             access_token: str = None, appid: Any = None) -> Optional[bool]:
        """
        发送文本消息
        :param title: 消息标题
        :param text: 消息内容
        :param userid: 消息发送对象的ID，为空则发给所有人
        :return: 发送状态，错误信息
        """
        if text:
            conent = "%s\n%s" % (title, text.replace("\n\n", "\n"))
        else:
            conent = title

        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "text",
            "agentid": appid,
            "text": {
                "content": conent
            },
            "safe": 0,
            "enable_id_trans": 0,
            "enable_duplicate_check": 0
        }
        return await self.__post_request(
            access_token=access_token,
            req_json=req_json,
            appid=appid,
            title=title,
            text=text,
            userid=userid,
        )

    async def __send_image_message(self, title: str, image_url: str, text: str = None, userid: str = None,
                                   access_token: str = None, appid: Any = None) -> Optional[bool]:
        """
        发送图文消息
        :param title: 消息标题
        :param text: 消息内容
        :param image_url: 图片地址
        :param userid: 消息发送对象的ID，为空则发给所有人
        :return: 发送状态，错误信息
        """
        if text:
            text = text.replace("\n\n", "\n")
        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "news",
            "agentid": appid,
            "news": {
                "articles": [
                    {
                        "title": title,
                        "description": text,
                        "picurl": image_url,
                        "url": ''
                    }
                ]
            }
        }
        return await self.__post_request(
            access_token=access_token,
            req_json=req_json,
            appid=appid,
            title=title,
            text=text,
            userid=userid,
        )

    async def __post_request(self, access_token: str, req_json: dict, appid: Any, title: str, retry: int = 0,
                             text: str = None, userid: str = None) -> bool:
        """向微信发送请求并记录成功的消息。"""
        message_url = self._send_msg_url % (self._wechat_proxy, access_token)
        try:
            async with AsyncRequestUtils(content_type='application/json').response_manager(
                    "POST",
                    message_url,
                    data=json.dumps(req_json, ensure_ascii=False).encode('utf-8'),
            ) as res:
                if res is None:
                    logger.error(f"转发 配置 {appid} 消息 {title} {req_json} 失败，未获取到返回信息")
                    return False
                if res.status_code != 200:
                    reason = getattr(res, "reason_phrase", "")
                    logger.error(
                        f"转发 配置 {appid} 消息 {title} {req_json} 失败，错误码：{res.status_code}，错误原因：{reason}"
                    )
                    return False

                ret_json = res.json()
                if ret_json.get('errcode') == 0:
                    logger.info(f"转发 配置 {appid} 消息 {title} {req_json} 成功")
                    history = await self.async_get_data('history') or []
                    if isinstance(history, dict):
                        history = [history]
                    if not isinstance(history, list):
                        history = []
                    wechat_conf = self._wechat_token_pattern_confs.get(appid) or {}
                    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                    history.append({
                        "appid": appid,
                        "remark": f"({wechat_conf.get('remark')})" if wechat_conf.get('remark') else "",
                        "title": title,
                        "text": text,
                        "userid": userid,
                        "time": now,
                    })
                    cutoff = time.time() - self._history_days * 24 * 60 * 60
                    valid_history = []
                    for record in history:
                        if not isinstance(record, dict):
                            continue
                        try:
                            record_time = datetime.strptime(
                                record.get("time"), '%Y-%m-%d %H:%M:%S'
                            ).timestamp()
                        except (TypeError, ValueError):
                            continue
                        if record_time >= cutoff:
                            valid_history.append(record)
                    await self.async_save_data(key="history", value=valid_history)
                    return True

                if ret_json.get('errcode') == 81013:
                    return False

                logger.error(f"转发 配置 {appid} 消息 {title} {req_json} 失败，错误信息：{ret_json}")
                if ret_json.get('errcode') not in (42001, 40014) or retry >= 3:
                    return False

            logger.info("token已过期，正在重新刷新token重试")
            access_token = await self.__flush_access_token(appid=appid, force=True)
            if not access_token:
                return False
            return await self.__post_request(
                access_token=access_token,
                req_json=req_json,
                appid=appid,
                title=title,
                retry=retry + 1,
                text=text,
                userid=userid,
            )
        except Exception as err:
            logger.error(f"转发 配置 {appid} 消息 {title} {req_json} 异常，错误信息：{str(err)}")
            return False

    def __get_access_token(self, corpid: str, appsecret: str):
        """同步获取微信令牌，供插件初始化阶段使用。"""
        try:
            token_url = self._token_url % (self._wechat_proxy, corpid, appsecret)
            with RequestUtils().response_manager("GET", token_url) as res:
                if res:
                    ret_json = res.json()
                    if ret_json.get('errcode') == 0:
                        return (
                            ret_json.get('access_token'),
                            ret_json.get('expires_in'),
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    logger.error(f"{ret_json.get('errmsg')}")
                    return None, None, None
                logger.error(f"{corpid} 获取token失败")
                return None, None, None
        except Exception as e:
            logger.error(f"获取微信access_token失败，错误信息：{str(e)}")
            return None, None, None

    async def __get_access_token_async(self, corpid: str, appsecret: str):
        """异步获取微信令牌，供通知事件和过期重试使用。"""
        try:
            token_url = self._token_url % (self._wechat_proxy, corpid, appsecret)
            async with AsyncRequestUtils().response_manager("GET", token_url) as res:
                if res:
                    ret_json = res.json()
                    if ret_json.get('errcode') == 0:
                        return (
                            ret_json.get('access_token'),
                            ret_json.get('expires_in'),
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    logger.error(f"{ret_json.get('errmsg')}")
                    return None, None, None
                logger.error(f"{corpid} 获取token失败")
                return None, None, None
        except Exception as e:
            logger.error(f"获取微信access_token失败，错误信息：{str(e)}")
            return None, None, None

    def stop_service(self):
        """插件没有常驻任务，无需额外停止操作。"""
        return None
