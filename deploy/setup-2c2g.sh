#!/usr/bin/env bash
# lolita-agents —— 2C2G（2 GiB）低配档系统准备
#
# 做四件事：基础依赖、运行用户、目录、2 GiB swap 与 swappiness、防火墙。
# 幂等：已存在的用户/目录/swap/fstab 条目/ufw 规则都会跳过，可重复执行。
# 失败即退出：任何一步返回非 0 就停，不会带着半成品继续。
#
# 不包含任何购买、注册、DNS 解析、证书签发动作——那些要么在云控制台，
# 要么需要人工输入密码/邮箱，按 deploy/README.md 的顺序单独做。
#
# 用法（在服务器上，root 或 sudo）：
#   sudo bash deploy/setup-2c2g.sh
# 路径可覆盖（一般不用改）：
#   sudo APP_DIR=/srv/lolita-agents RUN_USER=lolita bash deploy/setup-2c2g.sh
#
# 注意：Ubuntu 22.04 自带的 python3 是 3.10；本仓未声明最低版本，若装依赖时
# 报语法/版本问题，再按 docs/deploy.md 3.4 换成 3.11（deadsnakes 源）。

set -euo pipefail

APP_DIR="${APP_DIR:-/srv/lolita-agents}"
UPSTREAM_DIR="${UPSTREAM_DIR:-/srv/lolita-rag}"
RUN_USER="${RUN_USER:-lolita}"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"

SWAPFILE="${SWAPFILE:-/swapfile}"
SWAP_MB="${SWAP_MB:-2048}"
SWAPPINESS="${SWAPPINESS:-30}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "请用 root 或 sudo 执行"

# ---------------------------------------------------------------- 1. 基础依赖
log "安装基础依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg rsync git ufw \
    python3 python3-venv python3-pip build-essential libgomp1 \
    nginx certbot apache2-utils

# ---------------------------------------------------------------- 2. 运行用户
if id -u "$RUN_USER" >/dev/null 2>&1; then
    log "用户 $RUN_USER 已存在，跳过"
else
    log "创建运行用户 $RUN_USER"
    useradd -m -s /bin/bash "$RUN_USER"
fi

# ---------------------------------------------------------------- 3. 目录
log "准备目录 $APP_DIR 与上游父目录"
install -d -m 755 -o "$RUN_USER" -g "$RUN_GROUP" "$APP_DIR"
install -d -m 755 -o "$RUN_USER" -g "$RUN_GROUP" "$APP_DIR/data"
install -d -m 755 "$(dirname "$UPSTREAM_DIR")"
install -d -m 755 /var/www/certbot

# ---------------------------------------------------------------- 4. swap
log "配置 ${SWAP_MB}MB swap：${SWAPFILE}"
if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
    log "swap 已启用，跳过创建"
else
    if [[ ! -f "$SWAPFILE" ]]; then
        # fallocate 在部分文件系统（如 overlayfs）上不被支持，用 dd 兜底。
        if ! fallocate -l "${SWAP_MB}M" "$SWAPFILE"; then
            log "fallocate 失败，改用 dd"
            dd if=/dev/zero of="$SWAPFILE" bs=1M count="$SWAP_MB" status=none
        fi
    fi
    if [[ "$(blkid -o value -s TYPE "$SWAPFILE" 2>/dev/null || true)" != "swap" ]]; then
        chmod 600 "$SWAPFILE"
        mkswap "$SWAPFILE"
    fi
    swapon "$SWAPFILE"
fi
chmod 600 "$SWAPFILE"

if awk -v f="$SWAPFILE" '$1 == f && $3 == "swap" { found = 1 } END { exit !found }' /etc/fstab; then
    log "fstab 已包含交换文件，跳过"
else
    log "写入 fstab（开机自动挂载）"
    printf '%s none swap sw 0 0\n' "$SWAPFILE" >> /etc/fstab
fi

# 这档内存建议 swappiness=30：冷页早点换出，又不至于把热模型页换走。
log "设置 vm.swappiness=$SWAPPINESS"
printf 'vm.swappiness=%s\n' "$SWAPPINESS" > /etc/sysctl.d/99-swap.conf
sysctl --system >/dev/null

# ---------------------------------------------------------------- 5. 防火墙
log "放行 22/80/443 并启用 ufw（云厂商安全组需另行放行同样端口）"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ---------------------------------------------------------------- 6. 自检
log "自检"
swapon --show
free -h
ufw status | head -n 10

cat <<'NEXT'

完成。接下来按 deploy/README.md 继续：
  1) 把代码与数据放到 APP_DIR（仓库 clone + rsync 不进 git 的 data/）
  2) cp deploy/env/2c2g.env.example "$APP_DIR/.env" 并替换占位符
  3) 安装 deploy/systemd/lolita-agents.service 并替换占位符
  4) 安装 nginx 配置、签证书、生成 basic auth 口令
  5) 按 README 的验证步骤检查 RSS / swap / 并发第二条请求
NEXT
