from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.utilities import StringUtils


class LuckyStatusData(BaseModel):
    """Lucky HomePage 接口返回的状态数据。"""

    model_config = ConfigDict(populate_by_name=True)

    total_cnt: int = Field(description="反向代理规则总数")
    enabled_cnt: int = Field(description="已启用的反向代理规则数")
    closed_cnt: int = Field(description="已停用的反向代理规则数")
    ipaddr: Optional[str] = Field(default=None, description="DDNS 当前 IPv4 地址")
    expire_time: Optional[str] = Field(default=None, description="首个证书的到期日期")
    connections: int = Field(description="当前连接总数")
    traffic_in: str = Field(alias="trafficIn", description="格式化后的入站流量")
    traffic_out: str = Field(alias="trafficOut", description="格式化后的出站流量")


class Lucky(_PluginBase):
    # 插件名称
    plugin_name = "Lucky"
    # 插件描述
    plugin_desc = "Lucky HomePage自定义API。"
    # 插件图标
    plugin_icon = "Lucky_A.png"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "lucky_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 任务执行间隔
    _enabled = False
    _open_token: Optional[str] = None
    _base_url: Optional[str] = None
    _request: Optional[RequestUtils] = None

    def init_plugin(self, config: dict = None):
        """读取配置并重建外部请求客户端，避免热重载沿用旧凭据。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._base_url = (
            str(config.get("baseUrl") or "").strip().rstrip("/") or None
        )
        self._open_token = str(config.get("openToken") or "").strip() or None
        self._request = RequestUtils(timeout=20) if self._base_url and self._open_token else None

    def _request_json(self, path: str) -> Optional[dict]:
        """请求 Lucky OpenToken API，并拒绝失败或非对象响应。"""
        if not self._request or not self._base_url or not self._open_token:
            logger.warning("Lucky 地址或 OpenToken 未配置完整")
            return None

        try:
            with self._request.response_manager(
                "GET",
                f"{self._base_url}{path}",
                params={"_": int(time.time() * 1000), "openToken": self._open_token},
                raise_exception=True,
            ) as response:
                if response is None:
                    return None
                response.raise_for_status()
                payload = response.json()
        except Exception as error:
            logger.warning("Lucky API 请求失败：%s", type(error).__name__)
            return None

        if not isinstance(payload, dict):
            logger.warning("Lucky API 返回了非对象响应")
            return None
        if payload.get("ret") != 0:
            logger.warning("Lucky API 返回失败状态：%s", payload.get("ret"))
            return None
        return payload

    def get_rules(self) -> Tuple[List[dict], int, int, int]:
        """汇总 Lucky 反向代理规则和连接流量统计。"""
        rules = []
        connections = 0
        traffic_in = 0
        traffic_out = 0
        payload = self._request_json("/api/webservice/rules") or {}
        for rule in payload.get("ruleList") or []:
            if not isinstance(rule, dict):
                continue
            proxy_list = rule.get("ProxyList")
            if isinstance(proxy_list, list):
                rules.extend(item for item in proxy_list if isinstance(item, dict))
        statistics = payload.get("statistics")
        if isinstance(statistics, dict):
            for statistic in statistics.values():
                if not isinstance(statistic, dict):
                    continue
                connections += self._as_int(statistic.get("Connections"))
                traffic_in += self._as_int(statistic.get("TrafficIn"))
                traffic_out += self._as_int(statistic.get("TrafficOut"))
        return rules, connections, traffic_in, traffic_out

    def get_ip(self) -> Optional[str]:
        """返回 Lucky 第一个 DDNS 任务的 IPv4 地址。"""
        payload = self._request_json("/api/ddnstasklist") or {}
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        return data[0].get("IpAddr") or data[0].get("Ipv4Addr")

    def get_ssl(self) -> Optional[str]:
        """返回 Lucky 第一个证书的到期时间。"""
        payload = self._request_json("/api/ssl") or {}
        certificates = payload.get("list")
        if (
            not isinstance(certificates, list)
            or not certificates
            or not isinstance(certificates[0], dict)
        ):
            return None
        cert_info = certificates[0].get("CertsInfo")
        if isinstance(cert_info, list):
            cert_info = cert_info[0] if cert_info else None
        return cert_info.get("NotAfterTime") if isinstance(cert_info, dict) else None

    @staticmethod
    def _as_int(value: Any) -> int:
        """将 Lucky 统计字段归一化为非负整数。"""
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def get_state(self) -> bool:
        return self._enabled

    def lucky(self) -> Dict[str, Any]:
        """
        返回 Lucky 反向代理、连接、流量、DDNS 和证书状态。
        """
        rules, connections, traffic_in, traffic_out = self.get_rules()
        enabled_cnt = 0
        closed_cnt = 0
        for rule in rules:
            if rule.get('Enable'):
                enabled_cnt += 1
            else:
                closed_cnt += 1

        ipaddr = self.get_ip()
        expire_time = self.get_ssl()
        if expire_time:
            expire_time = expire_time.split(' ')[0].replace('-', '')

        logger.info(
            f"Proxy Rules Total: {len(rules)}\n"
            f"Proxy Rules Enabled: {enabled_cnt}\n"
            f"Proxy Rules Closed: {closed_cnt}\n"
            f"Connections: {connections}\n"
            f"TrafficIn: {traffic_in}\n"
            f"TrafficOut: {traffic_out}\n"
            f"Lucky IP: {ipaddr}\n"
            f"SSL Expire Time: {expire_time}\n")

        return {
            'total_cnt': len(rules),
            'enabled_cnt': enabled_cnt,
            'closed_cnt': closed_cnt,
            'ipaddr': ipaddr,
            'expire_time': expire_time,
            'connections': connections,
            'trafficIn': StringUtils.str_filesize(traffic_in),
            'trafficOut': StringUtils.str_filesize(traffic_out)
        }

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令。"""
        return []

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
        return [{
            "path": "/lucky",
            "endpoint": self.lucky,
            "methods": ["GET"],
            "summary": "Lucky HomePage自定义API",
            "description": "Lucky",
            "auth": "apikey",
            "response_model": LuckyStatusData,
        }]

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
                                            'label': '启用插件',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'baseUrl',
                                            'label': 'Lucky地址',
                                            'placeholder': 'http://localhost:16601 (结尾没有/)'
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
                                            'model': 'openToken',
                                            'label': 'openToken',
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
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Lucky.md',
                                                    'target': '_blank'
                                                },
                                                'text': '需自行前往Lucky设置开启OpenToken并重启Lucky。'
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
                                            'variant': 'tonal',
                                            'text': '如安装完启用插件后，HomePage提示404，重启MoviePilot即可。'
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
            "baseUrl": "",
            "openToken": "",
        }

    def get_page(self) -> List[dict]:
        data = self.lucky()
        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '总配置数量'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('total_cnt')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '启用配置数量'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('enabled_cnt')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '关闭配置数量'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('closed_cnt')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '公网ip地址'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('ipaddr')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '证书过期日期'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('expire_time')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '链接数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('connections')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '流量In'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('trafficIn')
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
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '流量Out'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': data.get('trafficOut')
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
                    },
                ]
            }]

    def stop_service(self):
        """
        退出插件
        """
        pass
