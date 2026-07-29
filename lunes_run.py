import os
import sys
import json
import time
import re
import socket
import urllib.parse
import subprocess
import asyncio
import requests
from playwright.async_api import async_playwright

# ==================== 1. Telegram 通知 (强制直连，不受代理影响) ====================
def send_telegram_msg(msg: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not bot_token or not chat_id:
        print("[TG通知] 未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳过发送消息。", flush=True)
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML"
    }
    
    try:
        # 强制不设置 proxies，走 GitHub Runner 的直连网络发送 Telegram
        resp = requests.post(url, json=payload, proxies={"http": None, "https": None}, timeout=15)
        if resp.status_code == 200:
            print("[TG通知] Telegram 消息成功发送给机器人！", flush=True)
        else:
            print(f"[TG通知] Telegram 发送失败，状态码: {resp.status_code}, 返回内容: {resp.text}", flush=True)
    except Exception as e:
        print(f"[TG通知] Telegram 发送过程发生异常: {e}", flush=True)

# ==================== 2. HY2 代理解析与启动 ====================
def parse_hy2_url(hy2_url: str):
    hy2_url = hy2_url.strip()
    
    parsed = urllib.parse.urlparse(hy2_url)
    if parsed.scheme in ['hysteria2', 'hy2']:
        password = urllib.parse.unquote(parsed.username or "")
        server = parsed.hostname or ""
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)
    else:
        pattern = r"^(?:hysteria2|hy2)://([^@]+)@([^:/?#]+)(?::(\d+))?\?(.*)$"
        match = re.match(pattern, hy2_url)
        if not match:
            raise ValueError("无法解析该 HY2 链接格式，请检查变量内容。")
        password = urllib.parse.unquote(match.group(1))
        server = match.group(2)
        port = int(match.group(3)) if match.group(3) else 443
        query_str = match.group(4).split('#')[0]
        query = urllib.parse.parse_qs(query_str)

    sni = query.get('sni', [server])[0]
    insecure = query.get('insecure', ['0'])[0] in ['1', 'true'] or query.get('allowInsecure', ['0'])[0] in ['1', 'true']

    return {
        "password": password,
        "server": server,
        "port": port,
        "sni": sni,
        "insecure": insecure
    }

def is_port_open(host="127.0.0.1", port=10808):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    result = sock.connect_ex((host, port))
    sock.close()
    return result == 0

def start_hy2_proxy():
    hy2_url = os.getenv("HY2_URL", "").strip()
    if not hy2_url:
        print("[INFO] 未检测到 HY2_URL 环境变量，将以 GitHub 直连模式运行。", flush=True)
        return None, None

    print("[INFO] 检测到 HY2 节点，正在生成 Sing-box 客户端配置...", flush=True)
    try:
        node_info = parse_hy2_url(hy2_url)
        
        sing_box_config = {
            "log": {"level": "warn"},
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
                    "tls": {
                        "enabled": True,
                        "server_name": node_info["sni"],
                        "insecure": node_info["insecure"]
                    }
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
        
        for _ in range(5):
            time.sleep(1)
            if is_port_open():
                proxy_local = "http://127.0.0.1:10808"
                os.environ["HTTP_PROXY"] = proxy_local
                os.environ["HTTPS_PROXY"] = proxy_local
                print(f"[SUCCESS] HY2 代理启动成功并监听于: {proxy_local}", flush=True)
                return proxy_local, proc

        stdout, stderr = proc.communicate()
        print(f"[ERROR] Sing-box 进程启动失败！日志输出:\n{stderr or stdout}", flush=True)
        return None, None

    except Exception as e:
        print(f"[ERROR] 解析 HY2 节点或生成配置时发生异常: {e}", flush=True)
        return None, None

# ==================== 3. Cookie 解析 ====================
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

# ==================== 4. 主任务运行逻辑 ====================
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

        print("[INFO] 正在启动 Playwright Chromium 浏览器...", flush=True)
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
                print(f"[INFO] 已向浏览器注入 {len(cookies)} 个 Cookie 参数。", flush=True)
            else:
                print("[WARNING] 检测到 LUNES_COOKIE 环境变量，但解析为空！", flush=True)
        else:
            print("[WARNING] 未找到 LUNES_COOKIE 环境变量，将以未登录状态直接访问。", flush=True)

        page = await context.new_page()

        try:
            target_url = "https://lunes.host"
            print(f"[INFO] 正在打开目标页面: {target_url}", flush=True)
            
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            print(f"[INFO] 页面加载完成，状态码: {response.status if response else 'Unknown'}", flush=True)
            
            await asyncio.sleep(5) # 缓冲等待页面渲染

            current_url = page.url
            page_title = await page.title()
            
            print("==================== 登录与运行结果 ====================", flush=True)
            print(f"📌 当前网页标题: {page_title}", flush=True)
            print(f"📌 当前实际网址: {current_url}", flush=True)

            # 判断登录与 Cloudflare 验证状态
            if "Just a moment..." in page_title or "Cloudflare" in page_title:
                print("⚠️ [检测] 触发了 Cloudflare 盾，正在额外等待 10 秒...", flush=True)
                await asyncio.sleep(10)
                page_title = await page.title()
                current_url = page.url

            # 探测登录状态
            has_email_input = await page.locator("input[type='email']").count() > 0
            if "login" in current_url.lower() or has_email_input:
                is_logged_in = False
            else:
                is_logged_in = True

            if not is_logged_in:
                status_text = "❌ 续期失败：Cookie 已失效/被退回登录界面。"
                print(f"📌 登录状态判定: {status_text}", flush=True)
                print("========================================================", flush=True)
                
                send_telegram_msg(f"❌ <b>Lunes 自动续期报告</b>\n<b>状态:</b> Cookie 已过期失效，页面被重定向回登录页。\n<b>当前URL:</b> {current_url}")
                sys.exit(1)

            status_text = "✅ 登录状态有效，访问续期页面成功！"
            print(f"📌 登录状态判定: {status_text}", flush=True)
            print("========================================================", flush=True)
            
            # 发送成功 TG 通知
            send_telegram_msg(f"✅ <b>Lunes 自动续期成功报告</b>\n<b>网页标题:</b> {page_title}\n<b>状态:</b> Cookie 验证正常，保活访问成功！")

        except Exception as e:
            err_detail = f"❌ <b>Lunes 自动续期异常</b>\n<b>详细报错:</b> {str(e)}"
            print(f"[ERROR] 执行出错: {e}", flush=True)
            send_telegram_msg(err_detail)
            sys.exit(1)
            
        finally:
            await browser.close()
            if proxy_proc and proxy_proc.poll() is None:
                proxy_proc.terminate()

if __name__ == "__main__":
    asyncio.run(main())
