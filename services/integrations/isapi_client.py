# -*- coding: utf-8 -*-
"""Hikvision ISAPI: channel discovery and historical playback download."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse

import requests
from requests import exceptions as requests_exc
from requests.auth import HTTPDigestAuth

FFMPEG_PATH = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"


class IsapiDownloadCancelled(Exception):
    """Raised when cancel_check returns True during download."""


def normalize_channel_id(raw_id: str) -> Optional[int]:
    s = (raw_id or "").strip()
    if not s:
        return None
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    val = int(m.group(1))
    if val >= 100 and val % 100 in (1, 2):
        return val // 100
    return val


def extract_channels_from_xml(xml_text: str) -> List[dict]:
    items: List[dict] = []
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            tag = elem.tag.lower()
            if not (tag.endswith("streamingchannel") or tag.endswith("inputproxychannel")):
                continue
            children = list(elem)
            raw_id = ""
            name = ""
            for ch in children:
                ctag = ch.tag.lower()
                txt = (ch.text or "").strip()
                if not txt:
                    continue
                if ctag.endswith("id"):
                    raw_id = txt
                elif ctag.endswith("name"):
                    name = txt
            ch_id = normalize_channel_id(raw_id)
            if ch_id is None:
                continue
            items.append(
                {
                    "id": ch_id,
                    "name": name or f"通道{ch_id}",
                    "raw_id": raw_id or str(ch_id),
                }
            )
    except Exception:
        pass
    uniq: Dict[int, dict] = {}
    for it in items:
        uniq[it["id"]] = it
    return [uniq[k] for k in sorted(uniq)]


def discover_channels(host: str, username: str, password: str) -> List[dict]:
    auth = HTTPDigestAuth(username, password)
    endpoints = [
        f"http://{host}/ISAPI/Streaming/channels",
        f"http://{host}/ISAPI/ContentMgmt/InputProxy/channels",
    ]
    last_err: Optional[Exception] = None
    for url in endpoints:
        try:
            resp = requests.get(url, auth=auth, timeout=(10, 30))
            resp.raise_for_status()
            channels = extract_channels_from_xml(resp.text)
            if channels:
                return channels
        except Exception as e:
            last_err = e
    msg = "无法识别设备通道，请确认 IP/账号密码正确且设备支持 ISAPI。"
    if last_err:
        msg += f" ({last_err})"
    raise RuntimeError(msg)


def build_isapi_url(
    host: str,
    username: str,
    password: str,
    channel: int,
    start_time: str,
    end_time: str,
) -> str:
    return (
        f"isapi://{host}"
        f"?user={quote_plus(username)}"
        f"&password={quote_plus(password)}"
        f"&channel={channel}"
        f"&start={start_time}"
        f"&end={end_time}"
    )


def build_rtsp_url(
    host: str,
    username: str,
    password: str,
    channel: int,
    *,
    stream: str = "main",
    port: int = 554,
) -> str:
    """
    海康 NVR/DVR 实时 RTSP 地址。
    逻辑通道 N：主码流 N*100+1，子码流 N*100+2。
    """
    host = (host or "").strip()
    if not host:
        raise ValueError("设备 IP 不能为空")
    channel = int(channel)
    stream = (stream or "main").lower().strip()
    stream_suffix = 1 if stream in ("main", "1", "primary") else 2
    stream_id = channel * 100 + stream_suffix
    user = quote(username or "", safe="")
    pwd = quote(password or "", safe="")
    if user and pwd:
        auth = f"{user}:{pwd}@"
    elif user:
        auth = f"{user}@"
    else:
        auth = ""
    return f"rtsp://{auth}{host}:{port}/Streaming/channels/{stream_id}"


def parse_isapi_source(raw_path: str) -> Optional[dict]:
    if not raw_path.startswith("isapi://"):
        return None
    parsed = urlparse(raw_path)
    qs = parse_qs(parsed.query)
    host = parsed.netloc
    user = unquote(qs.get("user", [""])[0])
    password = unquote(qs.get("password", [""])[0])
    channel = int(qs.get("channel", ["1"])[0])
    start_time = qs.get("start", [""])[0]
    end_time = qs.get("end", [""])[0]
    if not (host and user and password and start_time and end_time):
        raise ValueError("ISAPI 参数不完整，需包含 host/user/password/start/end")
    return {
        "host": host,
        "user": user,
        "password": password,
        "channel": channel,
        "start_time": start_time,
        "end_time": end_time,
    }


def format_task_time_label(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d_%H-%M")
    except Exception:
        return ts.replace("T", "_").replace(":", "-")[:16]


def auto_task_name(channel: int, start: str, end: str) -> str:
    return (
        f"海康回放_通道{channel:02d}_"
        f"{format_task_time_label(start)}_{format_task_time_label(end)}"
    )


def _time_span_hint(cfg: dict) -> str:
    return f"{cfg.get('start_time', '')} ～ {cfg.get('end_time', '')}"


def _extract_playback_uris(xml_text: str) -> List[str]:
    uris: List[str] = []
    try:
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            if elem.tag.endswith("playbackURI") and elem.text:
                u = elem.text.strip()
                if u:
                    uris.append(u)
    except Exception:
        pass
    if not uris:
        uris.extend(
            m.strip() for m in re.findall(r"<playbackURI>(.*?)</playbackURI>", xml_text, re.S)
        )
    return list(dict.fromkeys(u for u in uris if u))


def _request_timeouts() -> Tuple[int, int, int]:
    connect = int(os.environ.get("ISAPI_CONNECT_TIMEOUT", "30"))
    search_read = int(os.environ.get("ISAPI_SEARCH_READ_TIMEOUT", "60"))
    download_read = int(os.environ.get("ISAPI_DOWNLOAD_READ_TIMEOUT", "3600"))
    return connect, search_read, download_read


def _merge_segments(segment_paths: List[str], output_file: str) -> None:
    if not segment_paths:
        raise RuntimeError("ISAPI 未下载到任何片段")

    if len(segment_paths) == 1:
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-i",
            segment_paths[0],
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            output_file,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=7200)
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(f"ISAPI 分段合并失败: {err}") from e
        if (not os.path.exists(output_file)) or os.path.getsize(output_file) == 0:
            raise RuntimeError("ISAPI 分段合并后输出为空")
        return

    list_file = f"{output_file}.concat.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in segment_paths:
                esc = p.replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{esc}'\n")
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file,
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            output_file,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=7200)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"ISAPI 分段合并失败: {err}") from e
    finally:
        if os.path.exists(list_file):
            try:
                os.remove(list_file)
            except OSError:
                pass

    if (not os.path.exists(output_file)) or os.path.getsize(output_file) == 0:
        raise RuntimeError("ISAPI 分段合并后输出为空")


def check_ffmpeg() -> bool:
    return bool(shutil.which(FFMPEG_PATH) or shutil.which("ffmpeg"))


def download_video(
    raw_path: str,
    output_file: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    cfg = parse_isapi_source(raw_path)
    if cfg is None:
        raise ValueError("无效 ISAPI 源")

    connect_t, search_read_t, download_read_t = _request_timeouts()
    search_timeout = (connect_t, search_read_t)
    download_timeout = (connect_t, download_read_t)

    search_id = "{" + str(uuid.uuid4()).upper() + "}"
    search_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <searchID>{search_id}</searchID>
    <trackIDList>
        <trackID>{cfg["channel"]}01</trackID>
    </trackIDList>
    <timeSpanList>
        <timeSpan>
            <startTime>{cfg["start_time"]}</startTime>
            <endTime>{cfg["end_time"]}</endTime>
        </timeSpan>
    </timeSpanList>
    <contentTypeList>
        <contentType>video</contentType>
    </contentTypeList>
    <maxResults>200</maxResults>
</CMSearchDescription>"""

    search_url = f"http://{cfg['host']}/ISAPI/ContentMgmt/search"
    search_resp = requests.post(
        search_url,
        auth=HTTPDigestAuth(cfg["user"], cfg["password"]),
        data=search_xml,
        headers={"Content-Type": "application/xml"},
        timeout=search_timeout,
    )
    search_resp.raise_for_status()
    playback_uris = _extract_playback_uris(search_resp.text)
    if not playback_uris:
        span = _time_span_hint(cfg)
        raise RuntimeError(
            f"所选日期/时间段在设备上未返回可下载录像（{span}）。"
            "请确认该日是否有录像、通道号与起止时间是否正确。"
        )

    download_url = f"http://{cfg['host']}/ISAPI/ContentMgmt/download"
    max_attempts = int(os.environ.get("ISAPI_DOWNLOAD_RETRIES", "3"))
    seg_dir = os.path.join(
        os.path.dirname(output_file) or ".",
        f"isapi_parts_{uuid.uuid4().hex[:8]}",
    )
    os.makedirs(seg_dir, exist_ok=True)
    seg_paths: List[str] = []
    total_units = max(1, len(playback_uris) * 1000)
    try:
        for idx, playback_uri in enumerate(playback_uris, start=1):
            if cancel_check and cancel_check():
                raise IsapiDownloadCancelled()
            download_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<downloadRequest version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <playbackURI>{playback_uri.replace("&", "&amp;")}</playbackURI>
