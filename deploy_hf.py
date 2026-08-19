#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
创作雷达 → Hugging Face Spaces 一键部署（需用户提供 HF 令牌）。

用法:
    pip install huggingface_hub
    HF_TOKEN=hf_xxx python3 deploy_hf.py

说明:
- 令牌只用于本次 API 调用，不会写入代码/镜像/前端。
- 4 个密钥通过 Space Secrets 注入（运行时环境变量），本地 .env 不会被上传。
- 部署完成后得到稳定网址: https://<你的名>-chuangzuo-radar.hf.space
"""
import os
import sys

try:
    from huggingface_hub import HfApi
except ImportError:
    sys.exit("请先安装依赖: pip install huggingface_hub")


def read_local_env(path=".env"):
    """只读本地 .env，用于把密钥注入 Secrets（密钥不会进镜像）。"""
    d = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return d


def main():
    token = os.getenv("HF_TOKEN")
    if not token:
        sys.exit("错误: 请先设置 HF_TOKEN 环境变量（你的 hf_... 令牌）。\n"
                 "例如: HF_TOKEN=hf_xxx python3 deploy_hf.py")

    local = read_local_env()
    secret_keys = ["OPENROUTER_API_KEY", "OPENROUTER_MODEL",
                   "XHS_PROVIDER", "TAVILY_API_KEY"]

    api = HfApi(token=token)
    me = api.whoami()
    user = me.get("name") or me.get("id")
    if not user:
        sys.exit("错误: 无法读取 HF 账号信息，令牌可能无效。")
    space = f"{user}/chuangzuo-radar"
    print(f"[1/3] 已登录 HF 账号: {user}")
    print(f"      目标 Space: {space}")

    # 1) 创建 Space（Docker SDK）
    api.create_repo(repo_id=space, repo_type="space",
                    space_sdk="docker", private=False, exist_ok=True)
    print("[2/3] Space 已创建/已存在")

    # 2) 注入密钥为 Secrets（不写镜像）
    for k in secret_keys:
        v = local.get(k)
        if not v:
            print(f"      [跳过] 本地未找到 {k}")
            continue
        api.add_space_secret(repo_id=space, key=k, value=v)
        print(f"      [OK] 已注入 Secret: {k}")

    # 3) 上传代码（排除 .env / 部署脚本等）
    api.upload_folder(
        repo_id=space,
        repo_type="space",
        folder_path=".",
        ignore_patterns=[".env", ".env.example", ".git", ".gitignore",
                         "*.bak", "__pycache__", "*.pyc", "deploy_hf.py"],
    )
    url = f"https://{space.replace('/', '-')}.hf.space"
    print("[3/3] 代码已上传，Space 正在构建……")
    print("=" * 56)
    print("部署完成！稳定网址:")
    print("  " + url)
    print("=" * 56)
    print("首次构建约 1-3 分钟，打开后右上角应显示「已连接 · 实时检索」。")


if __name__ == "__main__":
    main()
