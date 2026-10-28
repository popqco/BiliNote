import base64
import hashlib
import os
import re
import subprocess
import time
from typing import Callable, Optional

from app.exceptions.task_cancel import TaskCancelled
from app.utils.ffmpeg_limiter import ffmpeg_slots
from app.utils.proc import no_window_kwargs
from concurrent.futures import ThreadPoolExecutor, as_completed
import ffmpeg
from PIL import Image, ImageDraw, ImageFont

from app.utils.logger import get_logger
from app.utils.path_helper import get_app_dir

logger = get_logger(__name__)

# 拼图长边上限。视觉模型收到大图后都会先缩到 ~1.5K 长边再切块计费，拼一张
# 2880×1620（3×3 × 960×540）意味着 3/4 的像素是白传的：实测单张 700KB，
# 一条 2h53m 的视频 64 张 = 45MB（base64 后 60MB），一次总结请求就此变成
# 上游难以承受的巨型请求（2026-10-01 实测：免费网关连续 APIConnectionError）。
# 压到 1568px 后单张 ~215KB，同样的 64 张只需 13MB，模型看到的细节没有可感差异。
MAX_GRID_LONG_SIDE = 1568

# 帧文件名里的时间戳：分钟可超过两位（100 分钟以上的视频 frame_101_52.jpg），
# 旧正则 \d{2} 会匹配失败 → 排序 key 变 inf（顺序全乱）、拼图上时间戳消失
_FRAME_TS_RE = re.compile(r"frame_(\d{1,4})_(\d{2})\.jpg")
# 整段抽帧的中间产物名（frame_000001.jpg），重命名后即消失
_NUMBERED_FRAME_RE = re.compile(r"frame_\d{6}\.jpg")

# 整段抽帧（单进程 fps 过滤）vs 逐帧 seek 的选择阈值。2026-10-28 实测（110MB /
# 14 分钟 1080p 视频）：逐帧 seek 8 并发 40 帧要 6.7s（其中 3.6s 是 40 次进程
# 创建的内核开销），882 帧约 147s；整段 fps=1 解一遍只要 22s，且输出直接缩到
# 格子尺寸（单帧 60KB vs 147KB，磁盘与后续 PIL 缩放都省）。但采样间隔稀疏时
# 整段解码（成本与时长成正比）反而比 seek（成本与帧数成正比）贵，故设间隔上限。
FRAME_BATCH_MIN_FRAMES = int(os.getenv("FRAME_BATCH_MIN_FRAMES", "40") or 40)
FRAME_BATCH_MAX_INTERVAL = int(os.getenv("FRAME_BATCH_MAX_INTERVAL", "3") or 3)


