#!/usr/bin/env bash
# 端到端调用示例。前置：docker compose up -d 已启动。
# 用法: BASE=http://localhost:8000 bash examples/demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-LIVE-001","title":"仲夏之夜现场录音"}'; echo

say "2) 导入播放（此时还没有协议 => 之后会进待处理）"
# 100 分，权利人 A 62.5% / B 30% / C 7.5%（协议后补后：62/30，余 1 按小数部分给 C）
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo

say "3) 同编号同内容重复导入 => 幂等 duplicate"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo

say "4) 同编号异内容 => 409 拒绝"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":200}'; echo

say "5) 登记权利人（协议登记也会自动建档，此处显式登记可选）"
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RS-A","name":"甲唱片公司"}'; echo
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RS-B","name":"乙艺人"}'; echo
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RS-C","name":"丙词曲方"}'; echo

say "6) 登记分成协议（合计 100%，适用 2026 全年）"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[
    {"holder_code":"RS-A","percent":"62.5"},
    {"holder_code":"RS-B","percent":"30"},
    {"holder_code":"RS-C","percent":"7.5"}
  ]
}'; echo

say "7) 再登记一个与上者区间重叠的协议 => 409"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2026-03-01T00:00:00+08:00",
  "valid_to":"2026-05-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"100"}]
}'; echo

say "8) 比例合计不是 100% => 422"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"99"}]
}'; echo

say "9) 登记第二段相邻协议（半开区间，边界相接允许）"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"100"}]
}'; echo

say "10) 更正播放：引用原编号、给新编号（金额 100 -> 80，链尾变更）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R1","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":80,"corrects_id":"PLAY-1001"}'; echo

say "11) 再有人更正同一条原记录 => 分叉，409"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-RX","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":90,"corrects_id":"PLAY-1001"}'; echo

say "12) 查看更正链（原记录留痕，仅链尾参与核算）"
$C "$BASE/plays/PLAY-1001/chain" | python3 -m json.tool; echo

say "13) 试算 2026 年 2 月期间（80 分：50/24/6）"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" | python3 -m json.tool; echo

say "14) 出具初次单（50/24/6，无待处理，因为协议已后补）"
INIT=$($C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}')
echo "$INIT" | python3 -m json.tool
INIT_ID=$(echo "$INIT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["batch_id"])')

say "14a) 初次单逐笔归因：按更正链原始编号 PLAY-1001 归集，含链尾/作品/时间/协议/比例/逐笔分摊"
$C "$BASE/statements/$INIT_ID/attributions?page=1&page_size=20" | python3 -m json.tool

say "14b) 只看权利人 RS-A 的归因；attributed_total 与原明细行金额一致(reconciles)"
$C "$BASE/statements/$INIT_ID/attributions?holder_code=RS-A" | python3 -m json.tool

say "15) 再次更正（80 -> 30），再出具 => 差额追补单（可正可负）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R2","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":30,"corrects_id":"PLAY-1001-R1"}' >/dev/null
SUP=$($C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}')
echo "$SUP" | python3 -m json.tool
SUP_ID=$(echo "$SUP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["batch_id"])')

say "15a) 追补单逐笔归因：removed(撤销旧链尾 80 的分摊，负数保留) 与 added(新链尾 30 的分摊) 分列"
$C "$BASE/statements/$SUP_ID/attributions" | python3 -m json.tool

say "15b) 历史不被日后数据改写：初次单归因仍指向旧链尾 PLAY-1001-R1(80)"
$C "$BASE/statements/$INIT_ID/attributions?holder_code=RS-A" | python3 -m json.tool

say "15c) 错误用法：不存在的单据 404；页码非法 422；权利人不存在 404"
$C -i "$BASE/statements/999999/attributions" | head -1
$C -i "$BASE/statements/$INIT_ID/attributions?page=0" | head -1
$C -i "$BASE/statements/$INIT_ID/attributions?holder_code=NOBODY" | head -1

say "16) 数据无变化再出具 => 不产生新单据（也不重复生成归因）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' | python3 -m json.tool

say "17) 列出该期间全部单据（1 张初次 + 1 张追补）"
$C "$BASE/statements?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" | python3 -m json.tool; echo

say "18) 待处理清单：导入另一作品的播放（无协议）并对 3 月出具"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{"code":"W-LIVE-002","title":"安可集"}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-2001","work_code":"W-LIVE-002","occurred_at":"2026-03-05T21:00:00+08:00","amount":500}'; echo
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-03-01T00:00:00+08:00","end":"2026-04-01T00:00:00+08:00"}' | python3 -m json.tool; echo

say "19) 财务付款核对：RS-A 单据累计（初次 + 负追补）与当前余额"
$C "$BASE/rightsholders/RS-A/ledger" | python3 -m json.tool

say "20) 登记一笔已完成付款（权利人编号 + 外部流水号 + 正整数分额，不超过正余额）"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}' | python3 -m json.tool

