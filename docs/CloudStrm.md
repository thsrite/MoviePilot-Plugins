# 云盘strm生成

### 使用说明

目录监控格式：

- 1.增量目录#监控目录#目的目录#媒体服务器内源文件路径
- 2.增量目录#监控目录#目的目录#cd2#cd2挂载本地跟路径#cd2服务地址
- 3.增量目录#监控目录#目的目录#alist#alist挂载本地跟路径#alist服务地址

路径：

- 增量目录：转存到云盘的路径，插件只会扫描该路径下的文件，移动到监控路径，生成目的路径的strm文件
- 监控目录：源文件目录即云盘挂载到MoviePilot中的路径
- 目的路径：MoviePilot中strm生成路径
- 媒体服务器内源文件路径：源文件目录即云盘挂载到媒体服务器的路径

示例：

- 增量目录：/increment`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- MoviePilot上云盘源文件路径 /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- MoviePilot上strm生成路径 /mnt/link/aliyun`/tvshow/爸爸去哪儿/Season 5/14.特别版.strm`

- 媒体服务器内源文件路径 /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- 监控配置为：/increment#/mount/cloud/aliyun/emby#/mnt/link/aliyun#/mount/cloud/aliyun/emby


保留路径：

扫描到增量目录的文件，会移动到监控目录，并生成目的路径的strm文件，删除空的增量目录，如果想保留某些父目录，可以将它们添加到保留路径中。

例如：

/increment/series/庆余年/Season 1/1.第一集.mp4

保留路径为series

则文件移动到目的路径名后，会删除庆余年/Season 1，父路径/increment/series保留

