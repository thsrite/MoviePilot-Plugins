import sqlite3

from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger


class SqlExecute(_PluginBase):
    # 插件名称
    plugin_name = "Sql执行器"
    # 插件描述
    plugin_desc = "自定义MoviePilot数据库Sql执行。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/sqlite.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "sqlexecute_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _onlyonce = None
    _sql = None

    def init_plugin(self, config: dict = None):
        if config:
            self._onlyonce = config.get("onlyonce")
            self._sql = config.get("sql")

            if self._onlyonce and self._sql:
                # 读取sqlite数据
                try:
                    gradedb = sqlite3.connect("/config/user.db")
                except Exception as e:
                    logger.error(f"数据库链接失败 {str(e)}")
                    return

                # 创建游标cursor来执行executeＳＱＬ语句
                cursor = gradedb.cursor()

                # 执行SQL语句
                try:
                    for sql in self._sql.split("\n"):
                        logger.info(f"开始执行SQL语句 {sql}")
                        # 执行SQL语句
                        cursor.execute(sql)

                        print(cursor.fetchall())
                except Exception as e:
                    logger.error(f"SQL语句执行失败 {str(e)}")
                    return
                finally:
                    # 关闭游标
                    cursor.close()

                    self._onlyonce = False
                    self.update_config({
                        "onlyonce": self._onlyonce,
                        "sql": self._sql
                    })

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
                                            'model': 'onlyonce',
                                            'label': '执行sql'
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
                                            'model': 'sql',
                                            'rows': '2',
                                            'label': 'sql语句',
                                            'placeholder': '一行一条'
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
                                                'text': '执行日志将会输出到控制台，请谨慎操作。'
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
            "onlyonce": False,
            "sql": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