say "21) 同流水号同内容重试 => 幂等返回原记录(status=duplicate, HTTP 200)，不重复入账"
$C -i -X POST "$BASE/payments" -H 'Content-Type:application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}' | head -1
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}' | python3 -m json.tool

say "22) 同流水号异内容 => 409 payment_ref_conflict；超额/余额非正 => 409 payment_exceeds_balance"
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":10}' | head -1
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"BANK-20260301-0001","amount":19}' | head -1
# RS-A 此刻余额为 0（19 已付清），再付 1 分被拒且不留付款
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0002","amount":1}' | head -1
# 权利人不存在 404；金额非正整数 422
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"NOBODY","external_ref":"X","amount":1}' | head -1
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"X","amount":0}' | head -1

say "23) 付款后对账：付款流水已入，余额 0；历史付款不被负额追补撤销（负额留待后续抵扣）"
$C "$BASE/rightsholders/RS-A/ledger" | python3 -m json.tool

say "24) 银行回单核验准备：再登记一笔 RS-B 付款 9 分（回单将错记为 RS-C => 权利人不符）"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"BANK-20260302-0002","amount":9}' | python3 -m json.tool

say "25) 批次导入银行回单：匹配 / 权利人不符 / 付款未登记 各一行，导入即冻结第 1 版结论"
$C -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"RC-20260301-01",
  "receipts":[
    {"receipt_no":"RC-1","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19},
    {"receipt_no":"RC-2","external_ref":"BANK-20260302-0002","holder_code":"RS-C","amount":9},
    {"receipt_no":"RC-3","external_ref":"BANK-20260303-0003","holder_code":"RS-C","amount":2}
  ]
}' | python3 -m json.tool

say "26) 同批次同内容重试 => 幂等返回原批次(status=duplicate, HTTP 200)"
$C -i -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"RC-20260301-01",
  "receipts":[
    {"receipt_no":"RC-3","external_ref":"BANK-20260303-0003","holder_code":"RS-C","amount":2},
    {"receipt_no":"RC-1","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19},
    {"receipt_no":"RC-2","external_ref":"BANK-20260302-0002","holder_code":"RS-C","amount":9}
  ]
}' | head -1

say "27) 批次号/回单编号异内容复用 => 409；批内重复流水号、无效字段 => 422（均整批拒绝）"
$C -i -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-01","receipts":[{"receipt_no":"RC-9","external_ref":"BANK-X","holder_code":"RS-A","amount":1}]}' | head -1
$C -i -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-09","receipts":[{"receipt_no":"RC-1","external_ref":"BANK-X","holder_code":"RS-A","amount":1}]}' | head -1
$C -i -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-10","receipts":[{"receipt_no":"RA","external_ref":"BANK-Y","holder_code":"RS-A","amount":1},{"receipt_no":"RB","external_ref":"BANK-Y","holder_code":"RS-A","amount":2}]}' | head -1
$C -i -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-11","receipts":[{"receipt_no":"RA","external_ref":"BANK-Z","holder_code":"RS-A","amount":0}]}' | head -1

say "28) 付款迟到：补登 RS-C 2 分后复核 => 结论变化生成第 2 版；再复核结论不变不新增版本"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-C","external_ref":"BANK-20260303-0003","amount":2}' | python3 -m json.tool
$C -X POST "$BASE/receipts/RC-20260301-01/reverify" | python3 -m json.tool
$C -X POST "$BASE/receipts/RC-20260301-01/reverify" | python3 -m json.tool

