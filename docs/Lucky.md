# Lucky HomePage自定义API

![img.png](../img/HomePage/img.png)

HomePage services.yaml配置
```angular2html
- Media:
    - Lucky:
        icon: /icons/icon/lucky.png
        href: http://lucky_ip:lucky_port
        ping: http://lucky_ip:lucky_port
        # server: unraid
        # container: lucky
        showStats: true
        widget:
            type: customapi
            url: http://MoviePilot_IP:NGINX_PORT/api/v1/plugin/Lucky/lucky?apikey=api_token
            method: GET
            mappings:
                - field: enabled_cnt
                  label: 启用配置数量
                - field: closed_cnt
                  label: 关闭配置数量
                - field: ipaddr
                  label: 公网ip地址
                - field: expire_time
                  label: 证书过期日期
                - field: total_cnt
                  label: 总配置数量
                # - field: connections
                #   label: 链接数
                # - field: trafficIn
                #   label: 流量In
                # - field: trafficOut
                #   label: 流量Out
```

### HomePage自定义API文档
https://gethomepage.dev/latest/widgets/services/customapi/#custom-request-body