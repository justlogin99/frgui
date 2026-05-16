#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import asyncio
import aiohttp
import random
import re
import subprocess
import json
import datetime
import requests
from seleniumbase import SB

try:
    from nacl import encoding, public
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

# 从环境变量中读取链接配置，保护隐私
LOGIN_URL = os.environ.get("LOGIN_URL")
BASE_URL = os.environ.get("BASE_URL")

# 获取当前脚本所在目录，用于定位同目录下的 proxy_handler.py
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_HANDLER_PATH = os.path.join(SCRIPT_DIR, "proxy_handler.py")

# ==============================================================================
# 原有 FreeMCHost 辅助函数 & 脱敏函数
# ==============================================================================
def mask_ip(text):
    """
    匹配并掩码 IPv4 和端口。
    例如: '204.12.204.4:4597' -> '204.12.204.***'
          '204.12.204.4'      -> '204.12.204.***'
    """
    if not text: 
        return text
    # 匹配前三个网段、第四个网段以及可选的端口号
    ip_pattern = r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.)\d{1,3}(:\d{1,5})?\b'
    # 替换为 前三个网段 + ***
    return re.sub(ip_pattern, r'\g<1>***', text)

def mask_sensitive(text, show_chars=3):
    if not text: return "***"
    text = str(text)
    if len(text) <= show_chars * 2: return "*" * len(text)
    return text[:show_chars] + "*" * (len(text) - show_chars * 2) + text[-show_chars:]

def mask_email(email):
    if not email or "@" not in email: return mask_sensitive(email)
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2: return "*" * len(local) + "@" + domain
    return local[0] + "*" * (len(local) - 2) + local[-1] + "@" + domain

def mask_server_id(server_id):
    if not server_id: return "***"
    if len(server_id) <= 4: return "*" * len(server_id)
    return server_id[:2] + "*" * (len(server_id) - 4) + server_id[-2:]

def parse_accounts():
    accounts_str = os.environ.get("ACCOUNTS", "").strip()
    if not accounts_str: return []
    try:
        accounts = json.loads(accounts_str)
        return [acc for acc in accounts if isinstance(acc, dict) and acc.get("id") and acc.get("email") and acc.get("password")]
    except:
        return []

def get_remaining_seconds(sb):
    try:
        text = sb.execute_script(
            "var el = document.getElementById('session-remaining');"
            "if (el) return el.innerText.trim(); return null;"
        )
        if not text: return None
        match = re.search(r'(\d+)min\s+(\d+)s', text)
        if match: return int(match.group(1)) * 60 + int(match.group(2))
        match_h = re.search(r'(\d+)h\s+(\d+)min', text)
        if match_h: return int(match_h.group(1)) * 3600 + int(match_h.group(2)) * 60
        return None
    except:
        return None

def format_remaining(seconds):
    if seconds is None: return "unknown"
    if seconds <= 0: return "expired"
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h > 0: return f"{str(h).zfill(2)}:{str(m).zfill(2)}:{str(s).zfill(2)}"
    return f"{str(m).zfill(2)}:{str(s).zfill(2)}"

def activate_browser_window():
    try:
        result = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "chrome"], capture_output=True, text=True, timeout=3)
        window_ids = result.stdout.strip().split('\n')
        if window_ids and window_ids[0]:
            subprocess.run(["xdotool", "windowactivate", window_ids[0]], timeout=2, stderr=subprocess.DEVNULL)
            time.sleep(0.1)
    except:
        pass

