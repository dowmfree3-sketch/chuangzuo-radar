# 创作雷达 · 部署镜像（适用于 Hugging Face Spaces Docker SDK / 任意容器平台）
FROM python:3.11-slim

WORKDIR /app

# 仅标准库，不装依赖；复制全部源码
COPY . .

# 平台会注入 PORT（Hugging Face Spaces 默认 7860；Render/Railway 亦提供）
ENV PORT=7860

EXPOSE 7860

CMD ["python", "server.py"]
