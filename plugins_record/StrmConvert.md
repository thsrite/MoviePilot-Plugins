# Strm文件模式转换

### 更新记录

- 1.0 Strm文件内容转为本地路径或者cd2/alist API路径

### 使用说明

#### 本地模式
- MoviePilot上strm视频根路径  /mnt/link/aliyun`/tvshow/爸爸去哪儿/Season 5/14.特别版.strm`
- 云盘源文件挂载本地后 挂载`进媒体服务器的路径`，与上方对应   /mount/cloud/aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- 转换配置为：`/mnt/link/aliyun#/mount/cloud/aliyun/emby`

#### API模式
- MoviePilot上strm视频根路径  /mnt/link/aliyun`/tvshow/爸爸去哪儿/Season 5/14.特别版.strm`
- cd2挂载后路径 /aliyun/emby`/tvshow/爸爸去哪儿/Season 5/14.特别版.mp4`

- 转换配置为：`/mnt/link/aliyun#/aliyun/emby#cd2#192.168.31.103:19798`


## 具体自己多尝试吧。