def click_cf_turnstile(sb):
    try:
        sb.execute_script(
            "var container = document.querySelector('.cf-turnstile');"
            "if (container) container.scrollIntoView({behavior: 'instant', block: 'center'});"
        )
        time.sleep(0.2)
        info = sb.execute_script("""
            var container = document.querySelector('.cf-turnstile');
            var rect = null; var w = 0;
            var iframe = container ? container.querySelector('iframe') : document.querySelector('iframe');
            if (iframe) { rect = iframe.getBoundingClientRect(); w = rect.width; }
            if (!rect || w === 0) {
                if (!container) return null;
                rect = container.getBoundingClientRect(); w = rect.width;
            }
            return {
                screenX: window.screenX || 0, screenY: window.screenY || 0,
                outerHeight: window.outerHeight, innerHeight: window.innerHeight,
                cf_x: rect.x, cf_y: rect.y, cf_w: w, cf_h: rect.height
            };
        """)
        if not info: return False
        
        bar = info["outerHeight"] - info["innerHeight"]
        actual_cf_left = info["cf_x"] + (info["cf_w"] - 300) / 2 if info["cf_w"] > 320 else info["cf_x"]
        
        abs_x = actual_cf_left + 30 + info["screenX"]
        abs_y = info["cf_y"] + (info["cf_h"] / 2) + info["screenY"] + bar

        activate_browser_window()
        subprocess.run(["xdotool", "mousemove", str(int(abs_x)), str(int(abs_y))], timeout=2, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
        return True
    except:
        return False

def is_btn_enabled(sb, selector):
    try: return sb.execute_script(f"var btn = document.querySelector('{selector}'); return btn && !btn.disabled;")
    except: return False

def handle_cf_for_btn(sb, btn_selector):
    for i in range(5):
        if is_btn_enabled(sb, btn_selector): return True
        time.sleep(1)

    for attempt in range(4):
        if is_btn_enabled(sb, btn_selector): return True
        click_cf_turnstile(sb)
        for _ in range(15):
            time.sleep(0.5)
            if is_btn_enabled(sb, btn_selector): return True
    return False

def login(sb, email, password):
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=3)

    print("[*] ⏳ 等待页面加载...")
    time.sleep(5)

    try:
        if sb.is_element_visible('button.fc-close'):
            print("[*] 🖱️ 关闭广告弹窗...")
            sb.click('button.fc-close')
            time.sleep(2)
            sb.execute_script("document.body.style.overflow = 'auto';")
    except Exception as e:
        print(f"[*] ⚠️ 弹窗处理异常: {e}")

    if "/auth" not in sb.get_current_url():
        return True

    try:
        print("[*] ⌨️ 输入账号密码...")
        sb.click('input[type="email"]')
        sb.clear('input[type="email"]')
        sb.send_keys('input[type="email"]', email)

        sb.click('input[type="password"]')
        sb.clear('input[type="password"]')
        sb.send_keys('input[type="password"]', password)

        time.sleep(1)
        sb.execute_script("""
            document.querySelectorAll('input').forEach(el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            });
        """)

    except Exception as e:
        print(f"❌ 输入失败: {e}")
        return False

    handle_cf_for_btn(sb, 'button[type="submit"]')

    print("[*] ⏳ 等待 CF 完成...")
    time.sleep(3)

    try:
        cf_token = sb.execute_script("""
            let el = document.querySelector('[name="cf-turnstile-response"]');
            return el ? el.value : null;
        """)
        print(f"[*] CF Token: {cf_token}")
    except:
        print("[*] ⚠️ 获取 CF Token 失败")

    sb.execute_script("""
        let btn = document.querySelector('button[type="submit"]');
        if (btn) {
            btn.disabled = false;
            btn.removeAttribute('disabled');
        }
    """)
    time.sleep(1)

    print("[*] 🖱️ 点击登录...")
    try:
        sb.execute_script("""
            let btn = document.querySelector('button[type="submit"]');
            if (btn) btn.click();
        """)
    except:
        try:
            sb.click('button[type="submit"]')
        except:
            pass

    for i in range(10):
        time.sleep(1)
        if "/auth" not in sb.get_current_url():
            return True

    return False

def remove_ads(sb):
    for _ in range(5):
        sb.execute_script("""
            document.querySelectorAll('div[role="dialog"], .modal, .popup').forEach(el => el.remove());
            document.querySelectorAll('div').forEach(el => {
                if (el.innerText && el.innerText.includes('Unlock more content')) {
                    el.remove();
                }
            });
            document.querySelectorAll('iframe').forEach(el => el.remove());
            document.querySelectorAll('*').forEach(el => {
                let style = window.getComputedStyle(el);
                if (style.position === 'fixed' && parseInt(style.zIndex) > 1000) {
                    el.remove();
                }
            });
            document.body.style.overflow = 'auto';
            document.documentElement.style.overflow = 'auto';
        """)
        time.sleep(0.5)


