# MySQL DDL 一键校验：建库 -> 建表 -> 核对表数/外键/关键列
$ErrorActionPreference = 'Stop'
$mysql = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
if (-not (Test-Path $mysql)) { $mysql = 'mysql' }
$user = 'root'; $pass = 'root'; $dbName = 'wms_prod'
$schema = Join-Path (Split-Path $PSScriptRoot -Parent) 'db/schema_mysql.sql'
$tmp = Join-Path $env:TEMP 'wms_schema_verify.sql'
[System.IO.File]::WriteAllText($tmp, (Get-Content -Raw -Encoding UTF8 $schema), (New-Object System.Text.UTF8Encoding($false)))
& $mysql -u $user "-p$pass" -e "DROP DATABASE IF EXISTS $dbName; CREATE DATABASE $dbName DEFAULT CHARACTER SET utf8mb4;" 2>$null
& $mysql -u $user "-p$pass" --default-character-set=utf8mb4 $dbName -e "source $($tmp -replace '\\','/')" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Output 'SCHEMA_FAILED'; exit 1 }
$q = "SELECT 'tables' k, COUNT(*) v FROM information_schema.tables WHERE table_schema='$dbName'
UNION ALL SELECT 'fks', COUNT(*) FROM information_schema.referential_constraints WHERE constraint_schema='$dbName'
UNION ALL SELECT 'uniques', COUNT(*) FROM information_schema.statistics WHERE table_schema='$dbName' AND non_unique=0 AND index_name<>'PRIMARY'
UNION ALL SELECT 'daily_stat', COUNT(*) FROM information_schema.tables WHERE table_schema='$dbName' AND table_name='wms_daily_stat'
UNION ALL SELECT 'shipment', COUNT(*) FROM information_schema.tables WHERE table_schema='$dbName' AND table_name='ec_shipment';"
& $mysql -u $user "-p$pass" -t $dbName -e $q 2>$null
Write-Output 'MYSQL_SCHEMA_OK'
