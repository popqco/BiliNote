from app.downloaders.bilibili_downloader import BilibiliDownloader
from app.downloaders.douyin_downloader import DouyinDownloader
from app.downloaders.kuaishou_downloader import KuaiShouDownloader
from app.downloaders.local_downloader import LocalDownloader
from app.downloaders.xiaohongshu_downloader import XiaohongshuDownloader
from app.downloaders.youtube_downloader import YoutubeDownloader

# 存类型而不是实例：下载器带 cancel_check 等每任务状态，共享单例会让并发任务
# 互相覆盖取消谓词（后写的任务把前一个任务的停止信号顶掉）。
SUPPORT_PLATFORM_MAP = {
    'youtube': YoutubeDownloader,
    'bilibili': BilibiliDownloader,
    'tiktok': DouyinDownloader,
    'kuaishou': KuaiShouDownloader,
    'douyin': DouyinDownloader,
    'xiaohongshu': XiaohongshuDownloader,
    'local': LocalDownloader,
}
