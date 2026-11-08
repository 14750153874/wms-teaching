# V4 电商作业闭环自测：波次拣货 → 复核 → 打包 → 发货 → 售后四类型 + 质检
$ErrorActionPreference = 'Stop'
$Base = 'http://127.0.0.1:8010'
function Call($method, $path, $body, $token, $idem) {
  $headers = @{}
  if ($token) { $headers['Authorization'] = "Bearer $token" }
  if ($idem) { $headers['X-Idempotency-Key'] = $idem }
  $args = @{ Method = $method; Uri = "$Base$path"; Headers = $headers; TimeoutSec = 10 }
  if ($null -ne $body) { $args['Body'] = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 8)); $args['ContentType'] = 'application/json; charset=utf-8' }
  try { return (Invoke-RestMethod @args) } catch {
    $detail = $_.ErrorDetails.Message
    if ($detail) { $d = $detail | ConvertFrom-Json; return [pscustomobject]@{ code = $d.code; message = $d.message; data = $null } }
    throw
  }
}
function Line($label, $obj) {
  $json = if ($null -eq $obj) { 'null' } else { $obj | ConvertTo-Json -Depth 3 -Compress }
  if ($json.Length -gt 260) { $json = $json.Substring(0, 260) + '...' }
  Write-Output ("[{0}] {1}" -f $label, $json)
}
function Ok($label, $resp) {
  if ($resp.code -ne 200) { Write-Output ("[{0}] FAIL code={1} msg={2}" -f $label, $resp.code, $resp.message); return $false }
  return $true
}

$t = (Call 'POST' '/api/v1/auth/login' @{ username = 'admin'; password = '123456' }).data.accessToken
Write-Output "TOKEN_OK=$([bool]$t)"

# --- SPU / SKU ---
$spus = (Call 'GET' '/api/v1/spus' $null $t).data.list
$multi = $spus | Where-Object { $_.skus.Count -gt 1 } | Select-Object -First 1
Write-Output ("SPU_COUNT={0} 多规格示例={1}（{2} 个SKU）" -f $spus.Count, $multi.spu_name, $multi.skus.Count)
$multi.skus | ForEach-Object { Write-Output ("   - {0} {1} {2} 库存 {3}" -f $_.goods_code, $_.goods_name, $_.spec_attrs, $_.stock_qty) }

# --- 造 2 张待发货订单 ---
$goods = (Call 'GET' '/api/v1/shop/goods?shopId=1' $null $t).data.list | Where-Object { $_.sale_status -eq 1 -and $_.saleable -gt 10 }
$g1 = $goods[0]; $g2 = $goods[1]
$ids = @()
foreach ($i in 1..2) {
  $o = Call 'POST' '/api/v1/shop/orders' @{ shop_id = 1; buyer_name = "波次测试$i"; receiver_name = "测试$i"; receiver_phone = '13800000000'; receiver_address = "测试地址$i"; items = @(@{ goods_id = $g1.goods_id; qty = 2 }, @{ goods_id = $g2.goods_id; qty = 1 }) } $t
  if (-not (Ok "create-order$i" $o)) { exit 1 }
  $p = Call 'POST' "/api/v1/shop/orders/$($o.data.id)/pay" @{} $t "wave-test-$i"
  if (-not (Ok "pay-order$i" $p)) { exit 1 }
  $ids += $o.data.id
  Write-Output ("ORDER{0}={1} payAmount={2} status={3}" -f $i, $o.data.orderNo, $o.data.payAmount, $p.data.status)
}

# --- 生成波次 ---
$wave = Call 'POST' '/api/v1/ec/waves' @{ order_ids = $ids } $t
if (-not (Ok 'wave-create' $wave)) { exit 1 }
Line 'wave-create' $wave.data
$wid = $wave.data.id

$detail = (Call 'GET' "/api/v1/ec/waves/$wid" $null $t).data
Write-Output ("WAVE_STATUS={0} TASKS={1} ORDERS={2}" -f $detail.statusText, $detail.tasks.Count, $detail.orders.Count)
$detail.tasks | ForEach-Object { Write-Output ("   拣货路径: {0} {1} x{2} ({3})" -f $_.loc_code, $_.goods_code, $_.qty, $_.order_no) }

# --- 短拣 → 复核必须失败 ---
$first = $detail.tasks[0]
Line 'pick-short' (Call 'POST' "/api/v1/ec/waves/$wid/pick" @{ task_id = $first.id; picked_qty = 1 } $t).data
$rev1 = Call 'POST' "/api/v1/ec/waves/$wid/review" @{ order_id = $ids[0] } $t
Write-Output ("REVIEW_WITH_SHORT => code={0} msg={1}" -f $rev1.code, $rev1.message)

# --- 整波拣完 ---
Line 'pick-all' (Call 'POST' "/api/v1/ec/waves/$wid/pick" @{ all = $true } $t).data
Line 'review-1' (Call 'POST' "/api/v1/ec/waves/$wid/review" @{ order_id = $ids[0] } $t)
Line 'review-2' (Call 'POST' "/api/v1/ec/waves/$wid/review" @{ order_id = $ids[1] } $t)
$waveAfter = (Call 'GET' "/api/v1/ec/waves/$wid" $null $t).data
Write-Output ("WAVE_AFTER_REVIEW={0}" -f $waveAfter.statusText)

