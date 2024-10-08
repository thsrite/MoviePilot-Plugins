# 云盘助手

### 使用说明

提供四种方式，具体看示例
```

直接转移--softlink回本地
{
  "transfer_type": "move",
  "return_mode": "softlink",
  "monitor_dirs": [
      {
          "retention_time": 0,
          "monitor_mode": "fast",
          /* 监控模式 compatibility/ */
          "dest_path": "/series/link",
          /* MP媒体库文件夹 */
          "mount_path": "/115/CloudDrive/115/video",
          /* MP网盘挂载文件夹 */
          "return_path": "/115link/link",
          /* 软连接生成文件夹 */
          "delete_dest": "false",
          /* 是否删除种子下载文件夹 */
          "dest_preserve_hierarchy": 0,
          /* 保留监控路径目录层级，例如 1：表示保留监控目录后一层目录结构，0：表示仅保留到监控目录 */
          "delete_history": "false",
          /* 是否删除MoviePilot中转移历史记录 */
          "delete_src": "false",
          /* 是否删除做种文件 */
          "src_paths": "/series/download",
          /* 做种文件夹 */
          "src_preserve_hierarchy": 0,
          /* 保留做种文件夹目录层级，0：表示仅监控到源文件目录，1：表示监控源文件目录及其一级子目录 */
          "only_media": "true",
          /* 是否只监控媒体文件 */
          "overwrite": "false",
          /* 是否覆盖已存在云盘文件 */
          "upload_cloud": "true"
          /* 是否上传到云盘, false则直接软连接或者strm回本地 */
      }
  ]
}

直接转移--strm回本地
{
    "transfer_type": "copy/move",
    "return_mode": "strm",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "dest_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "return_path": "/mnt/strm/movies",
            "library_dir": "/mnt/movies",
            "cloud_type": "alist/cd2",
            "cloud_path": "/CloudNas",
            "cloud_url": "http://localhost:19798",
            "cloud_scheme": "http/https",
            "delete_dest": "false",
            "dest_preserve_hierarchy": 0,
            "delete_history": "false",
            "delete_src": "false",
            "src_paths": "/mnt/media/movies, /mnt/media/series",
            "src_preserve_hierarchy": 0,
            "only_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}


cd2方式上传--softlink回本地（暂时移除）
{
    "cd2_url": "cd2地址：http://localhost:19798",
    "username": "用户名",
    "password": "密码",
    "return_mode": "softlink",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "dest_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "cd2_path": "/115/media/movies",
            "return_path": "/mnt/softlink/movies",
            "delete_dest": "false",
            "dest_preserve_hierarchy": 0,
            "delete_history": "false",
            "delete_src": "false",
            "src_paths": "/mnt/media/movies, /mnt/media/series",
            "src_preserve_hierarchy": 0,
            "only_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}

cd2方式上传--strm回本地（暂时移除）
{
    "cd2_url": "cd2地址：http://localhost:19798",
    "username": "用户名",
    "password": "密码",
    "return_mode": "strm",
    "monitor_dirs": [
        {
            "monitor_mode": "监控模式 compatibility/fast",
            "dest_path": "/mnt/link/movies",
            "mount_path": "/mnt/cloud/115/media/movies",
            "cd2_path": "/115/media/movies",
            "return_path": "/mnt/strm/movies",
            "library_dir": "/mnt/movies",
            "cloud_type": "alist/cd2",
            "cloud_path": "/CloudNas",
            "cloud_url": "http://localhost:19798",
            "cloud_scheme": "http/https",
            "delete_dest": "false",
            "dest_preserve_hierarchy": 0,
            "delete_history": "false",
            "delete_src": "false",
            "src_paths": "/mnt/media/movies, /mnt/media/series",
            "src_preserve_hierarchy": 0,
            "only_media": "true",
            "overwrite": "false",
            "upload_cloud": "true"
        }
    ]
}


```
- return_mode: 云盘文件回本地模式：softlink/strm
- return_path：MoviePilot中软链接/strm生成路径

- retention_time: 本地文件保留时长（小时） 当前日期与文件创建日期的时间差（小时），大于此值的文件将被转移
- monitor_mode：监控模式 compatibility/fast
- tranfer_type：转移类型，可选值：copy/move
- dest_path: MoviePilot本地刮削好的文件路径（MoviePilot媒体库目录）
- mount_path：MoviePilot中云盘挂载路径

- delete_dest：是否删除媒体库文件
- dest_preserve_hierarchy：保留监控路径目录层级，例如 1：表示保留监控目录后一层目录结构，0：表示仅保留到监控目录

- delete_history：是否删除MoviePilot中转移历史记录

- delete_src：是否删除源文件，仅上述监控路径查询到转移记录时才生效，删除转移记录的源文件路径
- src_paths：转移前的源文件路径，多个目录用逗号分隔（MoviePilot下载目录）
- src_preserve_hierarchy：保留源文件路径目录层级，0：表示仅监控到源文件目录，1：表示监控源文件目录及其一级子目录
- 
- only_media：是否只监控媒体文件
- overwrite：是否覆盖已存在云盘文件
- upload_cloud: 是否上传到云盘, false则直接软连接或者strm回本地
- notify_url: 软连接或者strm回本地成功后，通知接口地址，post请求参数：`{"path": "文件路径", "type": "add"}`

- strm配置具体看[CloudStrm.md](CloudStrm.md)
- library_dir：strm模式下，媒体服务器内源文件路径
- cloud_type：strm模式下，云盘类型，可选值：alist/cd2  （`不填就是本地模式`）
- cloud_path：strm模式下，cd2/alist挂载本地跟路径
- cloud_url：strm模式下，cd2/alist地址
- cloud_scheme：strm模式下，cd2/alist地址 http/https（strm模式可参考云盘Strm生成插件）
- 
[//]: # (- cd2_url：cd2地址)
[//]: # (- username：cd2用户名)
[//]: # (- password：cd2密码)
[//]: # (- cd2_path：cd2中云盘挂载路径)

路径：

- 监控目录：源文件目录即云盘挂载到MoviePilot中的路径
- 目的路径：MoviePilot中strm生成路径
- 媒体服务器内源文件路径：源文件目录即云盘挂载到媒体服务器的路径

示例：

- MoviePilot上云盘源文件路径 /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- MoviePilot上strm生成路径 /mnt/link/aliyun`/tvshow/爸爸去哪儿/Season 5/14.特别版.strm`

- 媒体服务器内源文件路径 /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- 监控配置为：/mount/cloud/aliyun/emby#/mnt/link/aliyun#/mount/cloud/aliyun/emby
