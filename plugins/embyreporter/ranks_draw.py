import os
from pathlib import Path

import pytz
import random
from io import BytesIO
from PIL import Image
from PIL import ImageFont
from PIL import ImageDraw
from datetime import datetime
from utils import draw_text_psd_style

"""
Misty 周榜海报样式
你可以根据你的需求自行封装或更改为你自己的周榜海报样式！
"""


class RanksDraw:
    def __init__(self, emby, res_path):
        # 默认路径 默认图
        if not res_path:
            res_path = os.path.join(Path(__file__).parent, "res")
        # 绘图文件路径初始化
        bg_path = os.path.join(res_path, "bg")
        mask_path = os.path.join(res_path, "cover-ranks-mask-2.png")
        font_path = os.path.join(res_path, "PingFang Bold.ttf")
        # 随机调取背景, 路径: res/ranks/bg/...
        bg_list = os.listdir(bg_path)
        bg_path = os.path.join(bg_path, bg_list[random.randint(0, len(bg_list) - 1)])
        # 初始绘图对象
        self.bg = Image.open(bg_path)
        mask = Image.open(mask_path)
        self.bg.paste(mask, (0, 0), mask)
        self.font = ImageFont.truetype(font_path, 18)
        self.font_small = ImageFont.truetype(font_path, 14)
        self.font_count = ImageFont.truetype(font_path, 12)
        # 初始化封面对象
        self.emby = emby

    def draw(self, movies, tvshows, show_count=True):
        # 合并绘制
        all_ranks = movies + tvshows
        index, offset_y = (0, 0)
        for i in all_ranks:
            # 榜单项数据
            user_id, item_id, item_type, name, count, duarion = tuple(i)
            print(item_type, item_id, name, count)
            # 图片获取，剧集主封面获取
            if item_type != "Movie":
                # 获取剧ID
                success, data = self.emby.items(user_id, item_id)
                if not success:
                    exit(data)
                item_id = data["SeriesId"]
            # 封面图像获取
            success, data = self.emby.primary(item_id)
            if not success:
                exit(data)
            # 剧集Y偏移
            if index >= 5:
                index = 0
                offset_y = 331
            # 名称显示偏移
            font_offset_y = 0
            temp_font = self.font
            # 名称超出长度缩小省略
            if self.font.getlength(name) > 110:
                temp_font = self.font_small
                font_offset_y = 4
                for i in range(len(name)):
                    name = name[:len(name) - 1]
                    if self.font.getlength(name) <= 110:
                        break
                name += ".."
            # 绘制封面
            cover = Image.open(BytesIO(data))
            cover = cover.resize((108, 159))
            self.bg.paste(cover, (73 + 145 * index, 379 + offset_y))
            # 绘制 播放次数、影片名称
            text = ImageDraw.Draw(self.bg)
            if show_count:
                draw_text_psd_style(text, (177 + 145 * index - self.font_count.getlength(str(count)), 353 + offset_y),
                                    str(count), self.font_count, 126)
            draw_text_psd_style(text, (74 + 145 * index, 542 + font_offset_y + offset_y), name, temp_font, 126)
            index += 1

    def save(self, save_path=os.path.join("/public", "ranks",
                                          datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d.jpg"))):
        self.bg.save(save_path)
        return save_path
