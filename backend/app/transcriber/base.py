from abc import ABC, abstractmethod

from app.models.transcriber_model import TranscriptResult


class Transcriber(ABC):
    # 单次请求的音频大小上限（字节）；None = 无上限（本地引擎不经上传，整文件处理）。
    # 有上限的远端引擎覆写此值，分段转写编排器（segmented.py）据此触发切分。
    max_audio_size_bytes = None

    @abstractmethod
    def transcript(self,file_path:str)->TranscriptResult:
        '''

        :param file_path:音频路径
        :return: 返回一个 TranscriptResult 类
        '''
        pass

    def on_finish(self,video_path:str,result: TranscriptResult)->None:
        '''
        当音频转录完成时调用
        :param video_path: 视频路径
        :param result: 识别结果
        :return:
        '''
        pass