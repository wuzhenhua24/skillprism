#!/usr/bin/env bash
# 检查 ticket.md 的结构：第一行是 TICKET-V1，三节按顺序出现，严重级别合法。
set -u
file=ticket.md
[ -f "$file" ] || { echo "ticket.md 不存在"; exit 1; }
[ "$(head -n 1 "$file" | tr -d '[:space:]')" = "TICKET-V1" ] || { echo "第一行不是 TICKET-V1"; exit 1; }

prev=0
for heading in "## Summary" "## Severity" "## Steps to Reproduce"; do
  line=$(grep -n -F -x "$heading" "$file" | head -n 1 | cut -d: -f1)
  [ -n "$line" ] || { echo "缺少 $heading"; exit 1; }
  [ "$line" -gt "$prev" ] || { echo "$heading 的位置不对"; exit 1; }
  prev=$line
done

severity=$(awk '/^## Severity/{getline; print; exit}' "$file" | tr -d '[:space:]')
case "$severity" in
  P0|P1|P2|P3) echo "结构正确，严重级别 $severity" ;;
  *) echo "严重级别不合法：$severity"; exit 1 ;;
esac
