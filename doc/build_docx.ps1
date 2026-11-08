param(
    [Parameter(Mandatory = $true)][string]$Html,
    [Parameter(Mandatory = $true)][string]$Docx,
    [Parameter(Mandatory = $true)][string]$Pdf,
    [string]$HeadingsTsv = ""
)
# 用 WPS（KWps.Application）把 HTML 转成 docx/pdf。
# Word.Application 在本机 COM 下无法 Documents.Add（报"Word 未能引发事件"），WPS 可以。
#
# 两遍处理：
#   第一遍：打开 HTML → 另存为 .docx（此时 h1/h2/h3 只是直接格式，不是 Word 标题样式）
#   第二遍：重新打开 .docx → 按 _headings.tsv 套内置标题样式（并设置大纲级别）→ 插入目录域并更新 → 存 docx + pdf
$ErrorActionPreference = "Stop"

$app = $null
# 临时文件名带时间戳：避免上次被 WPS 占用（超时中断）的残留文件导致 Remove-Item 失败
$tmpDocx = Join-Path (Split-Path -Parent $Docx) ("_pass1_" + (Get-Date -Format "HHmmss") + ".docx")
try {
    $app = New-Object -ComObject KWps.Application
    $app.Visible = $false
    $app.DisplayAlerts = 0

    # ---------- 第一遍：HTML → docx ----------
    $doc = $app.Documents.Open($Html, $false, $false)
    $doc.SaveAs2($tmpDocx, 16)
    $doc.Close(0)
    Write-Host "第一遍完成：HTML -> docx ($tmpDocx)"

    # ---------- 第二遍：重开 docx，套样式 + 建目录 ----------
    $doc = $app.Documents.Open($tmpDocx, $false, $false)

    $ps = $doc.PageSetup
    $ps.PageWidth = $app.CentimetersToPoints(21)
    $ps.PageHeight = $app.CentimetersToPoints(29.7)
    $ps.TopMargin = $app.CentimetersToPoints(2.5)
    $ps.BottomMargin = $app.CentimetersToPoints(2.5)
    $ps.LeftMargin = $app.CentimetersToPoints(2.2)
    $ps.RightMargin = $app.CentimetersToPoints(2.2)

    $footer = $doc.Sections(1).Footers(1)
    $footer.Range.ParagraphFormat.Alignment = 1
    [void]$footer.PageNumbers.Add(1, $true)

    # ---- 插图：WPS 导入 HTML 时会把 <img> 换成指向本地文件的 INCLUDEPICTURE 链接域（拷走就丢图），
    #      所以 build_doc.py 输出的是 {{FIG:相对路径|题注}} 标记段落，这里用 AddPicture 真实嵌入并限宽居中 ----
    $textWidth = $ps.PageWidth - $ps.LeftMargin - $ps.RightMargin
    $maxW = $textWidth * 0.98
    $srcDir = Split-Path -Parent $Html
    Write-Host "  [进度] 开始嵌入插图（路径基准：$srcDir）…"
    $figDone = 0
    $figFail = 0
    foreach ($para in $doc.Paragraphs) {
        $t = $para.Range.Text.Trim([char]13, [char]7, [char]10, [char]32)
        if ($t -notlike "*{{FIG:*}}*") { continue }
        $m = [regex]::Match($t, '\{\{FIG:([^|}]+)\|([^}]*)\}\}')
        if (-not $m.Success) { continue }
        $rel = $m.Groups[1].Value.Trim()
        $imgPath = Join-Path $srcDir $rel
        if (-not (Test-Path $imgPath)) { Write-Host "    [警告] 找不到图片：$imgPath"; $figFail++; continue }
        try {
            $rng = $para.Range
            $rng.MoveEnd(1, -1) | Out-Null      # 1 = wdCharacter，排除段落标记
            $rng.Text = ""
            $at = $doc.Range($rng.Start, $rng.Start)
            $shp = $doc.InlineShapes.AddPicture($imgPath, $false, $true, $at)
            $shp.Range.ParagraphFormat.Alignment = 1        # 居中
            # WPS 的 AddPicture 默认尺寸可能极小（实测 24pt×13pt），必须显式设宽度并按比例算高度
            $ratio = 1.0
            if ($shp.Width -gt 0) { $ratio = $shp.Height / $shp.Width }
            $shp.Width = $maxW
            try { $shp.Height = $maxW * $ratio } catch { }
            $figDone++
        } catch {
            Write-Host "    [警告] 嵌入失败 $rel : $($_.Exception.Message)"
            $figFail++
        }
    }
    Write-Host "  插图嵌入完成：成功 $figDone 张，失败 $figFail 张，宽度限制 $([Math]::Round($maxW / 28.35, 1)) cm"

    # ---- 表格：按窗口自适应列宽（否则窄列会逐字换行），首行跨页重复，行不跨页 ----
    $tblCount = $doc.Tables.Count
    for ($i = 1; $i -le $tblCount; $i++) {
        $tbl = $doc.Tables.Item($i)
        try { $tbl.AutoFitBehavior(2) } catch { }                    # 2 = wdAutoFitWindow
        try { $tbl.Rows.Item(1).HeadingFormat = $true } catch { }    # 表头跨页重复
        try { $tbl.Rows.AllowBreakAcrossPages = $false } catch { }
    }
    Write-Host "  表格排版完成：共 $tblCount 张"

    if ($HeadingsTsv -and (Test-Path $HeadingsTsv)) {
        $map = @{}
        foreach ($line in [System.IO.File]::ReadAllLines($HeadingsTsv, [System.Text.Encoding]::UTF8)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $parts = $line -split "`t", 2
            if ($parts.Count -eq 2) { $map[$parts[1].Trim()] = [int]$parts[0] }
        }
        $styled = 0
        $outlineFail = 0
        $seen = 0
        Write-Host "  [进度] 开始逐段套用标题样式（共约 $($doc.Paragraphs.Count) 段，WPS COM 较慢）…"
        foreach ($para in $doc.Paragraphs) {
            $seen++
            if ($seen % 1000 -eq 0) { Write-Host "    …已扫描 $seen 段，命中 $styled 个标题" }
            if ($para.Range.Information(12)) { continue }        # 12 = wdWithInTable
            $t = $para.Range.Text.Trim([char]13, [char]7, [char]10, [char]32)
            if ([string]::IsNullOrWhiteSpace($t)) { continue }
            if ($map.ContainsKey($t)) {
                $level = $map[$t]
                if ($level -ge 1 -and $level -le 3) {
                    try { $para.Style = @(-2, -3, -4)[$level - 1]; $styled++ } catch { }
                    try { $para.OutlineLevel = $level } catch { $outlineFail++ }
                }
                $map.Remove($t)
            }
        }
        Write-Host "  标题样式已套用 $styled 个；未匹配 $($map.Count) 个；大纲级别设置失败 $outlineFail 个"
        foreach ($k in $map.Keys) { Write-Host "    未匹配: $k" }
    }

    # 目录：把 {{TOC}} 段落换成目录域
    $target = $null
    foreach ($para in $doc.Paragraphs) {
        if ($para.Range.Text -like "*{{TOC}}*") { $tocPara = $para; break }
    }
    if ($tocPara) {
        # WPS 下 TablesOfContents.Add 生成的域更新后仍显示"错误！未找到目录项"，
        # 必须用 Fields.Add 显式带 \u（按大纲级别收集），并且先 Repaginate 再 Update。
        $rng = $tocPara.Range
        try { $rng.MoveEnd(1, -1) | Out-Null } catch { }   # 1 = wdCharacter，排除段落标记
        $rng.Text = ""
        $insertAt = $doc.Range($rng.Start, $rng.Start)
        $doc.Repaginate()
        [void]$doc.Fields.Add($insertAt, -1, 'TOC \o "1-3" \h \z \u', $true)
        $doc.Repaginate()
        try { $doc.Fields.Update() } catch { }
        $doc.Repaginate()
        $check = $doc.Range($rng.Start, [Math]::Min($rng.Start + 500, $doc.Content.End)).Text
        if ($check -match '项目概述') {
            Write-Host "  目录生成成功（含页码条目）"
        } else {
            Write-Host "  [警告] 目录可能为空，前 120 字：$($check.Substring(0, [Math]::Min(120, $check.Length)))"
        }
    } else {
        Write-Host "  未找到 {{TOC}} 占位符"
    }

    Write-Host "  [进度] 保存 docx / pdf …"
    if (Test-Path $Docx) { Remove-Item $Docx -Force -ErrorAction SilentlyContinue }
    if (Test-Path $Pdf) { Remove-Item $Pdf -Force -ErrorAction SilentlyContinue }
    $doc.SaveAs2($Docx, 16)
    $doc.SaveAs2($Pdf, 17)
    $doc.Close(0)
    Remove-Item $tmpDocx -Force -ErrorAction SilentlyContinue
    # 清理历史遗留的 _pass1_*.docx
    Get-ChildItem (Split-Path -Parent $Docx) -Filter "_pass1_*.docx" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Write-Host "OK: docx=$((Get-Item $Docx).Length) bytes, pdf=$((Get-Item $Pdf).Length) bytes"
}
catch {
    Write-Host "失败: $($_.Exception.Message)"
    throw
}
finally {
    if ($app) { try { $app.Quit() } catch { } }
}
