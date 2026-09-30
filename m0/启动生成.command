#!/bin/zsh
set -e

cd "$(dirname "$0")"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "首次运行，正在准备环境..."
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv --python python3.11
    uv pip install -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
  fi
fi

read "product?请用一句话介绍商品（名称、卖点、人群、价格）：\n> "
if [[ -z "$product" ]]; then
  echo "商品介绍不能为空。"
  read "?按回车退出..."
  exit 1
fi

echo
read "image?请将商品白底图拖到这里（直接回车则使用测试图）：\n> "
image=${image#\"}
image=${image%\"}

args=("$product" --real --plans 3)
if [[ -n "$image" ]]; then
  args+=(--product-image "$image")
fi

.venv/bin/python cli.py "${args[@]}"

echo
read "?生成完成。按回车关闭窗口..."