def do_renew(sb, server_id):
    renew_url = server_id if server_id.startswith("http") else BASE_URL + server_id
    sb.uc_open_with_reconnect(renew_url, reconnect_time=3)
    time.sleep(2)

    remove_ads(sb)
    
    if "/auth" in sb.get_current_url(): return False

    try:
        present = sb.execute_script("return document.querySelector('.dismissModal') !== null || document.querySelector('.closeModal') !== null;")
        if present:
            sb.execute_script("var btn = document.querySelector('.dismissModal') || document.querySelector('.closeModal'); if(btn) btn.click();")
            time.sleep(0.5)
    except: pass

    handle_cf_for_btn(sb, '#renewSessionBtn')
    
    ready = False
    for i in range(10):
        if is_btn_enabled(sb, '#renewSessionBtn'):
            ready = True
            break
        time.sleep(1)

    if not ready: return False

    sb.execute_script("document.getElementById('renewSessionBtn').click();")
    
    for i in range(10):
        time.sleep(1)
        try:
            success = sb.execute_script("var body = document.body.innerText || ''; return body.includes('renewed successfully') || body.includes('Session renewed');")
            if success: return True
        except: pass
    return True

async def tg_notify(message):
    token, chat_id = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat_id: return
    async with aiohttp.ClientSession() as session:
        try: await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"})
        except: pass

# ==============================================================================
# 代理服务模块
# ==============================================================================
def restart_singbox():
    print("♻️ 正在重启 sing-box...")
    try:
        subprocess.run(['pkill', '-f', 'sing-box run'], stderr=subprocess.DEVNULL)
        time.sleep(2)
        subprocess.Popen(['./sing-box', 'run', '-c', 'config.json'], stdout=open('singbox.log', 'a'), stderr=subprocess.STDOUT)
        time.sleep(5)
        check_process = subprocess.run(['pgrep', '-f', 'sing-box run'], stdout=subprocess.DEVNULL)
        if check_process.returncode != 0:
            print("❌ sing-box 启动失败！")
            try:
                with open("singbox.log", "r") as f: print(f.read()[-500:])
            except: pass
            return False
        print("✅ sing-box 重启成功")
        return True
    except Exception as e:
        print(f"❌ 重启 sing-box 异常: {e}")
        return False

def check_proxy_connectivity(proxy_url, max_retries=2, timeout=5):
    print(f"🔍 正在测试代理连通性 ({max_retries}次重试)...")
    proxies = {"http": proxy_url, "https": proxy_url}
    test_url = "https://api.ipify.org"
    for attempt in range(max_retries):
        try:
            resp = requests.get(test_url, proxies=proxies, timeout=timeout)
            if resp.status_code == 200: return True, resp.text.strip()
        except requests.exceptions.ProxyError:
            return False, "Proxy Refused"
        except: pass
    return False, "Timeout/Unreachable"

