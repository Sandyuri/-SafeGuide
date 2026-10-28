param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$DrawEdges
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$mapPath = Join-Path $Root 'navigation_sandbox\maps\hospital_1f.navigation.json'
$anchorPath = Join-Path $Root 'navigation_sandbox\artifacts\floorplan\hospital_1f.floorplan_anchor_mapping.json'
$edgeRoutePath = Join-Path $Root 'navigation_sandbox\artifacts\floorplan\hospital_1f.edge_routes.json'
$floorplanPath = Join-Path $Root 'navigation_sandbox\artifacts\floorplan\source\hospital_1f_floorplan.jpg'
$outputName = if ($DrawEdges) { 'hospital_1f.all_nodes_with_paths.png' } else { 'hospital_1f.all_nodes_overview.png' }
$outputPath = Join-Path $Root "navigation_sandbox\artifacts\floorplan\$outputName"

$navigation = Get-Content -Raw $mapPath | ConvertFrom-Json
$anchors = Get-Content -Raw $anchorPath | ConvertFrom-Json
$edgeRoutes = Get-Content -Raw $edgeRoutePath | ConvertFrom-Json
$nodeById = @{}
foreach ($node in $navigation.nodes) { $nodeById[$node.id] = $node }

$records = @(
    foreach ($anchor in $anchors.nodes) {
        $node = $nodeById[$anchor.node_id]
        if ($null -ne $node) {
            [PSCustomObject]@{
                DisplayNumber = [int]$anchor.display_number
                NodeId = $node.id
                NameZh = $node.name_zh
                AccessZone = $node.access_zone
                PixelX = [double]$anchor.floorplan_pixel_xy[0]
                PixelY = [double]$anchor.floorplan_pixel_xy[1]
                DisplayX = if ($null -ne $anchor.display_floorplan_pixel_xy) { [double]$anchor.display_floorplan_pixel_xy[0] } else { [double]$anchor.floorplan_pixel_xy[0] }
                DisplayY = if ($null -ne $anchor.display_floorplan_pixel_xy) { [double]$anchor.display_floorplan_pixel_xy[1] } else { [double]$anchor.floorplan_pixel_xy[1] }
            }
        }
    }
) | Sort-Object DisplayNumber

if ($records.Count -ne $navigation.nodes.Count) {
    throw "Only $($records.Count) of $($navigation.nodes.Count) navigation nodes have floorplan anchors."
}

function Get-NodeColor([object]$record) {
    if ($record.NodeId -like 'nav-f1-spine-*') { return [System.Drawing.ColorTranslator]::FromHtml('#dc2626') }
    if ($record.NodeId -like 'nav-f1-safe-stop-*') { return [System.Drawing.ColorTranslator]::FromHtml('#16a34a') }
    if ($record.AccessZone -eq 'clinical_controlled') { return [System.Drawing.ColorTranslator]::FromHtml('#d97706') }
    return [System.Drawing.ColorTranslator]::FromHtml('#2563eb')
}

$canvasWidth = 4400
$canvasHeight = 2700
$mapLeft = 45
$mapTop = 190
$mapSize = 2470
$sidebarLeft = 2565
$sourceWidth = 6709.0
$sourceHeight = 6702.0

$image = New-Object System.Drawing.Bitmap $canvasWidth, $canvasHeight
$graphics = [System.Drawing.Graphics]::FromImage($image)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
$graphics.Clear([System.Drawing.Color]::FromArgb(248, 250, 252))

$fontFamily = New-Object System.Drawing.FontFamily 'Microsoft YaHei'
$titleFont = [System.Drawing.Font]::new($fontFamily, [single]33, [System.Drawing.FontStyle]::Bold)
$subtitleFont = [System.Drawing.Font]::new($fontFamily, [single]16)
$legendFont = [System.Drawing.Font]::new($fontFamily, [single]15)
$listFont = [System.Drawing.Font]::new($fontFamily, [single]18)
$bubbleFont = [System.Drawing.Font]::new($fontFamily, [single]12, [System.Drawing.FontStyle]::Bold)
$numberFont = [System.Drawing.Font]::new($fontFamily, [single]11, [System.Drawing.FontStyle]::Bold)
$edgeFont = [System.Drawing.Font]::new($fontFamily, [single]10, [System.Drawing.FontStyle]::Bold)
$darkBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#172554'))
$mutedBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#64748b'))
$whiteBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
$linePen = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#cbd5e1')), 2

