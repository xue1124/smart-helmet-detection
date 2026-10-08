# -*- coding: utf-8 -*-
"""
本地 ISAPI Mock 服务 — 用于测试 Smart Construction 的 ISAPI 通道识别与历史下载。

用法:
  python tools/isapi_mock_server.py --video-ch1 test1.mp4 --video-ch2 test2.mp4
  python tools/isapi_mock_server.py --port 8080 --video-ch1 test1.mp4

默认录像:
  通道1 → 项目根目录/test1.mp4
  通道2 → 项目根目录/test2.mp4

前端 ISAPI 配置:
  IP: 127.0.0.1:8080
  用户/密码: admin / 12345
  任填起止时间均可下载对应通道视频。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import sys
from pathlib import Path
from typing import Dict

from werkzeug.datastructures import Headers
from werkzeug.serving import run_simple
from werkzeug.wrappers import Request, Response

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_VIDEO_CH1 = PROJECT_ROOT / "test1.mp4"
DEFAULT_VIDEO_CH2 = PROJECT_ROOT / "test2.mp4"

CHANNELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<StreamingChannelList version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <StreamingChannel>
        <id>101</id>
        <channelName>Mock 通道1</channelName>
    </StreamingChannel>
    <StreamingChannel>
        <id>201</id>
        <channelName>Mock 通道2</channelName>
    </StreamingChannel>
</StreamingChannelList>"""


def _track_id_to_channel(track_id: int) -> int:
    if track_id >= 100 and track_id % 100 in (1, 2):
        return track_id // 100
    return track_id


def _parse_channel_from_search(xml_text: str) -> int:
    m = re.search(r"<trackID>\s*(\d+)\s*</trackID>", xml_text, re.I)
    if not m:
        return 1
    return _track_id_to_channel(int(m.group(1)))


def _parse_channel_from_playback(uri: str) -> int:
    m = re.search(r"mock://[^/]+/ch(\d+)/", uri, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"[?&]channel=(\d+)", uri, re.I)
    if m:
        return int(m.group(1))
    return 1


def _playback_uri(channel: int) -> str:
    return f"mock://local/ch{channel}/seg/1"


def _search_result_xml(channel: int) -> str:
    playback_uri = _playback_uri(channel)
    safe_uri = playback_uri.replace("&", "&amp;")
    track_id = channel * 100 + 1
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <responseStatus>true</responseStatus>
    <responseStatusStrg>OK</responseStatusStrg>
    <numOfMatches>1</numOfMatches>
    <matchList>
        <searchMatchItem>
            <trackID>{track_id}</trackID>
            <timeSpan>
                <startTime>1970-01-01T00:00:00Z</startTime>
                <endTime>2099-12-31T23:59:59Z</endTime>
            </timeSpan>
            <mediaSegmentDescriptor>
                <playbackURI>{safe_uri}</playbackURI>
            </mediaSegmentDescriptor>
        </searchMatchItem>
    </matchList>
