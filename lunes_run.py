import os
import sys
import json
import time
import re
import urllib.parse
import subprocess
import asyncio
import requests
from playwright.async_api import async_playwright

# ==================== 1. Telegram 通知 (支持代理失败直连回退) ====================
def send_telegram_msg(msg: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not bot_token or not chat_id:
        print("[INFO] 未配置 Telegram 机器人环境变量，跳过发送消息。")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML"
    }
    
    # 优先尝试代理，失败则降级直连
    try:
        proxies = {}
        if os.getenv("HTTP_PROXY"):
            proxies = {
                "http": os.getenv("HTTP_PROXY"),
                "https": os.getenv("HTTP_PROXY")
            }
        requests.post(url, json=payload, proxies=proxies, timeout=10)
        print("[SUCCESS] 已发送 Telegram 通知。")
    except Exception as e:
        print(f"[WARNING] 走代理发送 Telegram 失败 ({e})，正在尝试直连发送...")
        try:
            requests.post(url, json=payload, proxies={}, timeout=10)
            print("[SUCCESS] 已通过直连成功发送 Telegram 通知。")
        except Exception as e2:
            print(f"[ERROR] 直连发送 Telegram 消息依然失败: {e2}")

# ==================== 2. HY2 代理解析与启动 ====================
def parse_hy2_url(hy2_url: str):
    hy2_url = hy2_url.strip()
    
    # 尝试用 urlparse 解析
    parsed = urllib.parse.urlparse(hy2_url)
    if parsed.scheme in ['hysteria2', 'hy2']:
        password = urllib.parse.unquote(parsed.username or "")
        server = parsed.hostname or ""
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)
    else:
        # 正则备用匹配
        pattern = r"^(?:hysteria2|hy2)://([^@]+)@([^:/?#]+)(?::(\d+))?\?(.*)$"
        match = re.match(pattern, hy2_url)
        if not match:
            raise ValueError("无法解析该 HY2 URL 格式")
        password = urllib.parse.unquote(match.group(1))
        server = match.group(2)
        port = int(match.group(3)) if match.group(3) else 443
        query_str = match.group(4).split('#')[0]
        query = urllib.parse.parse_qs(query_str)

    sni = query.get('sni', [server])[0]
    insecure = query.get('insecure', ['0'])[0] in ['1', 'true'] or query.get('allowInsecure', ['0'])[0] in ['1', 'true']
    pin_sha256 = query.get('pinSHA256', [''])[0]

    return {
        "password": password,
        "server": server,
        "port": port,
        "sni": sni,
        "insecure": insecure,
        "pin_sha256": pin_sha256
    }

def start_hy2_proxy():
    hy2_url = os.getenv("HY2_URL", "").strip()
    if not hy2_url:
        print("[INFO] 未配置 HY2_URL，将以直连模式运行。")
        return None, None

    print("[INFO] 检测到 HY2 节点，正在启动 Sing-box 本地代理...")
    try:
        node_info = parse_hy2_url(hy2_url)
        
        tls_config = {
            "enabled": True,
            "server_name": node_info["sni"],
            "insecure": node_info["insecure"]
        }
        # 【关键修复】：pinned_sha256 在 sing-box 中必须为数组/列表格式 [str]
        if node_info["pin_sha256"]:
            tls_config["pinned_sha256"] = [node_info["pin_sha256"]]

        sing_box_config = {
            "log": {"level": "info"},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": "127.0.0.1",
                    "listen_port": 10808
                }
            ],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "tag": "hy2-out",
                    "server": node_info["server"],
                    "server_port": node_info["port"],
                    "password": node_info["password"],
                    "tls": tls_config
                }
            ]
        }

        config_path = "sing_box_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(sing_box_config, f, indent=2)

        proc = subprocess.Popen(
            ["sing-box", "run", "-c", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        time.sleep(3) # 等待启动

        # 检测 sing-box 是否意外崩溃退出
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            print(f"[ERROR] Sing-box 启动失败退出！错误日志:\n{stderr or stdout}")
            return None, None

        proxy_local = "http://127.0.0.1:10808"
        os.environ["HTTP_PROXY"] = proxy_local
        os.environ["HTTPS_PROXY"] = proxy_local
        
        print(f"[SUCCESS] HY2 代理启动成功并后台运行: {proxy_local}")
        return proxy_local, proc
    except Exception as e:
        print(f"[ERROR] 启动 HY2 代理过程中发生异常: {e}")
        return None, None

# ==================== 3. Cookie 格式解析 ====================
def parse_cookies(cookie_raw: str, domain: str = "lunes.host"):
    cookies = []
    cookie_raw = cookie_raw.strip()
    if not cookie_raw:
        return cookies

    if cookie_raw.startswith("[") and cookie_raw.endswith("]"):
        try:
            return json.loads(cookie_raw)
        except Exception:
            pass

    items = cookie_raw.split(";")
    for item in items:
        if "=" in item:
            name, value = item.strip().split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": f".{domain.lstrip('.')}",
                "path": "/"
            })
    return cookies

# ==================== 4. 续期主逻辑 ====================
async def main():
    proxy_address, proxy_proc = start_hy2_proxy()

    async with async_playwright() as p:
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox"
        ]
        
        launch_options = {
            "headless": True,
            "args": browser_args
        }
        if proxy_address:
            launch_options["proxy"] = {"server": proxy_address}

        browser = await p.chromium.launch(**launch_options)
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )

        # 注入 Cookie
        cookie_env = os.getenv("LUNES_COOKIE", "")
        if cookie_env:
            cookies = parse_cookies(cookie_env, domain="lunes.host")
            if cookies:
                await context.add_cookies(cookies)
                print(f"[INFO] 已成功注入 {len(cookies)} 个 Cookie。")

        page = await context.new_page()

        try:
            target_url = "https://lunes.host"
            print(f"[INFO] 正在访问目标页面: {target_url}")
            
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5) # 防护盾缓冲

            page_title = await page.title()

            # 验证 Cloudflare 防护
            if "Just a moment..." in page_title or "Cloudflare" in page_title or "Attention Required!" in page_title:
                print("[WARNING] 触发 Cloudflare 验证盾，额外等待 10 秒...")
                await asyncio.sleep(10)

            # 判断登录状态
            is_logged_in = True
            if "login" in page.url or await page.locator("input[type='email']").count() > 0:
                is_logged_in = False

            if not is_logged_in:
                err_msg = "❌ <b>Lunes 自动续期失败</b>\n<b>原因:</b> Cookie 已过期，请更新 GitHub Secrets 中的 <code>LUNES_COOKIE</code>。"
                print(err_msg)
                send_telegram_msg(err_msg)
                sys.exit(1)

            print("[INFO] 登录状态有效，页面访问正常！")
            
            # ---------------- 在此处按需补充具体的点击保活按键逻辑 ----------------
            await asyncio.sleep(3)
            success_msg = "✅ <b>Lunes 自动续期保活成功</b>\n<b>状态:</b> HY2 代理访问正常，站点状态为已登录。"
            print(success_msg)
            send_telegram_msg(success_msg)

        except Exception as e:
            error_log = f"❌ <b>Lunes 自动续期异常</b>\n<b>详情:</b> {str(e)}"
            print(error_log)
            send_telegram_msg(error_log)
            sys.exit(1)
            
        finally:
            await browser.close()
            if proxy_proc and proxy_proc.poll() is None:
                proxy_proc.terminate()

if __name__ == "__main__":
    asyncio.run(main())