try {
    $graphics.DrawString('RoboGuard 医院一层｜全部导航节点位置与中文名称', $titleFont, $darkBrush, 45, 38)
    $subtitle = if ($DrawEdges) { "当前导航 JSON：$($records.Count) 个节点、$($navigation.edges.Count) 条连接边；青色线为拓扑边，标签为边编号与 distance_m。" } else { "当前导航 JSON：$($records.Count) 个节点；圆点编号与右侧中文名称一一对应。未绘制路径连线，便于核对地点位置。" }
    $graphics.DrawString($subtitle, $subtitleFont, $mutedBrush, 48, 95)

    $floorplan = [System.Drawing.Image]::FromFile($floorplanPath)
    try {
        $graphics.DrawImage($floorplan, $mapLeft, $mapTop, $mapSize, $mapSize)
    }
    finally { $floorplan.Dispose() }
    $graphics.DrawRectangle($linePen, $mapLeft, $mapTop, $mapSize, $mapSize)

    if ($DrawEdges) {
        $anchorByNode = @{}
        foreach ($record in $records) { $anchorByNode[$record.NodeId] = $record }
        $edgePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(175, 8, 145, 178)), 4
        $edgePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $labelBackBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(235, 255, 255, 255))
        try {
            foreach ($edge in $navigation.edges) {
                $from = $anchorByNode[$edge.from]
                $to = $anchorByNode[$edge.to]
                if ($null -eq $from -or $null -eq $to) { throw "Edge $($edge.id) has no floorplan anchor." }
                $points = New-Object System.Collections.Generic.List[System.Drawing.PointF]
                $points.Add([System.Drawing.PointF]::new($mapLeft + ($from.PixelX / $sourceWidth * $mapSize), $mapTop + ($from.PixelY / $sourceHeight * $mapSize)))
                $routeProperty = $edgeRoutes.edge_routes.PSObject.Properties[$edge.id]
                if ($null -ne $routeProperty) {
                    foreach ($waypoint in $routeProperty.Value) {
                        $points.Add([System.Drawing.PointF]::new($mapLeft + ([double]$waypoint[0] / $sourceWidth * $mapSize), $mapTop + ([double]$waypoint[1] / $sourceHeight * $mapSize)))
                    }
                }
                $points.Add([System.Drawing.PointF]::new($mapLeft + ($to.PixelX / $sourceWidth * $mapSize), $mapTop + ($to.PixelY / $sourceHeight * $mapSize)))
                if ($points.Count -gt 1) { $graphics.DrawLines($edgePen, $points.ToArray()) }
                $middle = $points[[math]::Floor(($points.Count - 1) / 2)]
                $edgeLabel = "$($edge.id)  $([double]$edge.distance_m)m"
                $labelSize = $graphics.MeasureString($edgeLabel, $edgeFont)
                $labelRect = New-Object System.Drawing.RectangleF ($middle.X - $labelSize.Width / 2 - 3), ($middle.Y - $labelSize.Height / 2 - 2), ($labelSize.Width + 6), ($labelSize.Height + 4)
                $graphics.FillRectangle($labelBackBrush, $labelRect)
                $graphics.DrawString($edgeLabel, $edgeFont, $darkBrush, $labelRect.X + 3, $labelRect.Y + 2)
            }
        }
        finally { $edgePen.Dispose(); $labelBackBrush.Dispose() }
    }

    $index = 1
    foreach ($record in $records) {
        $color = Get-NodeColor $record
        $brush = New-Object System.Drawing.SolidBrush $color
        $border = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 4
        $x = $mapLeft + ($record.DisplayX / $sourceWidth * $mapSize)
        $y = $mapTop + ($record.DisplayY / $sourceHeight * $mapSize)
        if ($record.DisplayX -ne $record.PixelX -or $record.DisplayY -ne $record.PixelY) {
            $routeX = $mapLeft + ($record.PixelX / $sourceWidth * $mapSize)
            $routeY = $mapTop + ($record.PixelY / $sourceHeight * $mapSize)
            $leader = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(180, 71, 85, 105), [single]2)
            $leader.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
            $graphics.DrawLine($leader, [single]$routeX, [single]$routeY, [single]$x, [single]$y)
            $leader.Dispose()
        }
        $diameter = 37
        $graphics.FillEllipse($brush, $x - $diameter / 2, $y - $diameter / 2, $diameter, $diameter)
        $graphics.DrawEllipse($border, $x - $diameter / 2, $y - $diameter / 2, $diameter, $diameter)
        $text = '{0:D2}' -f $record.DisplayNumber
        $size = $graphics.MeasureString($text, $bubbleFont)
        $graphics.DrawString($text, $bubbleFont, $whiteBrush, $x - $size.Width / 2, $y - $size.Height / 2 - 1)
        $brush.Dispose(); $border.Dispose()
        $index++
    }

    $graphics.DrawString('节点索引（编号与图中圆点对应）', $titleFont, $darkBrush, $sidebarLeft, 190)
    $categories = @(
        @{ Color = '#2563eb'; Text = '公共服务 / 通道 / 设施' },
        @{ Color = '#d97706'; Text = '诊疗区域入口' },
        @{ Color = '#dc2626'; Text = '按红线重构的主通道骨架' },
        @{ Color = '#16a34a'; Text = '安全停靠点' }
    )
    $legendX = $sidebarLeft
    foreach ($category in $categories) {
        $legendBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($category.Color))
        $graphics.FillEllipse($legendBrush, $legendX, 260, 19, 19)
        $graphics.DrawString($category.Text, $legendFont, $mutedBrush, $legendX + 28, 258)
        $legendBrush.Dispose()
        $legendX += 360
    }

    $index = 1
    foreach ($record in $records) {
        $column = [math]::Floor(($index - 1) / 24)
        $row = ($index - 1) % 24
        $x = $sidebarLeft + ($column * 900)
        $y = 320 + ($row * 85)
        $color = Get-NodeColor $record
        $brush = New-Object System.Drawing.SolidBrush $color
        $graphics.FillEllipse($brush, $x, $y + 4, 28, 28)
        $n = '{0:D2}' -f $record.DisplayNumber
        $nSize = $graphics.MeasureString($n, $numberFont)
        $graphics.DrawString($n, $numberFont, $whiteBrush, $x + 14 - $nSize.Width / 2, $y + 18 - $nSize.Height / 2)
        $graphics.DrawString($record.NameZh, $listFont, $darkBrush, $x + 42, $y)
        $graphics.DrawString($record.NodeId, $legendFont, $mutedBrush, $x + 42, $y + 34)
        $brush.Dispose()
        $index++
    }

    $footer = if ($DrawEdges) { '注：路径线是当前 navigation.json 拓扑边；其中折线来自人工平面图走廊路点，尚非 NavMesh 可执行轨迹。' } else { '注：本图是节点标注总览，不代表已完成 NavMesh 碰撞验证或真实行驶轨迹。' }
    $graphics.DrawString('位置依据：hospital_1f.floorplan_anchor_mapping.json；名称依据：hospital_1f.navigation.json。', $legendFont, $mutedBrush, 48, 2660)
    $graphics.DrawString($footer, $legendFont, $mutedBrush, 1710, 2660)

    $outputDir = Split-Path -Parent $outputPath
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    $image.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output "Wrote $outputPath ($($records.Count) nodes)"
}
finally {
    $linePen.Dispose(); $darkBrush.Dispose(); $mutedBrush.Dispose(); $whiteBrush.Dispose()
    $titleFont.Dispose(); $subtitleFont.Dispose(); $legendFont.Dispose(); $listFont.Dispose(); $bubbleFont.Dispose(); $numberFont.Dispose(); $edgeFont.Dispose(); $fontFamily.Dispose()
    $graphics.Dispose(); $image.Dispose()
}
