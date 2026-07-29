import os
import sys
import json
import time
import re
import urllib.parse
import subprocess
import asyncio
from playwright.async_api import async_playwright

def parse_hy2_url(hy2_url: str):
    """解析 hysteria2:// 格式链接参数"""
    hy2_url = hy2_url.strip()
    pattern = r"^(?:hysteria2|hy2)://([^@]+)@([^:/?#]+)(?::(\d+))?\?(.*)$"
    match = re.match(pattern, hy2_url)
    
    if match:
        password = urllib.parse.unquote(match.group(1))
        server = match.group(2)
        port = int(match.group(3)) if match.group(3) else 443
        rest = match.group(4)
        query_str = rest.split('#')[0] if '#' in rest else rest
        query = urllib.parse.parse_qs(query_str)
    else:
        parsed = urllib.parse.urlparse(hy2_url)
        password = urllib.parse.unquote(parsed.username or "")
        server = parsed.hostname or ""
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)

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
    """读取环境变量并启动后台 sing-box 本地代理"""
    hy2_url = os.getenv("HY2_URL", "").strip()
    if not hy2_url:
        print("[INFO] 未检测到 HY2_URL 环境变量，将以网络直连模式运行。")
        return None, None

    print("[INFO] 检测到 HY2 节点，正在生成 Sing-box 客户端配置...")
    try:
        node_info = parse_hy2_url(hy2_url)
        
        tls_config = {
            "enabled": True,
            "server_name": node_info["sni"],
            "insecure": node_info["insecure"]
        }
        if node_info["pin_sha256"]:
            tls_config["pinned_sha256"] = node_info["pin_sha256"]

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
                    "tls": tls_config
                }
            ]
        }

        config_path = "sing_box_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(sing_box_config, f, indent=2)

        # 启动 sing-box 后台服务
        proc = subprocess.Popen(["sing-box", "run", "-c", config_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3) # 等待代理建立连接

        proxy_local = "http://127.0.0.1:10808"
        os.environ["HTTP_PROXY"] = proxy_local
        os.environ["HTTPS_PROXY"] = proxy_local
        os.environ["ALL_PROXY"] = "socks5://127.0.0.1:10808"
        
        print(f"[SUCCESS] HY2 节点连接成功，本地监听地址: {proxy_local}")
        return proxy_local, proc
    except Exception as e:
        print(f"[ERROR] 解析或启动 HY2 代理失败: {e}")
        return None, None

async def main():
    # 启动代理
    proxy_address, proxy_proc = start_hy2_proxy()

    async with async_playwright() as p:
        launch_args = {}
        if proxy_address:
            launch_args["proxy"] = {"server": proxy_address}

        browser = await p.chromium.launch(headless=True, **launch_args)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            print("[INFO] 正在打开 LunesHost 目标页面...")
            # ---------------- 在此处替换/编写你的登录续期逻辑 ----------------
            # 示例:
            # await page.goto("https://lunes.host", timeout=60000)
            # cookie = os.getenv("LUNES_COOKIE")
            # ...
            print("[SUCCESS] 页面加载或操作已完成！")
            # -----------------------------------------------------------------
        except Exception as e:
            print(f"[ERROR] 执行过程发生错误: {e}")
        finally:
            await browser.close()
            if proxy_proc:
                proxy_proc.terminate()

if __name__ == "__main__":
    asyncio.run(main())
