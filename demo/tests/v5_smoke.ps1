# V5 自测：成本(移动加权) / 盘点 / 调拨 / 批处理 / 报表（服务跑在 8010）
$ErrorActionPreference = 'Stop'
$Base = 'http://127.0.0.1:8010'
function Call($method, $path, $token, $body, $idem) {
  $h = @{}
  if ($token) { $h['Authorization'] = "Bearer $token" }
  if ($idem) { $h['X-Idempotency-Key'] = $idem }
  $args = @{ Method = $method; Uri = "$Base$path"; Headers = $h; TimeoutSec = 15 }
  if ($null -ne $body) { $args['Body'] = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 8)); $args['ContentType'] = 'application/json; charset=utf-8' }
  try { return (Invoke-RestMethod @args) } catch {
    $d = $_.ErrorDetails.Message
    if ($d) { $d = $d | ConvertFrom-Json; return [pscustomobject]@{ code = $d.code; message = $d.message } }
    throw
  }
}
function Check($label, $cond, $extra) {
  $ok = if ($cond) { 'PASS' } else { 'FAIL' }
  Write-Output ("[{0}] {1} {2}" -f $ok, $label, $extra)
  if (-not $cond) { $script:failed++ }
}
$failed = 0
$t = (Call 'POST' '/api/v1/auth/login' $null @{ username = 'admin'; password = '123456' }).data.accessToken

# ---------- 1. 成本：入库更新移动加权 ----------
$goods = (Call 'GET' '/api/v1/goods?size=200' $t).data.list
$g = $goods | Where-Object { $_.goods_code -eq 'SP003' } | Select-Object -First 1
$before = [double]$g.avg_cost
Check '初始均价 = 成本价' ($before -eq [double]$g.cost_price) ("avg=$before cost=" + $g.cost_price)
$loc = ((Call 'GET' '/api/v1/locations' $t).data.leafList | Where-Object { $_.loc_code -eq 'A01-02' } | Select-Object -First 1)
$in = Call 'POST' '/api/v1/in-orders' $t @{ order_type = 1; partner_id = 1; wh_id = 1; remark = 'V5成本测试';
  lines = @(@{ goods_id = $g.id; loc_id = $loc.id; plan_num = 100; batch_no = 'BV5A'; expire_date = '2027-06-30' }) }
Check '建入库单' ($in.code -eq 200) ("orderNo=" + $in.data.orderNo)
Call 'POST' ("/api/v1/in-orders/" + $in.data.id + "/audit") $t @{} | Out-Null
$conf = Call 'POST' ("/api/v1/in-orders/" + $in.data.id + "/confirm") $t @{} ('v5-in-' + $in.data.id)
Check '入库过账' ($conf.code -eq 200) ("total=" + $conf.data.totalActualNum)
$g2 = ((Call 'GET' '/api/v1/goods?size=200' $t).data.list | Where-Object { $_.id -eq $g.id } | Select-Object -First 1)
Check '入库后均价不变（同价入库）' ([math]::Abs([double]$g2.avg_cost - $before) -lt 0.001) ("avg=" + $g2.avg_cost)
$txn = (Call 'GET' ("/api/v1/inventory/txn?bizType=1&size=5") $t).data.list | Select-Object -First 1
Check '入库流水带成本' ([double]$txn.unit_cost -gt 0 -and [double]$txn.cost_amount -gt 0) ("unit=" + $txn.unit_cost + " amount=" + $txn.cost_amount)

