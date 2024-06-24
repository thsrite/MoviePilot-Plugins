import sqlite3
from pathlib import Path
from typing import List, Tuple, Dict, Any

from app.core.config import Settings
from app.log import logger
from app.plugins import _PluginBase


class LinkToSrc(_PluginBase):
    # 插件名称
    plugin_name = "源文件恢复"
    # 插件描述
    plugin_desc = "根据MoviePilot的转移记录中的硬链文件恢复源文件"
    # 插件图标
    plugin_icon = "Time_machine_A.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "linktosrc_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    _onlyonce: bool = False
    _link_dirs: str = None

    def init_plugin(self, config: dict = None):
        if config:
            self._onlyonce = config.get("onlyonce")
            self._link_dirs = config.get("link_dirs")

        if self._onlyonce:
            # 执行替换
            self._task()
            self._onlyonce = False
        self.__update_config()

    def _task(self):
        db_path = Settings().CONFIG_PATH / 'user.db'
        try:
            gradedb = sqlite3.connect(db_path)
        except Exception as e:
            logger.error(f"无法打开数据库文件 {db_path}，请检查路径是否正确：{str(e)}")
            return

        transfer_history = []
        # 创建游标cursor来执行executeＳＱＬ语句
        cursor = gradedb.cursor()
        if self._link_dirs:
            link_dirs = self._link_dirs.split("\n")
            for link_dir in link_dirs:
                sql = f'''
                       SELECT
                           src,
                           dest
                       FROM
                           transferhistory  
                       WHERE
                           src IS NOT NULL and dest IS NOT NULL and dest like '{link_dir}%';
                           '''
                cursor.execute(sql)
                transfer_history += cursor.fetchall()
        else:
            sql = '''
                   SELECT
                       src,
                       dest
                   FROM
                       transferhistory  
                   WHERE
                       src IS NOT NULL and dest IS NOT NULL;
                       '''
            cursor.execute(sql)
            transfer_history = cursor.fetchall()
        logger.info(f"查询到历史记录{len(transfer_history)}条")
        cursor.close()

        if not transfer_history:
            logger.error("未获取到历史记录，停止处理")
            return

        for history in transfer_history:
            src = history[0]
            dest = history[1]
            # 判断源文件是否存在
            if Path(src).exists():
                logger.warn(f"源文件{src}已存在，跳过处理")
                continue
            # 源文件不存在，目标文件也不存在，跳过
            if not Path(dest).exists():
                logger.warn(f"源文件{src}不存在且硬链文件{dest}不存在，跳过处理")
                continue
            # 创建源文件目录，防止目录不存在无法执行
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            # 目标文件硬链回源文件
            Path(src).hardlink_to(dest)
            logger.info(f"硬链文件{dest}重新链接回源文件{src}")

        logger.info("全部处理完成")

    def __update_config(self):
        self.update_config({
            "onlyonce": self._onlyonce,
            "link_dirs": self._link_dirs
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'link_dirs',
                                            'label': '需要恢复的硬链接目录',
                                            'rows': 5,
                                            'placeholder': '硬链接目录 （一行一个）'
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
                                            'text': '根据转移记录中的硬链接恢复源文件',
                                            'style': 'white-space: pre-line;'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "onlyonce": False,
            "link_dirs": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def get_state(self) -> bool:
        return self._onlyonce

    def stop_service(self):
        pass
