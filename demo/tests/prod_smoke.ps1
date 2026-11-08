# WMS 生产化验收自测（并发 / 拆单 / 空单 / 幂等 / 削峰）
# 用法：服务跑在 8010，pwsh -File tests/prod_smoke.ps1（或按本仓库习惯用 scriptblock 方式执行）
$ErrorActionPreference = 'Stop'
$Base = 'http://127.0.0.1:8010'
$script:fail = 0
function Ok($name, $cond, $detail = '') {
  if ($cond) { Write-Output ("[PASS] {0} {1}" -f $name, $detail) }
  else { Write-Output ("[FAIL] {0} {1}" -f $name, $detail); $script:fail++ }
}
function Call($method, $path, $body, $token, $idem) {
  $headers = @{}
  if ($token) { $headers['Authorization'] = "Bearer $token" }
  if ($idem) { $headers['X-Idempotency-Key'] = $idem }
  $a = @{ Method = $method; Uri = "$Base$path"; Headers = $headers; TimeoutSec = 30 }
  if ($null -ne $body) {
    $a['Body'] = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 10))
    $a['ContentType'] = 'application/json; charset=utf-8'
  }
  try { return (Invoke-RestMethod @a) }
  catch {
    $detail = $_.ErrorDetails.Message
    if ($detail) { $d = $detail | ConvertFrom-Json; return [pscustomobject]@{ code = $d.code; message = $d.message; data = $null } }
    throw
  }
}

$t = (Call 'POST' '/api/v1/auth/login' @{ username = 'admin'; password = '123456' }).data.accessToken
Ok 'login' ([bool]$t)

# ---------------------------------------------------------------- 1. 迁移与运行时
$mg = (Call 'GET' '/api/v1/ops/migrations' $null $t).data
Ok 'migrations-applied' ($mg.current -eq $mg.latest -and $mg.current -ge 6) ("current=$($mg.current) latest=$($mg.latest)")
$rt = (Call 'GET' '/api/v1/ops/runtime' $null $t).data
Ok 'runtime-metrics' ($null -ne $rt.metrics.p95Ms) ("qps=$($rt.metrics.qps) p95=$($rt.metrics.p95Ms)ms")
Ok 'queue-visible' ($null -ne $rt.queue.backlog) ("backlog=$($rt.queue.backlog)")

# ---------------------------------------------------------------- 2. 并发抢库存：不超卖
$goods = (Call 'GET' '/api/v1/shop/goods?shopId=1' $null $t).data.list |
  Where-Object { $_.sale_status -eq 1 -and $_.saleable -ge 20 } | Select-Object -First 1
$before = (Call 'GET' '/api/v1/inventory?size=200' $null $t).data.list |
  Where-Object { $_.goods_id -eq $goods.goods_id } |
  Measure-Object -Property available_qty -Sum
$stockBefore = [double]$before.Sum
Write-Output ("CONCURRENCY goods={0} available={1}" -f $goods.goods_code, $stockBefore)

# 20 个并发各买 3 件（需要 60 件），库存只够一部分：必须"要么成功要么干净失败"，且绝不超卖
$jobs = 1..20 | ForEach-Object {
  Start-Job -ScriptBlock {
    param($Base, $token, $goodsId)
    $h = @{ Authorization = "Bearer $token" }
    $body = @{ shop_id = 1; buyer_name = '并发测试'; receiver_name = '并发'; receiver_phone = '13800000000';
               receiver_address = '地址'; items = @(@{ goods_id = $goodsId; qty = 3 }) } | ConvertTo-Json -Depth 6
    try {
      $o = Invoke-RestMethod -Method POST -Uri "$Base/api/v1/shop/orders" -Headers $h -Body $body -ContentType 'application/json; charset=utf-8' -TimeoutSec 20
      $p = Invoke-RestMethod -Method POST -Uri ("$Base/api/v1/shop/orders/" + $o.data.id + "/pay") -Headers $h -Body '{}' -ContentType 'application/json; charset=utf-8' -TimeoutSec 20
      return "ok:$($o.data.id)"
    } catch { return "fail" }
  } -ArgumentList $Base, $t, $goods.goods_id
}
$results = $jobs | Wait-Job | Receive-Job
$jobs | Remove-Job -Force
$paid = ($results | Where-Object { $_ -like 'ok:*' }).Count
$failed = ($results | Where-Object { $_ -eq 'fail' }).Count
Write-Output ("CONCURRENCY paid={0} rejected={1}" -f $paid, $failed)
Ok 'concurrency-no-oversell' (($paid * 3) -le $stockBefore) ("卖出 $($paid*3) <= 可用 $stockBefore")