say "29) 旧版本与原始回单始终可查：版本列表、第 1 版冻结结论、原始回单行"
$C "$BASE/receipts/RC-20260301-01/versions" | python3 -m json.tool
$C "$BASE/receipts/RC-20260301-01/versions/1" | python3 -m json.tool
$C "$BASE/receipts/RC-20260301-01" | python3 -m json.tool

say "30) 跨批次同一流水号：后续批次同号回单标为重复回单，不参与核验"
$C -X POST "$BASE/receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"RC-20260301-02",
  "receipts":[
    {"receipt_no":"RC-4","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19}
  ]
}' | python3 -m json.tool

say "31) 银行退票：凭原付款流水号 + 退回流水号 + 正整数分额登记（原付款/单据不变）"
# RS-B 已付 9 分（BANK-20260302-0002），银行先退回 4 分 => 余额加回为 4
$C -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-20260302-0002",
  "refund_ref":"BANK-RET-20260305-0001",
  "amount":4
}' | python3 -m json.tool

say "32) 同退回流水号同内容重试 => 幂等原记录(status=duplicate, HTTP 200)；异内容 => 409"
$C -i -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-20260302-0002",
  "refund_ref":"BANK-RET-20260305-0001",
  "amount":4
}' | head -1
# 金额不同 => 409 refund_ref_conflict（回显原原付款流水号/金额）
$C -i -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-20260302-0002",
  "refund_ref":"BANK-RET-20260305-0001",
  "amount":3
}' | head -1

say "33) 原付款不存在 404；金额非正整数 422；累计退回超过原付款 409 且不留记录"
$C -i -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-NOT-EXIST","refund_ref":"X","amount":1}' | head -1
$C -i -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-20260302-0002","refund_ref":"X","amount":0}' | head -1
# 已退 4，再退 6 => 累计 10 > 原付款 9 => 409 refund_exceeds_payment
$C -i -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-20260302-0002",
  "refund_ref":"BANK-RET-20260305-0002",
  "amount":6
}' | head -1

say "34) 再退 5 分恰好退清（累计 9 = 原付款 9），余额加回至 9"
$C -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "payment_ref":"BANK-20260302-0002",
  "refund_ref":"BANK-RET-20260305-0003",
  "amount":5
}' | python3 -m json.tool

say "35) 对账单：保留付款总额，另列退回总额 refunded_total、逐笔退回 refunds[] 与余额"
$C "$BASE/rightsholders/RS-B/ledger" | python3 -m json.tool

say "36) 退回加回的余额可供后续付款（仍只允许在正余额内登记，超额拒绝）"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"BANK-20260310-0010","amount":8}' \
  | python3 -m json.tool
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"BANK-20260310-0011","amount":2}' | head -1

say "37) 回单核验联动：匹配仍按原付款字段；复核另冻结累计退回额，退回额变化才出新版本"
# RC-20260301-01 的 R2 对 BANK-20260302-0002（RS-B 9 分）原判权利人不符；
# 该付款现已累计退回 9 分。复核 => 结论不变但冻结退回额 0->9，生成新版本；
# 再复核 => status=unchanged，不新增版本；旧版冻结值不改写。
$C -X POST "$BASE/receipts/RC-20260301-01/reverify" | python3 -m json.tool
$C -X POST "$BASE/receipts/RC-20260301-01/reverify" | python3 -m json.tool
$C "$BASE/receipts/RC-20260301-01/versions/1" | python3 -m json.tool

# ---- 付款额度预留指令（先预留、后确认付款；独立演示作品/权利人）----

say "38) 预留演示建档：作品 W-RESV-001 + 权利人 RS-R（100%），出单 100 分"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-RESV-001","title":"预留演示录音"}' | python3 -m json.tool
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
    "work_code":"W-RESV-001",
    "valid_from":"2026-01-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "shares":[{"holder_code":"RS-R","percent":"100"}]}' | python3 -m json.tool
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"RESV-PLAY-1","work_code":"W-RESV-001",
       "occurred_at":"2026-02-20T20:00:00+08:00","amount":100}' | python3 -m json.tool
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool

say "39) 预留额度：凭权利人编号 + 唯一指令号 + 正整数分额；仅余额扣除全部待确认预留后仍足额才创建"
# 预留 60 => 余额 100、待确认预留 60、可用额 40，指令 pending
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0001","amount":60}' \
  | python3 -m json.tool
# 再预留 41 > 可用额 40 => 409 payment_exceeds_balance（detail 含 balance/reserved_total/available）
$C -i -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0002","amount":41}' | head -1
# 待确认预留占用期间，新付款必须避开预留：直接付款 1 分也 409
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","external_ref":"BANK-RESV-DIRECT-1","amount":1}' | head -1
# 同号同内容重试 => 200 status=duplicate 返回原指令；同号异内容 => 409 reservation_ref_conflict
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0001","amount":60}' | python3 -m json.tool
$C -i -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0001","amount":61}' | head -1
# 再预留恰好 40（可用额归 0）
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0003","amount":40}' | python3 -m json.tool

say "40) 取消待确认指令：额度释放（重复取消幂等；已确认指令不可取消）"
$C -X POST "$BASE/payment-reservations/RESV-CMD-0003/cancel" | python3 -m json.tool
$C -X POST "$BASE/payment-reservations/RESV-CMD-0003/cancel" | python3 -m json.tool

say "41) 负额追补 FIFO：更早创建的待确认指令优先受保护"
# 当前：余额 100、待确认 RESV-CMD-0001=60（更早）。再预留 30（更晚，可用 10）。
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0004","amount":30}' | python3 -m json.tool
# 播放 100 -> 50（负额追补 -50）：余额变 50；更早 60 + 本指令 30 = 90 > 50，
# 更晚指令即使自身 30 <= 50 也拒绝（指令保持 pending，不留付款）
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"RESV-PLAY-1-R1","work_code":"W-RESV-001",
       "occurred_at":"2026-02-20T20:00:00+08:00","amount":50,
       "corrects_id":"RESV-PLAY-1"}' | python3 -m json.tool
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool
$C -i -X POST "$BASE/payment-reservations/RESV-CMD-0004/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-RESV-0004"}' | head -1
# 更早指令 60 也 > 余额 50 => 同样拒绝且指令不变
$C -i -X POST "$BASE/payment-reservations/RESV-CMD-0001/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-RESV-0001"}' | head -1
# 播放恢复 50 -> 100（余额回到 100）：按 FIFO 先确认更早的 60，再确认 30
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"RESV-PLAY-1-R2","work_code":"W-RESV-001",
       "occurred_at":"2026-02-20T20:00:00+08:00","amount":100,
       "corrects_id":"RESV-PLAY-1-R1"}' | python3 -m json.tool
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool
$C -X POST "$BASE/payment-reservations/RESV-CMD-0001/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-RESV-0001"}' \
  | python3 -m json.tool
$C -X POST "$BASE/payment-reservations/RESV-CMD-0004/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-RESV-0004"}' \
  | python3 -m json.tool

say "42) 确认幂等与流水号占用：同流水号重试返回原付款(200)；异流水号/被占用流水号 409"
$C -X POST "$BASE/payment-reservations/RESV-CMD-0001/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-RESV-0001"}' \
  | python3 -m json.tool
$C -i -X POST "$BASE/payment-reservations/RESV-CMD-0001/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-RESV-OTHER"}' | head -1
# 已确认指令不可取消 => 409 reservation_not_pending
$C -i -X POST "$BASE/payment-reservations/RESV-CMD-0001/cancel" | head -1
# 新建一笔 10 分预留，用已被占用的流水号确认 => 409 payment_ref_conflict，指令保持 pending
$C -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-R","instruction_ref":"RESV-CMD-0005","amount":10}' \
  | python3 -m json.tool
$C -i -X POST "$BASE/payment-reservations/RESV-CMD-0005/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-RESV-0001"}' | head -1
$C -X POST "$BASE/payment-reservations/RESV-CMD-0005/confirm" \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-RESV-0005"}' \
  | python3 -m json.tool

say "43) 权利人对账：待确认预留 reserved_total、可用额 available_balance 与指令状态 instructions[]"
$C "$BASE/rightsholders/RS-R/ledger" | python3 -m json.tool
$C "$BASE/payment-reservations/RESV-CMD-0001" | python3 -m json.tool

say "完成"
