"""测试用的数据构造函数"""
from comments import Comment
from danmaku import Danmaku
from ranking import VideoInfo


def make_comment(rpid: int, content: str = "好看", **kwargs) -> Comment:
    fields = dict(oid=1, mid=rpid, member_name="user", ctime=1700000000, like=0, rcount=0)
    fields.update(kwargs)
    return Comment(rpid=rpid, content=content, **fields)


def make_danmaku(progress_ms: int, content: str = "哈哈", **kwargs) -> Danmaku:
    fields = dict(id=progress_ms, mode=1, fontsize=25, color=0xFFFFFF, mid_hash="abc",
                  ctime=1700000000, weight=5, pool=0)
    fields.update(kwargs)
    return Danmaku(progress=progress_ms, content=content, **fields)


def make_video(aid: int = 123, **kwargs) -> VideoInfo:
    fields = dict(bvid="", cid=456, title="测试视频", owner_name="up", owner_mid=7, view=10,
                  danmaku=0, reply=0, favorite=0, coin=0, share=0, like=1, duration=600,
                  width=0, height=0, pic="", pubdate=0)
    fields.update(kwargs)
    return VideoInfo(aid=aid, **fields)
