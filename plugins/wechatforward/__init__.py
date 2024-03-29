import json
import re
import time
from datetime import datetime

from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.modules.wechat import WeChat
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
    plugin_version = "1.3"
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
    _wechat = None
    _pattern = None
    _ignore_userid = None
    _extra_confs = None
    _pattern_token = {}
    _extra_msg_history = {}

    # 企业微信发送消息URL
    _send_msg_url = f"{settings.WECHAT_PROXY}/cgi-bin/message/send?access_token=%s"
    # 企业微信获取TokenURL
    _token_url = f"{settings.WECHAT_PROXY}/cgi-bin/gettoken?corpid=%s&corpsecret=%s"

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._wechat = config.get("wechat")
            self._pattern = config.get("pattern")
            self._ignore_userid = config.get("ignore_userid")
            self._extra_confs = config.get("extra_confs")

            # 获取token存库
            if self._enabled and self._wechat:
                self.__save_wechat_token()

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
                                    'md': 6
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
                                            'model': 'wechat',
                                            'rows': '5',
                                            'label': '应用配置',
                                            'placeholder': 'appid:corpid:appsecret（一行一个配置）'
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
                                            'model': 'pattern',
                                            'rows': '6',
                                            'label': '正则配置',
                                            'placeholder': '对应上方应用配置，一行一个，一一对应'
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
                                            'model': 'extra_confs',
                                            'rows': '2',
                                            'label': '额外配置',
                                            'placeholder': '开始下载 > userid > 后台下载任务已提交，请耐心等候入库通知。 > appid'
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
                                            'text': '根据正则表达式，把MoviePilot的消息转发到多个微信应用。'
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
                                            'text': '应用配置可加注释：'
                                                    'appid:corpid:appsecret#站点通知'
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
            "wechat": "",
            "pattern": "",
            "ignore_userid": "",
            "extra_confs": ""
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event):
        """
        消息转发
        """
        if not self._enabled:
            return

        # 消息体
        data = event.event_data
        channel = data['channel']
        if channel and channel != MessageChannel.Wechat:
            return

        title = data['title']
        text = data['text']
        image = data['image']
        userid = data['userid']

        # 正则匹配
        patterns = self._pattern.split("\n")
        for index, pattern in enumerate(patterns):
            msg_match = re.search(pattern, title)
            if msg_match:
                access_token, appid = self.__flush_access_token(index)
                if not access_token:
                    logger.error("未获取到有效token，请检查配置")
                    continue

                # 忽略userid正则表达式
                if self._ignore_userid and re.search(self._ignore_userid, title):
                    userid = None

                # 发送消息
                if image:
                    self.__send_image_message(title, text, image, userid, access_token, appid, index)
                else:
                    self.__send_message(title, text, userid, access_token, appid, index)

                # 开始下载 > userid > {name} 后台下载任务已提交，请耐心等候入库通知。 > appid
                # 已添加订阅 > userid > {name} 电视剧正在更新，已添加订阅，待更新后自动下载。 > appid
                if self._extra_confs:
                    self.__send_extra_msg(title, text)

    def __send_extra_msg(self, title, text):
        """
        根据自定义规则发送额外消息
        """
        self._extra_msg_history = self.get_data(key="extra_msg") or {}
        is_save_history = False
        extra_confs = self._extra_confs.split("\n")
        for extra_conf in extra_confs:
            extras = str(extra_conf).split(" > ")
            if len(extras) != 4:
                continue
            extra_pattern = extras[0]
            extra_userid = extras[1]
            extra_title = extras[2]
            extra_appid = extras[3]
            if str(extra_title).find('{name}') != -1:
                extra_title = extra_title.replace('{name}', self.__parse_tv_title(title))
            if re.search(extra_pattern, title):
                logger.info(f"{title} 正则匹配到额外消息 {extra_pattern}")
                # 搜索消息，获取消息text中的用户
                userid_pattern = r"用户：(.*?)\n"
                result = re.search(userid_pattern, text)
                if not result:
                    # 订阅消息，获取消息text中的用户
                    pattern = r"来自用户：(.*?)$"
                    result = re.search(pattern, text)
                    if not result:
                        continue
                # 获取消息text中的用户
                user_id = result.group(1)
                logger.info(f"获取到消息用户 {user_id}")
                if user_id and any(user_id == user for user in extra_userid.split(",")):
                    if "开始下载" in str(title):
                        # 判断是否重复发送，10分钟内重复消息title、重复userid算重复消息
                        extra_history_time = self._extra_msg_history.get(
                            f"{user_id}-{self.__parse_tv_title(title)}") or None
                        # 只处理下载消息
                        if extra_history_time:
                            logger.info(
                                f"获取到额外消息上次发送时间 {datetime.strptime(extra_history_time, '%Y-%m-%d %H:%M:%S')}")
                            if (datetime.now() - datetime.strptime(extra_history_time,
                                                                   '%Y-%m-%d %H:%M:%S')).total_seconds() < 600:
                                logger.warn(
                                    f"额外消息 {self.__parse_tv_title(title)} 十分钟内重复发送，跳过。")
                                continue
                        # 判断当前用户是否订阅，是否订阅后续消息
                        subscribes = SubscribeOper().list(state="R")
                        is_subscribe = False
                        for subscribe in subscribes:
                            if subscribe.type == MediaType.TV.value and str(subscribe.username) == str(user_id):
                                # 匹配订阅title
                                if f"{subscribe.name} ({subscribe.year})" in title:
                                    is_subscribe = True
                        # 电视剧之前该用户订阅下载过，不再发送额外消息
                        if is_subscribe:
                            logger.warn(
                                f"额外消息 {self.__parse_tv_title(title)} 用户 {user_id} 已订阅，不再发送额外消息。")
                            continue

                    logger.info(f"消息用户{user_id} 匹配到目标用户 {extra_userid}")
                    # 发送额外消息
                    if str(settings.WECHAT_APP_ID) == str(extra_appid):
                        # 直接发送
                        WeChat().send_msg(title=extra_title, userid=user_id)
                        logger.info(f"{settings.WECHAT_APP_ID} 发送额外消息 {extra_title} 成功")
                        # 保存已发送消息
                        if "开始下载" in str(title):
                            self._extra_msg_history[f"{user_id}-{self.__parse_tv_title(title)}"] = time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                            is_save_history = True
                    else:
                        for wechat_idx in self._pattern_token.keys():
                            wechat_conf = self._pattern_token.get(wechat_idx)
                            if (wechat_conf and wechat_conf.get("appid")
                                    and str(wechat_conf.get("appid")) == str(extra_appid)):
                                access_token, appid = self.__flush_access_token(wechat_idx)
                                if not access_token:
                                    logger.error("未获取到有效token，请检查配置")
                                    continue
                                self.__send_message(title=extra_title,
                                                    userid=user_id,
                                                    access_token=access_token,
                                                    appid=appid,
                                                    index=wechat_idx)
                                logger.info(f"{appid} 发送额外消息 {extra_title} 成功")
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
            # 电视剧 追风者 (2024) S01 E01-E04 已添加订阅
            if 'E' in sub_title_str:
                continue
            if '开始下载' in sub_title_str:
                continue
            if '已添加订阅' in sub_title_str:
                continue
            _title += f"{sub_title_str} "
        return str(_title.rstrip())

    def __save_wechat_token(self):
        """
        获取并存储wechat token
        """
        # 解析配置
        wechats = self._wechat.split("\n")
        for index, wechat in enumerate(wechats):
            # 排除注释
            wechat = wechat.split("#")[0]
            wechat_config = wechat.split(":")
            if len(wechat_config) != 3:
                logger.error(f"{wechat} 应用配置不正确")
                continue
            appid = wechat_config[0]
            corpid = wechat_config[1]
            appsecret = wechat_config[2]

            # 已过期，重新获取token
            access_token, expires_in, access_token_time = self.__get_access_token(corpid=corpid,
                                                                                  appsecret=appsecret)
            if not access_token:
                # 没有token，获取token
                logger.error(f"wechat配置 appid = {appid} 获取token失败，请检查配置")
                continue

            self._pattern_token[index] = {
                "appid": appid,
                "corpid": corpid,
                "appsecret": appsecret,
                "access_token": access_token,
                "expires_in": expires_in,
                "access_token_time": access_token_time,
            }

    def __flush_access_token(self, index: int, force: bool = False):
        """
        获取第i个配置wechat token
        """
        wechat_token = self._pattern_token[index]
        if not wechat_token:
            logger.error(f"未获取到第 {index} 条正则对应的wechat应用token，请检查配置")
            return None
        access_token = wechat_token['access_token']
        expires_in = wechat_token['expires_in']
        access_token_time = wechat_token['access_token_time']
        appid = wechat_token['appid']
        corpid = wechat_token['corpid']
        appsecret = wechat_token['appsecret']

        # 判断token有效期
        if force or (datetime.now() - access_token_time).seconds >= expires_in:
            # 重新获取token
            access_token, expires_in, access_token_time = self.__get_access_token(corpid=corpid,
                                                                                  appsecret=appsecret)
            if not access_token:
                logger.error(f"wechat配置 appid = {appid} 获取token失败，请检查配置")
                return None, None

        self._pattern_token[index] = {
            "appid": appid,
            "corpid": corpid,
            "appsecret": appsecret,
            "access_token": access_token,
            "expires_in": expires_in,
            "access_token_time": access_token_time,
        }
        return access_token, appid

    def __send_message(self, title: str, text: str = None, userid: str = None, access_token: str = None,
                       appid: str = None, index: int = None) -> Optional[bool]:
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
        return self.__post_request(access_token=access_token, req_json=req_json, index=index, title=title)

    def __send_image_message(self, title: str, text: str, image_url: str, userid: str = None,
                             access_token: str = None, appid: str = None, index: int = None) -> Optional[bool]:
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
        return self.__post_request(access_token=access_token, req_json=req_json, index=index, title=title)

    def __post_request(self, access_token: str, req_json: dict, index: int, title: str, retry: int = 0) -> bool:
        message_url = self._send_msg_url % access_token
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
                    logger.info(f"转发消息 {title} 成功")
                    return True
                else:
                    if ret_json.get('errcode') == 81013:
                        return False

                    logger.error(f"转发消息 {title} 失败，错误信息：{ret_json}")
                    if ret_json.get('errcode') == 42001 or ret_json.get('errcode') == 40014:
                        logger.info("token已过期，正在重新刷新token重试")
                        # 重新获取token
                        access_token, appid = self.__flush_access_token(index=index,
                                                                        force=True)
                        if access_token:
                            retry += 1
                            # 重发请求
                            if retry <= 3:
                                return self.__post_request(access_token=access_token,
                                                           req_json=req_json,
                                                           index=index,
                                                           title=title,
                                                           retry=retry)
                    return False
            elif res is not None:
                logger.error(f"转发消息 {title} 失败，错误码：{res.status_code}，错误原因：{res.reason}")
                return False
            else:
                logger.error(f"转发消息 {title} 失败，未获取到返回信息")
                return False
        except Exception as err:
            logger.error(f"转发消息 {title} 异常，错误信息：{str(err)}")
            return False

    def __get_access_token(self, corpid: str, appsecret: str):
        """
        获取微信Token
        :return： 微信Token
        """
        try:
            token_url = self._token_url % (corpid, appsecret)
            res = RequestUtils().get_res(token_url)
            if res:
                ret_json = res.json()
                if ret_json.get('errcode') == 0:
                    access_token = ret_json.get('access_token')
                    expires_in = ret_json.get('expires_in')
                    access_token_time = datetime.now()

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