class VideoReader:
    def __init__(self,
                 video_path: str,
                 grid_size=(3, 3),
                 frame_interval=2,
                 dedupe_enabled=True,
                 unit_width=960,
                 unit_height=540,
                 save_quality=90,
                 font_path="fonts/arial.ttf",
                 frame_dir=None,
                 grid_dir=None,
                 max_frames=1000,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 progress_cb: Optional[Callable[[int, int], None]] = None):
        self.video_path = video_path
        self.grid_size = grid_size
        self.frame_interval = frame_interval
        self.dedupe_enabled = dedupe_enabled
        self.unit_width = unit_width
        self.unit_height = unit_height
        self.save_quality = save_quality
        self.frame_dir = frame_dir or get_app_dir("output_frames")
        self.grid_dir = grid_dir or get_app_dir("grid_output")
        print(f"视频路径：{video_path}",self.frame_dir,self.grid_dir)
        self.font_path = font_path
        self.max_frames = max_frames
        # 用户「停止生成」谓词：抽帧是逐帧 ffmpeg 子进程（长视频上千次），
        # 每个 worker 取下一帧前检查一次，点停止后几十毫秒内整体退出
        self.cancel_check = cancel_check
        # 抽帧进度回调 (extracted, total)：整段解码一跑几十秒、界面上只有一个
        # 「下载中」，用户完全看不出还在动；调用方用它写 status message。
        self.progress_cb = progress_cb
        self._last_progress_at = 0.0

    def _check_cancelled(self) -> None:
        if self.cancel_check and self.cancel_check():
            raise TaskCancelled()

    @staticmethod
    def _calculate_file_md5(file_path: str) -> str:
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def format_time(self, seconds: float) -> str:
        mm = int(seconds // 60)
        ss = int(seconds % 60)
        return f"{mm:02d}_{ss:02d}"

    def extract_time_from_filename(self, filename: str) -> float:
        match = _FRAME_TS_RE.search(filename)
        if match:
            mm, ss = map(int, match.groups())
            return mm * 60 + ss
        return float('inf')

    def _extract_single_frame(self, ts: int) -> str | None:
        """提取单帧，返回输出路径或 None（失败时）。"""
        self._check_cancelled()
        time_label = self.format_time(ts)
        output_path = os.path.join(self.frame_dir, f"frame_{time_label}.jpg")
        cmd = ["ffmpeg", "-ss", str(ts), "-i", self.video_path, "-frames:v", "1", "-q:v", "2", "-y", output_path,
               "-hide_banner", "-loglevel", "error"]
        try:
            with ffmpeg_slots:
                subprocess.run(cmd, check=True, timeout=120, **no_window_kwargs())
            return output_path
        except subprocess.CalledProcessError:
            return None
        except subprocess.TimeoutExpired:
            logger.warning(f"抽帧超时，跳过 (t={ts}s)")
            return None

    def _report_progress(self, done: int, total: int, force: bool = False) -> None:
        """抽帧进度回调（默认 2s 节流：status 写入带 fsync，太密会拖慢磁盘）。"""
        if not self.progress_cb:
            return
        now = time.monotonic()
        if not force and now - self._last_progress_at < 2.0:
            return
        self._last_progress_at = now
        try:
            self.progress_cb(int(done), int(total))
        except Exception:
            pass

    def _should_use_batch(self, n_frames: int) -> bool:
        """整段解码是否划算：实测见 FRAME_BATCH_* 注释。

        稀疏采样（间隔大）时 seek 的成本只与帧数成正比，整段解码却要按视频时长
        全解一遍，所以间隔超过阈值就退回逐帧。
        """
        if os.getenv("FRAME_EXTRACT_LEGACY", "") == "1":
            return False
        return (
            n_frames >= FRAME_BATCH_MIN_FRAMES
            and 1 <= self.frame_interval <= FRAME_BATCH_MAX_INTERVAL
        )

    def _count_numbered_frames(self) -> int:
        try:
            return sum(1 for n in os.listdir(self.frame_dir) if _NUMBERED_FRAME_RE.fullmatch(n))
        except OSError:
            return 0

    def _extract_frames_batch(self, timestamps: list[int]) -> list[str]:
        """单进程整段抽帧：`fps=1/间隔` + 直接缩到格子尺寸。

        先输出 frame_%06d.jpg，再按 index×interval 换算成 frame_MM_SS.jpg，
        下游（拼图角标、截图编排、降级复用帧缓存）对命名格式完全无感。
        """
        limit = len(timestamps)
        pattern = os.path.join(self.frame_dir, "frame_%06d.jpg")
        vf = f"fps=1/{self.frame_interval}"
        if self.unit_width and self.unit_height:
            vf += f",scale={self.unit_width}:{self.unit_height}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", self.video_path,
            "-vf", vf,
            "-frames:v", str(limit),
            "-q:v", "2",
            pattern,
        ]
        # 超时按「2 倍速解码」兜底：正常 1080p 单流解码有 5-10 倍实时，卡住必是
        # 坏文件/坏管道；留足余量避免误杀大视频。
        timeout_seconds = max(600.0, float(timestamps[-1] + self.frame_interval) * 0.5)
        deadline = time.monotonic() + timeout_seconds
        with ffmpeg_slots:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **no_window_kwargs(),
            )
            try:
                last_progress = 0.0
                while proc.poll() is None:
                    if self.cancel_check and self.cancel_check():
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise TaskCancelled()
                    if time.monotonic() > deadline:
                        proc.kill()
                        raise RuntimeError(f"批量抽帧超时（>{timeout_seconds:.0f}s）")
                    now = time.monotonic()
                    if now - last_progress >= 2.0:
                        last_progress = now
                        self._report_progress(self._count_numbered_frames(), limit, force=True)
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
        if proc.returncode != 0:
            raise RuntimeError(f"批量抽帧失败（ffmpeg rc={proc.returncode}）")

        numbered = []
        for name in os.listdir(self.frame_dir):
            m = re.fullmatch(r"frame_(\d{6})\.jpg", name)
            if m:
                numbered.append((int(m.group(1)), os.path.join(self.frame_dir, name)))
        if not numbered:
            raise RuntimeError("批量抽帧没有产出任何帧")
        numbered.sort()

        paths: list[str] = []
        for idx, (_, src) in enumerate(numbered):
            ts = idx * self.frame_interval
            dst = os.path.join(self.frame_dir, f"frame_{ts // 60:02d}_{ts % 60:02d}.jpg")
            os.replace(src, dst)
            paths.append(dst)
        self._report_progress(len(paths), limit, force=True)
        return paths

    def _dedupe_adjacent(self, image_paths: list[str]) -> list[str]:
        """相邻完全相同的帧去重（截图编排依赖：留下的帧=画面刚变化的位置）。

        先比文件大小再算 MD5——整段解码出来的帧尺寸一致，大小不同必然不同内容，
        能省掉绝大多数全文件读取（每帧几百 KB，长视频上千帧）。
        """
        kept: list[str] = []
        last_hash = None
        last_size = None
        for path in image_paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size == last_size and last_hash is not None:
                frame_hash = self._calculate_file_md5(path)
                if frame_hash == last_hash:
                    os.remove(path)
                    continue
                last_hash = frame_hash
            else:
                last_hash = self._calculate_file_md5(path)
            last_size = size
            kept.append(path)
        return kept

    def extract_frames(self, max_frames=None) -> list[str]:
        # max_frames 参数保留（旧调用点/测试直接传参）；缺省用构造时注入的预算
        limit = self.max_frames if max_frames is None else max_frames
        try:
            os.makedirs(self.frame_dir, exist_ok=True)
            duration = float(ffmpeg.probe(self.video_path)["format"]["duration"])
            timestamps = [i for i in range(0, int(duration), self.frame_interval)][:limit]
            if not timestamps:
                return []
            if self._should_use_batch(len(timestamps)):
                try:
                    paths = self._extract_frames_batch(timestamps)
                    return self._dedupe_adjacent(paths) if self.dedupe_enabled else paths
                except TaskCancelled:
                    raise
                except Exception as e:
                    logger.warning(f"整段抽帧失败，回退逐帧模式：{e}")
                    for name in os.listdir(self.frame_dir):
                        if name.startswith("frame_"):
                            try:
                                os.remove(os.path.join(self.frame_dir, name))
                            except OSError:
                                pass
            return self._extract_frames_parallel(timestamps)
        except TaskCancelled:
            raise
        except Exception as e:
            logger.error(f"分割帧发生错误：{str(e)}")
            raise ValueError("视频处理失败")

    def _extract_frames_parallel(self, timestamps: list[int]) -> list[str]:
        """逐帧 seek 抽取（稀疏采样 / 整段解码失败时的兜底路径）。"""
        # 并行提取帧
        max_workers = min(os.cpu_count() or 4, 8, len(timestamps))
        frame_results: dict[int, str | None] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._extract_single_frame, ts): ts for ts in timestamps}
            for future in as_completed(futures):
                ts = futures[future]
                # 取消异常会让池内剩余任务快速失败退出，这里原样上抛
                frame_results[ts] = future.result()
                done += 1
                self._report_progress(done, len(timestamps))

        # 按时间戳顺序整理结果，并进行去重
        image_paths = []
        for ts in timestamps:
            self._check_cancelled()
            output_path = frame_results.get(ts)
            if not output_path or not os.path.exists(output_path):
                continue
            image_paths.append(output_path)
        return self._dedupe_adjacent(image_paths) if self.dedupe_enabled else image_paths

    def group_images(self) -> list[list[str]]:
        image_files = [os.path.join(self.frame_dir, f) for f in os.listdir(self.frame_dir) if
                       f.startswith("frame_") and f.endswith(".jpg")]
        image_files.sort(key=lambda f: self.extract_time_from_filename(os.path.basename(f)))
        group_size = self.grid_size[0] * self.grid_size[1]
        return [image_files[i:i + group_size] for i in range(0, len(image_files), group_size)]

    def concat_images(self, image_paths: list[str], name: str, cols: int = None, rows: int = None) -> str:
        os.makedirs(self.grid_dir, exist_ok=True)

        cols = cols or self.grid_size[0]
        rows = rows or self.grid_size[1]
        unit_w, unit_h = self.unit_width, self.unit_height
        long_side = max(unit_w * cols, unit_h * rows)
        if long_side > MAX_GRID_LONG_SIDE:
            # 按比例缩格子，整张拼图跟着变小；时间戳字号同步缩放，别盖住画面
            k = MAX_GRID_LONG_SIDE / long_side
            unit_w, unit_h = max(1, int(unit_w * k)), max(1, int(unit_h * k))
        font_size = max(10, int(48 * unit_w / self.unit_width))
        font = ImageFont.truetype(self.font_path, font_size) if os.path.exists(self.font_path) else ImageFont.load_default()
        images = []

        for path in image_paths:
            img = Image.open(path).convert("RGB").resize((unit_w, unit_h), Image.Resampling.LANCZOS)
            timestamp = _FRAME_TS_RE.search(os.path.basename(path))
            time_text = f"{timestamp.group(1)}:{timestamp.group(2)}" if timestamp else ""
            draw = ImageDraw.Draw(img)
            draw.text((10, 10), time_text, fill="yellow", font=font, stroke_width=1, stroke_fill="black")
            images.append(img)

        grid_img = Image.new("RGB", (unit_w * cols, unit_h * rows), (255, 255, 255))

        for i, img in enumerate(images):
            x = (i % cols) * unit_w
            y = (i // cols) * unit_h
            grid_img.paste(img, (x, y))

        save_path = os.path.join(self.grid_dir, f"{name}.jpg")
        grid_img.save(save_path, quality=self.save_quality)
        return save_path

    def encode_images_to_base64(self, image_paths: list[str]) -> list[str]:
        def _encode(path: str) -> str:
            with open(path, "rb") as img_file:
                encoded_string = base64.b64encode(img_file.read()).decode("utf-8")
                return f"data:image/jpeg;base64,{encoded_string}"

        if len(image_paths) <= 1:
            return [_encode(p) for p in image_paths]
        # 纯读文件+编码，互不依赖，并行省一大截 IO（2026-10-11）
        with ThreadPoolExecutor(max_workers=min(4, len(image_paths))) as pool:
            return list(pool.map(_encode, image_paths))

    def run(self, reuse_frames: bool = False) -> list[str]:
        logger.info("开始提取视频帧...")
        try:
            self._check_cancelled()
            # 确保目录存在
            os.makedirs(self.frame_dir, exist_ok=True)
            os.makedirs(self.grid_dir, exist_ok=True)
            # reuse_frames=True（降级升格重建 grid2/grid3 用）：采样间隔没变，
            # 帧缓存与原抽取完全一致，跳过全量重新抽帧（长视频省几分钟 ffmpeg）。
            has_cached_frames = any(
                f.startswith("frame_") for f in os.listdir(self.frame_dir)
            )
            if reuse_frames and has_cached_frames:
                logger.info("采样间隔未变，复用已抽取的帧缓存")
            else:
                # FFMPEG_BIN_PATH 只有在 check_ffmpeg_exists 真正跑过一次后才会前置进
                # 进程 PATH；健康轮询从未触发过的进程（纯后台自动化）若不在这里补一次，
                # 所有抽帧都会以 WinError 2 告终（2026-10-05 自动化 30 轮全败的根因）。
                # 复用帧缓存的重建路径完全不碰 ffmpeg，无需这步。
                try:
                    from ffmpeg_helper import ensure_ffmpeg_or_raise

                    ensure_ffmpeg_or_raise()
                except ImportError:
                    pass
                # 清空帧文件夹
                for file in os.listdir(self.frame_dir):
                    if file.startswith("frame_"):
                        os.remove(os.path.join(self.frame_dir, file))
                self.extract_frames()
            # 清空网格文件夹（重建必然要重拼，旧拼图作废）
            for file in os.listdir(self.grid_dir):
                if file.startswith("grid_"):
                    os.remove(os.path.join(self.grid_dir, file))
            logger.info("开始拼接网格图...")
            groups = self.group_images()
            group_size = self.grid_size[0] * self.grid_size[1]

            def _concat_one(idx: int, group: list[str]) -> str:
                self._check_cancelled()
                if len(group) < group_size:
                    # 尾组不再整组丢弃（旧逻辑让视频结尾几分钟完全没有截图）：
                    # 保持列数不变、行数按剩余张数收窄，空位留白（无时间戳角标，
                    # 模型会当成空白格）
                    tail_rows = -(-len(group) // self.grid_size[0])
                    logger.info(
                        f"第 {idx} 组为尾组（{len(group)}/{group_size} 张），"
                        f"按 {self.grid_size[0]}×{tail_rows} 小拼图保留"
                    )
                    return self.concat_images(
                        group, f"grid_{idx}", cols=self.grid_size[0], rows=tail_rows
                    )
                return self.concat_images(group, f"grid_{idx}")

            # 各组互不依赖、各写各的文件，并行拼图（2026-10-11：抽帧早已并行，
            # 拼图一直串行，长视频上百组时这里能省几十秒）。pool.map 保序。
            workers = max(1, min(4, len(groups)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                image_paths = list(
                    pool.map(lambda t: _concat_one(*t), enumerate(groups, start=1))
                )

            logger.info("📤 开始编码图像...")
            self._check_cancelled()
            urls = self.encode_images_to_base64(image_paths)
            return urls
        except TaskCancelled:
            raise
        except Exception as e:
            logger.error(f"发生错误：{str(e)}")
            raise ValueError("视频处理失败")


