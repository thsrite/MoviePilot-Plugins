import pytz
import requests
from cacheout import Cache
from datetime import datetime, timedelta

cache = Cache()


class EmbyService():
    PLAYBACK_REPORTING_TYPE_MOVIE = "ItemName"
    PLAYBACK_REPORTING_TYPE_TVSHOWS = "substr(ItemName,0, instr(ItemName, ' - '))"

    def __init__(self, host, api_key):
        self.host = host
        self.base_url = host + "/emby/{0}?api_key=" + api_key
        self.api_key = api_key

    @cache.memoize(ttl=600)
    def primary(self, item_id, width=200, height=300, quality=90, ret_url=False):
        url = self.host + f"/emby/Items/{item_id}/Images/Primary?maxHeight={height}&maxWidth={width}&quality={quality}"
        if ret_url:
            return url
        resp = requests.get(url)

        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        return True, resp.content

    @cache.memoize(ttl=600)
    def backdrop(self, item_id, width=1920, quality=70, ret_url=False):
        url = self.host + f"/emby/Items/{item_id}/Images/Backdrop/0?&maxWidth={width}&quality={quality}"
        if ret_url:
            return url
        resp = requests.get(url)

        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        return True, resp.content

    @cache.memoize(ttl=600)
    def logo(self, item_id, quality=70, ret_url=False):
        url = self.host + f"/emby/Items/{item_id}/Images/Logo?quality={quality}"
        if ret_url:
            return url
        resp = requests.get(url)

        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        return True, resp.content

    @cache.memoize(ttl=300)
    def items(self, user_id, item_id):
        url = self.base_url.format(f"Users/{user_id}/Items/{item_id}")
        resp = requests.get(url)

        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        return True, resp.json()

    def get_report(self, types=None, user_id=None, days=7, end_date=datetime.now(pytz.timezone("Asia/Shanghai")), limit=10):
        if not types:
            types = self.PLAYBACK_REPORTING_TYPE_MOVIE
        sub_date = end_date - timedelta(days=days)
        start_time = sub_date.strftime("%Y-%m-%d 00:00:00")
        end_time = end_date.strftime("%Y-%m-%d 23:59:59")
        sql = "SELECT UserId, ItemId, ItemType, "
        sql += types + " AS name, "
        sql += "COUNT(1) AS play_count, "
        sql += "SUM(PlayDuration - PauseDuration) AS total_duarion "
        sql += "FROM PlaybackActivity "
        sql += f"WHERE ItemType = '{'Movie' if types==self.PLAYBACK_REPORTING_TYPE_MOVIE else 'Episode'}' "
        sql += f"AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}' "
        sql += "AND UserId not IN (select UserId from UserList) "
        if user_id:
            sql += f"AND UserId = '{user_id}' "
        sql += "GROUP BY name "
        sql += "ORDER BY play_count DESC "
        sql += "LIMIT " + str(limit)

        url = self.base_url.format(f"user_usage_stats/submit_custom_query")
        data = {
            "CustomQueryString": sql,
            "ReplaceUserId": False
        }
        resp = requests.post(url, data)
        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        ret = resp.json()
        if len(ret["colums"]) == 0:
            return False, ret["message"]
        return True, ret["results"]


class LibraryService:
    def __init__(self, host, api_key):
        self.base_url = host + "/Library/{0}?api_key=" + api_key

    """
    Gets all user media folders
    """

    @cache.memoize(ttl=600)
    def folders(self):
        url = self.base_url.format("SelectableMediaFolders")
        resp = requests.get(url)

        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        return True, resp.json()