</downloadRequest>"""
            seg_file = os.path.join(seg_dir, f"seg_{idx:04d}.mp4")
            for attempt in range(1, max_attempts + 1):
                if attempt > 1 and os.path.exists(seg_file):
                    try:
                        os.remove(seg_file)
                    except OSError:
                        pass
                try:
                    with requests.get(
                        download_url,
                        auth=HTTPDigestAuth(cfg["user"], cfg["password"]),
                        data=download_xml,
                        headers={"Content-Type": "application/xml"},
                        stream=True,
                        timeout=download_timeout,
                    ) as resp:
                        resp.raise_for_status()
                        seg_total = int(resp.headers.get("Content-Length", 0) or 0)
                        seg_downloaded = 0
                        with open(seg_file, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                if cancel_check and cancel_check():
                                    raise IsapiDownloadCancelled()
                                if not chunk:
                                    continue
                                f.write(chunk)
                                seg_downloaded += len(chunk)
                                if progress_callback and seg_total > 0:
                                    base_units = (idx - 1) * 1000
                                    cur_units = base_units + int(
                                        min(1000, (seg_downloaded * 1000) / seg_total)
                                    )
                                    progress_callback(cur_units, total_units)
                    if progress_callback:
                        progress_callback(idx * 1000, total_units)
                    break
                except IsapiDownloadCancelled:
                    raise
                except (requests_exc.ReadTimeout, requests_exc.ConnectionError) as e:
                    if attempt >= max_attempts:
                        raise RuntimeError(
                            f"ISAPI 分段下载失败（片段 {idx}/{len(playback_uris)}，"
                            f"已重试 {max_attempts} 次）: {e}. "
                            f"可加大读间隔: set ISAPI_DOWNLOAD_READ_TIMEOUT=7200 "
                            f"(当前 connect={connect_t}s read={download_read_t}s)"
                        ) from e
                    wait_s = min(30, 2 ** (attempt - 1))
                    time.sleep(wait_s)
                except requests_exc.HTTPError as e:
                    raise RuntimeError(
                        f"ISAPI 下载 HTTP 错误（片段 {idx}/{len(playback_uris)}）: "
                        f"{e.response.status_code if e.response else ''} {e}"
                    ) from e
            seg_paths.append(seg_file)

        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except OSError:
                pass
        _merge_segments(seg_paths, output_file)
    finally:
        try:
            shutil.rmtree(seg_dir, ignore_errors=True)
        except Exception:
            pass

    if (not os.path.exists(output_file)) or os.path.getsize(output_file) == 0:
        span = _time_span_hint(cfg)
        raise RuntimeError(
            f"所选时间段录像下载为空（{span}），可能该时段无录像或设备未存盘。"
        )
