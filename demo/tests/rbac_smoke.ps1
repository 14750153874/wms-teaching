# RBAC 自测：菜单/按钮权限 + 仓库数据权限（服务需跑在 8010）
$ErrorActionPreference = 'Stop'
$Base = 'http://127.0.0.1:8010'

function Login($u, $p) {
  $body = @{ username = $u; password = $p } | ConvertTo-Json
  try {
    $r = Invoke-RestMethod -Method POST -Uri "$Base/api/v1/auth/login" -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
    return $r.data
  } catch { $d = $_.ErrorDetails.Message | ConvertFrom-Json; return [pscustomobject]@{ code = $d.code; message = $d.message } }
}
function Call($method, $path, $token, $body) {
  $h = @{ Authorization = "Bearer $token" }
  $args = @{ Method = $method; Uri = "$Base$path"; Headers = $h; TimeoutSec = 10 }
  if ($null -ne $body) { $args['Body'] = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 6)); $args['ContentType'] = 'application/json; charset=utf-8' }
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

# ---- 1. admin ----
$admin = Login 'admin' '123456'
Check 'admin 登录' ($admin.accessToken -ne $null) ("roles=" + (($admin.user.roles | ForEach-Object { $_.code }) -join ','))
Check 'admin 通配权限' ($admin.user.perms -contains '*') ("perms=" + ($admin.user.perms -join ','))
$sys = Call 'GET' '/api/v1/sys/users' $admin.accessToken $null
Check 'admin 可读系统管理' ($sys.code -eq 200 -and $sys.data.list.Count -ge 6) ("users=" + $sys.data.list.Count + " roles=" + $sys.data.roles.Count + " whs=" + $sys.data.warehouses.Count)

# ---- 2. viewer：只读，写操作必须 403 ----
$viewer = Login 'viewer01' '123456'
Check 'viewer 登录' ($viewer.accessToken -ne $null) ("perms=" + ($viewer.user.perms.Count))
$r1 = Call 'GET' '/api/v1/ec/orders' $viewer.accessToken $null
Check 'viewer 可看订单' ($r1.code -eq 200) ("total=" + $r1.data.total)
$r2 = Call 'POST' '/api/v1/ec/orders/1/ship' $viewer.accessToken @{ carrier = 'x'; tracking_no = 'y' }
Check 'viewer 发货被拒' ($r2.code -eq 40300) ("code=" + $r2.code + " msg=" + $r2.message)
$r3 = Call 'GET' '/api/v1/sys/users' $viewer.accessToken $null
Check 'viewer 系统管理被拒' ($r3.code -eq 40300) ("code=" + $r3.code)
$r4 = Call 'POST' '/api/v1/goods' $viewer.accessToken @{ goods_code = 'X1'; goods_name = 'x'; unit = '个' }
Check 'viewer 新增商品被拒' ($r4.code -eq 40300) ("code=" + $r4.code)

# ---- 3. picker：能拣货，不能建波次/发货 ----
$picker = Login 'picker01' '123456'
$p1 = Call 'GET' '/api/v1/ec/waves' $picker.accessToken $null
Check 'picker 可看波次' ($p1.code -eq 200) ("total=" + $p1.data.total)
$p2 = Call 'POST' '/api/v1/ec/waves' $picker.accessToken @{ order_ids = @(1) }
Check 'picker 建波次被拒' ($p2.code -eq 40300) ("code=" + $p2.code + " msg=" + $p2.message)
$p3 = Call 'POST' '/api/v1/ec/orders/1/ship' $picker.accessToken @{ carrier = 'x'; tracking_no = 'y' }
Check 'picker 发货被拒' ($p3.code -eq 40300) ("code=" + $p3.code)

