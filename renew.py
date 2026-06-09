#!/usr/bin/env python3
"""
FreeMCHost 自动续期脚本
使用 Playwright 模拟真实浏览器登录并续期
变量格式: 邮箱-----密码-----serverid (多账号换行分隔)
"""

import os
import re
import sys
import time
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://new.freemchost.com"


def mask_email(email: str) -> str:
    """脱敏邮箱：保留首字符和@后域名首字符，其余替换为*
    例: justloginvip@proton.me → j**********@p*****.me
    """
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked_local = local[0] + "*" * (len(local) - 1) if len(local) > 1 else "*"
    domain_parts = domain.split(".")
    masked_domain = domain_parts[0][0] + "*" * (len(domain_parts[0]) - 1)
    suffix = "." + ".".join(domain_parts[1:]) if len(domain_parts) > 1 else ""
    return f"{masked_local}@{masked_domain}{suffix}"


def mask_server_id(server_id: str) -> str:
    """脱敏 ServerID：只显示前8位
    例: 56059b0f-9531-4443-... → 56059b0f...
    """
    return server_id[:8] + "..." if len(server_id) > 8 else server_id


def parse_time_text(text: str) -> str:
    """从页面 aria-label 或文本中提取剩余时间"""
    # 匹配 "1d 23h 34m 12s remaining"
    m = re.search(r"(\d+d\s+\d+h\s+\d+m\s+\d+s)\s+remaining", text)
    if m:
        return m.group(1)
    return text.strip()


def get_remaining_time(page) -> str:
    """读取页面上的倒计时剩余时间"""
    try:
        # 等待倒计时组件出现
        timer = page.locator('[role="timer"]').first
        timer.wait_for(timeout=15000)
        label = timer.get_attribute("aria-label") or ""
        if label:
            return parse_time_text(label)
        # 备用：读取各数字格子
        digits = timer.locator(".tabular-nums").all_text_contents()
        units = timer.locator(".uppercase.tracking-wider").all_text_contents()
        if len(digits) >= 4 and len(units) >= 4:
            return f"{digits[0]}{units[0]} {digits[1]}{units[1]} {digits[2]}{units[2]} {digits[3]}{units[3]}"
    except Exception as e:
        log.warning(f"读取剩余时间失败: {e}")
    return "无法获取"


def process_account(email: str, password: str, server_id: str) -> bool:
    log.info("=" * 55)
    log.info(f"账号: {mask_email(email)}  ServerID: {mask_server_id(server_id)}")

    manage_url = f"{BASE_URL}/app/servers/{server_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        try:
            # ── Step 1: 登录 ──────────────────────────────────────
            log.info("Step 1: 访问登录页...")
            page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=30000)
            log.info(f"  当前页面: {page.url}")

            # 填写邮箱
            page.locator('input[type="email"], input[name="email"], input[name="user"]').first.fill(email)
            # 填写密码
            page.locator('input[type="password"]').first.fill(password)
            # 点击登录按钮
            page.locator('button[type="submit"]').first.click()

            # 等待跳转到 dashboard 或 app
            try:
                page.wait_for_url(re.compile(r"/app|/dashboard"), timeout=20000)
                log.info(f"  登录成功，跳转到: {page.url}")
            except PWTimeout:
                # 检查是否有错误提示
                body_text = page.locator("body").inner_text()
                if any(k in body_text.lower() for k in ["invalid", "incorrect", "error", "wrong"]):
                    log.error("  登录失败：邮箱或密码错误")
                    return False
                log.warning(f"  未检测到跳转，当前页: {page.url}，继续尝试...")

            # ── Step 2: 访问 Manage 页面 ──────────────────────────
            log.info("Step 2: 访问服务器 Manage 页面...")
            page.goto(manage_url, wait_until="networkidle", timeout=30000)

            # 点击 Manage Tab（如果不是默认选中）
            manage_tab = page.locator('[role="tab"]:has-text("Manage")')
            if manage_tab.count() > 0:
                manage_tab.first.click()
                page.wait_for_timeout(1500)
                log.info("  已点击 Manage 标签")

            # ── Step 3: 获取续期前剩余时间 ────────────────────────
            log.info("Step 3: 读取续期前剩余时间...")
            time_before = get_remaining_time(page)
            log.info(f"  续期前: {time_before}")

            # ── Step 4: 点击 Renew Now ───────────────────────────
            log.info("Step 4: 点击 Renew Now...")
            renew_btn = page.locator('button:has-text("Renew now"), button:has-text("Renew Now")')
            if renew_btn.count() == 0:
                log.error("  未找到 Renew Now 按钮，请确认页面结构")
                page.screenshot(path="/tmp/debug.png")
                return False

            renew_btn.first.click()
            log.info("  已点击 Renew Now，等待响应...")
            page.wait_for_timeout(3000)

            # 处理可能弹出的确认对话框
            confirm = page.locator('button:has-text("Confirm"), button:has-text("Yes"), button:has-text("确认")')
            if confirm.count() > 0:
                confirm.first.click()
                log.info("  已确认弹窗")
                page.wait_for_timeout(2000)

            # ── Step 5: 刷新并读取续期后时间 ─────────────────────
            log.info("Step 5: 刷新页面，读取续期后剩余时间...")
            page.reload(wait_until="networkidle", timeout=30000)

            # 重新点击 Manage Tab
            manage_tab = page.locator('[role="tab"]:has-text("Manage")')
            if manage_tab.count() > 0:
                manage_tab.first.click()
                page.wait_for_timeout(1500)

            time_after = get_remaining_time(page)
            log.info(f"  续期后: {time_after}")

            # ── Step 6: 判断结果 ─────────────────────────────────
            if time_after == "无法获取":
                log.warning("  无法读取续期后时间，无法判断结果")
                return False

            if time_before != time_after:
                log.info(f"✅ 续期成功！{time_before} → {time_after}")
                return True
            else:
                log.info(f"⚠️  时间未变化: {time_after}（可能已续期或已是最长周期）")
                return True  # 不变化也算操作完成

        except Exception as e:
            log.error(f"操作异常: {e}")
            try:
                page.screenshot(path="/tmp/debug.png")
                log.info("  调试截图已保存到 /tmp/debug.png")
            except Exception:
                pass
            return False
        finally:
            browser.close()


def main():
    accounts_raw = os.environ.get("ACCOUNTS", "").strip()
    if not accounts_raw:
        log.error("未找到环境变量 ACCOUNTS，请设置后重试")
        sys.exit(1)

    entries = [e.strip() for e in re.split(r"[\n|]+", accounts_raw) if e.strip()]

    results = []
    for entry in entries:
        parts = entry.split("-----")
        if len(parts) != 3:
            log.warning(f"格式不正确（应为 邮箱-----密码-----serverid），跳过: {entry[:30]}...")
            continue
        email, password, server_id = [p.strip() for p in parts]
        ok = process_account(email, password, server_id)
        results.append((email, server_id, ok))

    log.info("\n" + "=" * 55)
    log.info("执行结果汇总:")
    all_ok = True
    for email, sid, ok in results:
        status = "✅ 成功" if ok else "❌ 失败"
        log.info(f"  {mask_email(email)} [{mask_server_id(sid)}]: {status}")
        if not ok:
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