# --- 打包 + 发货 ---
Line 'pack-1' (Call 'POST' "/api/v1/ec/orders/$($ids[0])/pack" @{ weight_g = 850; remark = '气泡膜加固' } $t)
Line 'pack-2' (Call 'POST' "/api/v1/ec/orders/$($ids[1])/pack" @{ weight_g = 620 } $t)
$early = Call 'POST' "/api/v1/ec/orders/$($ids[1])/ship" @{ carrier = '中通快递'; tracking_no = 'ZT7000000002' } $t
Line 'ship-1' (Call 'POST' "/api/v1/ec/orders/$($ids[0])/ship" @{ carrier = '顺丰速运'; tracking_no = 'SF7000000001' } $t)
Line 'ship-2' (Call 'POST' "/api/v1/ec/orders/$($ids[1])/ship" @{ carrier = '中通快递'; tracking_no = 'ZT7000000002' } $t)
$waveDone = (Call 'GET' "/api/v1/ec/waves/$wid" $null $t).data
Write-Output ("WAVE_DONE={0}" -f $waveDone.statusText)
$o1 = (Call 'GET' "/api/v1/ec/orders/$($ids[0])" $null $t).data
Write-Output ("ORDER1_STATUS={0} PACKAGE={1} SHIPMENT={2}" -f $o1.statusText, $o1.package.package_no, $o1.shipment.tracking_no)

# --- 售后：退货退款 + 质检分良品/残次 ---
$as1 = Call 'POST' '/api/v1/ec/after-sales' @{ order_id = $ids[0]; goods_id = $g1.goods_id; qty = 2; type = 1; reason = '尺寸不符' } $t
Line 'as1-create' $as1.data
Line 'as1-approve' (Call 'POST' "/api/v1/ec/after-sales/$($as1.data.id)/approve" @{} $t).data
$bad = Call 'POST' "/api/v1/ec/after-sales/$($as1.data.id)/receive" @{ good_qty = 1; bad_qty = 0 } $t
Write-Output ("AS1_BAD_QTY_CHECK => code={0} msg={1}" -f $bad.code, $bad.message)
Line 'as1-receive' (Call 'POST' "/api/v1/ec/after-sales/$($as1.data.id)/receive" @{ good_qty = 1; bad_qty = 1 } $t).data

# --- 售后：仅退款 ---
$as2 = Call 'POST' '/api/v1/ec/after-sales' @{ order_id = $ids[0]; goods_id = $g2.goods_id; qty = 1; type = 2; reason = '未收到货' } $t
Line 'as2-create' $as2.data
Line 'as2-approve' (Call 'POST' "/api/v1/ec/after-sales/$($as2.data.id)/approve" @{} $t).data

# --- 售后：补发（自动生成补发单）---
$as4 = Call 'POST' '/api/v1/ec/after-sales' @{ order_id = $ids[1]; goods_id = $g1.goods_id; qty = 2; type = 4; reason = '漏发' } $t
Line 'as4-create' $as4.data
$ap4 = Call 'POST' "/api/v1/ec/after-sales/$($as4.data.id)/approve" @{} $t
Line 'as4-approve(自动建补发单)' $ap4.data

# --- 售后：换货（自动生成换货单，等原件退回质检）---
$as3 = Call 'POST' '/api/v1/ec/after-sales' @{ order_id = $ids[1]; goods_id = $g2.goods_id; qty = 1; type = 3; reason = '颜色发错' } $t
Line 'as3-create' $as3.data
$ap3 = Call 'POST' "/api/v1/ec/after-sales/$($as3.data.id)/approve" @{} $t
Line 'as3-approve(自动建换货单)' $ap3.data
Line 'as3-receive' (Call 'POST' "/api/v1/ec/after-sales/$($as3.data.id)/receive" @{ good_qty = 1; bad_qty = 0 } $t).data

# --- 一致性 & 列表 ---
$checks = (Call 'GET' '/api/v1/ops/checks' $null $t).data
Write-Output "CHECKS_ALL_PASSED=$($checks.allPassed)"
$checks.checks | ForEach-Object { Write-Output ("  - {0}: {1} (bad={2})" -f $_.name, $_.level, $_.badRows) }
Line 'after-sales' ((Call 'GET' '/api/v1/ec/after-sales' $null $t).data.list | ForEach-Object { "{0} {1} {2} {3} 良{4}残{5} ref={6}" -f $_.after_no, $_.typeText, $_.statusText, $_.goods_name, $_.good_qty, $_.bad_qty, $_.ref_order_no })
Line 'waves' ((Call 'GET' '/api/v1/ec/waves' $null $t).data.list | ForEach-Object { "{0} {1} orders={2} qty={3}" -f $_.wave_no, $_.statusText, $_.order_count, $_.total_qty })
Line 'ec-orders' ((Call 'GET' '/api/v1/ec/orders' $null $t).data.list | ForEach-Object { "{0} {1} {2}" -f $_.order_no, $_.statusText, $_.kindText })
Line 'inventory' ((Call 'GET' '/api/v1/inventory?size=4' $null $t).data.list | ForEach-Object { "{0} {1} 实物{2} 可用{3}" -f $_.loc_code, $_.goods_code, $_.quantity, $_.available_qty })
Line 'ops-metrics' (Call 'GET' '/api/v1/ops/metrics' $null $t).data
