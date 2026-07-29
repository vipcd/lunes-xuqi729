def start_hy2_proxy():
    hy2_url = os.getenv("HY2_URL", "").strip()
    if not hy2_url:
        print("[INFO] 未配置 HY2_URL，将以直连模式运行。")
        return None, None

    print("[INFO] 检测到 HY2 节点，正在启动 Sing-box 本地代理...")
    try:
        node_info = parse_hy2_url(hy2_url)

        # sing-box TLS 配置：去掉不支持的 pinned_sha256
        tls_config = {
            "enabled": True,
            "server_name": node_info["sni"],
            "insecure": node_info["insecure"],
        }

        sing_box_config = {
            "log": {"level": "info"},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": "127.0.0.1",
                    "listen_port": 10808,
                }
            ],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "tag": "hy2-out",
                    "server": node_info["server"],
                    "server_port": node_info["port"],
                    "password": node_info["password"],
                    "tls": tls_config,
                }
            ],
        }

        config_path = "sing_box_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(sing_box_config, f, indent=2)

        proc = subprocess.Popen(
            ["sing-box", "run", "-c", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(3)  # 等待启动

        # 检测 sing-box 是否崩溃
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