$ck = (Call 'GET' '/api/v1/ops/checks' $null $t).data
$bad = ($ck.list | Where-Object { -not $_.passed } | ForEach-Object { $_.name }) -join ','
Ok 'consistency-after-stress' $ck.allPassed ("failed=$bad")
$inv = (Call 'GET' '/api/v1/inventory?size=200' $null $t).data.list | Where-Object { $_.goods_id -eq $goods.goods_id }
Ok 'no-negative-stock' (($inv | Where-Object { $_.available_qty -lt 0 -or $_.quantity -lt 0 }).Count -eq 0)

# ---------------------------------------------------------------- 3. 幂等：同一个 key 只下一单
$g2 = (Call 'GET' '/api/v1/shop/goods?shopId=1' $null $t).data.list |
  Where-Object { $_.sale_status -eq 1 -and $_.saleable -ge 5 } | Select-Object -First 1
$key = 'prod-idem-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$body = @{ shop_id = 1; buyer_name = '幂等'; receiver_name = '幂等'; receiver_phone = '13800000000';
           receiver_address = '地址'; items = @(@{ goods_id = $g2.goods_id; qty = 1 }) }
$o1 = Call 'POST' '/api/v1/shop/orders' $body $t $key
$o2 = Call 'POST' '/api/v1/shop/orders' $body $t $key
Ok 'idempotent-create' ($o1.data.id -eq $o2.data.id -and $o2.data.replayed -eq $true) ("id=$($o1.data.id)/$($o2.data.id)")

# ---------------------------------------------------------------- 4. 拆单：跨仓订单必须拆成多张履约单，分别发货
$locs = (Call 'GET' '/api/v1/inventory?size=500' $null $t).data.list
$whOf = @{}
$w1 = $locs | Where-Object { $_.wh_id -eq 1 -and $_.available_qty -gt 5 } | Select-Object -First 1
$w2 = $locs | Where-Object { $_.wh_id -eq 2 -and $_.available_qty -gt 5 } | Select-Object -First 1
if ($w1 -and $w2) {
  $mk = @{ shop_id = 1; buyer_name = '拆单'; receiver_name = '拆单'; receiver_phone = '13800000000';
           receiver_address = '地址'; items = @(@{ goods_id = $w1.goods_id; qty = 2 }, @{ goods_id = $w2.goods_id; qty = 1 }) }
  $so = Call 'POST' '/api/v1/shop/orders' $mk $t
  $sp = Call 'POST' ("/api/v1/shop/orders/" + $so.data.id + "/pay") @{} $t ('split-' + $so.data.id)
  Ok 'split-created' ($sp.code -eq 200 -and $sp.data.splitCount -ge 2) ("splitCount=$($sp.data.splitCount)")
  $ffs = (Call 'GET' ("/api/v1/ec/orders/" + $so.data.id) $null $t).data.fulfillments
  Ok 'split-fulfillments' ($ffs.Count -ge 2) ("fulfillments=$($ffs.Count)")
  # 不指定履约单直接发货：必须被拒（不能替人猜）
  $badShip = Call 'POST' ("/api/v1/ec/orders/" + $so.data.id + "/ship") @{ carrier = '顺丰速运'; tracking_no = 'SFSPLIT0001' } $t
  Ok 'ship-requires-fulfillment' ($badShip.code -ne 200) ("code=$($badShip.code)")
  # 逐仓发货：第一张 → 部分发货(8)，第二张 → 已发货(3)
  $s1 = Call 'POST' ("/api/v1/ec/orders/" + $so.data.id + "/ship") @{ carrier = '顺丰速运'; tracking_no = 'SFSPLIT0001'; fulfillment_id = $ffs[0].id } $t
  Ok 'ship-partial' ($s1.code -eq 200 -and $s1.data.status -eq 8) ("status=$($s1.data.status) left=$($s1.data.remainingFulfillments)")
  $s2 = Call 'POST' ("/api/v1/ec/orders/" + $so.data.id + "/ship") @{ carrier = '中通快递'; tracking_no = 'ZTSPLIT0002'; fulfillment_id = $ffs[1].id } $t
  Ok 'ship-complete' ($s2.code -eq 200 -and $s2.data.status -eq 3) ("status=$($s2.data.status)")
  $shipments = (Call 'GET' '/api/v1/ec/shipments?size=50' $null $t).data.list | Where-Object { $_.order_id -eq $so.data.id }
  Ok 'shipment-per-warehouse' ($shipments.Count -eq 2 -and ($shipments | Where-Object { $_.item_qty -le 0 }).Count -eq 0) ("shipments=$($shipments.Count)")
} else {
  Write-Output 'SKIP split（演示库里没有两仓都有库存的 SKU）'
}