</CMSearchResult>"""


class DigestAuth:
    """Minimal HTTP Digest auth (compatible with requests.HTTPDigestAuth)."""

    def __init__(self, username: str, password: str, realm: str = "ISAPI Mock"):
        self.username = username
        self.password = password
        self.realm = realm

    def challenge(self) -> Response:
        nonce = secrets.token_hex(16)
        opaque = secrets.token_hex(8)
        www = (
            f'Digest realm="{self.realm}", qop="auth", '
            f'nonce="{nonce}", opaque="{opaque}", algorithm=MD5'
        )
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": www})

    @staticmethod
    def _parse_header(value: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if not value.startswith("Digest "):
            return out
        for part in re.findall(r'(\w+)=(".*?"|[^,]+)', value[7:]):
            key, raw = part[0], part[1].strip()
            out[key] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        return out

    def verify(self, request: Request) -> bool:
        params = self._parse_header(request.headers.get("Authorization", ""))
        if params.get("username") != self.username:
            return False
        uri = params.get("uri") or request.path
        ha1 = hashlib.md5(f"{self.username}:{self.realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{request.method}:{uri}".encode()).hexdigest()
        nonce = params.get("nonce", "")
        nc = params.get("nc", "")
        cnonce = params.get("cnonce", "")
        qop = params.get("qop", "")
        if qop:
            expected = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return secrets.compare_digest(params.get("response", ""), expected)


def _validate_videos(channel_videos: Dict[int, Path]) -> None:
    missing = [f"通道{ch}: {p}" for ch, p in sorted(channel_videos.items()) if not p.is_file()]
    if missing:
        print("[错误] 以下录像文件不存在:", file=sys.stderr)
        for line in missing:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)


def create_app(channel_videos: Dict[int, Path], auth: DigestAuth):
    def app(environ, start_response):
        request = Request(environ)
        if not auth.verify(request):
            resp = auth.challenge()
            start_response(f"{resp.status_code} Unauthorized", resp.headers.to_wsgi_list())
            return [resp.get_data()]

        path = request.path.rstrip("/") or "/"
        method = request.method.upper()

        if method == "GET" and path in (
            "/ISAPI/Streaming/channels",
            "/ISAPI/ContentMgmt/InputProxy/channels",
        ):
            resp = Response(CHANNELS_XML, mimetype="application/xml")
        elif method == "POST" and path == "/ISAPI/ContentMgmt/search":
            body = request.get_data(as_text=True)
            channel = _parse_channel_from_search(body)
            resp = Response(_search_result_xml(channel), mimetype="application/xml")
        elif method == "GET" and path == "/ISAPI/ContentMgmt/download":
            body = request.get_data(as_text=True)
            if "playbackURI" not in body:
                resp = Response("missing playbackURI", status=400)
            else:
                m = re.search(r"<playbackURI>(.*?)</playbackURI>", body, re.S | re.I)
                playback_uri = (m.group(1) if m else body).replace("&amp;", "&").strip()
                channel = _parse_channel_from_playback(playback_uri)
                video_path = channel_videos.get(channel) or channel_videos.get(1)
                if video_path is None or not video_path.is_file():
                    resp = Response(f"mock video not found for channel {channel}: {video_path}", status=404)
                else:

                    def generate():
                        with open(video_path, "rb") as f:
                            while True:
                                chunk = f.read(1024 * 1024)
                                if not chunk:
                                    break
                                yield chunk

                    size = video_path.stat().st_size
                    headers = Headers(
                        [
                            ("Content-Type", "video/mp4"),
                            ("Content-Length", str(size)),
                        ]
                    )
                    start_response("200 OK", headers.to_wsgi_list())
                    return generate()
        else:
            resp = Response(f"not found: {method} {path}", status=404)

        start_response(
            f"{resp.status_code} OK" if resp.status_code == 200 else str(resp.status_code),
            resp.headers.to_wsgi_list(),
        )
        return [resp.get_data()]

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Hikvision ISAPI mock server")
    parser.add_argument("--host", default="0.0.0.0", help="listen host")
    parser.add_argument("--port", type=int, default=8080, help="listen port")
    parser.add_argument("--user", default="admin", help="digest username")
    parser.add_argument("--password", default="12345", help="digest password")
    parser.add_argument("--video-ch1", default=str(DEFAULT_VIDEO_CH1), help="channel 1 mp4")
    parser.add_argument("--video-ch2", default=str(DEFAULT_VIDEO_CH2), help="channel 2 mp4")
    args = parser.parse_args()

    channel_videos = {
        1: Path(args.video_ch1),
        2: Path(args.video_ch2),
    }
    _validate_videos(channel_videos)

    auth = DigestAuth(args.user, args.password)
    app = create_app(channel_videos, auth)

    print("=" * 52)
    print("ISAPI Mock 已启动")
    print(f"  地址: http://127.0.0.1:{args.port}")
    print(f"  账号: {args.user} / {args.password}")
    print(f"  通道1: {channel_videos[1]}")
    print(f"  通道2: {channel_videos[2]}")
    print("  前端 ISAPI → IP 填: 127.0.0.1:{port}".format(port=args.port))
    print("  起止时间任意填写均可")
    print("=" * 52)

    run_simple(args.host, args.port, app, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
