"""监控本地目录，新文件先多次探测 115 秒传，全部未命中后再普通上传。"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase
from app.schemas.file import FileItem
from app.sdk.logging import logger
from app.sdk.services import StorageHelper

U115_CHUNK = 1024 * 1024
U115_STORAGE = "u115"


def _sha1_pair(path: Path, prefix_size: int = 128 * 1024 * 1024) -> Tuple[str, str]:
    """
    一次读取文件，同时计算全量 SHA1 和前 prefix_size 字节 SHA1。

    115 秒传使用这两个值：全量 SHA1 作为 fileid，前 128MB SHA1 作为 preid。
    """
    full = hashlib.sha1()
    prefix = hashlib.sha1()
    remaining = prefix_size
    with open(path, "rb") as file:
        while True:
            chunk = file.read(U115_CHUNK)
            if not chunk:
                break
            full.update(chunk)
            if remaining > 0:
                part = chunk[:remaining]
                if part:
                    prefix.update(part)
                    remaining -= len(part)
    return full.hexdigest(), prefix.hexdigest()


def _get_115_api():
    """取得宿主 115 适配器实例；当前版本不兼容时返回 None。"""
    try:
        from app.modules.filemanager.storages.u115 import U115Pan
    except ImportError as err:
        logger.error(f"当前 MoviePilot 没有 115 存储适配器：{err}")
        return None
    api = U115Pan()
    if not all(hasattr(api, name) for name in ("_request_api", "get_item", "get_folder", "upload")):
        logger.error("115 适配器接口已变化，插件需要适配当前 MoviePilot 版本")
        return None
    return api


def probe_rapid_upload_once(
    api,
    target_dir: FileItem,
    local_path: Path,
    new_name: Optional[str] = None,
    *,
    file_sha1: Optional[str] = None,
    file_preid: Optional[str] = None,
) -> Optional[FileItem]:
    """只执行一次 115 秒传探测，不触发普通上传。"""
    if not target_dir.fileid:
        raise ValueError("115 目标目录缺少 fileid")

    target_name = new_name or local_path.name
    target_path = Path(target_dir.path) / target_name
    file_size = local_path.stat().st_size
    if file_sha1 is None or file_preid is None:
        computed_sha1, computed_preid = _sha1_pair(local_path)
        file_sha1 = file_sha1 or computed_sha1
        file_preid = file_preid or computed_preid

    init_data = {
        "file_name": target_name,
        "file_size": file_size,
        "target": f"U_1_{target_dir.fileid}",
        "fileid": file_sha1,
        "preid": file_preid,
    }
    init_resp = api._request_api(
        "POST", "/open/upload/init", data=init_data, retry_limit=0
    )
    if not isinstance(init_resp, dict) or not init_resp.get("state"):
        logger.debug(f"【115秒传】初始化失败：{init_resp}")
        return None
    init_result = init_resp.get("data") or {}

    # 115 对部分文件要求二次内容校验，这仍属于秒传探测。
    if init_result.get("code") in (700, 701) and init_result.get("sign_check"):
        start_text, end_text = str(init_result["sign_check"]).split("-", 1)
        start, end = int(start_text), int(end_text)
        with open(local_path, "rb") as file:
            file.seek(start)
            sign_val = hashlib.sha1(file.read(end - start + 1)).hexdigest().upper()
        init_data.update(
            {
                "pick_code": init_result.get("pick_code"),
                "sign_key": init_result.get("sign_key"),
                "sign_val": sign_val,
            }
        )
        init_resp = api._request_api(
            "POST", "/open/upload/init", data=init_data, retry_limit=0
        )
        if not isinstance(init_resp, dict) or not init_resp.get("state"):
            logger.debug(f"【115秒传】二次认证失败：{init_resp}")
            return None
        init_result = init_resp.get("data") or {}

    logger.debug(
        f"【115秒传】code={init_result.get('code')} "
        f"status={init_result.get('status')} "
        f"sign_check={bool(init_result.get('sign_check'))}"
    )
    if init_result.get("status") != 2:
        return None

    file_id = init_result.get("file_id")
    if file_id:
        info_resp = api._request_api(
            "GET",
            "/open/folder/get_info",
            "data",
            params={"file_id": int(file_id)},
            retry_limit=0,
        )
        if isinstance(info_resp, dict):
            is_file = info_resp.get("file_category") == "1"
            return FileItem(
                storage=U115_STORAGE,
                fileid=str(info_resp["file_id"]),
                path=target_path.as_posix() + ("" if is_file else "/"),
                type="file" if is_file else "dir",
                name=info_resp["file_name"],
                basename=Path(info_resp["file_name"]).stem,
                extension=Path(info_resp["file_name"]).suffix[1:] if is_file else None,
                pickcode=info_resp.get("pick_code"),
                size=info_resp.get("size_byte") if is_file else None,
                modify_time=info_resp.get("utime"),
            )

    for _ in range(3):
        item = api.get_item(target_path)
        if item:
            return item
        time.sleep(1)
    return None


def rapid_upload_with_retry(
    api,
    target_dir: FileItem,
    local_path: Path,
    new_name: Optional[str] = None,
    *,
    retry_times: int = 3,
    retry_interval: float = 5.0,
    hash_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[FileItem]:
    """先探测 retry_times 次秒传，全部未命中后再调用宿主普通上传。"""
    stat = local_path.stat()
    cache_key = str(local_path)
    cached = hash_cache.get(cache_key) if hash_cache is not None else None

    # 115 秒传用的是 SHA1：全文件 SHA1 作为 fileid，前 128MB SHA1 作为 preid。
    # 第一次探测前必须算出这两个值；算完立即缓存，后续探测直接复用，不再读文件。
    if (
        isinstance(cached, dict)
        and cached.get("sha1")
        and cached.get("preid")
        and cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
    ):
        file_sha1 = cached["sha1"]
        file_preid = cached["preid"]
    else:
        file_sha1, file_preid = _sha1_pair(local_path)
        if hash_cache is not None:
            if len(hash_cache) >= 1000:
                hash_cache.pop(next(iter(hash_cache)), None)
            hash_cache[cache_key] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha1": file_sha1,
                "preid": file_preid,
            }

    for attempt in range(1, retry_times + 1):
        item = probe_rapid_upload_once(
            api,
            target_dir,
            local_path,
            new_name,
            file_sha1=file_sha1,
            file_preid=file_preid,
        )
        if item:
            logger.info(f"【115秒传】第 {attempt} 次探测命中：{item.path}")
            return item
        # 这里的 sleep 在本次探测失败返回之后才执行，
        # 计时起点是失败时刻，不是探测发起时刻。
        if attempt < retry_times:
            logger.info(
                f"【115秒传】第 {attempt}/{retry_times} 次未命中，"
                f"{retry_interval:g}s 后重试"
            )
            time.sleep(retry_interval)

    logger.info("【115秒传】探测次数已用尽，回退到宿主 upload()")
    return api.upload(target_dir, local_path, new_name)


class U115AutoUpload(_PluginBase):
    """监控本地目录，新增文件先多次探测 115 秒传，失败后普通上传。"""

    plugin_name = "115 目录秒传上传"
    plugin_desc = "监控本地目录，新文件先探测 115 秒传，重试 x 次后回退普通上传。"
    plugin_version = "1.0.0"
    plugin_label = "网盘"
    plugin_author = "local"
    plugin_config_prefix = "u115autoupload_"
    plugin_order = 100
    auth_level = 1

    # 不参与上传的临时文件后缀
    _ignore_suffixes = {
        ".part",
        ".tmp",
        ".temp",
        ".!qb",
        ".aria2",
        ".downloading",
        ".crdownload",
    }

    _enabled = False
    _watch_dir = ""
    _target_dir = ""
    _recursive = True
    _scan_interval = 60
    _rapid_retry = 3
    _rapid_interval = 5.0
    _stable_seconds = 30
    _delete_after_upload = False
    _upload_existing = False

    def __init__(self) -> None:
        """
        初始化扫描状态和并发锁。
        """
        super().__init__()
        self._scan_lock = threading.Lock()
        self._scan_state: Dict[str, Tuple[int, float]] = {}
        self._hash_cache: Dict[str, Dict[str, Any]] = {}
        self._last_scan = ""
        self._extension_set: set[str] = set()

    def init_plugin(self, config: dict = None) -> None:
        """
        读取插件配置并停止旧服务。
        """
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._watch_dir = str(config.get("watch_dir") or "").strip()
        self._target_dir = str(config.get("target_dir") or "").strip()
        self._recursive = bool(config.get("recursive", True))
        self._scan_interval = max(10, int(config.get("scan_interval") or 60))
        self._rapid_retry = max(0, int(config.get("rapid_retry") or 0))
        self._rapid_interval = max(0.0, float(config.get("rapid_interval") or 5))
        self._stable_seconds = max(0, int(config.get("stable_seconds") or 30))
        self._delete_after_upload = bool(config.get("delete_after_upload"))
        self._upload_existing = bool(config.get("upload_existing"))
        self._extension_set = self.__parse_extensions(config.get("extensions"))
        self._scan_state = {}
        self._last_scan = ""

    def get_state(self) -> bool:
        """
        返回插件启用状态。
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        不注册远程命令。
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册手动触发扫描的 API。
        """
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "立即扫描一次监控目录",
            }
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        返回插件配置表单和默认值。
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"},
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "watch_dir",
                            "label": "本地监控目录",
                            "placeholder": "/downloads/watch",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "target_dir",
                            "label": "115 目标目录",
                            "placeholder": "/我的备份",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {"model": "recursive", "label": "递归子目录并保留目录结构"},
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "scan_interval",
                            "label": "扫描间隔（秒）",
                            "type": "number",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "rapid_retry",
                            "label": "秒传探测次数",
                            "type": "number",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "rapid_interval",
                            "label": "失败后重试间隔（秒）",
                            "type": "number",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "stable_seconds",
                            "label": "文件稳定时间（秒）",
                            "type": "number",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "extensions",
                            "label": "只处理这些后缀（逗号分隔，留空为全部）",
                            "placeholder": ".mkv,.mp4,.iso",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "delete_after_upload",
                            "label": "上传成功后删除本地文件",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "upload_existing",
                            "label": "首次启用时也上传目录中已有文件",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "watch_dir": "",
            "target_dir": "",
            "recursive": True,
            "scan_interval": 60,
            "rapid_retry": 3,
            "rapid_interval": 5,
            "stable_seconds": 30,
            "extensions": "",
            "delete_after_upload": False,
            "upload_existing": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """
        返回插件状态页。
        """
        if not self._enabled:
            return None
        processed = self.get_data("processed")
        count = len(processed) if isinstance(processed, dict) else 0
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "text": f"监控目录：{self._watch_dir or '未设置'}"},
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "text": f"115 目标目录：{self._target_dir or '未设置'}"},
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "text": f"上次扫描：{self._last_scan or '尚未执行'}"},
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "text": f"已记录文件数：{count}"},
            },
        ]

    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """
        注册周期扫描服务。
        """
        if not self._enabled or not self._watch_dir or not self._target_dir:
            return []
        return [
            {
                "id": "U115AutoUpload",
                "name": "115 目录秒传上传",
                "trigger": "interval",
                "func": self.scan_once,
                "kwargs": {"seconds": self._scan_interval},
            }
        ]

    def stop_service(self) -> None:
        """
        停止插件时清理扫描状态。
        """
        self._scan_state.clear()
        self._hash_cache.clear()
        self._last_scan = ""

    def api_scan(self) -> Dict[str, Any]:
        """
        手动触发一次扫描，立即返回。
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        threading.Thread(target=self.scan_once, daemon=True).start()
        return {"success": True, "message": "扫描已触发"}

    def scan_once(self) -> None:
        """
        执行一次目录扫描；上一轮未结束时直接跳过。
        """
        if not self._enabled:
            return
        if not self._scan_lock.acquire(blocking=False):
            logger.info("【115自动上传】上一轮扫描仍在执行，跳过本轮")
            return
        try:
            self.__scan()
        except Exception as err:
            logger.error(f"【115自动上传】扫描失败：{err}")
        finally:
            self._scan_lock.release()

    def __scan(self) -> None:
        """
        扫描目录并上传尚未记录的新文件。
        """
        watch_dir = Path(self._watch_dir)
        if not watch_dir.is_dir():
            logger.error(f"【115自动上传】监控目录不存在：{watch_dir}")
            return
        if not self._target_dir:
            logger.error("【115自动上传】未设置 115 目标目录")
            return

        conf = StorageHelper().get_storage(U115_STORAGE)
        if not conf or not conf.config:
            logger.error("【115自动上传】主程序未配置 115，请先在 设置 -> 存储 中登录")
            return

        api = _get_115_api()
        if api is None:
            return

        self._last_scan = time.strftime("%Y-%m-%d %H:%M:%S")
        processed = self.get_data("processed")
        if not isinstance(processed, dict):
            processed = {}

        # 首次启用或监控目录变化时，可选择只记录现有文件而不上传。
        init_state = self.get_data("init_state")
        if (
            not isinstance(init_state, dict)
            or init_state.get("watch_dir") != str(watch_dir)
            or bool(init_state.get("upload_existing")) != self._upload_existing
        ):
            processed = {}
            if not self._upload_existing:
                for local_path in self.__iter_files(watch_dir):
                    try:
                        stat = local_path.stat()
                    except OSError:
                        continue
                    if time.time() - stat.st_mtime < self._stable_seconds:
                        continue
                    processed[local_path.relative_to(watch_dir).as_posix()] = {
                        "size": stat.st_size,
                        "mtime": int(stat.st_mtime),
                        "uploaded_at": 0,
                        "remote_path": "",
                    }
            self.save_data("processed", processed)
            self.save_data(
                "init_state",
                {
                    "watch_dir": str(watch_dir),
                    "upload_existing": self._upload_existing,
                    "at": int(time.time()),
                },
            )
            if not self._upload_existing:
                logger.info("【115自动上传】首次扫描已记录现有文件，不执行上传")
                return

        for local_path in self.__iter_files(watch_dir):
            if not self.__is_ready(local_path):
                continue
            key = local_path.relative_to(watch_dir).as_posix()
            try:
                stat = local_path.stat()
            except OSError:
                continue
            record = processed.get(key)
            if (
                isinstance(record, dict)
                and record.get("size") == stat.st_size
                and record.get("mtime") == int(stat.st_mtime)
            ):
                continue

            remote_dir = self.__remote_dir(watch_dir, local_path)
            try:
                target_item = self.__resolve_folder(api, remote_dir)
                if target_item is None:
                    logger.error(f"【115自动上传】无法获取/创建 115 目录：{remote_dir}")
                    continue
                uploaded = rapid_upload_with_retry(
                    api,
                    target_item,
                    local_path,
                    local_path.name,
                    retry_times=self._rapid_retry,
                    retry_interval=self._rapid_interval,
                    hash_cache=self._hash_cache,
                )
            except Exception as err:
                logger.error(f"【115自动上传】上传 {local_path} 失败：{err}")
                continue

            if not uploaded:
                logger.error(f"【115自动上传】上传 {local_path} 失败，下轮扫描重试")
                continue

            processed[key] = {
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "uploaded_at": int(time.time()),
                "remote_path": uploaded.path,
            }
            self.save_data("processed", processed)
            self._hash_cache.pop(str(local_path), None)
            logger.info(f"【115自动上传】成功：{local_path} -> {uploaded.path}")

            if self._delete_after_upload:
                try:
                    local_path.unlink()
                except Exception as err:
                    logger.warn(f"【115自动上传】删除本地文件失败：{local_path} - {err}")

    def __iter_files(self, watch_dir: Path):
        """
        按递归配置遍历候选文件。
        """
        iterator = watch_dir.rglob("*") if self._recursive else watch_dir.iterdir()
        for path in iterator:
            if not path.is_file():
                continue
            if not self.__match_extension(path):
                continue
            yield path

    def __match_extension(self, path: Path) -> bool:
        """
        判断文件后缀是否需要处理。
        """
        if path.name.startswith("."):
            return False
        suffix = path.suffix.lower()
        if suffix in self._ignore_suffixes:
            return False
        if not self._extension_set:
            return True
        return suffix in self._extension_set

    def __is_ready(self, path: Path) -> bool:
        """
        判断文件是否已写入稳定，避免上传正在复制的文件。
        """
        try:
            stat = path.stat()
        except OSError:
            return False
        if time.time() - stat.st_mtime < self._stable_seconds:
            return False
        previous = self._scan_state.get(str(path))
        self._scan_state[str(path)] = (stat.st_size, stat.st_mtime)
        if previous and previous[0] != stat.st_size:
            return False
        return True

    def __remote_dir(self, watch_dir: Path, path: Path) -> str:
        """
        计算文件对应的 115 目录，递归时保留相对目录结构。
        """
        target = self._target_dir.strip()
        if not target.startswith("/"):
            target = "/" + target
        target = target.rstrip("/") or "/"
        if not self._recursive:
            return target
        relative_parent = path.relative_to(watch_dir).parent
        if relative_parent.parts:
            return (Path(target) / relative_parent).as_posix()
        return target

    @staticmethod
    def __resolve_folder(api, remote_dir: str) -> Optional[FileItem]:
        """
        获取或创建 115 目标目录；根目录使用 fileid=0。
        """
        folder = api.get_folder(Path(remote_dir))
        if folder and folder.fileid:
            return folder
        if remote_dir.strip("/") == "":
            return FileItem(
                storage=U115_STORAGE,
                path="/",
                type="dir",
                fileid="0",
            )
        return folder

    @staticmethod
    def __parse_extensions(raw: Any) -> set[str]:
        """
        解析逗号分隔的后缀配置。
        """
        result: set[str] = set()
        for value in str(raw or "").replace("，", ",").split(","):
            value = value.strip().lower()
            if not value:
                continue
            if not value.startswith("."):
                value = "." + value
            result.add(value)
        return result