# ==============================================================================
# 融合后的主流程
# ==============================================================================
def main():
    accounts = parse_accounts()
    if not accounts: 
        print("❌ 未检测到配置的账号")
        return

    local_proxy_url = "http://127.0.0.1:8080"
    proxy_ready = False
    
    raw_proxy_url = os.environ.get("PROXY_URL", "").strip()
    if raw_proxy_url:
        fixed_proxy_url = re.sub(r'[\r\n\s]+', ',', raw_proxy_url)
        os.environ["PROXY_URL"] = fixed_proxy_url
    
    env_with_utf8 = os.environ.copy()
    env_with_utf8["PYTHONIOENCODING"] = "utf-8"

    try:
        print("\n📋 正在解析代理节点列表...")
        list_result = subprocess.run(['python', PROXY_HANDLER_PATH], capture_output=True, text=True, timeout=10, env=env_with_utf8)
        if list_result.returncode == 0 and "成功解析" in list_result.stdout:
            # 使用 mask_ip 对子进程的输出内容进行脱敏处理
            print(mask_ip(list_result.stdout.strip()))
        else:
            print("⚠️ 未能完全解析出节点列表或部分节点格式异常。")
        print("========================================")
    except Exception as e:
        print(f"⚠️ 读取节点列表时发生异常: {e}")

    try:
        result = subprocess.run(['python', PROXY_HANDLER_PATH, '--count'], capture_output=True, text=True, timeout=10, env=env_with_utf8)
        if result.returncode != 0:
            total_nodes = 0
        else:
            numbers = re.findall(r'\d+', result.stdout.strip())
            total_nodes = int(numbers[0]) if numbers else 0
    except Exception:
        total_nodes = 0

    if total_nodes > 0:
        if total_nodes == 1:
            daily_index = 0
            print("📌 单节点模式，跳过时间分片，直接使用唯一节点")
        else:
            if total_nodes <= 5: segments = total_nodes * 2 
            else: segments = total_nodes      
                
            minutes_per_segment = 1440 / segments
            now = datetime.datetime.now()
            current_minutes = now.hour * 60 + now.minute
            current_segment = int(current_minutes / minutes_per_segment)
            daily_index = current_segment % total_nodes
            
            print(f"⚔️ 检测到 {total_nodes} 个节点，启动自适应排期策略...")
            print(f"⏰ 首选节点索引: {daily_index}")

        for i in range(total_nodes):
            node_idx = (daily_index + i) % total_nodes
            print(f"\n🚀 尝试节点索引: {node_idx}...")

            gen_result = subprocess.run(['python', PROXY_HANDLER_PATH, '--index', str(node_idx)], capture_output=True, text=True, env=env_with_utf8)
            if gen_result.returncode != 0: continue

            if not restart_singbox(): continue

            is_alive, ip_info = check_proxy_connectivity(local_proxy_url)
            if not is_alive:
                print(f"🚫 节点 {node_idx} 代理不通 ({mask_ip(ip_info)})，寻找下一个节点...")
                continue
            else:
                print(f"✅ 节点 {node_idx} 代理连通正常！出口 IP: {mask_ip(ip_info)}")
                proxy_ready = True
                break
    else:
        print("\n⚠️ 无可用代理节点配置，将直接使用 Action 原生网络")

    print("\n🌐 正在确认当前浏览器会话的真实出口 IP...")
    try:
        proxies = {"http": local_proxy_url, "https": local_proxy_url} if proxy_ready else None
        current_ip = requests.get("https://api.ipify.org", proxies=proxies, timeout=5).text.strip()
        print(f"👉 【最终出口 IP】: {mask_ip(current_ip)} (代理状态: {'已开启' if proxy_ready else '直连'})")
    except Exception as e:
        print(f"👉 【最终出口 IP】: 获取失败 ({e})")
    print("========================================\n")

    results = []
    
    try:
        sb_kwargs = {
            "uc": True, 
            "test": True, 
            "locale": "zh-CN", 
            "headless": False, 
            "window_size": "1920,1080", 
            "chromium_arg": "--disable-dev-shm-usage,--no-sandbox,--disable-gpu,--force-device-scale-factor=1,--window-size=1920,1080,--start-maximized"
        }
        
        if proxy_ready:
            sb_kwargs["proxy"] = local_proxy_url

        with SB(**sb_kwargs) as sb:
            for i, account in enumerate(accounts):
                remark = account.get("remark", f"account{i+1}")
                server_id = account.get("id", "").strip()
                result = {"remark": remark, "server_id": server_id, "status": "error"}
                
                print(f"\n========================================")
                print(f"[*] 开始处理账号: {remark}")
                print(f"[*] 正在尝试登录...")
                
                if login(sb, account.get("email"), account.get("password")):
                    print(f"[+] ✅ 登录成功！")
                    print(f"[*] 正在尝试执行续期...")
                    if do_renew(sb, server_id):
                        time.sleep(3)
                        result["status"] = "success"
                        result["remaining"] = get_remaining_seconds(sb)
                        print(f"[+] ✅ 续期成功！当前剩余时间: {format_remaining(result['remaining'])}")
                    else:
                        print(f"[-] ❌ 续期失败，未能通过验证或未找到按钮。")
                        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        screenshot_name = f"renew_failed_{remark}_{timestamp}.png"
                        try:
                            sb.save_screenshot(screenshot_name)
                            print(f"[*] 📸 已保存续期失败截图: {screenshot_name}")
                        except Exception as e:
                            print(f"[*] ⚠️ 保存截图失败: {e}")
                else:
                    print(f"[-] ❌ 登录失败，请检查账号密码或 CF 盾阻拦。")
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    screenshot_name = f"login_failed_{remark}_{timestamp}.png"
                    try:
                        sb.save_screenshot(screenshot_name)
                        print(f"[*] 📸 已保存登录失败截图: {screenshot_name}")
                    except Exception as e:
                        print(f"[*] ⚠️ 保存截图失败: {e}")
                
                print(f"========================================")
                results.append(result)
                
    except Exception as e:
        error_msg = f"FreeMCHost ERROR: Browser failed -> {str(e)[:100]}"
        print(f"💥 {error_msg}")
        asyncio.run(tg_notify(error_msg))
        return

    lines = ["<b>FreeMCHost Renew</b>", ""]
    for r in results:
        icon = "✅" if r["status"] == "success" else "❌"
        lines.append(f"{icon} {r['remark']}")
        if r["status"] == "success": lines.append(f"  Remaining: {format_remaining(r.get('remaining'))}")
    asyncio.run(tg_notify("\n".join(lines)))

if __name__ == "__main__":
    main()