# ---------- 2. 盘点：全盘 → 录实盘 → 提交 → 审核入账 ----------
$st = Call 'POST' '/api/v1/stocktakes' $t @{ wh_id = 1; stocktake_type = 1; remark = 'V5全盘测试' }
Check '建盘点单' ($st.code -eq 200) ("orderNo=" + $st.data.orderNo + " plan=" + $st.data.planCount)
$detail = (Call 'GET' ("/api/v1/stocktakes/" + $st.data.id) $t).data
Check '盘点明细有账面数' ($detail.lines.Count -gt 0) ("lines=" + $detail.lines.Count)
# 造两个差异：第一行 +5（盘盈），第二行 -3（盘亏）
$l1 = $detail.lines[0]; $l2 = $detail.lines[1]
$lines = @(
  @{ detail_id = $l1.id; actual_qty = [double]$l1.book_qty + 5; diff_reason = '多出散件' },
  @{ detail_id = $l2.id; actual_qty = [double]$l2.book_qty - 3; diff_reason = '破损' }
)
$save = Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/save") $t @{ lines = $lines }
Check '录入实盘数' ($save.code -eq 200) ("saved=" + $save.data.saved)
$subEarly = Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/submit") $t @{}
Check '未盘完不能提交' ($subEarly.code -eq 40001) ("code=" + $subEarly.code + " msg=" + $subEarly.message)
# 其余行按账面数录入
$rest = @()
foreach ($l in $detail.lines) { if ($l.id -ne $l1.id -and $l.id -ne $l2.id) { $rest += @{ detail_id = $l.id; actual_qty = [double]$l.book_qty } } }
Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/save") $t @{ lines = $rest } | Out-Null
$sub = Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/submit") $t @{}
Check '提交待审' ($sub.code -eq 200) ("status=" + $sub.data.status)
$aud = Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/audit") $t @{}
Check '盘点审核过账' ($aud.code -eq 200) ("posted=" + $aud.data.postedLines)
$audAgain = Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/audit") $t @{}
Check '重复审核幂等' ($audAgain.data.duplicated -eq $true) ("duplicated=" + $audAgain.data.duplicated)
$invAfter = (Call 'GET' ("/api/v1/inventory?size=200&keyword=" + $l1.loc_code) $t).data.list |
  Where-Object { $_.goods_code -eq $l1.goods_code } | Select-Object -First 1
$expect = [double]$l1.book_qty + 5
Check '盘盈已入账' ([math]::Abs([double]$invAfter.quantity - $expect) -lt 0.001) ("now=" + $invAfter.quantity + " expect=" + $expect)
$txnGain = (Call 'GET' '/api/v1/inventory/txn?bizType=8&size=5' $t).data.list
$txnLoss = (Call 'GET' '/api/v1/inventory/txn?bizType=9&size=5' $t).data.list
Check '生成盘盈/盘亏流水' ($txnGain.Count -ge 1 -and $txnLoss.Count -ge 1) ("gain=" + $txnGain.Count + " loss=" + $txnLoss.Count)

# ---------- 3. 调拨：建单 → 审核预占 → 确认(两条流水) → 幂等 ----------
$from = (Call 'GET' '/api/v1/locations' $t).data.leafList | Where-Object { $_.loc_code -eq 'A01-01' } | Select-Object -First 1
$to = (Call 'GET' '/api/v1/locations' $t).data.leafList | Where-Object { $_.loc_code -eq 'C01-01' } | Select-Object -First 1
$tf = Call 'POST' '/api/v1/transfers' $t @{ goods_id = $l1.goods_id; from_loc_id = $from.id; to_loc_id = $to.id; qty = 7; batch_no = $l1.batch_no }
Check '建调拨单' ($tf.code -eq 200) ("orderNo=" + $tf.data.orderNo)
$tooMuch = Call 'POST' '/api/v1/transfers' $t @{ goods_id = $l1.goods_id; from_loc_id = $from.id; to_loc_id = $to.id; qty = 999999 }
Check '超量调拨被拒' ($tooMuch.code -eq 50201) ("code=" + $tooMuch.code)
$sameLoc = Call 'POST' '/api/v1/transfers' $t @{ goods_id = $l1.goods_id; from_loc_id = $from.id; to_loc_id = $from.id; qty = 1 }
Check '同库位调拨被拒' ($sameLoc.code -eq 40000) ("code=" + $sameLoc.code)
$tfAudit = Call 'POST' ("/api/v1/transfers/" + $tf.data.id + "/audit") $t @{}
Check '调拨审核(预占)' ($tfAudit.code -eq 200) ("status=" + $tfAudit.data.status)
$invLocked = (Call 'GET' ("/api/v1/inventory?size=200&keyword=A01-01") $t).data.list |
  Where-Object { $_.goods_code -eq $l1.goods_code -and $_.batch_no -eq $l1.batch_no } | Select-Object -First 1
Check '调出已预占' ([double]$invLocked.locked_qty -ge 7) ("locked=" + $invLocked.locked_qty)
$ok = (Call 'GET' '/api/v1/ops/checks' $t).data
Check '调拨预占后一致性仍通过' ($ok.allPassed -eq $true) ("checks=" + $ok.checks.Count)
$tfConf = Call 'POST' ("/api/v1/transfers/" + $tf.data.id + "/confirm") $t @{} ('tf-' + $tf.data.id)
Check '确认调拨' ($tfConf.code -eq 200) ("moved=" + $tfConf.data.movedQty)
$tfAgain = Call 'POST' ("/api/v1/transfers/" + $tf.data.id + "/confirm") $t @{} ('tf-' + $tf.data.id)
Check '重复确认幂等' ($tfAgain.data.duplicated -eq $true) ("duplicated=" + $tfAgain.data.duplicated)
$toInv = (Call 'GET' "/api/v1/inventory?size=200&keyword=C01-01" $t).data.list |
  Where-Object { $_.goods_code -eq $l1.goods_code } | Select-Object -First 1
