from app.plugins import _PluginBase
from app.db.subscribe_oper import SubscribeOper
from typing import Any, List, Dict, Tuple
from app.log import logger


class SubscribeClear(_PluginBase):
    # 插件名称
    plugin_name = "清理订阅缓存"
    # 插件描述
    plugin_desc = "清理订阅已下载集数。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugin-Market/main/icons/subscribeclear.png"
    # 主题色
    plugin_color = "#80bef7"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribeclear_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 任务执行间隔
    _subscribe_ids = None
    subscribe = None

    def init_plugin(self, config: dict = None):
        self.subscribe = SubscribeOper()
        if config:
            self._subscribe_ids = config.get("subscribe_ids")
            if self._subscribe_ids:
                # 遍历 清理订阅下载缓存
                for subscribe_id in self._subscribe_ids:
                    self.subscribe.update(subscribe_id, {'note': ""})
                    logger.info(f"订阅 {subscribe_id} 下载缓存已清理")

                self.update_config(
                    {
                        "subscribe_ids": []
                    }
                )

    def get_state(self) -> bool:
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        subscribe_options = [{"title": subscribe.name, "value": subscribe.id} for subscribe in
                             self.subscribe.list('R') if subscribe.type == '电视剧']
        return [
            {
                'component': 'VForm',
                'content': [
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
                                            'model': 'subscribe_ids',
                                            'label': '电视剧订阅',
                                            'items': subscribe_options
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
                                            'text': '请选择需要清理缓存的订阅，用于清理该订阅已下载集数。'
                                                    '注意！！！未入库的会被重新下载。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "subscribe_ids": []
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
