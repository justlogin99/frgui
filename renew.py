#!/usr/bin/env python3
"""
FreeMCHost 自动续期脚本
支持多账号，变量格式: email-----cookie
"""

import os
import re
import sys
import time
import logging
import requests
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://new.freemchost.com"
SERVER_ID = "56059b0f-9531-4443-8233-e418db2f52f9"
MANAGE_URL = f"{BASE_URL}/app/servers/{SERVER_ID}"
API_RENEW_URL = f"{BASE_URL}/api/client/servers/{SERVER_ID}/renew"
API_STATUS_URL = f"{BASE_URL}/api/client/servers/{SERVER_ID}"


def extract_essential_cookies(raw_cookie: str) -> dict:
    """从原始 cookie 字符串中提取登录所需的关键 cookie"""
    essential_keys = ["session-id", "__dpl", "cf_clearance", "__cf_bm"]
    cookies = {}
    for part in raw_cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k in essential_keys:
                cookies[k] = v.strip()
    return cookies


def build_headers(cookies: dict) -> dict:
    """构建请求头"""
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": MANAGE_URL,
        "Origin": BASE_URL,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": cookie_str,
    }


def get_remaining_time(session: requests.Session, headers: dict) -> Optional[str]:
    """获取服务器剩余时间（访问 Manage 页面解析倒计时）"""
    try:
        resp = session.get(MANAGE_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        # 从 aria-label 解析剩余时间，格式如 "1d 23h 34m 12s remaining"
        match = re.search(
            r'aria-label="([\d]+d\s[\d]+h\s[\d]+m\s[\d]+s)\s+remaining"',
            resp.text,
        )
        if match:
            return match.group(1)
        # 备用：尝试从 tabular-nums span 提取数字组合
        nums = re.findall(r'class="[^"]*tabular-nums[^"]*">(\d+)<', resp.text)
        units = re.findall(
            r'class="uppercase[^"]*text-\[10px\][^"]*">([dhms])<', resp.text
        )
        if len(nums) >= 4 and len(units) >= 4:
            return f"{nums[0]}{units[0]} {nums[1]}{units[1]} {nums[2]}{units[2]} {nums[3]}{units[3]}"
        return None
    except Exception as e:
        log.warning(f"获取剩余时间失败: {e}")
        return None


def renew_server(session: requests.Session, headers: dict) -> bool:
    """点击 Renew Now 按钮（调用续期 API）"""
    try:
        # 先访问 Manage 页面（模拟点击 Manage Tab）
        session.get(MANAGE_URL, headers=headers, timeout=20)
        log.info("已访问 Manage 页面")
        time.sleep(1)

        # 尝试常见的续期 API 路径
        renew_endpoints = [
            f"{BASE_URL}/api/client/servers/{SERVER_ID}/renew",
            f"{BASE_URL}/api/client/servers/{SERVER_ID}/extend",
            f"{BASE_URL}/api/client/servers/{SERVER_ID}/reset-expiry",
        ]

        post_headers = {**headers, "Content-Type": "application/json", "Content-Length": "0"}

        for endpoint in renew_endpoints:
            try:
                resp = session.post(endpoint, headers=post_headers, json={}, timeout=20)
                log.info(f"POST {endpoint} → HTTP {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    log.info(f"续期请求成功: {resp.text[:200]}")
                    return True
                elif resp.status_code == 404:
                    continue
                else:
                    log.warning(f"续期返回异常: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                log.warning(f"请求 {endpoint} 失败: {e}")

        # 如果 API 都不对，尝试 PUT
        try:
            resp = session.put(
                f"{BASE_URL}/api/client/servers/{SERVER_ID}/renew",
                headers=post_headers,
                json={},
                timeout=20,
            )
            log.info(f"PUT renew → HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (200, 201, 204):
                return True
        except Exception as e:
            log.warning(f"PUT 续期失败: {e}")

        return False
    except Exception as e:
        log.error(f"续期操作异常: {e}")
        return False


def process_account(email: str, raw_cookie: str) -> bool:
    """处理单个账号的续期流程"""
    log.info(f"{'='*50}")
    log.info(f"账号: {email}")

    cookies = extract_essential_cookies(raw_cookie)
    if not cookies:
        log.error("未能提取到有效 cookie，跳过")
        return False
    log.info(f"使用 cookie 键: {list(cookies.keys())}")

    session = requests.Session()
    headers = build_headers(cookies)

    # Step 1: 获取续期前的剩余时间
    log.info("Step 1: 获取续期前剩余时间...")
    time_before = get_remaining_time(session, headers)
    log.info(f"续期前剩余时间: {time_before or '无法获取'}")

    # Step 2: 执行续期
    log.info("Step 2: 执行 Renew Now...")
    success = renew_server(session, headers)
    if not success:
        log.warning("续期请求未返回成功状态，仍将验证时间变化")

    time.sleep(3)

    # Step 3: 刷新并获取续期后时间
    log.info("Step 3: 刷新页面，获取续期后剩余时间...")
    time_after = get_remaining_time(session, headers)
    log.info(f"续期后剩余时间: {time_after or '无法获取'}")

    # Step 4: 判断是否成功
    if time_before and time_after and time_before != time_after:
        log.info(f"✅ 续期成功！{time_before} → {time_after}")
        return True
    elif time_after:
        log.info(f"⚠️  时间未变化（可能已是最长或今日已续期）: {time_after}")
        # 如果 API 返回成功但时间没变，也算操作完成
        return success
    else:
        log.error("❌ 无法验证续期结果（无法读取剩余时间）")
        return False


def main():
    # 从环境变量 ACCOUNTS 读取，格式: email-----cookie
    # 多账号用换行或分号分隔
    accounts_raw = os.environ.get("ACCOUNTS", "").strip()
    if not accounts_raw:
        log.error("未找到环境变量 ACCOUNTS，请设置后重试")
        sys.exit(1)

    # 支持换行和 | 分隔多账号
    entries = [e.strip() for e in re.split(r"[\n|]+", accounts_raw) if e.strip()]

    results = []
    for entry in entries:
        if "-----" not in entry:
            log.warning(f"格式不正确，跳过: {entry[:30]}...")
            continue
        email, cookie = entry.split("-----", 1)
        email = email.strip()
        cookie = cookie.strip()
        ok = process_account(email, cookie)
        results.append((email, ok))

    log.info(f"\n{'='*50}")
    log.info("执行结果汇总:")
    all_ok = True
    for email, ok in results:
        status = "✅ 成功" if ok else "❌ 失败"
        log.info(f"  {email}: {status}")
        if not ok:
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
