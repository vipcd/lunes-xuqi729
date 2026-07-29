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
                requests.post(url, data={"chat_id": chat_id, "caption": "翼龙面板保活实况截图"}, files={"photo": f}, proxies={"http": None, "https": None}, timeout=30)
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
            # ===== 阶段 1：登录 Lunes 客户主站 =====
            print("【阶段 1/3】正在访问 Lunes Host 客户中心...")
            sb.uc_open_with_reconnect("https://betadash.lunes.host/login", reconnect_time=10)
            sb.sleep(6)

            print("正在检测最外层 Cloudflare 盾...")
            try:
                sb.uc_gui_click_captcha()
                sb.sleep(6)
            except Exception as e:
                print(f"外层盾过盾跳过: {e}")

            print("正在填充主站账号密码...")
            sb.wait_for_element_visible("input[type='email']", timeout=20)
            sb.update_text("input[type='email']", LUNES_EMAIL.strip())
            sb.sleep(1)
            sb.update_text("input[type='password']", LUNES_PASSWORD.strip())
            sb.sleep(1)

            if sb.is_element_visible("input[type='checkbox']"):
                sb.click("input[type='checkbox']")

            print("正在检测主站 Turnstile 验证框...")
            try:
                sb.uc_gui_click_captcha()
                sb.sleep(5)
            except Exception as e:
                print(f"主站内嵌盾跳过: {e}")

            submit_btn = "button:contains('Continue'), button:contains('Zaloguj'), button[type='submit']"
            if sb.is_element_visible(submit_btn):
                print("正在点击主站 Continue 提交按钮...")
                sb.click(submit_btn)
                sb.sleep(10)

            # ===== 阶段 2：跳转并登录 翼龙游戏面板 =====
            print(f"\n【阶段 2/3】正在跳转至翼龙面板: {SERVER_URL}")
            sb.open(SERVER_URL)
            sb.sleep(8)

            ptero_user_selector = "input[name='username'], input[type='text'], input[type='email']"
            ptero_pass_selector = "input[name='password'], input[type='password']"
            ptero_login_btn = "button:contains('LOGIN'), button:contains('Login'), button[type='submit']"

            # 检查是否要求在翼龙面板界面登录
            if sb.is_element_visible(ptero_pass_selector):
                print("⚠️ 检测到翼龙面板未登录，自动执行翼龙面板登录操作...")

                # 翼龙面板可能也有 Cloudflare 盾
                try:
                    sb.uc_gui_click_captcha()
                    sb.sleep(5)
                except Exception as e:
                    print(f"翼龙面板过盾跳过: {e}")

                print("正在填写翼龙面板账号与密码...")
                sb.wait_for_element_visible(ptero_user_selector, timeout=15)
                sb.update_text(ptero_user_selector, LUNES_EMAIL.strip())
                sb.sleep(1)
                sb.update_text(ptero_pass_selector, LUNES_PASSWORD.strip())
                sb.sleep(1)

                if sb.is_element_visible(ptero_login_btn):
                    print("正在点击翼龙面板 LOGIN 按钮...")
                    sb.click(ptero_login_btn)
                    sb.sleep(12) # 等待面板登录并跳转进入后台

            # ===== 阶段 3：最终保活确认与截图 =====
            print("\n【阶段 3/3】正在校验最终打卡结果...")
            sb.sleep(5) # 停留确保服务器接收到心跳

            # 再次检查密码框是否依然存在（存在说明仍停留在登录界面）
            still_login_page = sb.is_element_visible(ptero_pass_selector) or "login" in sb.get_current_url().lower()
            sb.save_screenshot("lunes_ptero_result.png")

            if still_login_page:
                msg = "❌ <b>翼龙游戏服务器面板登录打卡失败！</b>\n脚本尝试填充并提交了登录，但页面依然停留在翼龙登录页，请查看截图中的报错。"
                print(msg)
                send_tg_notification(msg, "lunes_ptero_result.png")
            else:
                msg = "✅ <b>翼龙游戏服务器面板保活打卡成功！</b>\n已成功登录并进入翼龙面板内部，完成了活跃心跳刷新。"
                print(msg)
                send_tg_notification(msg, "lunes_ptero_result.png")

    finally:
        if proxy_proc and proxy_proc.poll() is None:
            proxy_proc.terminate()

if __name__ == "__main__":
    run()
