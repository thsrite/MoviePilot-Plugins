import json
import re
import time
from datetime import datetime

from app.db.models.subscribehistory import SubscribeHistory
from app.db.subscribe_oper import SubscribeOper
from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas.types import EventType, MessageChannel, MediaType
from app.utils.http import RequestUtils
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger


class WeChatForward(_PluginBase):
    # 插件名称
    plugin_name = "微信消息转发"
    # 插件描述
    plugin_desc = "根据正则转发通知到其他WeChat应用。"
    # 插件图标
    plugin_icon = "Wechat_A.png"
    # 插件版本
    plugin_version = "2.8"
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
    _enabled = False
    _rebuild = False
    _wechat_confs = None
    _specify_confs = None
    _ignore_userid = None
    _wechat_token_pattern_confs = {}
    _extra_msg_history = {}
    _history_days = None
    _wechat_proxy = None

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

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._rebuild = config.get("rebuild")
            self._wechat_confs = config.get("wechat_confs") or []
            self._ignore_userid = config.get("ignore_userid")
            self._specify_confs = config.get("specify_confs")
            self._wechat_proxy = config.get("wechat_proxy")
            self._history_days = config.get("history_days") or 7

            # 获取token存库
            if self._enabled and self._wechat_confs:
                self.__save_wechat_token()

    def __save_wechat_token(self):
        """
        获取并存储wechat token
        """
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
            except Exception as e:
                self.__parse_token()

    def __parse_token(self):
        """
        解析token存库
        """
        # 解析配置
        for wechat in json.loads(self._wechat_confs):
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
            logger.info(f"WeChat配置 {remark} 配置成功：{self._wechat_token_pattern_confs[appid]}")

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
        """
        保存wechat配置
        """
        self.save_data(key="wechat_confs",
                       value=self._wechat_token_pattern_confs)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event):
        """
        消息转发
        """
        if not self._enabled or not self._wechat_token_pattern_confs:
            logger.error("插件未启用或未配置微信配置")
            return

        # 消息体
        data = event.event_data
        channel = data.get("channel")
        if channel and channel != MessageChannel.Wechat:
            return

        title = data.get("title")
        text = data.get("text")
        image = data.get("image")
        userid = data.get("userid")

        # 遍历配置 匹配正则 发送消息
        for wechat_appid in self._wechat_token_pattern_confs.keys():
            wechat_conf = self._wechat_token_pattern_confs.get(wechat_appid)
            if not wechat_conf or not wechat_conf.get("pattern"):
                continue

            # 匹配正则
            if re.search(wechat_conf.get("pattern"), title):
                # 忽略userid
                if self._ignore_userid and re.search(self._ignore_userid, title):
                    userid = None
                else:
                    # 特定消息指定用户
                    userid = self.__specify_userid(title=title, text=text, userid=userid)

                access_token = self.__flush_access_token(appid=wechat_appid)
                if not access_token:
                    logger.error("未获取到有效token，请检查配置")
                    continue

                # 发送消息
                if image:
                    self.__send_image_message(title=title, text=text, image_url=image, userid=userid,
                                              access_token=wechat_conf.get("access_token"), appid=wechat_appid)
                else:
                    self.__send_message(title=title, text=text, userid=userid,
                                        access_token=wechat_conf.get("access_token"),
                                        appid=wechat_appid)

            # 发送额外消息
            # 开始下载 > userid > {name} 后台下载任务已提交，请耐心等候入库通知。 > appid
            # 已添加订阅 > userid > {name} 电视剧正在更新，已添加订阅，待更新后自动下载。 > appid
            if wechat_conf.get("extra_confs"):
                self.__send_extra_msg(wechat_appid=wechat_appid,
                                      extra_confs=wechat_conf.get("extra_confs"),
                                      access_token=wechat_conf.get("access_token"),
                                      title=title,
                                      text=text)

    def __specify_userid(self, title, text, userid):
        """
        特定消息指定用户
        """
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
                if re.search(specify[0], title) and (re.search(specify[1], text) or re.search(specify[1], title)):
                    userid = specify[2]
                    logger.info(f"消息 {title} {text} 指定用户 {userid}")
                    break

        return userid

    def __send_extra_msg(self, wechat_appid, extra_confs, access_token, title, text):
        """
        根据自定义规则发送额外消息
        """
        self._extra_msg_history = self.get_data(key="extra_msg") or {}
        is_save_history = False
        for extra_conf in extra_confs:
            if not extra_conf:
                continue

            extra_pattern = extra_conf.get("pattern")
            extra_userid = extra_conf.get("userid")
            extra_msg = extra_conf.get("msg")

            # 正则匹配额外消息表达式
            if re.search(extra_pattern, title):
                logger.info(f"{title} 正则匹配到额外消息 {extra_pattern}")

                # 处理变量{name}
                if str(extra_msg).find('{name}') != -1:
                    extra_msg = extra_msg.replace('{name}', self.__parse_tv_title(title))

                # 订阅完成消息单独处理
                if "已完成订阅" in str(title):
                    # 查订阅历史的用户
                    subscribes = SubscribeHistory().list()
                    # 倒叙
                    subscribes = sorted(subscribes, key=lambda x: x.id, reverse=True)
                    for subscribe in subscribes:
                        # 匹配订阅title
                        if f"{subscribe.name} ({subscribe.year}) 已完成订阅" == title \
                                or f"{subscribe.name} ({subscribe.year}) S{str(subscribe.season).rjust(2, '0')} 已完成订阅" == title:
                            user_id = subscribe.username
                            logger.info(f"{title} 获取到订阅用户 {user_id}")
                            if user_id and any(user_id == user for user in extra_userid.split(",")):
                                logger.info(f"{title} 消息用户 {user_id} 匹配到目标用户 {extra_userid}")
                                self.__send_image_message(title=title,
                                                          text=extra_msg,
                                                          userid=user_id,
                                                          access_token=access_token,
                                                          appid=wechat_appid,
                                                          image_url=subscribe.backdrop)
                                logger.info(f"{wechat_appid} 发送额外消息 {extra_msg} 成功")
                            break
                else:
                    # 搜索消息，获取消息text中的用户
                    result = re.search(r"用户：(.*?)\n", text)
                    if not result:
                        result = re.search(r"\*用户\*：(.*?)\n", text)
                        if not result:
                            # 订阅消息，获取消息text中的用户
                            pattern = r"来自用户：(.*?)$"
                            result = re.search(pattern, text)
                            if not result:
                                logger.error(f"{title} 未获取到用户，跳过处理")
                                continue

                    # 获取消息text中的用户
                    user_id = result.group(1)
                    logger.info(f"{title} 获取到消息用户 {user_id}")
                    if user_id and any(user_id == user for user in extra_userid.split(",")):
                        if "开始下载" in str(title):
                            # 判断是否重复发送，10分钟内重复消息title、重复userid算重复消息
                            extra_history_time = self._extra_msg_history.get(
                                f"{user_id}-{self.__parse_tv_title(title)}") or None
                            # 只处理下载消息
                            if extra_history_time:
                                logger.info(
                                    f"{title} 获取到额外消息上次发送时间 {datetime.strptime(extra_history_time, '%Y-%m-%d %H:%M:%S')}")
                                if (datetime.now() - datetime.strptime(extra_history_time,
                                                                       '%Y-%m-%d %H:%M:%S')).total_seconds() < 600:
                                    logger.warn(
                                        f"{title} 额外消息 {self.__parse_tv_title(title)} 十分钟内重复发送，跳过。")
                                    continue
                            # 判断当前用户是否订阅，是否订阅后续消息
                            subscribes = SubscribeOper().list_by_username(username=str(user_id),
                                                                          state="R",
                                                                          mtype=MediaType.TV.value)
                            is_subscribe = False
                            for subscribe in subscribes:
                                # 匹配订阅title
                                if f"{subscribe.name} ({subscribe.year})" in title:
                                    is_subscribe = True
                                    break

                            # 电视剧之前该用户订阅下载过，不再发送额外消息
                            if is_subscribe:
                                logger.warn(
                                    f"{title} 额外消息 {self.__parse_tv_title(title)} 用户 {user_id} 已订阅，不再发送额外消息。")
                                continue

                        logger.info(f"{title} 消息用户 {user_id} 匹配到目标用户 {extra_userid}")

                        self.__send_message(title=extra_msg,
                                            userid=user_id,
                                            access_token=access_token,
                                            appid=wechat_appid)
                        logger.info(f"{title} {wechat_appid} 发送额外消息 {extra_msg} 成功")
                        # 保存已发送消息
                        if "开始下载" in str(title):
                            self._extra_msg_history[
                                f"{user_id}-{self.__parse_tv_title(title)}"] = time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                            is_save_history = True

        # 保存额外消息历史
        if is_save_history:
            self.save_data(key="extra_msg",
                           value=self._extra_msg_history)

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

    def __flush_access_token(self, appid: int, force: bool = False):
        """
        获取appid wechat token
        """
        wechat_confs = self._wechat_token_pattern_confs[appid]
        if not wechat_confs:
            logger.error(f"未获取到 {appid} 配置信息，请检查配置")
            return None

        access_token = wechat_confs.get("access_token")
        expires_in = wechat_confs.get("expires_in")
        access_token_time = wechat_confs.get("access_token_time")
        corpid = wechat_confs.get("corpid")
        appsecret = wechat_confs.get("appsecret")

        # 判断token有效期
        if force or (datetime.now() - datetime.strptime(access_token_time, '%Y-%m-%d %H:%M:%S')).seconds >= expires_in:
            # 重新获取token
            access_token, expires_in, access_token_time = self.__get_access_token(corpid=corpid,
                                                                                  appsecret=appsecret)

            if not access_token:
                logger.error(f"WeChat配置 {appid} 获取token失败，请检查配置")
                return None

            # 更新token回配置
            wechat_confs.update({
                "access_token": access_token,
                "expires_in": expires_in,
                "access_token_time": access_token_time,
            })
            self._wechat_token_pattern_confs[appid] = wechat_confs
            # 更新回库
            self.__save_wechat_confs()

        return access_token

    def __send_message(self, title: str, text: str = None, userid: str = None,
                       access_token: str = None, appid: int = None) -> Optional[bool]:
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
        return self.__post_request(access_token=access_token, req_json=req_json, appid=appid, title=title, text=text,
                                   userid=userid)

    def __send_image_message(self, title: str, image_url: str, text: str = None, userid: str = None,
                             access_token: str = None, appid: int = None) -> Optional[bool]:
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
        return self.__post_request(access_token=access_token, req_json=req_json, appid=appid, title=title, text=text,
                                   userid=userid)

    def __post_request(self, access_token: str, req_json: dict, appid: int, title: str, retry: int = 0,
                       text: str = None, userid: str = None) -> bool:
        message_url = self._send_msg_url % (self._wechat_proxy, access_token)
        """
        向微信发送请求
        """
        try:
            res = RequestUtils(content_type='application/json').post(
                message_url,
                data=json.dumps(req_json, ensure_ascii=False).encode('utf-8')
            )
            if res and res.status_code == 200:
                ret_json = res.json()
                if ret_json.get('errcode') == 0:
                    logger.info(f"转发 配置 {appid} 消息 {title} {req_json} 成功")
                    # 读取历史记录
                    history = self.get_data('history') or []
                    history.append({
                        "appid": appid,
                        "remark": f"({self._wechat_token_pattern_confs.get(appid).get('remark')})" if self._wechat_token_pattern_confs.get(
                            appid).get('remark') else "",
                        "title": title,
                        "text": text,
                        "userid": userid,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                    })
                    thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                    history = [record for record in history if
                               datetime.strptime(record["time"],
                                                 '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
                    # 保存历史
                    self.save_data(key="history", value=history)
                    return True
                else:
                    if ret_json.get('errcode') == 81013:
                        return False

                    logger.error(f"转发 配置 {appid} 消息 {title} {req_json} 失败，错误信息：{ret_json}")
                    if ret_json.get('errcode') == 42001 or ret_json.get('errcode') == 40014:
                        logger.info("token已过期，正在重新刷新token重试")
                        # 重新获取token
                        access_token = self.__flush_access_token(appid=appid,
                                                                 force=True)
                        if access_token:
                            retry += 1
                            # 重发请求
                            if retry <= 3:
                                return self.__post_request(access_token=access_token,
                                                           req_json=req_json,
                                                           appid=appid,
                                                           title=title,
                                                           retry=retry,
                                                           text=text,
                                                           userid=userid)
                    return False
            elif res is not None:
                logger.error(
                    f"转发 配置 {appid} 消息 {title} {req_json} 失败，错误码：{res.status_code}，错误原因：{res.reason}")
                return False
            else:
                logger.error(f"转发 配置 {appid} 消息 {title} {req_json} 失败，未获取到返回信息")
                return False
        except Exception as err:
            logger.error(f"转发 配置 {appid} 消息 {title} {req_json} 异常，错误信息：{str(err)}")
            return False

    def __get_access_token(self, corpid: str, appsecret: str):
        """
        获取微信Token
        :return： 微信Token
        """
        try:
            token_url = self._token_url % (self._wechat_proxy, corpid, appsecret)
            res = RequestUtils().get_res(token_url)
            if res:
                ret_json = res.json()
                if ret_json.get('errcode') == 0:
                    access_token = ret_json.get('access_token')
                    expires_in = ret_json.get('expires_in')
                    access_token_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                    return access_token, expires_in, access_token_time
                else:
                    logger.error(f"{ret_json.get('errmsg')}")
                    return None, None, None
            else:
                logger.error(f"{corpid} {appsecret} 获取token失败")
                return None, None, None
        except Exception as e:
            logger.error(f"获取微信access_token失败，错误信息：{str(e)}")
            return None, None, None

    def stop_service(self):
        """
        退出插件
        """
        pass


if __name__ == '__main__':
    def __parse_tv_title(title):
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
            if '开始下载' in sub_title_str:
                continue
            if '已添加订阅' in sub_title_str:
                continue
            _title += f"{sub_title_str} "
        return __convert_season_episode(str(_title.rstrip()))


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


    print(__parse_tv_title("时光代理人 (2021) S02 E01-E22 开始下载"))