Check '调入库位已增加' ($toInv -and [double]$toInv.quantity -ge 7) ("C01-01 qty=" + $(if ($toInv) { $toInv.quantity } else { 'none' }))
$txn6 = (Call 'GET' '/api/v1/inventory/txn?bizType=6&size=3' $t).data.list
$txn7 = (Call 'GET' '/api/v1/inventory/txn?bizType=7&size=3' $t).data.list
Check '调拨生成两条流水(6/7)' ($txn6.Count -ge 1 -and $txn7.Count -ge 1) ("out=" + $txn6.Count + " in=" + $txn7.Count)

# ---------- 4. 批处理 ----------
$jobs = Call 'POST' '/api/v1/ops/jobs/run' $t @{}
Check '批处理可执行' ($jobs.code -eq 200 -and $jobs.data.results.Count -ge 3) (($jobs.data.results | ForEach-Object { "$($_.job)=$($_.affected)" }) -join ', ')
$jobLog = Call 'GET' '/api/v1/ops/jobs' $t
Check '任务日志留痕' ($jobLog.data.list.Count -ge 3) ("logs=" + $jobLog.data.list.Count)

# ---------- 5. 报表 ----------
$today = Get-Date -Format 'yyyy-MM-dd'
$monthBegin = Get-Date -Format 'yyyy-MM-01'
$stock = Call 'GET' ("/api/v1/reports/stock-summary?begin=$monthBegin&end=$today") $t
Check '收发存汇总' ($stock.code -eq 200 -and $stock.data.list.Count -gt 0) ("rows=" + $stock.data.list.Count)
$profit = Call 'GET' ("/api/v1/reports/profit?begin=$monthBegin&end=$today") $t
Check '毛利报表' ($profit.code -eq 200) ("sales=" + $profit.data.totalSales + " cost=" + $profit.data.totalCost + " profit=" + $profit.data.totalProfit + " rate=" + $profit.data.profitRate)
$turn = Call 'GET' '/api/v1/reports/turnover?days=30' $t
Check '动销/周转报表' ($turn.code -eq 200 -and $turn.data.skuCount -gt 0) ("sku=" + $turn.data.skuCount + " active=" + $turn.data.activeSku + " rate=" + $turn.data.activeRate)
$exp = Call 'GET' '/api/v1/reports/expiring?days=30' $t
Check '临期预警报表' ($exp.code -eq 200) ("rows=" + $exp.data.list.Count + " expired=" + $exp.data.expiredCount)
Check '临期报表能抓到种子批次' ($exp.data.list.Count -ge 1) ("first=" + $(if ($exp.data.list.Count) { $exp.data.list[0].batch_no + ' ' + $exp.data.list[0].days_left + '天' } else { 'none' }))

# ---------- 6. 权限：盘点员可盘点但不可审核 ----------
$stk = (Call 'POST' '/api/v1/auth/login' $null @{ username = 'keeper01'; password = '123456' }).data.accessToken
$stList = Call 'GET' '/api/v1/stocktakes' $stk
Check '仓管员可见盘点' ($stList.code -eq 200) ("total=" + $stList.data.total)
$stAuditDeny = Call 'POST' ("/api/v1/stocktakes/" + $st.data.id + "/audit") $stk @{}
Check '仓管员审核盘点被拒' ($stAuditDeny.code -eq 40300 -or $stAuditDeny.code -eq 40001) ("code=" + $stAuditDeny.code + " msg=" + $stAuditDeny.message)

# ---------- 7. 最终一致性 ----------
$final = (Call 'GET' '/api/v1/ops/checks' $t).data
$final.checks | ForEach-Object { Write-Output ("  - {0}: {1} (bad={2})" -f $_.name, $_.level, $_.badRows) }
Check '最终一致性全部通过' ($final.allPassed -eq $true) ("checks=" + $final.checks.Count)

Write-Output ("`nRESULT: " + $(if ($failed -eq 0) { 'ALL PASS' } else { "$failed FAILED" }))

