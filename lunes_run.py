import os
import sys
import time
import json
import socket
import subprocess
import urllib.parse
import re
import requests
from seleniumbase import SB

SERVER_URL = os.getenv("LUNES_SERVER_URL")
LUNES_EMAIL = os.getenv("LUNES_EMAIL")
LUNES_PASSWORD = os.getenv("LUNES_PASSWORD")

# ==================== 1. HY2 代理解析与启动 ====================
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
            raise ValueError("无法解析该 HY2 链接格式。")
        password = urllib.parse.unquote(match.group(1))
        server = match.group(2)
        port = int(match.group(3)) if match.group(3) else 443
        query = urllib.parse.parse_qs(match.group(4).split('#')[0])

    sni = query.get('sni', [server])[0]
    insecure = query.get('insecure', ['0'])[0] in ['1', 'true'] or query.get('allowInsecure', ['0'])[0] in ['1', 'true']
    return {"password": password, "server": server, "port": port, "sni": sni, "insecure": insecure}

def is_port_open(host="127.0.0.1", port=10808):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    result = sock.connect_ex((host, port))
    sock.close()
    return result == 0

def start_hy2_proxy():
    hy2_url = os.getenv("HY2_URL", "").strip()
    if not hy2_url:
        print("[INFO] 未检测到 HY2_URL，将以直连模式运行。", flush=True)
        return None, None

    print("[INFO] 检测到 HY2 节点，正在生成 Sing-box 客户端配置...", flush=True)
    try:
        node_info = parse_hy2_url(hy2_url)
        sing_box_config = {
            "log": {"level": "warn"},
            "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 10808}],
            "outbounds": [{
                "type": "hysteria2", "tag": "hy2-out",
                "server": node_info["server"], "server_port": node_info["port"],
                "password": node_info["password"],
                "tls": {"enabled": True, "server_name": node_info["sni"], "insecure": node_info["insecure"]}
            }]
        }
        with open("sing_box_config.json", "w", encoding="utf-8") as f:
            json.dump(sing_box_config, f, indent=2)

        proc = subprocess.Popen(["sing-box", "run", "-c", "sing_box_config.json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(5):
            time.sleep(1)
            if is_port_open():
                proxy_local = "http://127.0.0.1:10808"
                print(f"[SUCCESS] HY2 代理已成功监听于: {proxy_local}", flush=True)
                return proxy_local, proc
        print("[ERROR] Sing-box 启动超时！", flush=True)
        return None, None
    except Exception as e:
        print(f"[ERROR] 启动 HY2 代理异常: {e}", flush=True)
        return None, None

# ==================== 2. TG 通知逻辑 ====================
def send_tg_notification(message, photo_path=None):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        return

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, proxies={"http": None, "https": None}, timeout=15)
    except Exception as e:
        print(f"发送 TG 消息异常: {e}")

    if photo_path and os.path.exists(photo_path):
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            with open(photo_path, "rb") as f:
                requests.post(url, data={"chat_id": chat_id, "caption": "Lunes Host 运行实况截图"}, files={"photo": f}, proxies={"http": None, "https": None}, timeout=30)
        except Exception as e:
            print(f"发送 TG 截图异常: {e}")

# ==================== 3. 主任务逻辑 ====================
def run():
    if not SERVER_URL or not LUNES_EMAIL or not LUNES_PASSWORD:
        print("错误: 缺少 LUNES_SERVER_URL、LUNES_EMAIL 或 LUNES_PASSWORD 环境变量")
        return

    proxy_addr, proxy_proc = start_hy2_proxy()
    sb_kwargs = {"uc": True, "xvfb": True}
    if proxy_addr:
        sb_kwargs["proxy"] = proxy_addr

    try:
        with SB(**sb_kwargs) as sb:
            print("正在打开 Lunes Host 登录界面...")
            sb.uc_open_with_reconnect("https://betadash.lunes.host/login", reconnect_time=10)
            sb.sleep(6)

            # 第一次物理过盾 (最外层 Cloudflare 盾)
            print("正在检测最外层 Cloudflare 盾并尝试物理点击...")
            try:
                sb.uc_gui_click_captcha()
                sb.sleep(6)
            except Exception as e:
                print(f"外层盾点击跳过: {e}")

            # 检查并填写表单
            print("正在定位账号密码输入框并填充...")
            sb.wait_for_element_visible("input[type='email']", timeout=20)
            sb.update_text("input[type='email']", LUNES_EMAIL.strip())
            sb.sleep(1)
            sb.update_text("input[type='password']", LUNES_PASSWORD.strip())
            sb.sleep(1)

            if sb.is_element_visible("input[type='checkbox']"):
                sb.click("input[type='checkbox']")

            # 第二次物理过盾 (表单 Turnstile 人机框)
            print("正在检测表单 Turnstile 框并尝试物理点击...")
            try:
                sb.uc_gui_click_captcha()
                sb.sleep(6)
            except Exception as e:
                print(f"内嵌盾点击跳过: {e}")

            sb.save_screenshot("lunes_before_submit.png")

            # 点击登录提交
            submit_btn = "button:contains('Continue'), button:contains('Zaloguj'), button[type='submit']"
            if sb.is_element_visible(submit_btn):
                print("正在点击提交按钮...")
                sb.click(submit_btn)
                sb.sleep(12) # 预留充分的登录处理时间

            # 验证结果
            current_url = sb.get_current_url()
            sb.save_screenshot("lunes_result.png")

            if "login" in current_url or sb.is_element_visible("input[type='email']"):
                print("❌ 自动登录失败：仍停留在登录页面。")
                send_tg_notification("❌ <b>Lunes Host 登录失败</b>\n未能成功进入后台系统（请检查账号密码或 TG 截图中的报错提示）。", "lunes_result.png")
            else:
                print(f"✓ 登录成功！正在跳转至目标面板: {SERVER_URL}")
                sb.open(SERVER_URL)
                sb.sleep(15)
                sb.save_screenshot("lunes_result.png")
                
                send_tg_notification("✅ <b>Lunes Host 每日自动打卡成功！</b>\n已刷新后台活跃心跳状态。", "lunes_result.png")

    finally:
        if proxy_proc and proxy_proc.poll() is None:
            proxy_proc.terminate()

if __name__ == "__main__":
    run()
