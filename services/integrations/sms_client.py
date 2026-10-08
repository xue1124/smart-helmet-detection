# -*- coding: utf-8 -*-
"""互亿无线短信发送（对照 IhuyiSendSmsUtil.java）."""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

SUCCESS_CODE = "2"
DEFAULT_API_URL = "http://106.ihuyi.com/webservice/sms.php?method=Submit"


def _xml_field(root: ET.Element, name: str) -> str:
    """Read field from ihuyi XML (SubmitResult may use default xmlns)."""
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == name:
            return (elem.text or "").strip()
    return ""


def is_sms_enabled() -> bool:
    return os.environ.get("SMS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def send_sms(mobile: str, content: str, *, timeout: float = 15.0) -> bool:
    """
    发送短信。

    POST 表单: account, password, mobile, content
    返回 XML，<code>2</code> 表示成功。
    """
    api_url = (os.environ.get("SMS_API_URL") or DEFAULT_API_URL).strip()
    api_id = (os.environ.get("SMS_API_ID") or "").strip()
    api_key = (os.environ.get("SMS_API_KEY") or "").strip()
    mobile = (mobile or "").strip()
    content = (content or "").strip()

    if not api_id or not api_key:
        logger.error("短信未配置 SMS_API_ID / SMS_API_KEY")
        return False
    if not mobile or not content:
        logger.error("短信 mobile 或 content 为空")
        return False

    data = {
        "account": api_id,
        "password": api_key,
        "mobile": mobile,
        "content": content,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}

    try:
        resp = requests.post(api_url, data=data, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result = resp.text
        logger.info("短信发送返回: %s", result)

        root = ET.fromstring(result)
        code = _xml_field(root, "code")
        msg = _xml_field(root, "msg")
        logger.info("短信发送状态 code=%s msg=%s", code, msg)

        if code != SUCCESS_CODE:
            logger.warning("短信发送失败 code=%s msg=%s mobile=%s", code, msg, mobile)
            return False
    except Exception as e:
        logger.error("短信发送异常: %s", e)
        return False

    return True
