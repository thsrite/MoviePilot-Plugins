# 云盘助手

### 使用说明

提供四种方式，具体看示例

```
cd2方式上传--softlink回本地
{
    "cd2_url": "cd2地址：http://localhost:19798",
    "username": "用户名",
    "password": "密码",
    "return_mode": "softlink",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "local_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "cd2_path": "/115/media/movies",
            "return_path": "/mnt/softlink/movies",
            "delete_local": "false",
            "delete_history": "false",
            "just_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}

cd2方式上传--strm回本地
{
    "cd2_url": "cd2地址：http://localhost:19798",
    "username": "用户名",
    "password": "密码",
    "return_mode": "strm",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "local_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "cd2_path": "/115/media/movies",
            "return_path": "/mnt/strm/movies",
            "library_dir": "/mnt/movies",
            "cloud_type": "alist/cd2",
            "cloud_path": "/CloudNas",
            "cloud_url": "http://localhost:19798",
            "cloud_scheme": "http/https",
            "delete_local": "false",
            "delete_history": "false",
            "just_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}

直接转移--softlink回本地
{
    "transfer_type": "copy/move/rclone_move/rclone_copy",
    "return_mode": "softlink",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "local_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "return_path": "/mnt/softlink/movies",
            "delete_local": "false",
            "delete_history": "false",
            "just_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}

直接转移--strm回本地
{
    "transfer_type": "copy/move/rclone_move/rclone_copy",
    "return_mode": "strm",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "local_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "return_path": "/mnt/strm/movies",
            "library_dir": "/mnt/movies",
            "cloud_type": "alist/cd2",
            "cloud_path": "/CloudNas",
            "cloud_url": "http://localhost:19798",
            "cloud_scheme": "http/https",
            "delete_local": "false",
            "delete_history": "false",
            "just_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}
```

- return_mode: 云盘文件回本地模式：softlink/strm
- cd2_url：cd2地址
- username：cd2用户名
- password：cd2密码
- tranfer_type：转移类型，可选值：copy/move/rclone_move/rclone_copy
- local_path: MoviePilot本地上传路径
- mount_path：MoviePilot中云盘挂载路径
- cd2_path：cd2中云盘挂载路径
- return_path：MoviePilot中软链接/strm生成路径
- monitor_mode：监控模式 compatibility/fast
- delete_local：是否删除本地文件
- delete_history：是否删除MoviePilot中转移历史记录
- just_media：是否只监控媒体文件
- overwrite：是否覆盖已存在云盘文件
- upload_cloud: 是否上传到云盘,false则直接软连接或者strm回本地
- library_dir：strm模式下，媒体服务器内源文件路径
- cloud_type：strm模式下，云盘类型，可选值：alist/cd2
- cloud_path：strm模式下，cd2/alist挂载本地跟路径
- cloud_url：strm模式下，cd2/alist地址
- cloud_scheme：strm模式下，cd2/alist地址 http/https（strm模式可参考云盘Strm生成插件）

路径：

- 监控目录：源文件目录即云盘挂载到MoviePilot中的路径
- 目的路径：MoviePilot中strm生成路径
- 媒体服务器内源文件路径：源文件目录即云盘挂载到媒体服务器的路径

示例：

- MoviePilot上云盘源文件路径 /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- MoviePilot上strm生成路径 /mnt/link/aliyun`/tvshow/爸爸去哪儿/Season 5/14.特别版.strm`

- 媒体服务器内源文件路径 /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- 监控配置为：/mount/cloud/aliyun/emby#/mnt/link/aliyun#/mount/cloud/aliyun/emby
