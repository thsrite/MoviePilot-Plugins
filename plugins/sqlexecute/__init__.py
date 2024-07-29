import sqlite3

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas.types import EventType, MessageChannel


class SqlExecute(_PluginBase):
    # 插件名称
    plugin_name = "Sql执行器"
    # 插件描述
    plugin_desc = "自定义MoviePilot数据库Sql执行。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/sqlite.png"
    # 插件版本
    plugin_version = "1.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "sqlexecute_"
    # 加载顺序
    plugin_order = 99
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

                        if 'select' in sql.lower():
                            rows = cursor.fetchall()
                            # 获取列名
                            columns = [desc[0] for desc in cursor.description]
                            # 将查询结果转换为key-value对的列表
                            results = []
                            for row in rows:
                                result = dict(zip(columns, row))
                                results.append(result)
                            result = "\n".join([str(i) for i in results])
                            result = str(result).replace("'", "\"")
                            logger.info(result)
                        else:
                            gradedb.commit()
                            result = f"执行成功，影响行数：{cursor.rowcount}"
                            logger.info(result)
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

    @eventmanager.register(EventType.PluginAction)
    def execute(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "sql_execute":
                return
            args = event_data.get("args")
            if not args:
                return

            logger.info(f"收到命令，开始执行SQL ...{args}")

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
                # 执行SQL语句
                cursor.execute(args)
                if 'select' in args.lower():
                    rows = cursor.fetchall()
                    # 获取列名
                    columns = [desc[0] for desc in cursor.description]
                    # 将查询结果转换为key-value对的列表
                    results = []
                    for row in rows:
                        result = dict(zip(columns, row))
                        results.append(result)
                    result = "\n".join([str(i) for i in results])
                    result = str(result).replace("'", "\"")
                    logger.info(result)

                    if event.event_data.get("channel") == MessageChannel.Telegram:
                        result = f"```plaintext\n{result}\n```"
                    self.post_message(channel=event.event_data.get("channel"),
                                      title="SQL执行结果",
                                      text=result,
                                      userid=event.event_data.get("user"))
                else:
                    gradedb.commit()
                    result = f"执行成功，影响行数：{cursor.rowcount}"
                    logger.info(result)

                    if event.event_data.get("channel") == MessageChannel.Telegram:
                        result = f"```plaintext\n{result}\n```"
                    self.post_message(channel=event.event_data.get("channel"),
                                      title="SQL执行结果",
                                      text=result,
                                      userid=event.event_data.get("user"))

            except Exception as e:
                logger.error(f"SQL语句执行失败 {str(e)}")
                return
            finally:
                # 关闭游标
                cursor.close()

    def get_state(self) -> bool:
        return True

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/sql",
            "event": EventType.PluginAction,
            "desc": "自定义sql执行",
            "category": "",
            "data": {
                "action": "sql_execute"
            }
        }]

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
                                                'text': '可使用交互命令/sql select *****'
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