# ---- 4. 仓库数据权限：keeper02 只绑 WH02 ----
$k1 = Login 'keeper01' '123456'
$k2 = Login 'keeper02' '123456'
$adminInv = Call 'GET' '/api/v1/inventory?size=200' $admin.accessToken $null
$k1Inv = Call 'GET' '/api/v1/inventory?size=200' $k1.accessToken $null
$k2Inv = Call 'GET' '/api/v1/inventory?size=200' $k2.accessToken $null
$adminWh = ($adminInv.data.list | ForEach-Object { $_.loc_code.Substring(0,3) } | Sort-Object -Unique) -join ','
$k1Wh = ($k1Inv.data.list | ForEach-Object { $_.loc_code.Substring(0,3) } | Sort-Object -Unique) -join ','
$k2Wh = ($k2Inv.data.list | ForEach-Object { $_.loc_code.Substring(0,3) } | Sort-Object -Unique) -join ','
Check 'admin 看到两个仓' ($adminInv.data.total -gt $k1Inv.data.total -and $adminWh.Contains('C01')) ("admin=" + $adminInv.data.total + " wh=" + $adminWh)
Check 'keeper01 只看到 WH01（无 C01）' ($k1Wh -notmatch 'C01' -and $k1Inv.data.total -gt 0) ("keeper01=" + $k1Inv.data.total + " wh=" + $k1Wh)
Check 'keeper02 只看到 WH02（只有 C01）' ($k2Wh -eq 'C01' -and $k2Inv.data.total -gt 0) ("keeper02=" + $k2Inv.data.total + " wh=" + $k2Wh)
$k2Orders = Call 'GET' '/api/v1/in-orders' $k2.accessToken $null
Check 'keeper02 入库单也被过滤' ($k2Orders.code -eq 200 -and $k2Orders.data.total -eq 0) ("total=" + $k2Orders.data.total)
$k2New = Call 'POST' '/api/v1/in-orders' $k2.accessToken @{ order_type = 1; wh_id = 1; lines = @(@{ goods_id = 1; loc_id = 7; plan_num = 1 }) }
Check 'keeper02 不能给 WH01 建单' ($k2New.code -eq 40300) ("code=" + $k2New.code + " msg=" + $k2New.message)

# ---- 5. 角色权限改写后立即生效（viewer 去掉 ec:order:view） ----
$roles = (Call 'GET' '/api/v1/sys/roles' $admin.accessToken $null).data.list
$viewerRole = $roles | Where-Object { $_.role_code -eq 'viewer' }
$newPerms = $viewerRole.perms | Where-Object { $_ -ne 'ec:order:view' }
$upd = Call 'PUT' ("/api/v1/sys/roles/" + $viewerRole.id) $admin.accessToken @{ perm_codes = @($newPerms) }
Check '角色权限可修改' ($upd.code -eq 200) ("permCount=" + $upd.data.permCount)
$viewer2 = Login 'viewer01' '123456'
$r5 = Call 'GET' '/api/v1/ec/orders' $viewer2.accessToken $null
Check '改权限后 viewer 看订单被拒' ($r5.code -eq 40300) ("code=" + $r5.code)
# 还原
$restore = (Call 'GET' '/api/v1/sys/roles' $admin.accessToken $null).data.catalog | Where-Object { $_.module -eq '电商履约' -and $_.perm_type -eq 1 } | ForEach-Object { $_.perm_code }
Call 'PUT' ("/api/v1/sys/roles/" + $viewerRole.id) $admin.accessToken @{ perm_codes = @($viewerRole.perms) } | Out-Null

# ---- 6. admin 角色不可改；系统管理增改用户 ----
$adminRole = $roles | Where-Object { $_.role_code -eq 'admin' }
$bad = Call 'PUT' ("/api/v1/sys/roles/" + $adminRole.id) $admin.accessToken @{ perm_codes = @('dashboard:view') }
Check 'admin 角色不可改' ($bad.code -eq 40001) ("code=" + $bad.code + " msg=" + $bad.message)
$newUser = Call 'POST' '/api/v1/sys/users' $admin.accessToken @{ username = 'test_packer'; password = '123456'; real_name = '测试打包员'; role_codes = @('packer'); wh_ids = @(1) }
Check '新建用户' ($newUser.code -eq 200) ("id=" + $newUser.data.id)
$loginNew = Login 'test_packer' '123456'
Check '新用户可登录且带角色' ($loginNew.accessToken -ne $null -and $loginNew.user.roles[0].code -eq 'packer') ("roles=" + $loginNew.user.roles[0].code)
$disable = Call 'PUT' ("/api/v1/sys/users/" + $newUser.data.id) $admin.accessToken @{ status = 0 }
Check '停用用户' ($disable.code -eq 200) ("code=" + $disable.code)
$loginOff = Login 'test_packer' '123456'
Check '停用后不能登录' ($loginOff.accessToken -eq $null) ("code=" + $loginOff.code + " msg=" + $loginOff.message)

# ---- 7. 操作日志与一致性 ----
$checks = Call 'GET' '/api/v1/ops/checks' $admin.accessToken $null
Check '一致性校验仍全部通过' ($checks.data.allPassed -eq $true) ("checks=" + $checks.data.checks.Count)
$logs = Call 'GET' '/api/v1/logs?size=50' $admin.accessToken $null
$sysLogs = ($logs.data.list | Where-Object { $_.module -eq '系统管理' }).Count
Check '系统管理操作留痕' ($sysLogs -ge 2) ("sysLogs=" + $sysLogs)

Write-Output ("`nRESULT: " + $(if ($failed -eq 0) { 'ALL PASS' } else { "$failed FAILED" }))

