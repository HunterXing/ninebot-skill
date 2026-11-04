#!/usr/bin/env bash
# Ninebot skill — 一键安装到本机 Agent 生态
#
# 用法:
#   bash install.sh              # 装到检测到的所有 Agent
#   bash install.sh --hermes     # 只装 Hermes
#   bash install.sh --openclaw   # 只装 OpenClaw
#   bash install.sh --claude     # 只装 Claude Code
#
# 安装 = 软链或复制 SKILL.md + scripts/ 到目标 skills 目录。
# 默认用软链（本仓库更新即生效）；加 --copy 改为复制。
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="link"
TARGETS=()

for arg in "$@"; do
  case "$arg" in
    --hermes)   TARGETS+=("$HOME/.hermes/skills") ;;
    --openclaw) TARGETS+=("$HOME/.openclaw/skills") ;;
    --claude)   TARGETS+=("$HOME/.claude/skills") ;;
    --codex)    TARGETS+=("$HOME/.codex/skills") ;;
    --copy)     MODE="copy" ;;
    --help|-h)  sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

if [ ${#TARGETS[@]} -eq 0 ]; then
  for probe in \
    "$HOME/.hermes/skills:Hermes" \
    "$HOME/.openclaw/skills:OpenClaw" \
    "$HOME/.claude/skills:Claude Code" \
    "$HOME/.codex/skills:Codex"; do
    dir="${probe%%:*}"; label="${probe##*:}"
    [ -d "$dir" ] && TARGETS+=("$dir")
  done
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "没找到任何 Agent 的 skills 目录。手动指定，例如: bash install.sh --hermes" >&2
  exit 1
fi

for target_root in "${TARGETS[@]}"; do
  dest="$target_root/ninebot-skill"
  echo "→ 安装到 $dest"
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  if [ "$MODE" = "copy" ]; then
    mkdir -p "$dest"
    cp -r "$REPO_DIR/SKILL.md" "$REPO_DIR/scripts" "$REPO_DIR/references" "$dest/"
    chmod +x "$dest"/scripts/*.py
  else
    ln -s "$REPO_DIR" "$dest"
  fi
  echo "  完成"
done

cat <<'EOF'

下一步:
  1) 认证（账号后端，不需内测名额）:
       python3 scripts/ninebot_login.py --user <你的手机号>
     或官方内测 Key:
       export NINEBOT_DEVICESERVICE_KEY=<key>
  2) 验证:
       python3 scripts/ninebot_query.py
EOF