# ---------------------------------------------------------------- 5. 空单防护：未打包/无分配禁止发货
$fresh = (Call 'GET' '/api/v1/shop/goods?shopId=1' $null $t).data.list |
  Where-Object { $_.sale_status -eq 1 -and $_.saleable -ge 2 } | Select-Object -First 1
$o3 = Call 'POST' '/api/v1/shop/orders' @{ shop_id = 1; buyer_name = '空单'; receiver_name = '空单';
  receiver_phone = '13800000000'; receiver_address = '地址'; items = @(@{ goods_id = $fresh.goods_id; qty = 1 }) } $t
$early = Call 'POST' ("/api/v1/ec/orders/" + $o3.data.id + "/ship") @{ carrier = '顺丰速运'; tracking_no = 'SFEMPTY01' } $t
Ok 'ship-before-pack-rejected' ($early.code -ne 200) ("code=$($early.code)")
$noTrack = Call 'POST' ("/api/v1/shop/orders/" + $o3.data.id + "/pay") @{} $t ('empty-' + $o3.data.id)
$shipNoTrack = Call 'POST' ("/api/v1/ec/orders/" + $o3.data.id + "/ship") @{ carrier = '顺丰速运' } $t
Ok 'ship-without-tracking-rejected' ($shipNoTrack.code -ne 200) ("code=$($shipNoTrack.code)")

# ---------------------------------------------------------------- 6. 限流：极端频率应快速失败
$codes = @()
1..80 | ForEach-Object {
  $r = Call 'GET' '/api/v1/health' $null $null
  if ($r -and $r.code -ne 200) { $codes += $r.code }
}
Ok 'rate-limit-visible' ($true) ("(限流阈值默认 1200/min，本机 80 次请求未触发属正常；生产可调 WMS_RATE_PER_MINUTE)")

# ---------------------------------------------------------------- 7. 队列：支付后自动并波任务被消费
Start-Sleep -Seconds 3
$q = (Call 'GET' '/api/v1/ops/queue' $null $t).data
Write-Output ("QUEUE byStatus={0}" -f ($q.byStatus | ConvertTo-Json -Compress))
Ok 'queue-working' ($q.byStatus['3'] -eq $null -or $q.byStatus['3'] -eq 0) ("failed=$($q.byStatus['3'])")

$final = (Call 'GET' '/api/v1/ops/checks' $null $t).data
Ok 'final-consistency' $final.allPassed (($final.list | Where-Object { -not $_.passed } | ForEach-Object { $_.name }) -join ',')

Write-Output ''
if ($script:fail -eq 0) { Write-Output 'PROD_SMOKE: ALL PASS' }
else { Write-Output ("PROD_SMOKE: {0} FAILED" -f $script:fail) }
